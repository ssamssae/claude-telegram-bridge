# Release Checklist

Maintainer-only: this checklist and the export script below live in the
maintainer's private automation repo, not in this public repository. If you
forked this repo you can skip step 1 and run steps 2-4 directly against your
working tree.

Run these checks before publishing a repository or release.

1. Generate a clean export:

```bash
./scripts/claude-bridge-oss-export.sh
```

2. Confirm the export contains no private values. At minimum, scan for:

- private numeric chat ids
- private hostnames
- private node names
- maintainer usernames
- absolute home paths
- local secret paths

```bash
rg -n '<your-private-patterns-here>' dist/claude-telegram-bridge
```

Expected result: no matches.

3. Confirm no Bot API token-shaped strings exist:

```bash
rg -n '[0-9]{6,}:[A-Za-z0-9_-]{20,}' dist/claude-telegram-bridge
```

Expected result: no matches.

4. Compile and test import:

```bash
python3 -m py_compile \
  dist/claude-telegram-bridge/bridge_setup.py \
  dist/claude-telegram-bridge/bridge_watchdog.py \
  dist/claude-telegram-bridge/claude_telegram_bridge.py
python3 - <<'PY'
import importlib.util
import sys
spec = importlib.util.spec_from_file_location(
    "claude_telegram_bridge",
    "dist/claude-telegram-bridge/claude_telegram_bridge.py",
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
print(mod.BRIDGE_OWNER)
PY
```

5. Re-read README billing language. It must say billing classification is
unverified and must not claim subscription safety.

6. Build the PyPI distribution and validate it before any upload:

```bash
python3 -m build dist/claude-telegram-bridge --outdir dist/claude-telegram-bridge/pypi-dist
python3 -m twine check dist/claude-telegram-bridge/pypi-dist/*
```

Expected result: `twine check` reports `PASSED` for both the sdist and the wheel.

7. Public release (PyPI upload). Only after every check above passes AND the
intended maintainer explicitly accepts the operational risk. Requires the
maintainer's PyPI API token. Confirm the name is free/owned, upload, then verify
the self-update path resolves the new version:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/claude-telegram-bridge/json  # 404 = unclaimed
python3 -m twine upload dist/claude-telegram-bridge/pypi-dist/*
curl -s https://pypi.org/pypi/claude-telegram-bridge/json | python3 -c 'import sys, json; print(json.load(sys.stdin)["info"]["version"])'
```

The published name MUST equal `SELF_UPDATE_PACKAGE` in the bridge script, or the
in-app "update available" check (which queries `pypi.org/pypi/<name>/json`) never fires.
