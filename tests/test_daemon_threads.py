from codex_mailbridge.codex_exec import ExecTurnState, StartedTurn
from codex_mailbridge.daemon import MailBridgeDaemon
from codex_mailbridge.db import PendingTurn


class _FakeExec:
    def __init__(self) -> None:
        self.interrupted: list[str] = []
        self.started: list[dict] = []
        self.state = ExecTurnState()
        self.live_panes: set[str] = set()
        self.killed_sessions: list[str] = []

    def interrupt_turn(self, pane_id: str) -> None:
        self.interrupted.append(pane_id)

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

    def enqueue_turn(self, **kwargs) -> None:
        self.enqueued.append(kwargs)


def test_handle_incoming_running_thread_enqueues_without_interrupting(monkeypatch) -> None:
    from codex_mailbridge.daemon import IncomingMail

    daemon = object.__new__(MailBridgeDaemon)
    daemon.db = _QueueDB()
    daemon.exec = _FakeExec()
    monkeypatch.setattr("codex_mailbridge.daemon.save_attachments", lambda workspace, attachments: ([], []))

    daemon._handle_incoming(
        IncomingMail(
            uid="1",
            gmail_message_id="m3",
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
    assert daemon.db.enqueued == [
        {
            "agent_id": "agent-1",
            "gmail_message_id": "m3",
            "reply_to_message_id": "<reply@msg>",
            "text_body": "new request",
            "image_paths": [],
            "attachment_paths": [],
        }
    ]


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
