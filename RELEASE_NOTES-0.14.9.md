# Claude Telegram Bridge 0.14.9

- New: a one-line Telegram notice when the session's model changes, for example after a safeguard-refusal fallback. The notice includes the `/model` command that returns to the configured default.
- Code blocks that are split out of a progress message into copy-paste bubbles leave a marker (↓ ①) where they were. Code fences in the text before a question card are also sent as their own copy-paste bubbles.
- Approval cards for outbound messages carry the full text that will be sent.
- Progress cards list running subagents. A card that is buried under newer messages is posted again at the bottom.
- Question and permission cards are recognised in more screen states: questions drawn without the emoji variation selector, questions truncated with "…" in small windows, and auto-deny countdowns. A permission prompt that is about to auto-deny is reported as a silent one-line notice instead of a button card.
- Background tasks: API request snapshot rows no longer count as background tasks, and a task that ends without a completion notice is closed when its output ends. This removes stuck "typing" states.
- Replies to messages that were sent while the session was busy are no longer dropped by the operations noise filter.
- Session binding: headless Claude runs outside a tmux pane no longer take over the chat window's session, the binder follows transcripts that moved with a worktree change, and local `/clear` records are not read as model turns.
- `/effort` followed by `/clear` waits up to 60 seconds and answers the confirmation dialog. A submit retry presses Enter only when the leftover composer text is the injected directive.
- Packaging: the model-notice module ships in the export and in the wheel. The export now fails when a top-level local import is missing from the package.

Validation: public export, private-data/token scan with a positive control, module parity gate with two negative probes, manifest parity and 43 public tests, and an installed-wheel import check in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
