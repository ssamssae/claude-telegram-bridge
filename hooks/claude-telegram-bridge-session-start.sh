#!/usr/bin/env bash
# claude-telegram-bridge SessionStart sidecar writer.
#
# Records the authoritative transcript_path/sessionId/pane_pid mapping consumed
# by scripts/claude-telegram-bridge.py.  This hook is intentionally silent and
# non-blocking; absence of a valid sidecar makes the bridge fail closed.

set -u

INPUT="$(cat 2>/dev/null || true)"
STATE="${CLB_SESSION_SIDECAR:-$HOME/.claude/state/claude-telegram-bridge-sessions.json}"
TMUX_BIN="${CLB_TMUX_BIN:-tmux}"
TMUX_SOCKET="${CLB_TMUX_SOCKET:-default}"
TMUX_SESSION="${CLB_TMUX_SESSION:-claude}"

python3 - "$STATE" "$TMUX_BIN" "$TMUX_SOCKET" "$TMUX_SESSION" "$INPUT" <<'PY' >/dev/null 2>&1
import json
import os
import subprocess
import sys
import time
from pathlib import Path

state = Path(sys.argv[1]).expanduser()
tmux_bin, tmux_socket, tmux_session = sys.argv[2], sys.argv[3], sys.argv[4]
raw = sys.argv[5]
try:
    payload = json.loads(raw or "{}")
except json.JSONDecodeError:
    payload = {}

transcript = payload.get("transcript_path") or ""
session_id = payload.get("session_id") or payload.get("sessionId") or ""
if not transcript or not session_id:
    raise SystemExit(0)

pane_pid = 0


def pane_targets():
    env_pane = os.environ.get("TMUX_PANE", "").strip()
    if env_pane:
        yield env_pane
    target = tmux_session
    if not (target.startswith("%") or ":" in target or "." in target):
        target = f"={target}:"
    yield target


def tmux_commands(target):
    base = ["display-message", "-p", "-t", target, "#{pane_pid}"]
    yield [tmux_bin, "-L", tmux_socket, *base]
    yield [tmux_bin, *base]


def resolve_pane_pid():
    for target in pane_targets():
        for cmd in tmux_commands(target):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=3,
                    stdin=subprocess.DEVNULL,
                )
                if proc.returncode == 0 and proc.stdout.strip().isdigit():
                    return int(proc.stdout.strip())
            except Exception:
                continue
    return 0


pane_pid = resolve_pane_pid()

state.parent.mkdir(parents=True, exist_ok=True)
try:
    current = json.loads(state.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        current = {}
except Exception:
    current = {}
sessions = current.get("sessions")
if not isinstance(sessions, dict):
    sessions = {}

key = f"{Path(transcript).expanduser()}|{session_id}"
sessions[key] = {
    "bridge": "claude-telegram-bridge",
    "host": os.uname().nodename,
    "updated_at": time.time(),
    "transcript_path": str(Path(transcript).expanduser()),
    "sessionId": str(session_id),
    "pane_pid": pane_pid,
    "tmux_socket": tmux_socket,
    "tmux_session": tmux_session,
}
tmp = state.with_name(state.name + ".tmp")
tmp.write_text(json.dumps({"updated_at": time.time(), "sessions": sessions}, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(state)
PY

exit 0
