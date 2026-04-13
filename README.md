# codex-mailbridge

Local daemon that:

- polls a mailbox for messages from one allowed sender
- maps Gmail threads to direct interactive Codex TUI sessions instead of using `app-server`
- runs every mailbridge-owned Codex thread inside its own tmux session named `codex-mailbridge-<agent>`
- inherits the normal global Codex CLI settings instead of forcing model/auth/agent overrides
- saves attachments into the workspace named in the first message subject
- queues only the new email reply text into Codex without adding extra instructions
- interrupts the current turn if a newer email arrives for the same thread
- emails the first assistant update immediately, then the last assistant reply when the turn finishes
- emails Codex failures instead of silently stalling

The runtime config lives at `/home/d/.config/codex-mailbridge/config.toml`.
