from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ThreadRecord:
    agent_id: str
    codex_thread_id: str
    gmail_thread_id: str
    workspace_path: str
    canonical_subject: str
    last_email_message_id: str | None
    email_references: list[str]


@dataclass(slots=True)
class PendingTurn:
    id: int
    agent_id: str
    gmail_message_id: str
    reply_to_message_id: str | None
    text_body: str
    image_paths: list[str]
    attachment_paths: list[str]
    status: str
    codex_turn_id: str | None
    started_at: int | None
    runner_pane_id: str | None
    runner_log_path: str | None


class StateDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS threads (
                agent_id TEXT PRIMARY KEY,
                codex_thread_id TEXT NOT NULL UNIQUE,
                gmail_thread_id TEXT NOT NULL UNIQUE,
                workspace_path TEXT NOT NULL,
                canonical_subject TEXT NOT NULL,
                last_email_message_id TEXT,
                email_references_json TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_messages (
                gmail_message_id TEXT PRIMARY KEY,
                gmail_thread_id TEXT NOT NULL,
                rfc_message_id TEXT,
                direction TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL UNIQUE,
                reply_to_message_id TEXT,
                text_body TEXT NOT NULL,
                image_paths_json TEXT NOT NULL,
                attachment_paths_json TEXT NOT NULL,
                status TEXT NOT NULL,
                codex_turn_id TEXT,
                runner_pane_id TEXT,
                runner_log_path TEXT,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                error TEXT,
                FOREIGN KEY(agent_id) REFERENCES threads(agent_id)
            );

            CREATE TABLE IF NOT EXISTS seen_thread_items (
                item_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_type TEXT NOT NULL,
                origin TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turn_emails (
                turn_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                email_message_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(turn_id, kind)
            );
            """
        )
        self._ensure_column("pending_turns", "runner_pane_id", "TEXT")
        self._ensure_column("pending_turns", "runner_log_path", "TEXT")
        self._ensure_column("threads", "email_references_json", "TEXT NOT NULL DEFAULT '[]'")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row["name"] for row in rows}
        if column in names:
            return
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.close()

    def _thread_from_row(self, row: sqlite3.Row) -> ThreadRecord:
        data = dict(row)
        data["email_references"] = json.loads(data.pop("email_references_json"))
        return ThreadRecord(**data)

    def _pending_from_row(self, row: sqlite3.Row) -> PendingTurn:
        return PendingTurn(
            id=row["id"],
            agent_id=row["agent_id"],
            gmail_message_id=row["gmail_message_id"],
            reply_to_message_id=row["reply_to_message_id"],
            text_body=row["text_body"],
            image_paths=json.loads(row["image_paths_json"]),
            attachment_paths=json.loads(row["attachment_paths_json"]),
            status=row["status"],
            codex_turn_id=row["codex_turn_id"],
            started_at=row["started_at"],
            runner_pane_id=row["runner_pane_id"],
            runner_log_path=row["runner_log_path"],
        )

    def message_processed(self, gmail_message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_messages WHERE gmail_message_id = ?",
            (gmail_message_id,),
        ).fetchone()
        return row is not None

    def mark_message_processed(
        self,
        gmail_message_id: str,
        gmail_thread_id: str,
        rfc_message_id: str | None,
        direction: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO processed_messages
            (gmail_message_id, gmail_thread_id, rfc_message_id, direction, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (gmail_message_id, gmail_thread_id, rfc_message_id, direction, int(time.time())),
        )
        self.conn.commit()

    def get_thread_by_agent(self, agent_id: str) -> ThreadRecord | None:
        row = self.conn.execute(
            """
            SELECT agent_id, codex_thread_id, gmail_thread_id, workspace_path, canonical_subject, last_email_message_id, email_references_json
            FROM threads WHERE agent_id = ?
            """,
            (agent_id,),
        ).fetchone()
        return self._thread_from_row(row) if row else None

    def get_thread_by_gmail_thread(self, gmail_thread_id: str) -> ThreadRecord | None:
        row = self.conn.execute(
            """
            SELECT agent_id, codex_thread_id, gmail_thread_id, workspace_path, canonical_subject, last_email_message_id, email_references_json
            FROM threads WHERE gmail_thread_id = ?
            """,
            (gmail_thread_id,),
        ).fetchone()
        return self._thread_from_row(row) if row else None

    def upsert_thread(
        self,
        *,
        agent_id: str,
        codex_thread_id: str,
        gmail_thread_id: str,
        workspace_path: str,
        canonical_subject: str,
        last_email_message_id: str | None,
        email_references: list[str],
    ) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO threads
            (agent_id, codex_thread_id, gmail_thread_id, workspace_path, canonical_subject, last_email_message_id, email_references_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                codex_thread_id = excluded.codex_thread_id,
                gmail_thread_id = excluded.gmail_thread_id,
                workspace_path = excluded.workspace_path,
                canonical_subject = excluded.canonical_subject,
                last_email_message_id = COALESCE(excluded.last_email_message_id, threads.last_email_message_id),
                email_references_json = CASE
                    WHEN excluded.email_references_json = '[]' THEN threads.email_references_json
                    ELSE excluded.email_references_json
                END,
                updated_at = excluded.updated_at
            """,
            (
                agent_id,
                codex_thread_id,
                gmail_thread_id,
                workspace_path,
                canonical_subject,
                last_email_message_id,
                json.dumps(email_references),
                now,
                now,
            ),
        )
        self.conn.commit()

    def update_thread_codex_id(self, agent_id: str, codex_thread_id: str) -> None:
        self.conn.execute(
            "UPDATE threads SET codex_thread_id = ?, updated_at = ? WHERE agent_id = ?",
            (codex_thread_id, int(time.time()), agent_id),
        )
        self.conn.commit()

    def update_thread_gmail_id(self, agent_id: str, gmail_thread_id: str) -> None:
        self.conn.execute(
            "UPDATE threads SET gmail_thread_id = ?, updated_at = ? WHERE agent_id = ?",
            (gmail_thread_id, int(time.time()), agent_id),
        )
        self.conn.commit()

    def update_last_email_message_id(self, agent_id: str, message_id: str) -> None:
        self.conn.execute(
            "UPDATE threads SET last_email_message_id = ?, updated_at = ? WHERE agent_id = ?",
            (message_id, int(time.time()), agent_id),
        )
        self.conn.commit()

    def update_email_references(self, agent_id: str, email_references: list[str]) -> None:
        self.conn.execute(
            "UPDATE threads SET email_references_json = ?, updated_at = ? WHERE agent_id = ?",
            (json.dumps(email_references), int(time.time()), agent_id),
        )
        self.conn.commit()

    def update_turn_reply_to_message_id(self, pending_turn_id: int, reply_to_message_id: str | None) -> None:
        self.conn.execute(
            "UPDATE pending_turns SET reply_to_message_id = ? WHERE id = ?",
            (reply_to_message_id, pending_turn_id),
        )
        self.conn.commit()

    def enqueue_turn(
        self,
        *,
        agent_id: str,
        gmail_message_id: str,
        reply_to_message_id: str | None,
        text_body: str,
        image_paths: list[str],
        attachment_paths: list[str],
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO pending_turns
            (agent_id, gmail_message_id, reply_to_message_id, text_body, image_paths_json, attachment_paths_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                agent_id,
                gmail_message_id,
                reply_to_message_id,
                text_body,
                json.dumps(image_paths),
                json.dumps(attachment_paths),
                int(time.time()),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def next_queued_turn(self, agent_id: str) -> PendingTurn | None:
        row = self.conn.execute(
            """
            SELECT id, agent_id, gmail_message_id, reply_to_message_id, text_body, image_paths_json, attachment_paths_json, status, codex_turn_id, runner_pane_id, runner_log_path
                   , started_at
            FROM pending_turns
            WHERE agent_id = ? AND status = 'queued'
            ORDER BY id ASC
            LIMIT 1
            """,
            (agent_id,),
        ).fetchone()
        return self._pending_from_row(row) if row else None

    def pending_turns_for_agent(
        self,
        agent_id: str,
        statuses: tuple[str, ...] = ("queued", "submitted", "running"),
    ) -> list[PendingTurn]:
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"""
            SELECT id, agent_id, gmail_message_id, reply_to_message_id, text_body, image_paths_json, attachment_paths_json, status, codex_turn_id, runner_pane_id, runner_log_path
                   , started_at
            FROM pending_turns
            WHERE agent_id = ? AND status IN ({placeholders})
            ORDER BY id ASC
            """,
            (agent_id, *statuses),
        ).fetchall()
        return [self._pending_from_row(row) for row in rows]

    def mark_turn_running(
        self,
        pending_turn_id: int,
        *,
        codex_turn_id: str | None = None,
        runner_pane_id: str | None = None,
        runner_log_path: str | None = None,
    ) -> None:
        synthetic_turn_id = codex_turn_id or f"pending:{pending_turn_id}"
        self.conn.execute(
            """
            UPDATE pending_turns
            SET status = 'running',
                started_at = ?,
                codex_turn_id = ?,
                runner_pane_id = ?,
                runner_log_path = ?
            WHERE id = ?
            """,
            (int(time.time()), synthetic_turn_id, runner_pane_id, runner_log_path, pending_turn_id),
        )
        self.conn.commit()

    def mark_turn_submitted(
        self,
        pending_turn_id: int,
        *,
        runner_pane_id: str | None = None,
        runner_log_path: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE pending_turns
            SET status = 'submitted',
                started_at = ?,
                runner_pane_id = ?,
                runner_log_path = ?
            WHERE id = ?
            """,
            (int(time.time()), runner_pane_id, runner_log_path, pending_turn_id),
        )
        self.conn.commit()

    def pending_turn_by_codex_turn_id(self, codex_turn_id: str) -> PendingTurn | None:
        row = self.conn.execute(
            """
            SELECT id, agent_id, gmail_message_id, reply_to_message_id, text_body, image_paths_json, attachment_paths_json, status, codex_turn_id, runner_pane_id, runner_log_path
                   , started_at
            FROM pending_turns
            WHERE codex_turn_id = ?
            """,
            (codex_turn_id,),
        ).fetchone()
        return self._pending_from_row(row) if row else None

    def mark_turn_finished(self, pending_turn_id: int, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE pending_turns
            SET status = CASE WHEN ? IS NULL THEN 'completed' ELSE 'failed' END,
                completed_at = ?,
                error = ?,
                runner_pane_id = NULL
            WHERE id = ?
            """,
            (error, int(time.time()), error, pending_turn_id),
        )
        self.conn.commit()

    def delete_pending_turns(self, pending_turn_ids: list[int]) -> None:
        if not pending_turn_ids:
            return
        placeholders = ",".join("?" for _ in pending_turn_ids)
        self.conn.execute(
            f"DELETE FROM pending_turns WHERE id IN ({placeholders})",
            pending_turn_ids,
        )
        self.conn.commit()

    def seen_item(self, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_thread_items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return row is not None

    def record_item(self, *, item_id: str, agent_id: str, turn_id: str, item_type: str, origin: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen_thread_items
            (item_id, agent_id, turn_id, item_type, origin, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, agent_id, turn_id, item_type, origin, int(time.time())),
        )
        self.conn.commit()

    def turn_email_exists(self, turn_id: str, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM turn_emails WHERE turn_id = ? AND kind = ?",
            (turn_id, kind),
        ).fetchone()
        return row is not None

    def record_turn_email(self, turn_id: str, kind: str, email_message_id: str) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO turn_emails
            (turn_id, kind, email_message_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (turn_id, kind, email_message_id, int(time.time())),
        )
        self.conn.commit()

    def tracked_threads(self) -> list[ThreadRecord]:
        rows = self.conn.execute(
            """
            SELECT agent_id, codex_thread_id, gmail_thread_id, workspace_path, canonical_subject, last_email_message_id, email_references_json
            FROM threads
            ORDER BY updated_at ASC
            """
        ).fetchall()
        return [self._thread_from_row(row) for row in rows]
