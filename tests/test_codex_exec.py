from pathlib import Path

from codex_mailbridge.codex_exec import CodexExecManager, EXIT_SENTINEL_PREFIX
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


def test_build_codex_argv_uses_exec_resume_for_existing_session() -> None:
    manager = CodexExecManager(_config())

    argv = manager._build_codex_argv(
        workspace=Path("/tmp/work"),
        prompt="continue",
        image_paths=["/tmp/one.png"],
        resume_session_id="session-123",
    )

    assert argv[:3] == [manager.codex_bin, "exec", "--json"]
    assert "resume" in argv
    assert argv[-2:] == ["session-123", "continue"]
    assert argv.count("-i") == 1


def test_build_shell_command_streams_to_tmux_and_log() -> None:
    manager = CodexExecManager(_config())

    command = manager._build_shell_command(
        workspace=Path("/tmp/work"),
        prompt="continue",
        image_paths=[],
        log_path=Path("/tmp/state/turn.jsonl"),
        resume_session_id=None,
    )

    assert command.startswith("/usr/bin/bash -lc ")
    assert "tee /tmp/state/turn.jsonl" in command
    assert 'status=${PIPESTATUS[0]}' in command
    assert EXIT_SENTINEL_PREFIX in command


def test_build_tui_command_resumes_non_interactive_session_inline() -> None:
    manager = CodexExecManager(_config())

    command = manager._build_tui_command(
        workspace=Path("/tmp/work"),
        session_id="session-123",
    )

    assert command.startswith(f"{manager.codex_bin} resume")
    assert "--include-non-interactive" in command
    assert "--no-alt-screen" in command
    assert "session-123" in command


def test_read_turn_state_parses_progress_completion_and_exit_code(tmp_path: Path) -> None:
    log_path = tmp_path / "turn.jsonl"
    log_path.write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"session-123"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"working"}}',
                '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"done"}}',
                '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}',
                f"{EXIT_SENTINEL_PREFIX}0",
            ]
        )
        + "\n"
    )
    manager = CodexExecManager(_config())

    state = manager.read_turn_state(str(log_path))

    assert state.thread_id == "session-123"
    assert state.first_agent_text == "working"
    assert state.last_agent_text == "done"
    assert state.turn_completed is True
    assert state.exit_code == 0


def test_read_turn_state_parses_failure_and_interrupt(tmp_path: Path) -> None:
    log_path = tmp_path / "turn.jsonl"
    log_path.write_text(
        "\n".join(
            [
                '{"type":"error","message":"interrupted (Ctrl-C). Something went wrong? Hit `/feedback` to report the issue."}',
                '{"type":"turn.failed","error":{"message":"interrupted (Ctrl-C). Something went wrong? Hit `/feedback` to report the issue."}}',
                f"{EXIT_SENTINEL_PREFIX}130",
            ]
        )
        + "\n"
    )
    manager = CodexExecManager(_config())

    state = manager.read_turn_state(str(log_path))

    assert state.turn_failed is not None
    assert state.interrupted is True
    assert state.failure_text() == state.turn_failed
