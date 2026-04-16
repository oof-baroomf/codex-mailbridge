import subprocess
from pathlib import Path

from codex_mailbridge.codex_exec import ExecTurnState, StartedTurn
from codex_mailbridge.daemon import MailBridgeDaemon, _split_reply_commands
from codex_mailbridge.db import PendingTurn


class _FakeExec:
    def __init__(self) -> None:
        self.interrupted: list[str] = []
        self.sent_prompts: list[tuple[str, str]] = []
        self.started: list[dict] = []
        self.state = ExecTurnState()
        self.live_panes: set[str] = set()
        self.killed_sessions: list[str] = []

    def interrupt_turn(self, pane_id: str) -> None:
        self.interrupted.append(pane_id)

    def send_prompt(self, pane_id: str, prompt: str) -> None:
        self.sent_prompts.append((pane_id, prompt))

    def start_turn(self, **kwargs) -> StartedTurn:
        self.started.append(kwargs)
        self.live_panes.add("%1")
        return StartedTurn(
            pane_id="%1",
            log_path="/tmp/turn.jsonl",
            thread_id="session-123",
            turn_id="turn-123",
        )

    def read_turn_state(self, log_path: str | None, codex_turn_id: str | None = None) -> ExecTurnState:
        return self.state

    def find_turn_id_since(self, log_path: str | None, started_at: int) -> str | None:
        return None

    def pane_running_codex(self, pane_id: str) -> bool:
        return pane_id in self.live_panes

    def kill_agent_session(self, agent_id: str) -> None:
        self.killed_sessions.append(agent_id)

class _FakeGmail:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_assistant_reply(self, **kwargs):
        self.calls.append(kwargs)
        return "<sent@msg>"


class _QueueDB:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []
        self.submitted: list[tuple[int, str | None, str | None]] = []
        self.updated_reply_to: list[tuple[int, str | None]] = []
        self.updated_email: list[tuple[str, str]] = []
        self.deleted: list[int] = []
        self.thread = type(
            "Thread",
            (),
            {
                "agent_id": "agent-1",
                "codex_thread_id": "session-123",
                "gmail_thread_id": "g1",
                "workspace_path": "/tmp/work",
                "canonical_subject": "subject",
                "last_email_message_id": "<last@msg>",
            },
        )()

    def get_thread_by_gmail_thread(self, gmail_thread_id: str):
        return self.thread

    def get_thread_by_agent(self, agent_id: str):
        return self.thread

    def pending_turns_for_agent(self, agent_id: str, statuses=("queued", "running")):
        turns = [
            PendingTurn(
                id=2,
                agent_id=agent_id,
                gmail_message_id="m2",
                reply_to_message_id="<old@msg>",
                text_body="running",
                image_paths=[],
                attachment_paths=[],
                status="running",
                codex_turn_id="turn-2",
                started_at=123,
                runner_pane_id="%2",
                runner_log_path="/tmp/2.jsonl",
            ),
            PendingTurn(
                id=3,
                agent_id=agent_id,
                gmail_message_id="m3",
                reply_to_message_id=None,
                text_body="queued",
                image_paths=[],
                attachment_paths=[],
                status="queued",
                codex_turn_id=None,
                started_at=None,
                runner_pane_id=None,
                runner_log_path=None,
            ),
        ]
        return [turn for turn in turns if turn.status in statuses]

    def enqueue_turn(self, **kwargs) -> int:
        self.enqueued.append(kwargs)
        return 4

    def mark_turn_submitted(self, pending_turn_id: int, *, runner_pane_id: str | None = None, runner_log_path: str | None = None) -> None:
        self.submitted.append((pending_turn_id, runner_pane_id, runner_log_path))

    def update_turn_reply_to_message_id(self, pending_turn_id: int, reply_to_message_id: str | None) -> None:
        self.updated_reply_to.append((pending_turn_id, reply_to_message_id))

    def delete_pending_turns(self, pending_turn_ids: list[int]) -> None:
        self.deleted = pending_turn_ids

    def update_last_email_message_id(self, agent_id: str, message_id: str) -> None:
        self.updated_email.append((agent_id, message_id))
        self.thread.last_email_message_id = message_id


def test_split_reply_commands_separates_shell_lines() -> None:
    prompt_text, shell_commands = _split_reply_commands("first line\n  ! pwd\n\nsecond line\n! ls -1\n")

    assert prompt_text == "first line\n\nsecond line"
    assert shell_commands == ["pwd", "ls -1"]


