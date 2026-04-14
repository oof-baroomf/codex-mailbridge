from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time

from .config import Config


LOG = logging.getLogger(__name__)
DEFAULT_CODEX_BIN = "/home/d/.bun/bin/codex"
TMUX_SESSION_PREFIX = "codex-mailbridge"
CODEX_SESSION_DIR = Path.home() / ".codex" / "sessions"
POLL_INTERVAL_SECONDS = 0.25
READY_TIMEOUT_SECONDS = 30.0
SESSION_TIMEOUT_SECONDS = 45.0
TURN_TIMEOUT_SECONDS = 45.0
KEYSTROKE_DELAY_SECONDS = 0.02
SUBMIT_DELAY_SECONDS = 0.25
RESUBMIT_DELAY_SECONDS = 2.0


@dataclass(slots=True)
class StartedTurn:
    pane_id: str
    log_path: str
    thread_id: str | None = None
    turn_id: str | None = None


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

    def _session_name(self, agent_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", agent_id).strip("-") or "agent"
        return f"{TMUX_SESSION_PREFIX}-{sanitized[:40]}"

    def _window_name(self, agent_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", agent_id).strip("-") or "agent"
        return sanitized[:40]

    def _session_target(self, agent_id: str) -> str:
        return f"{self._session_name(agent_id)}:0"

    def ensure_tmux_session(
        self,
        *,
        agent_id: str,
        shell_command: str,
    ) -> tuple[str, str]:
        session_name = self._session_name(agent_id)
        exists = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if exists.returncode == 0:
            pane_id = self._session_pane_id(agent_id)
            if pane_id:
                return session_name, pane_id
            self.kill_agent_session(agent_id)
        proc = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-s",
                session_name,
                "-n",
                self._window_name(agent_id),
                shell_command,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        pane_id = proc.stdout.strip()
        if not pane_id:
            raise RuntimeError("tmux did not return a pane id")
        return session_name, pane_id

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
        session_path = self._session_path_for_id(resume_session_id) if resume_session_id else None
        if resume_session_id and session_path is None:
            raise RuntimeError(f"Could not locate Codex session file for {resume_session_id}")

        shell_command = self._build_shell_command(
            workspace=workspace,
            image_paths=image_paths,
            resume_session_id=resume_session_id,
        )
        existing_pane_id = self._session_pane_id(agent_id)
        pane_started_fresh = existing_pane_id is None or not self.pane_running_codex(existing_pane_id)
        if existing_pane_id and not self.pane_running_codex(existing_pane_id):
            self.kill_agent_session(agent_id)
            existing_pane_id = None

        launched_at = time.time()
        session_name, pane_id = self.ensure_tmux_session(agent_id=agent_id, shell_command=shell_command)
        if pane_started_fresh:
            self._wait_for_codex_ready(pane_id)

        turn_id: str | None = None
        if session_path is None:
            self._send_prompt(pane_id, prompt)
            session_path, resume_session_id = self._wait_for_new_session(workspace, launched_at, pane_id)
            turn_id = self._wait_for_turn_id(session_path, 0)
        assert session_path is not None
        if resume_session_id is not None and turn_id is None:
            pre_submit_size = session_path.stat().st_size if session_path.exists() else 0
            self._send_prompt(pane_id, prompt)
            turn_id = self._wait_for_turn_id(session_path, pre_submit_size)

        if not self._session_has_attached_clients(session_name):
            subprocess.run(["tmux", "select-window", "-t", self._session_target(agent_id)], check=False)
        return StartedTurn(
            pane_id=pane_id,
            log_path=str(session_path),
            thread_id=resume_session_id,
            turn_id=turn_id,
        )

    def interrupt_turn(self, pane_id: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-c"], check=False)

    def kill_agent_session(self, agent_id: str) -> None:
        session_name = self._session_name(agent_id)
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)

    def pane_exists(self, pane_id: str) -> bool:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == pane_id

    def pane_running_codex(self, pane_id: str) -> bool:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_dead} #{pane_current_command}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        parts = result.stdout.strip().split(maxsplit=1)
        if len(parts) != 2:
            return False
        pane_dead, current_command = parts
        return pane_dead == "0" and current_command in {"codex", "node"}

    def read_turn_state(self, log_path: str | None, codex_turn_id: str | None = None) -> ExecTurnState:
        state = ExecTurnState()
        if not log_path:
            return state
        path = Path(log_path)
        if not path.exists():
            return state

        target_turn_id = codex_turn_id if codex_turn_id and not codex_turn_id.startswith("pending:") else None
        capturing = target_turn_id is None
        active_turn_id: str | None = None

        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("Ignoring non-JSON Codex session line: %s", line)
                continue

            event_type = event.get("type")
            if event_type == "session_meta":
                payload = event.get("payload", {})
                thread_id = str(payload.get("id", "")).strip()
                if thread_id:
                    state.thread_id = thread_id
                continue

            if event_type != "event_msg":
                continue

            payload = event.get("payload", {})
            payload_type = str(payload.get("type", "")).strip()
            if payload_type == "task_started":
                next_turn_id = str(payload.get("turn_id", "")).strip() or None
                if target_turn_id and active_turn_id == target_turn_id and next_turn_id != target_turn_id and not state.turn_completed and state.turn_failed is None:
                    state.turn_failed = "Codex started a newer turn before finishing this one."
                    state.exit_code = 1
                    break
                active_turn_id = next_turn_id
                if target_turn_id is None:
                    capturing = True
                    state.first_agent_text = ""
                    state.last_agent_text = ""
                    state.errors.clear()
                    state.turn_completed = False
                    state.turn_failed = None
                    state.exit_code = None
                else:
                    capturing = active_turn_id == target_turn_id
                continue

            if not capturing:
                continue

            if payload_type == "agent_message":
                text = str(payload.get("message", "")).strip()
                if not text:
                    continue
                if not state.first_agent_text:
                    state.first_agent_text = text
                state.last_agent_text = text
                continue

            if payload_type == "task_complete":
                completed_turn_id = str(payload.get("turn_id", "")).strip()
                if target_turn_id and completed_turn_id != target_turn_id:
                    continue
                last_message = str(payload.get("last_agent_message", "")).strip()
                if last_message:
                    if not state.first_agent_text:
                        state.first_agent_text = last_message
                    state.last_agent_text = last_message
                state.turn_completed = True
                state.exit_code = 0
                continue

            if payload_type == "task_interrupted":
                state.turn_failed = str(payload.get("message", "")).strip() or "interrupted"
                state.exit_code = 130
                continue

            if payload_type == "error":
                message = str(payload.get("message", "")).strip()
                if message:
                    state.errors.append(message)
                continue

        return state

    def _build_shell_command(
        self,
        *,
        workspace: Path,
        image_paths: list[str],
        resume_session_id: str | None,
    ) -> str:
        argv = self._build_codex_argv(
            workspace=workspace,
            image_paths=image_paths,
            resume_session_id=resume_session_id,
        )
        return f"/usr/bin/bash -lc {shlex.quote(shlex.join(argv))}"

    def _build_codex_argv(
        self,
        *,
        workspace: Path,
        image_paths: list[str],
        resume_session_id: str | None,
    ) -> list[str]:
        if resume_session_id:
            argv = [
                self.codex_bin,
                "resume",
                "--no-alt-screen",
                "-C",
                str(workspace),
            ]
        else:
            argv = [
                self.codex_bin,
                "--no-alt-screen",
                "-C",
                str(workspace),
            ]
        for image_path in image_paths:
            argv.extend(["-i", image_path])
        if resume_session_id:
            argv.append(resume_session_id)
        return argv

    def _wait_for_codex_ready(self, pane_id: str) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            pane_text = self._capture_pane(pane_id)
            if "Press enter to continue" in pane_text:
                subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-m"], check=False)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if self._pane_text_indicates_ready(pane_text):
                return
            if not self.pane_exists(pane_id):
                raise RuntimeError("Codex pane exited before the TUI became ready.")
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("Timed out waiting for Codex TUI to become ready.")

    def _wait_for_new_session(self, workspace: Path, launched_at: float, pane_id: str) -> tuple[Path, str]:
        deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
        workspace_str = str(workspace)
        resubmitted = False
        resubmit_at = time.monotonic() + RESUBMIT_DELAY_SECONDS
        while time.monotonic() < deadline:
            candidates: list[tuple[float, Path, str]] = []
            for path in self._session_files():
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                if stat.st_mtime < launched_at - 1:
                    continue
                session_id, cwd = self._read_session_meta(path)
                if not session_id or cwd != workspace_str:
                    continue
                candidates.append((stat.st_mtime, path, session_id))
            if candidates:
                _, path, session_id = max(candidates, key=lambda item: item[0])
                return path, session_id
            if not resubmitted and time.monotonic() >= resubmit_at:
                pane_text = self._capture_pane(pane_id)
                if self._pane_text_indicates_ready(pane_text) and not self._pane_text_indicates_working(pane_text):
                    self._submit_prompt(pane_id)
                    resubmitted = True
                elif not self.pane_exists(pane_id):
                    raise RuntimeError("Codex pane exited before a new session file was created.")
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("Timed out waiting for a new Codex session file.")

    def _wait_for_turn_id(self, session_path: Path, start_size: int) -> str:
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for raw_line in self._session_lines_since(session_path, start_size):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "event_msg":
                    continue
                payload = event.get("payload", {})
                if payload.get("type") != "task_started":
                    continue
                turn_id = str(payload.get("turn_id", "")).strip()
                if turn_id:
                    return turn_id
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("Timed out waiting for Codex to accept the prompt.")

    def _send_prompt(self, pane_id: str, prompt: str) -> None:
        lines = prompt.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for index, line in enumerate(lines):
            if line:
                subprocess.run(["tmux", "send-keys", "-l", "-t", pane_id, "--", line], check=False)
                time.sleep(KEYSTROKE_DELAY_SECONDS)
            if index < len(lines) - 1:
                subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-j"], check=False)
                time.sleep(KEYSTROKE_DELAY_SECONDS)
        time.sleep(SUBMIT_DELAY_SECONDS)
        self._submit_prompt(pane_id)

    def _session_path_for_id(self, session_id: str | None) -> Path | None:
        if not session_id:
            return None
        matches = sorted(CODEX_SESSION_DIR.rglob(f"*{session_id}.jsonl"))
        if matches:
            return matches[-1]
        for path in self._session_files():
            found_session_id, _ = self._read_session_meta(path)
            if found_session_id == session_id:
                return path
        return None

    def _read_session_meta(self, path: Path) -> tuple[str | None, str | None]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first_line = handle.readline().strip()
        except FileNotFoundError:
            return None, None
        if not first_line:
            return None, None
        try:
            event = json.loads(first_line)
        except json.JSONDecodeError:
            return None, None
        if event.get("type") != "session_meta":
            return None, None
        payload = event.get("payload", {})
        session_id = str(payload.get("id", "")).strip() or None
        cwd = str(payload.get("cwd", "")).strip() or None
        return session_id, cwd

    def _session_files(self) -> list[Path]:
        if not CODEX_SESSION_DIR.exists():
            return []
        return [path for path in CODEX_SESSION_DIR.rglob("*.jsonl") if path.is_file()]

    def _session_lines_since(self, session_path: Path, start_size: int) -> list[str]:
        try:
            with session_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start_size)
                return handle.read().splitlines()
        except FileNotFoundError:
            return []

    def _capture_pane(self, pane_id: str) -> str:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout

    def _pane_text_indicates_ready(self, pane_text: str) -> bool:
        for line in pane_text.splitlines():
            stripped = line.strip()
            if stripped == "›" or stripped.startswith("› "):
                return True
        return False

    def _pane_text_indicates_working(self, pane_text: str) -> bool:
        lowered = pane_text.lower()
        return "esc to interrupt" in lowered or "working (" in lowered or "working…" in lowered

    def _submit_prompt(self, pane_id: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-m"], check=False)

    def _session_has_attached_clients(self, session_name: str) -> bool:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session_name, "#{session_attached}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        attached = result.stdout.strip()
        return attached not in {"", "0"}

    def _session_pane_id(self, agent_id: str) -> str | None:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self._session_target(agent_id), "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        pane_id = result.stdout.strip()
        if result.returncode != 0 or not pane_id:
            return None
        return pane_id
