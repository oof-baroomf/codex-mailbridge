from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import shlex
import time

from .codex_exec import CodexExecManager
from .config import Config
from .db import PendingTurn, StateDB, ThreadRecord
from .emailer import GmailClient, IncomingMail, save_attachments


LOG = logging.getLogger(__name__)
NOTES_DIR = Path("/home/d/notes").resolve()
DAEMON_TICK_SECONDS = 1.0


class SubjectParseError(RuntimeError):
    pass


def parse_subject(subject: str) -> tuple[str, str]:
    match = re.match(r'^\s*(?:"((?:[^"\\]|\\.)*)"|(\S+))(?:\s+(.*\S))?\s*$', subject)
    if not match:
        raise SubjectParseError("Subject must start with a path followed by an agent id")
    quoted_path, simple_path, agent_id = match.groups()
    path = shlex.split(f'"{quoted_path}"')[0] if quoted_path is not None else simple_path
    if not agent_id:
        raise SubjectParseError("Subject must include an agent id after the path")
    return path, agent_id


def normalize_workspace_path(raw_path: str) -> Path:
    if raw_path == "~":
        path = Path(raw_path).expanduser().resolve()
    elif raw_path.startswith("~/"):
        path = Path(raw_path).expanduser().resolve()
    elif raw_path.startswith("/"):
        path = Path(raw_path).resolve()
    else:
        raise SubjectParseError("Path must be absolute or start with ~/, or be exactly ~")
    if path == NOTES_DIR or NOTES_DIR in path.parents:
        raise SubjectParseError("~/notes is out of scope for this bridge")
    if path.exists() and not path.is_dir():
        raise SubjectParseError("Path points to a file, not a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "mailbridge.log", maxBytes=1_000_000, backupCount=5)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


_QUOTED_REPLY_MARKERS = (
    re.compile(r"^\s*On .+wrote:\s*$"),
    re.compile(r"^\s*Begin forwarded message:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*From:\s+.+$"),
)


def _extract_latest_reply_text(body: str) -> str:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        if any(pattern.match(line) for pattern in _QUOTED_REPLY_MARKERS):
            break
        kept.append(line.rstrip())
    text = "\n".join(kept)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_turn_failure(error_text: str) -> str:
    return f"Codex error:\n\n{error_text.strip()}"


def _session_id_for_thread(thread: ThreadRecord) -> str | None:
    if thread.codex_thread_id.startswith("local:"):
        return None
    return thread.codex_thread_id


class MailBridgeDaemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        configure_logging(config.runtime.log_dir)
        self.db = StateDB(config.runtime.state_dir / "state.sqlite3")
        self.gmail = GmailClient(config)
        self.exec = CodexExecManager(config)
        self.auth_warning_logged = False

    def run(self) -> None:
        self.exec.ensure_tmux_session()
        LOG.info("codex-mailbridge started")
        next_inbox_poll = 0.0
        while True:
            try:
                now = time.time()
                if now >= next_inbox_poll:
                    self._poll_inbox()
                    next_inbox_poll = now + self.config.runtime.poll_interval_seconds
                self._start_queued_turns()
                self._sync_running_turns()
            except Exception:
                LOG.exception("Daemon loop failed")
                time.sleep(DAEMON_TICK_SECONDS)
                continue
            time.sleep(DAEMON_TICK_SECONDS)

    def close(self) -> None:
        self.db.close()

    def _turn_key(self, pending: PendingTurn) -> str:
        return pending.codex_turn_id or f"pending:{pending.id}"

    def _fresh_thread(self, thread: ThreadRecord) -> ThreadRecord:
        current = self.db.get_thread_by_agent(thread.agent_id)
        return current or thread

    def _send_turn_reply(self, thread: ThreadRecord, pending: PendingTurn, body: str) -> None:
        current_thread = self._fresh_thread(thread)
        parent = pending.reply_to_message_id or current_thread.last_email_message_id
        email_id = self.gmail.send_assistant_reply(
            subject=_reply_subject(current_thread.canonical_subject),
            markdown_body=body,
            parent_message_id=parent,
            references=[current_thread.last_email_message_id] if current_thread.last_email_message_id else [],
        )
        self.db.record_turn_email(self._turn_key(pending), "assistant_reply", email_id)
        self.db.update_last_email_message_id(current_thread.agent_id, email_id)

    def _send_turn_progress_reply(self, thread: ThreadRecord, pending: PendingTurn, body: str) -> None:
        current_thread = self._fresh_thread(thread)
        parent = pending.reply_to_message_id or current_thread.last_email_message_id
        email_id = self.gmail.send_assistant_reply(
            subject=_reply_subject(current_thread.canonical_subject),
            markdown_body=body,
            parent_message_id=parent,
            references=[current_thread.last_email_message_id] if current_thread.last_email_message_id else [],
        )
        self.db.record_turn_email(self._turn_key(pending), "assistant_progress", email_id)
        self.db.update_last_email_message_id(current_thread.agent_id, email_id)

    def _fail_pending_turn(self, thread: ThreadRecord, pending: PendingTurn, error_text: str) -> None:
        self.db.mark_turn_finished(pending.id, error_text)
        if not self.db.turn_email_exists(self._turn_key(pending), "assistant_reply"):
            self._send_turn_reply(thread, pending, _format_turn_failure(error_text))

    def _poll_inbox(self) -> None:
        if not self.config.gmail.configured:
            if not self.auth_warning_logged:
                LOG.warning("Gmail auth is not configured yet; skipping mail polling")
                self.auth_warning_logged = True
            return
        try:
            messages = self.gmail.fetch_incoming()
        except Exception:
            LOG.exception("Inbox poll failed")
            return
        for msg in messages:
            if msg.from_address != self.config.gmail.allowed_from.lower():
                continue
            if self.db.message_processed(msg.gmail_message_id):
                self.gmail.mark_seen(msg.uid)
                continue
            try:
                self._handle_incoming(msg)
                self.db.mark_message_processed(msg.gmail_message_id, msg.gmail_thread_id, msg.rfc_message_id, "inbound")
                self.gmail.mark_seen(msg.uid)
            except Exception as exc:
                LOG.exception("Failed to process inbound mail")
                try:
                    self._send_error_reply(msg, str(exc))
                    self.gmail.mark_seen(msg.uid)
                except Exception:
                    LOG.exception("Failed to send error reply")

    def _handle_incoming(self, msg: IncomingMail) -> None:
        body_text = _extract_latest_reply_text(msg.body_text)
        thread = self.db.get_thread_by_gmail_thread(msg.gmail_thread_id)
        if thread is None:
            raw_path, agent_id = parse_subject(msg.subject)
            workspace = normalize_workspace_path(raw_path)
            if body_text.strip():
                if self.db.get_thread_by_agent(agent_id) is not None:
                    raise SubjectParseError(f"Agent id '{agent_id}' has already been used")
                thread = self._create_thread(
                    agent_id=agent_id,
                    workspace=workspace,
                    gmail_thread_id=msg.gmail_thread_id,
                    canonical_subject=msg.subject,
                    initial_message_id=msg.rfc_message_id,
                )
        else:
            workspace = Path(thread.workspace_path)
            if body_text.strip():
                self._supersede_pending_turns(thread)

        attachment_paths, image_paths = save_attachments(workspace, msg.attachments)
        if not body_text.strip():
            return
        assert thread is not None
        self.db.enqueue_turn(
            agent_id=thread.agent_id,
            gmail_message_id=msg.gmail_message_id,
            reply_to_message_id=msg.rfc_message_id,
            text_body=body_text,
            image_paths=image_paths,
            attachment_paths=attachment_paths,
        )

    def _create_thread(
        self,
        *,
        agent_id: str,
        workspace: Path,
        gmail_thread_id: str,
        canonical_subject: str,
        initial_message_id: str | None,
    ) -> ThreadRecord:
        self.db.upsert_thread(
            agent_id=agent_id,
            codex_thread_id=f"local:{agent_id}",
            gmail_thread_id=gmail_thread_id,
            workspace_path=str(workspace),
            canonical_subject=canonical_subject,
            last_email_message_id=initial_message_id,
        )
        thread = self.db.get_thread_by_agent(agent_id)
        assert thread is not None
        return thread

    def _supersede_pending_turns(self, thread: ThreadRecord) -> None:
        queued_ids: list[int] = []
        for pending in self.db.pending_turns_for_agent(thread.agent_id):
            if pending.status == "queued":
                queued_ids.append(pending.id)
                continue
            if pending.runner_pane_id:
                self.exec.interrupt_turn(pending.runner_pane_id)
        self.db.delete_pending_turns(queued_ids)

    def _start_queued_turns(self) -> None:
        for thread in self.db.tracked_threads():
            running = self.db.pending_turns_for_agent(thread.agent_id, statuses=("running",))
            if running:
                continue
            pending = self.db.next_queued_turn(thread.agent_id)
            if pending is None:
                continue
            try:
                started = self.exec.start_turn(
                    agent_id=thread.agent_id,
                    workspace=Path(thread.workspace_path),
                    pending_turn_id=pending.id,
                    prompt=pending.text_body,
                    image_paths=pending.image_paths,
                    resume_session_id=_session_id_for_thread(thread),
                )
            except Exception as exc:
                LOG.exception("Failed to start queued turn for %s", thread.agent_id)
                self._fail_pending_turn(thread, pending, str(exc))
                continue
            self.db.mark_turn_running(
                pending.id,
                runner_pane_id=started.pane_id,
                runner_log_path=started.log_path,
            )

    def _sync_running_turns(self) -> None:
        if not self.config.gmail.configured:
            return
        for thread in self.db.tracked_threads():
            for pending in self.db.pending_turns_for_agent(thread.agent_id, statuses=("running",)):
                self._sync_pending_turn(thread, pending)

    def _sync_pending_turn(self, thread: ThreadRecord, pending: PendingTurn) -> None:
        state = self.exec.read_turn_state(pending.runner_log_path)
        if state.thread_id and state.thread_id != thread.codex_thread_id:
            self.db.update_thread_codex_id(thread.agent_id, state.thread_id)
            thread = self._fresh_thread(thread)

        session_id = state.thread_id or _session_id_for_thread(thread)
        if session_id:
            self.exec.ensure_tui_window(
                agent_id=thread.agent_id,
                workspace=Path(thread.workspace_path),
                session_id=session_id,
            )

        turn_key = self._turn_key(pending)
        if state.first_agent_text and not self.db.turn_email_exists(turn_key, "assistant_progress"):
            self._send_turn_progress_reply(thread, pending, state.first_agent_text)

        if state.turn_completed:
            self.db.mark_turn_finished(pending.id, None)
            if state.last_agent_text and not self.db.turn_email_exists(turn_key, "assistant_reply"):
                self._send_turn_reply(thread, pending, state.last_agent_text)
            return

        if state.turn_failed or (state.exit_code is not None and state.exit_code != 0):
            error_text = state.failure_text()
            self.db.mark_turn_finished(pending.id, error_text)
            if not state.interrupted and not self.db.turn_email_exists(turn_key, "assistant_reply"):
                self._send_turn_reply(thread, pending, _format_turn_failure(error_text))
            return

        if state.exit_code == 0:
            if state.last_agent_text:
                self.db.mark_turn_finished(pending.id, None)
                if not self.db.turn_email_exists(turn_key, "assistant_reply"):
                    self._send_turn_reply(thread, pending, state.last_agent_text)
                return
            self._fail_pending_turn(thread, pending, "Codex exited without a final response.")
            return

        if pending.runner_pane_id and not self.exec.pane_exists(pending.runner_pane_id):
            self._fail_pending_turn(thread, pending, "Codex process exited without a final status.")

    def _send_error_reply(self, msg: IncomingMail, error_text: str) -> None:
        self.gmail.send_assistant_reply(
            subject=_reply_subject(msg.subject or "Codex mailbridge error"),
            markdown_body=f"Bridge error:\n\n{error_text}",
            parent_message_id=msg.rfc_message_id,
            references=msg.references,
        )