def test_handle_incoming_running_thread_injects_without_interrupting(monkeypatch) -> None:
    from codex_mailbridge.daemon import IncomingMail

    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _QueueDB()
    daemon.exec = _FakeExec()
    daemon.exec.live_panes.add("%2")
    monkeypatch.setattr("codex_mailbridge.daemon.save_attachments", lambda workspace, attachments: ([], []))

    daemon._handle_incoming(
        IncomingMail(
            uid="1",
            gmail_message_id="m4",
            gmail_thread_id="g1",
            rfc_message_id="<reply@msg>",
            subject="Re: subject",
            from_address="user@example.com",
            body_text="new request",
            attachments=[],
            references=[],
        )
    )

    assert daemon.exec.interrupted == []
    assert daemon.exec.sent_prompts == [("%2", "new request")]
    assert daemon.db.enqueued == [
        {
            "agent_id": "agent-1",
            "gmail_message_id": "m4",
            "reply_to_message_id": "<reply@msg>",
            "text_body": "new request",
            "image_paths": [],
            "attachment_paths": [],
        }
    ]
    assert daemon.db.submitted == [(4, "%2", "/tmp/2.jsonl")]
    assert daemon.db.updated_reply_to == []
    assert daemon.db.deleted == []


def test_handle_incoming_running_thread_executes_shell_lines_and_sends_trimmed_prompt(monkeypatch) -> None:
    from codex_mailbridge.daemon import IncomingMail

    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _QueueDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()
    daemon.exec.live_panes.add("%2")
    daemon._run_shell_command = lambda workspace, command: (f"$ {command}\n\n[stdout]\nok\n\n[exit 0]", 0)
    monkeypatch.setattr("codex_mailbridge.daemon.save_attachments", lambda workspace, attachments: ([], []))

    daemon._handle_incoming(
        IncomingMail(
            uid="1",
            gmail_message_id="m4",
            gmail_thread_id="g1",
            rfc_message_id="<reply@msg>",
            subject="Re: subject",
            from_address="user@example.com",
            body_text="Please check this\n! pwd\n! git status --short",
            attachments=[],
            references=[],
        )
    )

    assert daemon.exec.sent_prompts == [("%2", "Please check this")]
    assert daemon.db.enqueued == [
        {
            "agent_id": "agent-1",
            "gmail_message_id": "m4",
            "reply_to_message_id": "<reply@msg>",
            "text_body": "Please check this",
            "image_paths": [],
            "attachment_paths": [],
        }
    ]
    assert daemon.gmail.calls == [
        {
            "subject": "Re: subject",
            "markdown_body": "Shell command output from `/tmp/work`:\n\n```text\n$ pwd\n\n[stdout]\nok\n\n[exit 0]\n```\n\n```text\n$ git status --short\n\n[stdout]\nok\n\n[exit 0]\n```",
            "parent_message_id": "<reply@msg>",
            "references": ["<last@msg>"],
        }
    ]


def test_handle_incoming_command_only_reply_skips_codex(monkeypatch) -> None:
    from codex_mailbridge.daemon import IncomingMail

    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _QueueDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()
    daemon.exec.live_panes.add("%2")
    daemon._run_shell_command = lambda workspace, command: (f"$ {command}\n\n[stdout]\n/tmp/work\n\n[exit 0]", 0)
    monkeypatch.setattr("codex_mailbridge.daemon.save_attachments", lambda workspace, attachments: ([], []))

    daemon._handle_incoming(
        IncomingMail(
            uid="1",
            gmail_message_id="m4",
            gmail_thread_id="g1",
            rfc_message_id="<reply@msg>",
            subject="Re: subject",
            from_address="user@example.com",
            body_text="\n ! pwd\n\n\t! ls\n",
            attachments=[],
            references=[],
        )
    )

    assert daemon.exec.sent_prompts == []
    assert daemon.db.enqueued == []
    assert daemon.db.submitted == []
    assert daemon.db.deleted == []
    assert daemon.gmail.calls == [
        {
            "subject": "Re: subject",
            "markdown_body": "Shell command output from `/tmp/work`:\n\n```text\n$ pwd\n\n[stdout]\n/tmp/work\n\n[exit 0]\n```\n\n```text\n$ ls\n\n[stdout]\n/tmp/work\n\n[exit 0]\n```",
            "parent_message_id": "<reply@msg>",
            "references": ["<last@msg>"],
        }
    ]


def test_run_shell_command_timeout_decodes_partial_bytes(monkeypatch, tmp_path: Path) -> None:
    daemon = object.__new__(MailBridgeDaemon)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["bash", "-lc", "ping google.com"],
            timeout=120,
            output=b"PONG\n",
            stderr=b"warning\n",
        )

    monkeypatch.setattr("codex_mailbridge.daemon.subprocess.run", fake_run)

    output, exit_code = daemon._run_shell_command(tmp_path, "ping google.com")

    assert output == (
        "$ ping google.com\n\n"
        "[stdout]\n"
        "PONG\n\n"
        "[stderr]\n"
        "warning\n\n"
        "[timed out after 120s]"
    )
    assert exit_code is None


