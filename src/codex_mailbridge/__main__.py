from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .daemon import MailBridgeDaemon
from .emailer import GmailAuth


DEFAULT_CONFIG_PATH = Path("/home/d/.config/codex-mailbridge/config.toml")


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex-mailbridge")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "authorize-gmail"],
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.command == "authorize-gmail":
        GmailAuth(config).authorize()
        return

    daemon = MailBridgeDaemon(config)
    try:
        daemon.run()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.close()


if __name__ == "__main__":
    main()
