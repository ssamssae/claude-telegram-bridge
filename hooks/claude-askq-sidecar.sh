#!/usr/bin/env bash
# claude-askq-sidecar.sh — PreToolUse(AskUserQuestion) 질문 원본 사이드카 (T-261010-024).
#
# 왜: Claude Code 는 AskUserQuestion 의 tool_use 를 답한 뒤에야 세션 JSONL 에 쓴다
#   (2.1.295·2.1.296 재현 실측 — 질문이 떠 있는 동안 0건, 답/Esc 직후 2건). 텔레그램
#   브릿지는 머리줄이 잘린 좁은 화면(폰 53칸·tui=fullscreen)에서 원본과 화면을 대조해야
#   질문 카드를 만들 수 있는데, 원본이 답한 뒤에야 생겨 카드가 안 나갔다
#   (2026-10-10 macOS 노드 12:22·12:38). 이 훅이 질문이 뜨는 순간 같은 원본을 남긴다.
#
# 계약: 판정을 내지 않는다(permissionDecision·stdout 없음). 항상 exit 0, 실패는 조용히
#   지나간다(fail-open). 쓰는 곳 = ${CLB_ASKQ_SIDECAR_DIR:-~/.claude/state/claude-askq-sidecar}
#   /<session_id>.json (0600, 원자 교체, 세션당 최신 1건). 읽는 곳 =
#   scripts/claude-telegram-bridge.py read_askq_sidecar().
set -u
command -v python3 >/dev/null 2>&1 || exit 0
python3 -c '
import json, os, re, sys, tempfile, time
from pathlib import Path

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(payload, dict) or payload.get("tool_name") != "AskUserQuestion":
    sys.exit(0)
session_id = payload.get("session_id")
tool_use_id = payload.get("tool_use_id")
tool_input = payload.get("tool_input")
questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
if (not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id)
        or not isinstance(tool_use_id, str) or not tool_use_id
        or not isinstance(questions, list) or not questions):
    sys.exit(0)
raw = os.environ.get("CLB_ASKQ_SIDECAR_DIR", "").strip()
root = Path(raw).expanduser() if raw else Path.home() / ".claude" / "state" / "claude-askq-sidecar"
transcript = payload.get("transcript_path")
record = {
    "schema": 1, "session_id": session_id, "tool_use_id": tool_use_id,
    "transcript_path": transcript if isinstance(transcript, str) else "",
    "questions": questions, "written_at": time.time(),
}
tmp = None
try:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".askq-", suffix=".tmp", dir=str(root))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False)
    os.replace(tmp, str(root / (session_id + ".json")))
    tmp = None
except Exception:
    pass
finally:
    if tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass
sys.exit(0)
' 2>/dev/null
exit 0
