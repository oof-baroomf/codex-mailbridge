from pathlib import Path

from codex_mailbridge.codex_exec import CODEX_SESSION_DIR, CodexExecManager, TMUX_SESSION_PREFIX
from codex_mailbridge.config import (
    AppPasswordConfig,
    Config,
    GmailConfig,
    OAuthConfig,
    RuntimeConfig,
)


def _config() -> Config:
    return Config(
        gmail=GmailConfig(
            address="bridge@example.com",
            allowed_from="user@example.com",
            user_visible_from="user@example.com",
            imap_host="imap.example.com",
            imap_port=993,
            smtp_host="smtp.example.com",
            smtp_port=587,
            auth_mode="oauth",
            oauth=OAuthConfig(client_id="x", client_secret="y", refresh_token="z", token_uri="https://example.com/token"),
            app_password=AppPasswordConfig(password=""),
        ),
        runtime=RuntimeConfig(
            poll_interval_seconds=20,
            state_dir=Path("/tmp/state"),
            log_dir=Path("/tmp/log"),
        ),
        path_mode="absolute_or_home",
        config_path=Path("/tmp/config.toml"),
    )


def test_build_codex_argv_uses_interactive_resume() -> None:
    manager = CodexExecManager(_config())

    argv = manager._build_codex_argv(
        workspace=Path("/tmp/work"),
        image_paths=["/tmp/one.png"],
        resume_session_id="session-123",
    )

    assert argv[:5] == [manager.codex_bin, "resume", "--no-alt-screen", "-C", "/tmp/work"]
    assert argv[-1] == "session-123"
    assert argv.count("-i") == 1


def test_build_codex_argv_uses_interactive_new_session() -> None:
    manager = CodexExecManager(_config())

    argv = manager._build_codex_argv(
        workspace=Path("/tmp/work"),
        image_paths=[],
        resume_session_id=None,
    )

    assert argv == [manager.codex_bin, "--no-alt-screen", "-C", "/tmp/work"]


def test_session_name_is_per_agent() -> None:
    manager = CodexExecManager(_config())

    assert manager._session_name("some questions") == f"{TMUX_SESSION_PREFIX}-some-questions"


def test_build_shell_command_launches_interactive_codex() -> None:
    manager = CodexExecManager(_config())

    command = manager._build_shell_command(
        workspace=Path("/tmp/work"),
        image_paths=[],
        resume_session_id=None,
    )

    assert command.startswith("/usr/bin/bash -lc ")
    assert "--no-alt-screen" in command
    assert "exec --json" not in command


def test_read_turn_state_parses_specific_turn_from_session_file(tmp_path: Path) -> None:
    log_path = tmp_path / "turn.jsonl"
    log_path.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"session-123","cwd":"/tmp/work"}}',
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}',
                '{"type":"event_msg","payload":{"type":"agent_message","message":"working","phase":"commentary"}}',
                '{"type":"event_msg","payload":{"type":"agent_message","message":"done","phase":"final_answer"}}',
                '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1","last_agent_message":"done"}}',
            ]
        )
        + "\n"
    )
    manager = CodexExecManager(_config())

    state = manager.read_turn_state(str(log_path), "turn-1")

    assert state.thread_id == "session-123"
    assert state.first_agent_text == "working"
    assert state.last_agent_text == "done"
    assert state.turn_completed is True
    assert state.exit_code == 0


def test_read_turn_state_ignores_older_turns_in_same_session(tmp_path: Path) -> None:
    log_path = tmp_path / "turn.jsonl"
    log_path.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"session-123","cwd":"/tmp/work"}}',
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}',
                '{"type":"event_msg","payload":{"type":"agent_message","message":"old answer","phase":"final_answer"}}',
                '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1","last_agent_message":"old answer"}}',
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-2"}}',
                '{"type":"event_msg","payload":{"type":"agent_message","message":"new progress","phase":"commentary"}}',
            ]
        )
        + "\n"
    )
    manager = CodexExecManager(_config())

    state = manager.read_turn_state(str(log_path), "turn-2")

    assert state.thread_id == "session-123"
    assert state.first_agent_text == "new progress"
    assert state.last_agent_text == "new progress"
    assert state.turn_completed is False


def test_session_path_for_id_uses_filename_match(tmp_path: Path, monkeypatch) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    path = session_root / "rollout-2026-04-13T13-34-34-session-123.jsonl"
    path.write_text('{"type":"session_meta","payload":{"id":"session-123","cwd":"/tmp/work"}}\n')
    monkeypatch.setattr("codex_mailbridge.codex_exec.CODEX_SESSION_DIR", session_root)
    manager = CodexExecManager(_config())

    assert manager._session_path_for_id("session-123") == path


def test_module_uses_default_session_root() -> None:
    assert CODEX_SESSION_DIR == Path.home() / ".codex" / "sessions"
