# codex-mailbridge

Local daemon that:

- polls a mailbox for messages from one allowed sender
- maps Gmail threads to direct interactive Codex TUI sessions instead of using `app-server`
- runs every mailbridge-owned Codex thread inside its own tmux session named `codex-mailbridge-<agent>`
- inherits the normal global Codex CLI settings instead of forcing model/auth/agent overrides
- saves attachments into the workspace named in the first message subject
- queues only the new email reply text into Codex without adding extra instructions
- treats reply lines starting with `!` as `bash -lc` commands in the thread workspace and emails their output back
- skips Codex entirely when a reply only contains `!` commands and blank lines
- queues new replies behind the current turn for that agent instead of injecting into a busy TUI
- fails a submitted turn if Codex does not accept the injected email within 90 seconds
- emails the first assistant update immediately, then the last assistant reply when the turn finishes
- emails Codex failures instead of silently stalling
- raises on broken IMAP fetches, Gmail API send failures, tmux send failures, and malformed Codex session logs instead of using silent fallbacks

The runtime config lives at `/home/d/.config/codex-mailbridge/config.toml`.
