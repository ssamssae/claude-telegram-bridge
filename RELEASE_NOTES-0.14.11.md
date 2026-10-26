# Claude Telegram Bridge 0.14.11

- When the outbound policy drops a progress copy box (the boxed text sent before a question or approval card), the bridge now treats it as done. Before this release, it re-read the same transcript line for about ten minutes, and the next question card could not be sent in that time. The dropped text still reaches you with the following approval card.
- In a long turn whose start an attachment re-bundles, the bridge now uses the newest turn boundary. Before this release, progress messages, copy boxes and the full text before an approval card could silently go missing in long turns.

Validation: public export with the internal-symbol audit and the module parity gate, private-data/token scan with a positive control, public tests, twine check, and an installed-wheel import check from a neutral folder in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
