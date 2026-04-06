from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from .config import Config


LOG = logging.getLogger(__name__)
DEFAULT_CODEX_BIN = "/home/d/.bun/bin/codex"
TMUX_SESSION_NAME = "codex-mailbridge"
EXIT_SENTINEL_PREFIX = "__MAILBRIDGE_EXIT__ "


@dataclass(slots=True)
class StartedTurn:
    pane_id: str
    log_path: str


@dataclass(slots=True)
class ExecTurnState:
    thread_id: str | None = None
    first_agent_text: str = ""
    last_agent_text: str = ""
    errors: list[str] = field(default_factory=list)
    turn_completed: bool = False
    turn_failed: str | None = None
    exit_code: int | None = None

    @property
    def interrupted(self) -> bool:
        messages = [*self.errors]
        if self.turn_failed:
            messages.append(self.turn_failed)
        return self.exit_code == 130 or any("interrupted" in message.lower() for message in messages)

    def failure_text(self) -> str:
        if self.turn_failed:
            return self.turn_failed
        if self.errors:
            return self.errors[-1]
        if self.exit_code == 130:
            return "interrupted"
        if self.exit_code is not None:
            return f"Codex exited with status {self.exit_code}."
        return "Codex turn failed."


class CodexExecManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.codex_bin = shutil.which("codex") or DEFAULT_CODEX_BIN

    def ensure_tmux_session(self) -> None:
        exists = subprocess.run(
            ["tmux", "has-session", "-t", TMUX_SESSION_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
        if exists.returncode == 0:
            return
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", TMUX_SESSION_NAME, "-n", "mailbridge", "sleep infinity"],
            check=True,
        )

    def start_turn(
        self,
        *,
        agent_id: str,
        workspace: Path,
        pending_turn_id: int,
        prompt: str,
        image_paths: list[str],
        resume_session_id: str | None,
    ) -> StartedTurn:
        self.ensure_tmux_session()
        log_dir = self.config.runtime.state_dir / "runs" / agent_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{pending_turn_id}.jsonl"
        window_name = self._window_name(agent_id, pending_turn_id)
        shell_command = self._build_shell_command(
            workspace=workspace,
            prompt=prompt,
            image_paths=image_paths,
            log_path=log_path,
            resume_session_id=resume_session_id,
        )
        proc = subprocess.run(
            [
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id} #{pane_id}",
                "-t",
                TMUX_SESSION_NAME,
                "-n",
                window_name,
                shell_command,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        output = proc.stdout.strip().split()
        if len(output) != 2:
            raise RuntimeError("tmux did not return a window id and pane id")
        window_id, pane_id = output
        if not pane_id:
            raise RuntimeError("tmux did not return a pane id")
        if not self._session_has_attached_clients():
            subprocess.run(["tmux", "select-window", "-t", window_id], check=False)
        return StartedTurn(pane_id=pane_id, log_path=str(log_path))

    def interrupt_turn(self, pane_id: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-c"], check=False)

    def pane_exists(self, pane_id: str) -> bool:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == pane_id

    def read_turn_state(self, log_path: str | None) -> ExecTurnState:
        state = ExecTurnState()
        if not log_path:
            return state
        path = Path(log_path)
        if not path.exists():
            return state
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(EXIT_SENTINEL_PREFIX):
                suffix = line[len(EXIT_SENTINEL_PREFIX) :].strip()
                try:
                    state.exit_code = int(suffix)
                except ValueError:
                    LOG.warning("Ignoring invalid Codex exit sentinel: %s", line)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("Ignoring non-JSON Codex log line: %s", line)
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                thread_id = str(event.get("thread_id", "")).strip()
                if thread_id:
                    state.thread_id = thread_id
                continue
            if event_type == "item.completed":
                item = event.get("item", {})
                if item.get("type") != "agent_message":
                    continue
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                if not state.first_agent_text:
                    state.first_agent_text = text
                state.last_agent_text = text
                continue
            if event_type == "error":
                message = str(event.get("message", "")).strip()
                if message:
                    state.errors.append(message)
                continue
            if event_type == "turn.failed":
                error = event.get("error", {})
                message = str(error.get("message", "")).strip()
                if message:
                    state.turn_failed = message
                continue
            if event_type == "turn.completed":
                state.turn_completed = True
        return state

    def ensure_tui_window(self, *, agent_id: str, workspace: Path, session_id: str) -> None:
        self.ensure_tmux_session()
        window_name = self._tui_window_name(agent_id)
        if self._window_is_live(window_name):
            return
        if self._window_exists(window_name):
            subprocess.run(["tmux", "kill-window", "-t", f"{TMUX_SESSION_NAME}:{window_name}"], check=False)
        proc = subprocess.run(
            [
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}",
                "-t",
                TMUX_SESSION_NAME,
                "-n",
                window_name,
                self._build_tui_command(workspace=workspace, session_id=session_id),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        window_id = proc.stdout.strip()
        if window_id and not self._session_has_attached_clients():
            subprocess.run(["tmux", "select-window", "-t", window_id], check=False)

    def _build_shell_command(
        self,
        *,
        workspace: Path,
        prompt: str,
        image_paths: list[str],
        log_path: Path,
        resume_session_id: str | None,
    ) -> str:
        argv = self._build_codex_argv(
            workspace=workspace,
            prompt=prompt,
            image_paths=image_paths,
            resume_session_id=resume_session_id,
        )
        quoted_log_path = shlex.quote(str(log_path))
        sentinel_format = shlex.quote(EXIT_SENTINEL_PREFIX + "%s\n")
        shell_script = (
            "set -o pipefail\n"
            f"{shlex.join(argv)} 2>&1 | tee {quoted_log_path}\n"
            "status=${PIPESTATUS[0]}\n"
            f"printf {sentinel_format} \"$status\" >> {quoted_log_path}\n"
            "exit \"$status\""
        )
        return f"/usr/bin/bash -lc {shlex.quote(shell_script)}"

    def _build_tui_command(self, *, workspace: Path, session_id: str) -> str:
        argv = [
            self.codex_bin,
            "resume",
            "--include-non-interactive",
            "--cd",
            str(workspace),
            "--no-alt-screen",
            session_id,
        ]
        return shlex.join(argv)

    def _build_codex_argv(
        self,
        *,
        workspace: Path,
        prompt: str,
        image_paths: list[str],
        resume_session_id: str | None,
    ) -> list[str]:
        common = [
            self.codex_bin,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            str(workspace),
        ]
        for image_path in image_paths:
            common.extend(["-i", image_path])
        if resume_session_id:
            return [*common, "resume", resume_session_id, prompt]
        return [*common, prompt]

    def _window_name(self, agent_id: str, pending_turn_id: int) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", agent_id).strip("-") or "agent"
        return f"{pending_turn_id}-{sanitized[:40]}"

    def _tui_window_name(self, agent_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", agent_id).strip("-") or "agent"
        return f"tui-{sanitized[:36]}"

    def _window_exists(self, window_name: str) -> bool:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", f"{TMUX_SESSION_NAME}:{window_name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _window_is_live(self, window_name: str) -> bool:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{TMUX_SESSION_NAME}:{window_name}",
                "-F",
                "#{pane_dead} #{pane_current_command}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            dead, _, command = line.partition(" ")
            if dead == "0" and command.strip():
                return True
        return False

    def _session_has_attached_clients(self) -> bool:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", TMUX_SESSION_NAME, "#{session_attached}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        attached = result.stdout.strip()
        return attached not in {"", "0"}
