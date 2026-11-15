# Claude Telegram Bridge 0.14.22

- Tapping an answer on a question card with option previews no longer fails with "The screen changed. Choose from the new card." When you tapped, the bridge read that screen as a plain menu, so the tap never matched the card. It now reads the screen in the same order it used to build the card.
- More question screens reach your phone as cards: several questions at once (tabs with Submit), several questions with option previews, and narrow terminals that wrap the key hint onto two lines. On wide terminals, text from a preview box no longer ends up in an option name.
- After you answer a question card, the card keeps the original question and previews next to your choice, so you can still see what you approved.
- The `/feedback` card handles the send-confirm step that newer Claude Code versions show inside the draft box ("Send without reviewing … 2 to send"), also when a short screen pushes the box's bottom border out of view. The card also sends when the draft list is wide or the environment line lists subagents, and it waits a moment before pressing Enter so the key is not lost. Agent-control status now reports this screen as `waiting` instead of `unknown`.
- Messages you send while Claude Code compacts the conversation wait until compaction ends. Before, the bridge pasted them during compaction, reported "Bridge submission failed", and the final answer to that message did not reach your phone.
- A final answer now finds its Telegram message after compaction and in very long turns, so it reaches your phone as a reply instead of being dropped.
- A turn that Claude Code still marks as busy in its session file is no longer released while the screen is quiet, for example during a long automatic compaction. The typing indicator stays on until the turn ends.
- When Claude Code ends a turn with a usage-limit message, the bridge treats it as the end of the turn: the typing indicator stops, queued messages move on, and you get one notice, with the reset time when Claude Code reports one. The `/model` hint shown with that message no longer blocks queued messages or `/model`.
- The composer is recognized in named conversations (after `/rename` or in background sessions), where Claude Code draws the name in the composer's top border. Before, an idle named session showed as `unknown` in agent-control status.

Validation: each change is covered by unit tests in the maintainer's bridge suite, including real-screen fixtures for the preview-question tap and the feedback send-confirm step; the full public test suite; and an install check of the built wheel.

This release is published on GitHub and PyPI. Upgrading changes no installed hook or service file and needs no new settings.
