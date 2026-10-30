# Claude Telegram Bridge 0.14.13

- The progress card no longer keeps a finished job alive from the request copy that Claude Code writes to the transcript (`type=api-request-blob`). That copy repeats earlier tool calls. Before this release, the bridge registered them again, and a job that had already ended stayed on the card as "running" with a growing elapsed time. Completion notices that only appear in that copy still close their job.
- A background command that never started is now removed from the progress card. When a `run_in_background` command is refused before it runs (a guard, a permission denial or another error), no completion notice ever arrives. Before this release, the card kept it as "running" with a growing elapsed time. The bridge now drops the item when the command's result is an error or lacks the "running in background" confirmation. A result from the request copy alone never removes an item.

Validation: public export with the internal-symbol audit and the module parity gate, private-data and token scans with two independent scanners, 43 public tests on Python 3.14, twine check, and an installed-wheel resource check from a neutral folder in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
