# Claude Telegram Bridge 0.14.21

- Setup no longer starts Claude in your home folder. Claude Code does not remember folder trust for the home folder, so a session started there stopped at the folder-trust question on every start. The session now starts in the folder you run setup from, or in `~/claude-workspace` when that folder is your home folder. Choose another folder with `setup --workdir <folder>`. Setup saves the folder as `CLB_SESSION_WORKDIR`, and `claude-telegram-bridge attach` reuses it.
- Setup creates the Claude tmux session before it starts the service, and the bridge waits for a missing session instead of exiting. Before, a first start (or a start after a reboot, before the session existed) ended with `tmux session not found` and the service restarted again and again.
- When a setup self-check does not pass, it reads the Claude screen and says what to do: tap `Yes, I trust this folder` at the folder-trust question, run `/login` at the login screen, or run `/login` when the banner shows `API Usage Billing` and you use a Claude subscription. The hint goes to your phone and to the terminal.
- A message you send while Claude is busy now gets its answer as a reply to that message, even when an older turn lost its owner earlier.
- Telegram service messages, such as the notice that a message was pinned, no longer get a "no text or media caption" reply. The bridge logs them and moves on.

Validation: unit coverage for the session folder choice, session-before-service order, waiting for a missing tmux session, the self-check screen hints, the busy-message reply target and service messages; the full public test suite; and a dry run of the installed wheel on macOS and Linux layouts.

This release is published on GitHub and PyPI. Upgrading changes no installed hook or service file. An existing `bridge.env` keeps working; setup writes `CLB_SESSION_WORKDIR` the next time you run it.