class _EndDB:
    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.finished: list[tuple[int, str | None]] = []
        self.updated_email: list[tuple[str, str]] = []
        self.thread = type(
            "Thread",
            (),
            {
                "agent_id": "agent-1",
                "codex_thread_id": "session-123",
                "gmail_thread_id": "g1",
                "workspace_path": "/tmp/work",
                "canonical_subject": "subject",
                "last_email_message_id": "<last@msg>",
            },
        )()

    def get_thread_by_agent(self, agent_id: str):
        return self.thread

    def pending_turns_for_agent(self, agent_id: str):
        return [
            PendingTurn(
                id=1,
                agent_id=agent_id,
                gmail_message_id="m1",
                reply_to_message_id=None,
                text_body="old",
                image_paths=[],
                attachment_paths=[],
                status="queued",
                codex_turn_id="pending:1",
                started_at=None,
                runner_pane_id=None,
                runner_log_path=None,
            ),
            PendingTurn(
                id=2,
                agent_id=agent_id,
                gmail_message_id="m2",
                reply_to_message_id=None,
                text_body="running",
                image_paths=[],
                attachment_paths=[],
                status="running",
                codex_turn_id="turn-2",
                started_at=123,
                runner_pane_id="%2",
                runner_log_path="/tmp/2.jsonl",
            ),
        ]

    def delete_pending_turns(self, pending_turn_ids: list[int]) -> None:
        self.deleted = pending_turn_ids

    def mark_turn_finished(self, pending_turn_id: int, error: str | None = None) -> None:
        self.finished.append((pending_turn_id, error))

    def update_last_email_message_id(self, agent_id: str, message_id: str) -> None:
        self.updated_email.append((agent_id, message_id))
        self.thread.last_email_message_id = message_id


class _StartDB:
    def __init__(self) -> None:
        self.marked: list[tuple[int, str | None, str | None, str | None]] = []
        self.updated_thread_ids: list[tuple[str, str]] = []

    def tracked_threads(self):
        return [
            type(
                "Thread",
                (),
                {
                    "agent_id": "agent-1",
                    "codex_thread_id": "session-123",
                    "workspace_path": "/tmp/work",
                },
            )()
        ]

    def pending_turns_for_agent(self, agent_id: str, statuses=("queued", "running")):
        return []

    def next_queued_turn(self, agent_id: str) -> PendingTurn | None:
        return PendingTurn(
            id=7,
            agent_id=agent_id,
            gmail_message_id="m1",
            reply_to_message_id=None,
            text_body="continue",
            image_paths=["/tmp/one.png"],
            attachment_paths=[],
            status="queued",
            codex_turn_id=None,
            started_at=None,
            runner_pane_id=None,
            runner_log_path=None,
        )

    def update_thread_codex_id(self, agent_id: str, codex_thread_id: str) -> None:
        self.updated_thread_ids.append((agent_id, codex_thread_id))

    def mark_turn_running(self, pending_turn_id: int, *, runner_pane_id: str | None = None, runner_log_path: str | None = None, codex_turn_id: str | None = None) -> None:
        self.marked.append((pending_turn_id, codex_turn_id, runner_pane_id, runner_log_path))


def test_start_queued_turn_uses_resume_session_id() -> None:
    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _StartDB()
    daemon.exec = _FakeExec()

    daemon._start_queued_turns()

    assert daemon.exec.started == [
        {
            "agent_id": "agent-1",
            "workspace": daemon.exec.started[0]["workspace"],
            "pending_turn_id": 7,
            "prompt": "continue",
            "image_paths": ["/tmp/one.png"],
            "resume_session_id": "session-123",
        }
    ]
    assert daemon.db.updated_thread_ids == []
    assert daemon.db.marked == [(7, "turn-123", "%1", "/tmp/turn.jsonl")]


class _SyncDB:
    def __init__(self) -> None:
        self.finished: list[tuple[int, str | None]] = []
        self.recorded: list[tuple[str, str, str]] = []
        self.updated_email: list[tuple[str, str]] = []
        self.updated_thread_ids: list[tuple[str, str]] = []
        self.thread = type(
            "Thread",
            (),
            {
                "agent_id": "agent-1",
                "codex_thread_id": "local:agent-1",
                "gmail_thread_id": "g1",
                "workspace_path": "/tmp/work",
                "canonical_subject": "subject",
                "last_email_message_id": "<last@msg>",
            },
        )()

    def get_thread_by_agent(self, agent_id: str):
        return self.thread

    def update_thread_codex_id(self, agent_id: str, codex_thread_id: str) -> None:
        self.updated_thread_ids.append((agent_id, codex_thread_id))
        self.thread.codex_thread_id = codex_thread_id

    def mark_turn_finished(self, pending_turn_id: int, error: str | None = None) -> None:
        self.finished.append((pending_turn_id, error))

    def turn_email_exists(self, turn_id: str, kind: str) -> bool:
        return any(recorded_turn_id == turn_id and recorded_kind == kind for recorded_turn_id, recorded_kind, _ in self.recorded)

    def record_turn_email(self, turn_id: str, kind: str, email_message_id: str) -> None:
        self.recorded.append((turn_id, kind, email_message_id))

    def update_last_email_message_id(self, agent_id: str, message_id: str) -> None:
        self.updated_email.append((agent_id, message_id))
        self.thread.last_email_message_id = message_id


