#!/usr/bin/env bash
# PreToolUse hook: block Telegram MCP reply while claude-telegram-bridge owns
# egress for this transcript/session.
# ⚠️ 제거 금지 (DO NOT REMOVE) — PR #163 clb-fix: single-egress guard prevents MCP reply double-send.

set -u

INPUT="$(cat 2>/dev/null || true)"

python3 - "$HOME" "$INPUT" <<'PY'
import json
import hashlib
import os
import sys
import time
from pathlib import Path

home = Path(sys.argv[1])
raw = sys.argv[2]
try:
    payload = json.loads(raw or "{}")
except json.JSONDecodeError:
    raise SystemExit(0)

if payload.get("tool_name") != "mcp__plugin_telegram_telegram__reply":
    raise SystemExit(0)

transcript = payload.get("transcript_path") or ""
session_id = payload.get("session_id") or payload.get("sessionId") or ""
sidecar = Path(os.environ.get("CLB_EGRESS_SIDECAR", str(home / ".claude/state/claude-telegram-bridge-egress.json"))).expanduser()

def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    raise SystemExit(0)

def load_sidecar(path: Path):
    if not path.exists():
        return None, False
    for _ in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8")), False
        except Exception:
            time.sleep(0.05)
    return None, True

def proc_start_time(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return stat.rsplit(") ", 1)[1].split()[19]
    except Exception:
        return ""

def proc_cmdline_sha256(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return ""
    return hashlib.sha256(raw).hexdigest() if raw else ""

def daemon_identity_valid(item: dict) -> bool:
    try:
        pid = int(item.get("daemon_pid") or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    pid_file_raw = str(item.get("daemon_pid_file") or "")
    if not pid_file_raw:
        return False
    pid_file = Path(pid_file_raw).expanduser()
    try:
        pid_state = json.loads(pid_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    if int(pid_state.get("pid") or 0) != pid:
        return False
    current_start = proc_start_time(pid)
    expected_starts = [
        str(item.get("daemon_pid_start_time") or ""),
        str(pid_state.get("pid_start_time") or ""),
    ]
    if any(value and current_start != value for value in expected_starts):
        return False
    current_cmdline = proc_cmdline_sha256(pid)
    expected_cmdlines = [
        str(item.get("daemon_cmdline_sha256") or ""),
        str(pid_state.get("cmdline_sha256") or ""),
    ]
    if any(value and current_cmdline != value for value in expected_cmdlines):
        return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True

state, unreadable = load_sidecar(sidecar)
if unreadable:
    deny("claude-telegram-bridge egress sidecar is unreadable; refusing Telegram MCP reply because egress ownership is unclear.")
if not isinstance(state, dict):
    raise SystemExit(0)

sessions = state.get("sessions")
if not isinstance(sessions, dict):
    raise SystemExit(0)

now = time.time()
if not transcript or not session_id:
    raise SystemExit(0)

for item in sessions.values():
    if not isinstance(item, dict):
        continue
    if str(item.get("transcript_path") or "") != transcript:
        continue
    if str(item.get("sessionId") or "") != session_id:
        continue
    nonce = str(item.get("claimed_turn_nonce") or "")
    if not nonce.startswith("clb-"):
        continue
    ttl = int(item.get("ttl_seconds") or 900)
    updated_at = float(item.get("updated_at") or 0)
    if updated_at <= 0 or now - updated_at > ttl:
        continue
    if not daemon_identity_valid(item):
        continue
    deny("claude-telegram-bridge owns Telegram egress for this session/turn; MCP reply tool is blocked to prevent duplicate sends.")

raise SystemExit(0)
PY
