# Claude Telegram Bridge 0.14.7

- Show Go / Hold buttons under a final answer that asks for a GO. A tap is sent once, only for that answer, and the buttons expire after one hour. Request detection and button labels are currently Korean only.
- Submit long free-text answers to question cards reliably, including wrapped answers, narrow terminals and clipped questions. The bridge submits once, and only when the on-screen question and the full typed text still match.
- Cancel follow-up messages already absorbed into an interrupted turn. A following `/clear` no longer waits on the interrupted turn's final transcript writes.
- Keep the Telegram typing indicator on during long turns. Claude Code's rotating status line counts as live work, and reasoning-only records no longer close a pending response before its text arrives.
- Clear the typing indicator when background work ends, including tasks that finish mid-turn or are killed and sub-agents whose completion notice was lost.

Validation: public export, private-data/token scan, manifest parity, and 43 tests (1 Cursor-only skip) on Python 3.10 and 3.14. Source bundle, wheel and source distribution are included.

This is a GitHub release. It does not upload to PyPI or restart an installed bridge.