def _pending() -> PendingTurn:
    return PendingTurn(
        id=1,
        agent_id="agent-1",
        gmail_message_id="m1",
        reply_to_message_id="<reply@msg>",
        text_body="continue",
        image_paths=[],
        attachment_paths=[],
        status="running",
        codex_turn_id="turn-123",
        started_at=123,
        runner_pane_id="%1",
        runner_log_path="/tmp/turn.jsonl",
    )


def test_sync_pending_turn_sends_progress_and_final_reply() -> None:
    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _SyncDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()
    daemon.exec.state = ExecTurnState(
        thread_id="session-123",
        first_agent_text="working",
        last_agent_text="done",
        turn_completed=True,
    )

    daemon._sync_pending_turn(daemon.db.thread, _pending())

    assert daemon.db.updated_thread_ids == [("agent-1", "session-123")]
    assert daemon.db.finished == [(1, None)]
    assert [call["markdown_body"] for call in daemon.gmail.calls] == ["working", "done"]


def test_sync_pending_turn_completed_without_final_message_uses_error_reply() -> None:
    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _SyncDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()
    daemon.exec.state = ExecTurnState(
        errors=["Selected model is at capacity. Please try a different model."],
        turn_completed=True,
        exit_code=0,
    )

    daemon._sync_pending_turn(daemon.db.thread, _pending())

    assert daemon.db.finished == [(1, "Selected model is at capacity. Please try a different model.")]
    assert daemon.gmail.calls == [
        {
            "subject": "Re: subject",
            "markdown_body": "Codex error:\n\nSelected model is at capacity. Please try a different model.",
            "parent_message_id": "<reply@msg>",
            "references": ["<last@msg>"],
        }
    ]


def test_sync_pending_turn_emails_failure() -> None:
    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _SyncDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()
    daemon.exec.state = ExecTurnState(
        turn_failed="boom",
        exit_code=1,
    )

    daemon._sync_pending_turn(daemon.db.thread, _pending())

    assert daemon.db.finished == [(1, "boom")]
    assert daemon.gmail.calls == [
        {
            "subject": "Re: subject",
            "markdown_body": "Codex error:\n\nboom",
            "parent_message_id": "<reply@msg>",
            "references": ["<last@msg>"],
        }
    ]


def test_sync_pending_turn_does_not_email_interrupted_failure() -> None:
    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _SyncDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()
    daemon.exec.state = ExecTurnState(
        turn_failed="interrupted",
        exit_code=130,
    )

    daemon._sync_pending_turn(daemon.db.thread, _pending())

    assert daemon.db.finished == [(1, "interrupted")]
    assert daemon.gmail.calls == []


def test_sync_pending_turn_marks_missing_codex_process_as_failure() -> None:
    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _SyncDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()
    daemon.exec.live_panes.clear()

    daemon._sync_pending_turn(daemon.db.thread, _pending())

    assert daemon.db.finished == [(1, "Codex exited without a final status.")]
    assert daemon.gmail.calls == [
        {
            "subject": "Re: subject",
            "markdown_body": "Codex error:\n\nCodex exited without a final status.",
            "parent_message_id": "<reply@msg>",
            "references": ["<last@msg>"],
        }
    ]


def test_handle_end_command_interrupts_running_turns_kills_session_and_acks() -> None:
    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _EndDB()
    daemon.gmail = _FakeGmail()
    daemon.exec = _FakeExec()

    daemon._handle_end_command(daemon.db.thread, "<reply@msg>")

    assert daemon.exec.interrupted == ["%2"]
    assert daemon.exec.killed_sessions == ["agent-1"]
    assert daemon.db.deleted == [1]
    assert daemon.db.finished == [(2, "Ended by email command.")]
    assert daemon.gmail.calls == [
        {
            "subject": "Re: subject",
            "markdown_body": "Ended. The tmux session was stopped. Reply again on this thread to resume the same Codex session.",
            "parent_message_id": "<reply@msg>",
            "references": ["<last@msg>"],
        }
    ]
