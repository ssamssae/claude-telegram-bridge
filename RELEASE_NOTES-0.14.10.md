# Claude Telegram Bridge 0.14.10

- Multi-select questions can be answered from the phone. Each option is a button that checks or unchecks it (⬜/✅), and the card shows what is checked so far. The bridge confirms each change on the screen before it updates the card. "Done" works once at least one option is checked and moves to the next screen. The submit confirmation arrives as a normal question card and closes with "Submitted" when the answer is recorded. Before this release, the card asked you to choose in the terminal.
- Auto-deny countdowns that a narrow pane wraps across lines or draws inside a bordered box are left out of the card signature. The same permission prompt is no longer sent again every few seconds.
- When a progress message shortened the text before an approval card, the card carries the full preceding text.
- While the transcript shows an open question dialog, the bridge does not paste text into the terminal, whatever the screen reading says. Before this release, a busy-state misread could paste a message twice and Enter could submit the dialog's default option. The guard clears when the transcript shows the answer or a later assistant message.
- Progress cards close the rows of finished subagents when the completion notice is recorded as plain text content.
- Shared agent-control module: a Codex session that has not written its first turn yet is identified by its writer lock. Claude sessions are unchanged.

Validation: public export with the internal-symbol audit and the module parity gate, private-data/token scan with a positive control, 43 public tests, twine check, and an installed-wheel import check from a neutral folder in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
