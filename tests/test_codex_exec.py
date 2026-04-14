from pathlib import Path
import os

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


def test_pane_text_indicates_ready_for_resumed_prompt() -> None:
    manager = CodexExecManager(_config())

    pane_text = """
  previous output

› Explain this codebase

  gpt-5.4 high · ~/coding/cad-bench · Context [     ] · weekly 68%
"""

    assert manager._pane_text_indicates_ready(pane_text) is True


def test_pane_text_indicates_ready_for_empty_prompt() -> None:
    manager = CodexExecManager(_config())

    pane_text = """
╭────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.121.0-alpha.2)         │
╰────────────────────────────────────────────╯

›

  gpt-5.4 high · ~/coding · Context [     ] · weekly 68%
"""

    assert manager._pane_text_indicates_ready(pane_text) is True


def test_pane_text_indicates_not_ready_for_header_only_splash() -> None:
    manager = CodexExecManager(_config())

    pane_text = """
╭────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.121.0-alpha.2)         │
│                                            │
│ model:     gpt-5.4 high   /model to change │
│ directory: ~/coding                        │
╰────────────────────────────────────────────╯
"""

    assert manager._pane_text_indicates_ready(pane_text) is False


def test_pane_text_indicates_working_for_active_turn() -> None:
    manager = CodexExecManager(_config())

    pane_text = """
› Create a new github repo

• Working (0s • esc to interrupt)
"""

    assert manager._pane_text_indicates_working(pane_text) is True


def test_wait_for_new_session_resubmits_idle_prompt_once(tmp_path: Path, monkeypatch) -> None:
    manager = CodexExecManager(_config())
    workspace = tmp_path / "work"
    workspace.mkdir()
    session_path = tmp_path / "rollout-session-123.jsonl"
    state = {"submitted": 0, "clock": -0.5}

    def fake_submit(pane_id: str) -> None:
        assert pane_id == "%1"
        state["submitted"] += 1
        session_path.write_text(
            '{"type":"session_meta","payload":{"id":"session-123","cwd":"%s"}}\n' % workspace,
            encoding="utf-8",
        )
        os.utime(session_path, (10, 10))

    def fake_session_files() -> list[Path]:
        if state["submitted"]:
            return [session_path]
        return []

    def fake_monotonic() -> float:
        state["clock"] += 0.5
        return state["clock"]

    monkeypatch.setattr(manager, "_submit_prompt", fake_submit)
    monkeypatch.setattr(manager, "_session_files", fake_session_files)
    monkeypatch.setattr(manager, "_capture_pane", lambda pane_id: "› Create a new github repo")
    monkeypatch.setattr(manager, "pane_exists", lambda pane_id: True)
    monkeypatch.setattr("codex_mailbridge.codex_exec.time.monotonic", fake_monotonic)
    monkeypatch.setattr("codex_mailbridge.codex_exec.time.sleep", lambda seconds: None)

    found_path, session_id = manager._wait_for_new_session(workspace, launched_at=0.0, pane_id="%1")

    assert found_path == session_path
    assert session_id == "session-123"
    assert state["submitted"] == 1


def test_start_turn_reuses_existing_live_agent_pane(tmp_path: Path, monkeypatch) -> None:
    manager = CodexExecManager(_config())
    session_path = tmp_path / "session.jsonl"
    session_path.write_text('{"type":"session_meta","payload":{"id":"session-123","cwd":"/tmp/work"}}\n', encoding="utf-8")
    sent_prompts: list[tuple[str, str]] = []

    monkeypatch.setattr(manager, "_session_pane_id", lambda agent_id: "%9")
    monkeypatch.setattr(manager, "pane_running_codex", lambda pane_id: pane_id == "%9")
    monkeypatch.setattr(
        manager,
        "ensure_tmux_session",
        lambda *, agent_id, shell_command: (manager._session_name(agent_id), "%9"),
    )
    monkeypatch.setattr(manager, "_session_path_for_id", lambda session_id: session_path)
    monkeypatch.setattr(manager, "_send_prompt", lambda pane_id, prompt: sent_prompts.append((pane_id, prompt)))
    monkeypatch.setattr(manager, "_wait_for_turn_id", lambda path, start_size: "turn-456")
    monkeypatch.setattr(manager, "_session_has_attached_clients", lambda session_name: True)

    started = manager.start_turn(
        agent_id="some questions",
        workspace=Path("/tmp/work"),
        pending_turn_id=12,
        prompt="continue",
        image_paths=[],
        resume_session_id="session-123",
    )

    assert started.pane_id == "%9"
    assert started.log_path == str(session_path)
    assert started.thread_id == "session-123"
    assert started.turn_id == "turn-456"
    assert sent_prompts == [("%9", "continue")]
