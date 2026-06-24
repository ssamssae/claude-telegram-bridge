# Release Checklist

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
python3 -m py_compile dist/claude-telegram-bridge/claude_telegram_bridge.py
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

6. Public release only after the above checks pass and the intended maintainer
explicitly accepts the operational risk.
