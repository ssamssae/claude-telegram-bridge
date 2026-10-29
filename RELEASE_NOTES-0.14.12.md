# Claude Telegram Bridge 0.14.12

- Question screens whose option rows or question line scrolled above a small pane are now matched against the native question and sent as button cards. Before this release, the bridge asked you to answer in the terminal.
- Question and approval cards now carry each option's preview text. When a question refers to text written just before it, that text goes into the first option's preview, so it reaches the phone together with the card.
- Question screens that show a preview box are read as native questions and sent as cards. Choosing an option on such a screen sends the option number and Enter, and the bridge recognises the screen as waiting for an answer.
- Approval cards raised during a background turn now include the text written before them. Text that does not fit in the card is sent in full, in pieces, just before the card.
- Preview question screens whose header scrolled above the pane are sent as cards too.
- After /clear, a late write to the previous transcript no longer wins the sidecar ordering race. Within the file-time window the newest record wins.
- Doubled emoji variation selectors (U+FE0F) are folded before pasting, so the pasted prompt keeps the same fingerprint as the original message.

Validation: public export with the internal-symbol audit and the module parity gate, private-data and token scans with two independent scanners, 43 public tests on Python 3.14, twine check, and an installed-wheel resource check from a neutral folder in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
