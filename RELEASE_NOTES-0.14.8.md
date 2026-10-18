# Claude Telegram Bridge 0.14.8

- Handoff cards are redesigned: one receiver progress message per handoff, queued input is received, and the final reply stays linked to its card.
- Choice and question cards are no longer dropped by the operations keyword filter. Undelivered cards are reported and logged instead of vanishing.
- Inputs injected during a busy turn are closed once the turn has consumed them, instead of being promoted again as leftovers.
- Diagnostic tool replays are ignored when the bridge judges whether the session is idle.
- Send-now, app permission and folder trust prompts are recognised from one screen-vocabulary block, covered by screen regression tests.
- Typing status logs record start, stop and keep-alive with the reason and caller, which makes a stuck "typing" state diagnosable.

Validation: public export, private-data/token scan with a positive control, manifest parity, and 43 tests on Python 3.14. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
