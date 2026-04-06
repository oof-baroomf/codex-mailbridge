from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)


@dataclass(slots=True)
class AppPasswordConfig:
    password: str

    @property
    def configured(self) -> bool:
        return bool(self.password)


@dataclass(slots=True)
class GmailConfig:
    address: str
    allowed_from: str
    user_visible_from: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    auth_mode: str
    oauth: OAuthConfig
    app_password: AppPasswordConfig

    @property
    def configured(self) -> bool:
        if self.auth_mode == "oauth":
            return self.oauth.configured
        if self.auth_mode == "app_password":
            return self.app_password.configured
        return False


@dataclass(slots=True)
class RuntimeConfig:
    poll_interval_seconds: int
    state_dir: Path
    log_dir: Path


@dataclass(slots=True)
class Config:
    gmail: GmailConfig
    runtime: RuntimeConfig
    path_mode: str
    config_path: Path


def _require_str(section: dict, key: str, default: str | None = None) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Missing or invalid string for {key}")
    return value


def _require_int(section: dict, key: str, default: int | None = None) -> int:
    value = section.get(key, default)
    if not isinstance(value, int):
        raise ConfigError(f"Missing or invalid int for {key}")
    return value


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text())

    mail = data.get("mail", {})
    gmail = mail.get("gmail", {})
    gmail_oauth = gmail.get("oauth", {})
    gmail_app_password = gmail.get("app_password", {})
    runtime = data.get("runtime", {})
    cfg = Config(
        gmail=GmailConfig(
            address=_require_str(gmail, "address"),
            allowed_from=_require_str(gmail, "allowed_from"),
            user_visible_from=_require_str(gmail, "user_visible_from"),
            imap_host=_require_str(gmail, "imap_host", "imap.gmail.com"),
            imap_port=_require_int(gmail, "imap_port", 993),
            smtp_host=_require_str(gmail, "smtp_host", "smtp.gmail.com"),
            smtp_port=_require_int(gmail, "smtp_port", 587),
            auth_mode=_require_str(gmail, "auth_mode"),
            oauth=OAuthConfig(
                client_id=str(gmail_oauth.get("client_id", "")).strip(),
                client_secret=str(gmail_oauth.get("client_secret", "")).strip(),
                refresh_token=str(gmail_oauth.get("refresh_token", "")).strip(),
                token_uri=str(gmail_oauth.get("token_uri", "https://oauth2.googleapis.com/token")).strip(),
            ),
            app_password=AppPasswordConfig(
                password=str(gmail_app_password.get("password", "")).strip(),
            ),
        ),
        runtime=RuntimeConfig(
            poll_interval_seconds=_require_int(runtime, "poll_interval_seconds", 20),
            state_dir=Path(_require_str(runtime, "state_dir", "/home/d/.local/state/codex-mailbridge")),
            log_dir=Path(_require_str(runtime, "log_dir", "/home/d/.local/state/codex-mailbridge/log")),
        ),
        path_mode=_require_str(mail, "path_mode", "absolute_or_home"),
        config_path=path,
    )
    if cfg.gmail.auth_mode not in {"oauth", "app_password"}:
        raise ConfigError("mail.gmail.auth_mode must be oauth or app_password")
    return cfg
