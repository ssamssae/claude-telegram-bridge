#!/usr/bin/env python3
"""Telegram bridge for an existing interactive Claude Code tmux session.

This is a Claude-specific sibling of ``codex-repl-telegram-bridge.py``.  It
reuses the proven Telegram/tmux/state plumbing shape, but final replies are
detected from Claude's transcript JSONL and are only sent for bridge-injected
turns carrying a nonce.

The daemon intentionally does not create new Claude sessions.  It pastes into a
live tmux pane and tails the SessionStart-bound transcript sidecar.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import math
import mimetypes
import os
import re
import secrets
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


HOME = Path.home()
KST = timezone(timedelta(hours=9), "KST")
NODE_EMOJI_LINES = {"\U0001f34e", "\U0001f3ed", "\U0001fa9f", "\U0001f5a5", "\U0001f4bb", "\U0001f916"}
BRIDGE_OWNER = "claude-telegram-bridge"
BRIDGE_HEALTH_SLASH_COMMANDS = {"/ping", "/start"}
BRIDGE_STATUS_SLASH_COMMAND = "/status"
# /context 는 좁은 tmux 창에서 잘리므로 캡처 동안만 창을 이 폭으로 넓힌다 (codex parity, T-260702-14).
STATUS_WIDE_CAPTURE_COLUMNS = 132
CONTEXT_SLASH_COMMAND = "/context"
# read-only 정보 명령 — 넓힌 창에서 실행·캡처해 터미널 화면 그대로 폰에 미러 (T-260702-14/T-260703-01).
CAPTURE_MIRROR_SLASH_COMMANDS = {CONTEXT_SLASH_COMMAND, "/usage", "/cost"}
# /model 은 원문 주입 시 인터랙티브 선택창이 세션을 점유(8분 프리즈 실사고 T-260703-17) —
# 주입 전 인터셉트해 inline keyboard 로 처리하고, 적용은 비대화형 인자형(/model <alias>)으로만 주입한다.
MODEL_SLASH_COMMAND = "/model"
MODEL_CALLBACK = "clb-model"
# 선택지 목록: env CLB_MODEL_CHOICES(콤마) 우선. 아래 fallback 은 모델 id 가 아니라 CLI alias 토큰
# (하드코딩 최소화 — 새 모델은 env 로 주입, 현재 모델 표시는 settings SoT 에서 동적).
DEFAULT_MODEL_MENU_ALIASES = ("default", "fable", "opus", "sonnet", "haiku")
# 프리즈 가드 밖 원문 강제 주입 escape hatch: 메시지 맨 앞 '!' 1자 (예: !/theme).
SLASH_ESCAPE_PREFIX = "!"
# 인터셉트 없이 그대로 통과시키는 슬래시 — 대화상자를 열지 않는(프리즈 위험 0) 명령만.
SAFE_PASSTHROUGH_SLASH_COMMANDS = {"/clear", "/exit", "/quit"}
# 세션을 종료시키는 슬래시 — 통과 후 브릿지가 watchdog 자가복구를 앞당겨 트리거한다.
# /clear 는 세션을 죽이지 않으므로(컨텍스트 리셋만) 제외.
SESSION_LIFECYCLE_SLASH_COMMANDS = {"/exit", "/quit"}
# /model 콜백/인자형이 진행 중 턴을 만났을 때 안내문 (T-260703-23): busy 면 주입을 미룬다 —
# clear_composer() 의 Escape 가 그 턴을 끊지 않도록. 사용자는 턴 종료 후 다시 누르면 된다.
MODEL_BUSY_DEFER_TEXT = (
    "⏳ 지금 진행 중인 턴이 있어 모델 전환을 미뤘어요 (진행 중 턴을 끊지 않아요).\n"
    "턴이 끝난 뒤 다시 선택해 주세요."
)
MCP_TELEGRAM_REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
BRACKETED_PASTE_RE = re.compile(r"\x1b\[(?:200|201)~")
NONCE_RE = re.compile(r"clb-[0-9a-f]{8,64}")
OUTBOUND_CLB_ENVELOPE_RE = re.compile(r"</?claude-telegram-bridge\b[^>]*>|<clb-[0-9a-f]{8,64}/>")
OUTBOUND_CLB_NONCE_RE = re.compile(r"\bclb-[0-9a-f]{8,64}\b")
OUTBOUND_CLB_GAP_RE = re.compile(r"[ \t]{2,}")
REASONING_HEADER = "\U0001f9e0 클로드 사고"
REASONING_MIRROR_LIMIT = 3500
# ⚙️ flow mirror — relays intermediate tool_use steps to Telegram in real time so
# the user can see the work flow between final answers, not just endpoints. Default
# OFF (flag-file gated, runtime-toggleable, no restart). Created per-node only.
FLOW_MIRROR_HEADER = "⚙️ 작업 흐름"
AMBIENT_FINAL_HEADER = "✅ 노드 결과"
# ⚙️ ambient flow mirror — node-originated work(다른 노드/오케가 주입한 지시)의 트리거
# 프롬프트 카드. "✅ 노드 결과"만 떠서 무슨 지시로 나온 결과인지 맥락이 끊기던 문제 보완.
AMBIENT_DIRECTIVE_HEADER = "📥 받은 지시"
TERMINAL_INPUT_HEADER = "⌨️ 터미널 입력"
SENT_DIRECTIVE_HEADER = "📤 보낸 지시"
AMBIENT_DIRECTIVE_LIMIT = 400
FLOW_MIRROR_LIMIT = 1500
VOICE_PROMPT_HEADER = "[voice]"
VOICE_PROMPT_INSTRUCTION = (
    "음성 질문입니다. 2~3문장으로 짧게 한국어로 답하세요. "
    "무무 클론목소리로 바로 읽을 수 있게 본문만 답하세요."
)
VOICE_ECHO_NOTICE_THRESHOLD = 2000
# F9 (T-260705-04) — typing 루프 pulse/self-liveness 튜닝. 소등 호출을 놓친 경로가
# 있어도(예: ambient 턴 종료) 루프가 세션 유휴를 스스로 감지해 소등한다. probe 실패는
# 소등 사유가 아니다(legit 긴 턴 오탐 방지, TYPING_MAX deadline 이 최종 캡).
TYPING_PULSE_FIRST_WAIT = 1.0
TYPING_PULSE_WAIT = 4.0
TYPING_LIVENESS_GRACE_PULSES = 5
TYPING_LIVENESS_CHECK_EVERY = 5
FLOW_MIRROR_FLAG = os.path.expanduser(os.environ.get("CLB_FLOW_MIRROR_FLAG", "~/.config/claude-telegram-bridge/flow-mirror.on"))
ENVELOPE_SIDECAR_FLAG = Path(os.environ.get("CLB_ENVELOPE_SIDECAR_FLAG", "~/.config/claude-telegram-bridge/envelope-sidecar.on")).expanduser()
ENVELOPE_SIDECAR_OFF_FLAG = Path(
    os.environ.get("CLB_ENVELOPE_SIDECAR_OFF_FLAG", "~/.config/claude-telegram-bridge/envelope-sidecar.off")
).expanduser()
ENVELOPE_SIDECAR_PATH = Path(os.environ.get("CLB_ENVELOPE_SIDECAR_PATH", "~/.local/state/claude-telegram-bridge/envelope-sidecar.jsonl")).expanduser()
ENVELOPE_SIDECAR_SCHEMA = "claude-telegram-bridge-envelope-sidecar/v1"
DEFAULT_ENVELOPE_SIDECAR_TTL_SECONDS = 120.0
# ⚙️ flow mirror — localize harness tool names to Korean action labels so Claude
# cards read like the Korean Codex cards. Unmapped tools keep their original name.
TOOL_LABEL_KO = {
    "Bash": "실행",
    "Read": "읽기",
    "Write": "작성",
    "Edit": "편집",
    "MultiEdit": "편집",
    "NotebookEdit": "노트북편집",
    "Grep": "검색",
    "Glob": "파일찾기",
    "Task": "위임",
    "Agent": "위임",
    "Skill": "스킬",
    "ToolSearch": "도구검색",
    "TodoWrite": "할일",
    "WebFetch": "웹가져오기",
    "WebSearch": "웹검색",
}
APPROVAL_WAIT_RE = re.compile(
    # ⚠️ 제거 금지 (DO NOT REMOVE) — control-plane only: match the actual
    # Claude Code approval MENU (numbered Yes/No + cursor), NOT bare words in
    # answer prose. Bare "allow"/"approve"/"permission"/"do you want" matched the
    # assistant's own answer text and wedged the bridge in approval_wait forever
    # → every later telegram msg stuck "enqueued", typing never cleared.
    # (2026-06-28 라이덴 stuck-inject incident)
    r"(?im)"
    r"^\s*❯?\s*1\.\s*yes\b"
    r"|^\s*\d+\.\s*no,\s*(?:and\s+)?(?:tell|keep)\b"
    r"|do\s+you\s+want\s+to\s+(?:proceed|allow|make|create|run|delete|continue)\b",
)
HOOK_BLOCK_RE = re.compile(
    r"\b(hook\s+(?:blocked|denied|failed)|blocked\s+by\s+hook|"
    r"permission\s+denied\s+by\s+hook|pretooluse\s+(?:blocked|denied|failed))\b",
    re.IGNORECASE,
)
ACTIVE_SPINNER_RE = re.compile(
    r"(?im)^\s*[✶✻✳*]\s*"
    r"(?:working|germinating|thinking|running|processing|cogitating|"
    r"churning|cooking|brewing|baking)\b",
)
ACTIVE_INTERRUPT_RE = re.compile(
    r"(?i)\b(?:esc\s+to\s+inter\s*rupt|inter\s*rupt\s+to\s+stop|still\s+thinking)\b",
)
ACTIVE_FOREGROUND_TOOL_RE = re.compile(
    r"(?im)^\s*(?:[⎿└]\s*)?(?:running|waiting)(?:…|\.{3})\s*$",
)
ACTIVE_BACKGROUND_HINT_RE = re.compile(
    r"(?i)\bctrl\+b\b.{0,80}\brun\s+in\s+background\b",
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".weba"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
TELEGRAM_MEDIA_PROMPT_PREFIXES = (
    "[Telegram image received]",
    "[Telegram audio received]",
    "[Telegram video received]",
    "[Telegram file received]",
)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def release_hold_response(text: str) -> str | None:
    # Personal release-hold automation is stripped from the public export.
    return None


def int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def bool_env(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    return (env(name, fallback) or fallback).lower() in {"1", "true", "yes", "on"}


def busy_inject_enabled() -> bool:
    # T-260707-36 env 게이트 (기본 off). generating(진행 중) 턴 위에 Escape 없이 메시지를
    # 얹어 Claude Code TUI 의 native 큐잉에 실어 다음 턴으로 반영시키는 경로를 켠다. 안전
    # 롤아웃용 — off 면 기존 동작(idle 까지 큐 대기) 그대로. 매 drain 시점에 읽어 토글 반영.
    return bool_env("CLB_BUSY_INJECT", False)


def busy_submit_key() -> str:
    # busy(generating) 중 native 큐잉 제출키. 기본 Enter. codex 브릿지는 이 키가 Tab 이다
    # (Codex TUI 는 진행 중 Enter 가 composer 에 텍스트를 남길 수 있어 Tab 로 큐잉). Claude
    # Code TUI 의 generating 중 큐잉 제출키가 Enter 가 맞는지는 미검증 — 아니면 이 env 로 교체.
    return env("CLB_BUSY_SUBMIT_KEY", "Enter") or "Enter"


def busy_inject_promote_idle_stale_seconds() -> float:
    return float_env("CLB_BUSY_INJECT_PROMOTE_IDLE_STALE_SECONDS", 60.0)


def busy_inject_media_enabled() -> bool:
    # T-260710-15: 미디어(이미지/보이스) 프롬프트의 busy-inject 참여 스위치 (기본 ON).
    # T-260708-22 가 미디어를 전면 제외해 긴 턴 중 첨부가 3~27분 pending 정체
    # (+순서보존으로 뒤 텍스트까지 연쇄 지연)된 것이 실사고 근인 (2026-07-10 라이덴
    # update=568752417/420/421 실측). 제외 당시 우려던 composer 잔류는 이후 가드
    # (composer residual retry·native queue 부착 관측·T-260710-27 promote-idle 해제)로
    # 회수 경로가 생겨 재허용한다. 문제 시 CLB_BUSY_INJECT_MEDIA=0 으로 옛 idle-only 복귀.
    return bool_env("CLB_BUSY_INJECT_MEDIA", True)


def is_telegram_media_prompt(text: str) -> bool:
    return text.lstrip().startswith(TELEGRAM_MEDIA_PROMPT_PREFIXES)


def composer_lock_path() -> Path:
    # codex composer_lock_path() 미러 — busy-inject/idle 주입/슬래시 핸들러의 composer
    # 동시쓰기 경합을 파일 flock 으로 직렬화. codex 락과는 별도 파일(다른 TUI/pane).
    return Path(os.environ.get("CLB_COMPOSER_LOCK", "~/.local/state/claude-telegram-bridge/composer.lock")).expanduser()


def composer_residual_text(screen: str) -> str:
    """busy-inject 안전 가드: Claude composer 프롬프트 라인(``> …``)에 남아 있는 잔여 입력 추출.

    generating 중 composer 에 사용자가 이미 타이핑해 둔 텍스트가 있으면, 우리가 paste 한
    프롬프트가 그 뒤에 이어붙어 오염된 채 Enter 될 수 있다. 주입 전 이 잔여를 감지해 Escape
    없는 clear 로 확실히 비운다. 빈 문자열 = 깨끗한 composer.
    (2026-06-28 라이덴 stuck-inject 회귀 클래스 방지.)
    """
    if not screen:
        return ""
    for line in reversed(screen.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        core = stripped.strip("│").strip()  # box-drawing 테두리 제거
        if core.startswith(">"):
            return core[1:].strip()
        # 프롬프트가 아닌 콘텐츠가 최하단이면 composer 는 비어있다고 본다
        return ""
    return ""


def now_ts() -> str:
    return time.strftime("%H:%M:%S")


def log(label: str, message: str) -> None:
    print(f"[{now_ts()}] {label:<6} {message}", flush=True)


def is_tmux_session_lost_error(error: object) -> bool:
    return "tmux session not found" in str(error).lower()


# ─ codex-CLI-style startup version check + button/auto self-update ─────────────
# When a newer release exists on PyPI, offer a one-tap Telegram "update" button
# (or fully auto-update with CLB_AUTO_UPDATE=1). Only active for pip-installed
# copies — source/editable checkouts, offline state, errors, or opt-out all leave
# the running version untouched. Requested 2026-06-29 (Seonyeob Rim feedback).
SELF_UPDATE_PACKAGE = "claude-telegram-bridge"
SELF_UPDATE_MODULE = "claude_telegram_bridge"
SELF_UPDATE_PREFIX = "CLB"
SELF_UPDATE_CALLBACK = "clb_update"
SELF_UPDATE_PYPI_TIMEOUT = 4


def _self_update_installed_version() -> str | None:
    try:
        from importlib.metadata import version, PackageNotFoundError  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    try:
        return version(SELF_UPDATE_PACKAGE)
    except Exception:  # noqa: BLE001
        return None


def _self_update_is_pip_managed() -> bool:
    path = str(Path(__file__).resolve())
    return "site-packages" in path or "dist-packages" in path


def _self_update_version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text).split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _self_update_latest() -> str | None:
    try:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{SELF_UPDATE_PACKAGE}/json",
            headers={"User-Agent": f"{SELF_UPDATE_PACKAGE}-bridge"},
        )
        with urllib.request.urlopen(req, timeout=SELF_UPDATE_PYPI_TIMEOUT) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        latest = str((info.get("info") or {}).get("version") or "").strip()
        return latest or None
    except Exception as exc:  # noqa: BLE001 — offline / PyPI down is non-fatal
        log("UPDATE", f"version check skipped: {exc}")
        return None


def self_update_available() -> str | None:
    """Return the newer PyPI version string if an update should be offered, else None.
    Returns None for opt-out, source checkouts, already-updated lineage, offline,
    or when already current. Never raises."""
    try:
        if bool_env(f"{SELF_UPDATE_PREFIX}_NO_UPDATE_CHECK", False):
            return None
        if env(f"{SELF_UPDATE_PREFIX}_SELF_UPDATED"):
            return None
        if not _self_update_is_pip_managed():
            return None
        current = _self_update_installed_version()
        if not current:
            return None
        latest = _self_update_latest()
        if not latest:
            return None
        if _self_update_version_tuple(latest) <= _self_update_version_tuple(current):
            return None
        return latest
    except Exception as exc:  # noqa: BLE001
        log("UPDATE", f"version check error: {exc}")
        return None


def perform_self_update(latest: str) -> None:
    """pip-upgrade to `latest` then re-exec so the new code runs. Fail-safe: any
    error leaves the running version untouched."""
    try:
        log("UPDATE", f"upgrading {SELF_UPDATE_PACKAGE} -> {latest}")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", f"{SELF_UPDATE_PACKAGE}=={latest}"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            log("UPDATE", f"pip upgrade failed (staying put): {detail}")
            return
        os.environ[f"{SELF_UPDATE_PREFIX}_SELF_UPDATED"] = latest
        log("UPDATE", f"upgraded to {latest}; restarting")
        os.execv(sys.executable, [sys.executable, "-m", SELF_UPDATE_MODULE] + sys.argv[1:])
    except Exception as exc:  # noqa: BLE001
        log("UPDATE", f"self-update error (new version active next restart): {exc}")


def node_defaults() -> tuple[str, str]:
    return "claude", "\U0001f916"


# T-260701-68: the internal mesh bus/ledger layer is stripped from the public
# export, but call sites survive newer internal commits. Documented no-op stubs
# keep the public bridge on the direct Telegram API path (None => legacy send).
def mesh_ledger_record(*args, **kwargs):
    return None


def mesh_cutover_call(method, params):
    return None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_tmp(path: Path) -> tuple[int, Path]:
    # T-260704-37 F5: tmp 는 호출마다 유니크(mkstemp) — 고정 '<name>.tmp' 는 동시
    # 쓰기가 같은 경로를 공유하다 첫 replace 후 두번째 replace 가 FileNotFoundError
    # 로 죽는 race (라이덴 2026-07-04 23:36 크래시 실측, exit 2 → 브릿지 전체 다운).
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    return fd, Path(name)


def write_text_atomic(path: Path, value: str | int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _atomic_tmp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(value))
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _atomic_tmp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_start_time(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return stat.rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def proc_cmdline_sha256(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(raw).hexdigest() if raw else ""


def daemon_identity(pid_file: Path) -> dict[str, Any]:
    pid = os.getpid()
    return {
        "pid": pid,
        "pid_start_time": proc_start_time(pid),
        "cmdline_sha256": proc_cmdline_sha256(pid),
        "pid_file": str(pid_file),
        "started_at": time.time(),
    }


def load_token(token_file: Path) -> str:
    payload = read_json(token_file)
    if payload:
        for key in ("token", "TELEGRAM_BOT_TOKEN"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raw = read_text(token_file)
    if raw:
        return raw
    raise RuntimeError(f"Telegram token not found: {token_file}")


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = BRACKETED_PASTE_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    return text.strip()


def sanitize_text(text: str, limit: int = 12000) -> str:
    text = normalize_text(text)
    if len(text) > limit:
        text = text[:limit] + "\n\n[truncated by claude-telegram-bridge]"
    return text


def strip_ansi_control(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text or "")


def strip_bridge_nonce_markers(text: str) -> str:
    text = OUTBOUND_CLB_ENVELOPE_RE.sub("", text or "")
    text = OUTBOUND_CLB_NONCE_RE.sub("", text)
    lines = [OUTBOUND_CLB_GAP_RE.sub(" ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    return cleaned.strip("-")[:80] or "file"


def suffix_from_metadata(file_name: str = "", mime_type: str = "", default: str = ".bin") -> str:
    suffix = Path(file_name or "").suffix.lower()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed.lower() if guessed else default


def slash_token(text: str) -> str:
    stripped = (text or "").strip()
    if "\n" in stripped or not stripped.startswith("/"):
        return ""
    return stripped.split(maxsplit=1)[0].split("@", 1)[0].lower()


# /context TUI 그리드 문자 — 텔레그램 클라이언트 폰트가 못 그려 밑줄로 뭉개진다 (T-260703-01 스크린샷 실측)
CONTEXT_GRID_GLYPHS = "⛁⛀⛶⛝"
CONTEXT_BAR_SLOTS = 20
CONTEXT_TOKENS_RE = re.compile(
    r"(?P<used>[\d.,]+[kKmM]?)\s*/\s*(?P<total>[\d.,]+[kKmM]?)\s*tokens\s*\((?P<pct>\d{1,3}(?:\.\d+)?)%\)"
)
CONTEXT_CATEGORY_RE = re.compile(
    r"(?P<name>System prompt|System tools|MCP tools|Memory files|Skills|Messages|Free space)\s*:?\s*"
    r"(?P<size>[\d.,]+[kKmM]?)?\s*(?:tokens\s*)?\((?P<pct>\d{1,3}(?:\.\d+)?)%\)"
)


def render_context_bar(pct: float, slots: int = CONTEXT_BAR_SLOTS) -> str:
    filled = max(0, min(slots, round(slots * pct / 100.0)))
    return "[" + "█" * filled + "░" * (slots - filled) + "]"


def extract_context_screen(screen: str) -> str:
    # 코덱스 /status 패리티 (T-260703-01): raw TUI 캡처(⛁⛶ 그리드)를 그대로 보내면
    # 텔레그램 폰트가 밑줄로 뭉개므로, 숫자만 파싱해 텔레그램-안전 바(█░)로 재조립한다.
    # extract_codex_status_text(codex-repl-telegram-bridge.py) 와 동형 접근.
    # 파싱 실패(레이아웃 변경 등) 시 glyph 만 벗겨낸 평문 폴백 — raw 그리드 전송은 금지.
    raw = strip_ansi_control(screen or "").rstrip("\n")
    header_idx = raw.rfind("Context Usage")
    scan = raw[header_idx:] if header_idx != -1 else raw

    model = ""
    tokens = None
    cats: list[tuple[str, str, str]] = []
    for line in scan.splitlines():
        text = line.strip()
        if not text:
            continue
        if tokens is None:
            m = CONTEXT_TOKENS_RE.search(text)
            if m:
                tokens = m
                continue
        m = CONTEXT_CATEGORY_RE.search(text)
        if m:
            cats.append((m.group("name"), m.group("size") or "", m.group("pct")))
            continue
        stripped = text.strip(CONTEXT_GRID_GLYPHS + " ")
        if not model and stripped.startswith("claude-"):
            model = stripped.split()[0]

    if tokens is not None:
        pct = float(tokens.group("pct"))
        out = ["Claude context"]
        if model:
            out.append(f"Model: {model}")
        out.append(f"Context: {tokens.group('used')}/{tokens.group('total')} tokens ({tokens.group('pct')}%)")
        out.append(f"{render_context_bar(pct)} {tokens.group('pct')}%")
        for name, size, cpct in cats:
            if name in ("Messages", "Free space"):
                size_part = f"{size} " if size else ""
                out.append(f"{name}: {size_part}({cpct}%)")
        return "\n".join(out)

    # 폴백: glyph 제거 + 다중 공백 축약한 평문 (파싱 불가 레이아웃 대비)
    cleaned: list[str] = []
    for line in raw.splitlines():
        text = re.sub("[" + CONTEXT_GRID_GLYPHS + "]", "", line).strip()
        text = re.sub(r"\s{2,}", "  ", text)
        if text:
            cleaned.append(text)
    return "\n".join(cleaned).strip()


# /context 시각화가 쓰는 도형(draughts/board) 글리프 — 진행바 블록(█▓▒░)은 codex 식
# 텍스트 막대라 일부러 제외한다(보존).
CONTEXT_CHART_GLYPHS = "⛀⛁⛂⛃⛄⛅⛆⛇⛶⛷"
# T-260710-20: /context 렌더 원폭(20열 ≈ 40자)은 폰 pre 폭(~24자)을 넘어 행마다
# 2~3줄로 접혀 매트릭스 모양이 붕괴한다(아니키 스샷 실측 2026-07-10). 글리프를
# 읽기 순서대로 모아 10열(≈19자)로 재배열해 폰에서 무줄바꿈 표시.
CONTEXT_GRID_REFLOW_COLS = 10
CONTEXT_SYNTH_GRID_CELLS = 100
CONTEXT_DEFAULT_TOTAL_TOKENS = 1_000_000.0
_LEADING_CHART_RE = re.compile(r"^[\s" + re.escape(CONTEXT_CHART_GLYPHS) + r"]+")
CONTEXT_USED_RE = re.compile(r"\bContext:?\s*(?P<pct>\d{1,3}(?:\.\d+)?)%\s*used\b", re.IGNORECASE)
CONTEXT_TEXT_CATEGORY_RE = re.compile(
    r"(?P<name>System prompt|System tools|MCP tools|Custom agents|Memory|Memory files|Skills|Messages|Free space)"
    r"\s*:?\s*(?P<size>[\d.,]+[kKmM]?)?\s*(?:tokens\s*)?\((?P<pct>\d{1,3}(?:\.\d+)?)%\)"
)
CONTEXT_RATE_LIMIT_CACHE = Path(
    os.environ.get("CLB_RATE_LIMITS_CACHE", "~/.local/state/claude-telegram-bridge/rate-limits-last.json")
).expanduser()
CONTEXT_USAGE_SCRAPED_CACHE = Path(
    os.environ.get("CLB_USAGE_SCRAPED_CACHE", "~/.local/state/claude-telegram-bridge/usage_scraped.json")
).expanduser()

# T-260704-38 F6: 캡처에 섞이는 터미널 크롬 — 셸 프롬프트 줄(user@host:...)과 입력창
# 단축키 힌트. 스테이터스라인은 노드별 커스텀 포맷이라 패턴화하지 않고 블록 추출
# (extract_slash_command_block — 다음 프롬프트 마커에서 절단)로 걷어낸다.
CONTEXT_CHROME_RE = re.compile(
    r"^\S+@\S+:|bypass permissions|\? for shortcuts|esc to interrupt", re.IGNORECASE
)
_SEPARATOR_LINE_RE = re.compile(r"^[─━\s]+$")


def clean_context_screen(screen: str) -> str:
    # T-260703-36 (codex /status 톤): /context 캡처는 좌측 도형 차트(⛁⛶ 그리드) + 우측 텍스트가
    #   같은 행에 놓인 2단 레이아웃이다. 각 줄 선두의 (글리프|공백) 런을 걷어내 우측 텍스트만
    #   남기면 폭-무관·글리프-무관의 깔끔한 텍스트가 된다. 텍스트가 없는 순수 차트 줄은 버린다.
    #   진행바 블록은 CONTEXT_CHART_GLYPHS 에서 빠져 있어 그대로 보존된다.
    # T-260704-38 F6: ─/━ 구분선-only 줄과 터미널 크롬 줄도 미러에서 걷어낸다.
    out: list[str] = []
    for raw in strip_ansi_control(screen or "").splitlines():
        text = _LEADING_CHART_RE.sub("", raw).rstrip()
        if not text.strip():
            continue
        if _SEPARATOR_LINE_RE.match(text) or CONTEXT_CHROME_RE.search(text):
            continue
        out.append(text)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


def parse_context_token_amount(value: str) -> float | None:
    raw = (value or "").strip().replace(",", "")
    if not raw:
        return None
    unit = raw[-1].lower()
    factor = 1.0
    number = raw
    if unit == "k":
        factor = 1_000.0
        number = raw[:-1]
    elif unit == "m":
        factor = 1_000_000.0
        number = raw[:-1]
    try:
        parsed = float(number) * factor
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def format_context_token_amount(value: float) -> str:
    if value >= 1_000_000:
        scaled = value / 1_000_000.0
        suffix = "m"
    else:
        scaled = value / 1_000.0
        suffix = "k"
    if abs(scaled - round(scaled)) < 0.05:
        return f"{int(round(scaled))}{suffix}"
    return f"{scaled:.1f}".rstrip("0").rstrip(".") + suffix


def context_usage_header(scan: str) -> str:
    token_match = CONTEXT_TOKENS_RE.search(scan or "")
    if token_match:
        return f"{token_match.group('pct')}% · {token_match.group('used')}/{token_match.group('total')} tokens"

    pct = None
    context_match = CONTEXT_USED_RE.search(scan or "")
    if context_match:
        try:
            pct = float(context_match.group("pct"))
        except ValueError:
            pct = None

    total = None
    fallback_used = None
    fallback_pct = None
    for match in CONTEXT_TEXT_CATEGORY_RE.finditer(scan or ""):
        size = parse_context_token_amount(match.group("size") or "")
        try:
            cat_pct = float(match.group("pct"))
        except ValueError:
            cat_pct = 0.0
        if size is not None and cat_pct > 0:
            total = size / (cat_pct / 100.0)
            if match.group("name") == "Messages":
                fallback_used = size
                fallback_pct = cat_pct
            break
    if pct is None and fallback_pct is not None:
        pct = fallback_pct
    if pct is None:
        return ""

    if total is None or total <= 0:
        total = CONTEXT_DEFAULT_TOTAL_TOKENS
    used = (total * pct / 100.0) if pct is not None else fallback_used
    if used is None:
        return ""
    pct_text = f"{pct:.1f}".rstrip("0").rstrip(".")
    return f"{pct_text}% · {format_context_token_amount(used)}/{format_context_token_amount(total)} tokens"


def synth_context_grid(pct_text: str) -> list[str]:
    try:
        pct = float(str(pct_text).rstrip("%"))
    except ValueError:
        pct = 0.0
    filled = max(0, min(CONTEXT_SYNTH_GRID_CELLS, round(CONTEXT_SYNTH_GRID_CELLS * pct / 100.0)))
    glyphs = ["⛁"] * filled + ["⛶"] * (CONTEXT_SYNTH_GRID_CELLS - filled)
    cols = CONTEXT_GRID_REFLOW_COLS
    return [" ".join(glyphs[i : i + cols]) for i in range(0, len(glyphs), cols)]


def context_grid_text(screen: str) -> str:
    # T-260709-80 (아니키 "이 네모부분이 %와 함께 나오면 좋겠어" + "이미지말고 텍스트로"):
    # /context 의 동전 매트릭스(좌측 차트 열)만 %헤더와 함께 텍스트로 재조립한다.
    # 우측 카테고리 범례(선두 글리프 1개짜리 줄)는 절단 — 매트릭스 행은 글리프 2개 이상.
    # 전송은 모노스페이스(pre entity) 전제 — 옛 T-260703-01 "raw 그리드 금지" 는
    # 가변폭 폰트 뭉개짐이 근거였고 pre 로 해소된다. 파싱 실패 시 "" (콜사이트 폴백).
    raw = strip_ansi_control(screen or "")
    idx = raw.rfind("Context Usage")
    scan = raw[idx:] if idx != -1 else raw
    header = context_usage_header(scan)
    pct_line = header
    rows: list[str] = []
    for line in scan.splitlines():
        if not pct_line:
            m = CONTEXT_TOKENS_RE.search(line)
            if m:
                pct_line = f"{m.group('pct')}% · {m.group('used')}/{m.group('total')} tokens"
        lead = _LEADING_CHART_RE.match(line)
        if lead:
            run = lead.group(0).rstrip()
            if sum(1 for ch in run if ch in CONTEXT_CHART_GLYPHS) >= 2:
                rows.append(run)
    if not rows:
        if not pct_line:
            return ""
        pct = pct_line.split("%", 1)[0]
        return pct_line + "\n" + "\n".join(synth_context_grid(pct))
    # T-260710-20: 렌더 원폭 그대로 보내면 폰에서 행이 접힌다 — 10열 재배열(가시성).
    glyphs = [ch for row in rows for ch in row if ch in CONTEXT_CHART_GLYPHS]
    cols = CONTEXT_GRID_REFLOW_COLS
    reflowed = [" ".join(glyphs[i : i + cols]) for i in range(0, len(glyphs), cols)]
    return (pct_line or "context") + "\n" + "\n".join(reflowed)


def context_limit_pct(value: Any) -> str:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return ""
    if 0 <= pct <= 1:
        pct *= 100.0
    if pct < 0:
        return ""
    return f"{pct:.1f}".rstrip("0").rstrip(".")


def context_limit_reset_text(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    dt = datetime.fromtimestamp(timestamp, KST)
    now = datetime.now(KST)
    hhmm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return hhmm
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{weekdays[dt.weekday()]} {hhmm}"


def context_rate_limit_cache_paths() -> tuple[Path, Path]:
    rate_cache = Path(os.environ.get("CLB_RATE_LIMITS_CACHE", str(CONTEXT_RATE_LIMIT_CACHE))).expanduser()
    usage_cache = Path(os.environ.get("CLB_USAGE_SCRAPED_CACHE", str(CONTEXT_USAGE_SCRAPED_CACHE))).expanduser()
    return rate_cache, usage_cache


def fresh_context_cache(data: dict[str, Any], path: Path) -> bool:
    try:
        max_age = float(os.environ.get("CLB_CONTEXT_RATE_LIMIT_MAX_AGE_SEC", "21600"))
    except ValueError:
        max_age = 21600.0
    if max_age <= 0:
        return True
    stamp = data.get("cached_at") or data.get("scraped_at")
    try:
        timestamp = float(stamp)
    except (TypeError, ValueError):
        try:
            timestamp = path.stat().st_mtime
        except OSError:
            return False
    return (time.time() - timestamp) <= max_age


def context_rate_limit_footer_from_cache() -> list[str]:
    for path in context_rate_limit_cache_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict) or not fresh_context_cache(data, path):
            continue
        rate_limits = data.get("rate_limits")
        if not isinstance(rate_limits, dict):
            rate_limits = data

        def bucket(*names: str) -> dict[str, Any]:
            for name in names:
                value = rate_limits.get(name)
                if isinstance(value, dict):
                    return value
            return {}

        five = bucket("five_hour", "fiveHour", "primary", "5h")
        weekly = bucket("seven_day", "sevenDay", "secondary", "weekly", "week")
        lines: list[str] = []
        five_pct = context_limit_pct(
            five.get("used_percentage")
            or five.get("used_percent")
            or five.get("usedPercentage")
            or five.get("pct")
            or five.get("utilization")
        )
        five_reset = context_limit_reset_text(
            five.get("resets_at") or five.get("resetsAt") or five.get("reset_at") or five.get("resetAt")
        )
        if five_pct:
            suffix = f" 리셋 {five_reset}" if five_reset else ""
            lines.append(f"⏱ 5h {five_pct}%{suffix}")
        weekly_pct = context_limit_pct(
            weekly.get("used_percentage")
            or weekly.get("used_percent")
            or weekly.get("usedPercentage")
            or weekly.get("pct")
            or weekly.get("utilization")
        )
        weekly_reset = context_limit_reset_text(
            weekly.get("resets_at") or weekly.get("resetsAt") or weekly.get("reset_at") or weekly.get("resetAt")
        )
        if weekly_pct:
            suffix = f" 리셋 {weekly_reset}" if weekly_reset else ""
            lines.append(f"📅 W {weekly_pct}%{suffix}")
        if lines:
            return lines
    return []


def context_rate_limit_footer_from_screen(screen: str) -> list[str]:
    raw = strip_ansi_control(screen or "")
    lines: list[str] = []
    five = re.search(r"(?:^|[·\s])5h\s+(?P<pct>\d{1,3}(?:\.\d+)?)%(?:\s*리셋\s*(?P<reset>[^\n·]+))?", raw)
    weekly = re.search(
        r"(?:^|[·\s])(?:W|주간|Weekly)\s+(?P<pct>\d{1,3}(?:\.\d+)?)%(?:\s*리셋\s*(?P<reset>[^\n·]+))?",
        raw,
        re.IGNORECASE,
    )
    if five:
        reset = (five.group("reset") or "").strip()
        lines.append(f"⏱ 5h {five.group('pct')}%" + (f" 리셋 {reset}" if reset else ""))
    if weekly:
        reset = (weekly.group("reset") or "").strip()
        lines.append(f"📅 W {weekly.group('pct')}%" + (f" 리셋 {reset}" if reset else ""))
    return lines


def append_context_rate_limit_footer(text: str, screen: str) -> str:
    if not text:
        return text
    lines = context_rate_limit_footer_from_cache() or context_rate_limit_footer_from_screen(screen)
    if not lines:
        return text
    return text.rstrip() + "\n\n" + "\n".join(lines)


def context_capture_lines() -> int:
    # T-260710-03: MCP 도구·스킬 목록이 긴 노드(라이덴 실측)는 /context 그리드가
    # 마지막 120줄 캡처 밖으로 밀려나 raw 덤프 폴백이 나갔다 — history 를 넉넉히 뜬다.
    try:
        lines = int(os.environ.get("CLB_CONTEXT_CAPTURE_LINES", "400"))
    except ValueError:
        lines = 400
    return max(120, lines)


def extract_slash_command_block(screen: str, command_token: str) -> str:
    # T-260704-38 F6: 통째 pane 캡처에서 '❯ <명령>' 에코 ~ 다음 프롬프트 마커(❯) 사이
    # 블록만 남긴다 — 직전 대화와 입력창 아래 크롬(셸 프롬프트/스테이터스라인/힌트)이
    # 미러에 섞이는 것을 차단 (아니키 라이덴·맥미니 스샷 실측, 2026-07-04). 에코가
    # 화면에 없으면 빈 문자열 반환 → 호출측이 전체 화면 정리로 폴백한다.
    token = (command_token or "").strip().lower()
    if not token:
        return ""
    lines = (screen or "").splitlines()
    echo_idx = None
    for idx in range(len(lines) - 1, -1, -1):  # 마지막 에코 — 직전 대화의 같은 명령 무시
        stripped = strip_ansi_control(lines[idx]).lstrip()
        if stripped.startswith("❯") and token in stripped.lower():
            echo_idx = idx
            break
    if echo_idx is None:
        return ""
    block: list[str] = []
    for line in lines[echo_idx + 1 :]:
        if strip_ansi_control(line).lstrip().startswith("❯"):
            break
        block.append(line)
    return "\n".join(block)


ANSI_DEFAULT_FG = (229, 231, 235)
ANSI_DEFAULT_BG = (15, 23, 42)
ANSI_BOLD_FG = (248, 250, 252)
ANSI_16_COLORS = {
    30: (30, 41, 59),
    31: (239, 68, 68),
    32: (34, 197, 94),
    33: (234, 179, 8),
    34: (59, 130, 246),
    35: (168, 85, 247),
    36: (6, 182, 212),
    37: (226, 232, 240),
    90: (100, 116, 139),
    91: (248, 113, 113),
    92: (74, 222, 128),
    93: (250, 204, 21),
    94: (96, 165, 250),
    95: (196, 181, 253),
    96: (34, 211, 238),
    97: (248, 250, 252),
}
ANSI_256_COLORS = [
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
    (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]
for _r in (0, 95, 135, 175, 215, 255):
    for _g in (0, 95, 135, 175, 215, 255):
        for _b in (0, 95, 135, 175, 215, 255):
            ANSI_256_COLORS.append((_r, _g, _b))
for _i in range(24):
    _v = 8 + _i * 10
    ANSI_256_COLORS.append((_v, _v, _v))


def ansi_color_from_256(index: int) -> tuple[int, int, int]:
    if 0 <= index < len(ANSI_256_COLORS):
        return ANSI_256_COLORS[index]
    return ANSI_DEFAULT_FG


def parse_ansi_cells(text: str) -> list[list[tuple[str, tuple[int, int, int], tuple[int, int, int], bool]]]:
    fg = ANSI_DEFAULT_FG
    bg = ANSI_DEFAULT_BG
    bold = False
    lines: list[list[tuple[str, tuple[int, int, int], tuple[int, int, int], bool]]] = [[]]
    i = 0
    while i < len(text):
        if text[i] == "\x1b":
            match = ANSI_ESCAPE_RE.match(text, i)
            if match:
                code = match.group(0)
                if code.endswith("m") and code.startswith("\x1b["):
                    raw_parts = code[2:-1]
                    parts = [0] if raw_parts == "" else [int(p) if p.isdigit() else 0 for p in raw_parts.split(";")]
                    j = 0
                    while j < len(parts):
                        part = parts[j]
                        if part == 0:
                            fg = ANSI_DEFAULT_FG
                            bg = ANSI_DEFAULT_BG
                            bold = False
                        elif part == 1:
                            bold = True
                        elif part == 22:
                            bold = False
                        elif part == 39:
                            fg = ANSI_DEFAULT_FG
                        elif part == 49:
                            bg = ANSI_DEFAULT_BG
                        elif part in ANSI_16_COLORS:
                            fg = ANSI_16_COLORS[part]
                        elif 40 <= part <= 47:
                            fg_code = part - 10
                            bg = ANSI_16_COLORS.get(fg_code, ANSI_DEFAULT_BG)
                        elif 100 <= part <= 107:
                            fg_code = part - 10
                            bg = ANSI_16_COLORS.get(fg_code, ANSI_DEFAULT_BG)
                        elif part in (38, 48) and j + 2 < len(parts):
                            target_fg = part == 38
                            mode = parts[j + 1]
                            if mode == 5 and j + 2 < len(parts):
                                color = ansi_color_from_256(parts[j + 2])
                                if target_fg:
                                    fg = color
                                else:
                                    bg = color
                                j += 2
                            elif mode == 2 and j + 4 < len(parts):
                                color = tuple(max(0, min(255, parts[j + k])) for k in range(2, 5))
                                if target_fg:
                                    fg = color  # type: ignore[assignment]
                                else:
                                    bg = color  # type: ignore[assignment]
                                j += 4
                        j += 1
                i = match.end()
                continue
        ch = text[i]
        i += 1
        if ch == "\r":
            continue
        if ch == "\n":
            lines.append([])
            continue
        if unicodedata.category(ch).startswith("C"):
            continue
        lines[-1].append((ch, fg, bg, bold))
    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines or [[]]


def ansi_char_cells(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1


def context_image_font(size: int = 17):
    try:
        from PIL import ImageFont
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Pillow is required for /context image rendering") from exc
    candidates = [
        os.environ.get("CLB_CONTEXT_IMAGE_FONT", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/Library/Fonts/Menlo.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def render_ansi_png(text: str, output_path: Path) -> Path:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Pillow is required for /context image rendering") from exc

    font_size = int_env("CLB_CONTEXT_IMAGE_FONT_SIZE", 17, minimum=8)
    font = context_image_font(font_size)
    lines = parse_ansi_cells(text or "")
    probe = Image.new("RGB", (1, 1), ANSI_DEFAULT_BG)
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), "M", font=font)
    char_width = max(1, int(math.ceil(draw.textlength("M", font=font))))
    line_height = max(1, (bbox[3] - bbox[1]) + 6)
    padding_x = 16
    padding_y = 14
    max_cells = max((sum(ansi_char_cells(ch) for ch, _fg, _bg, _bold in line) for line in lines), default=1)
    width = max(320, padding_x * 2 + char_width * max(1, max_cells))
    height = max(96, padding_y * 2 + line_height * max(1, len(lines)))
    image = Image.new("RGB", (width, height), ANSI_DEFAULT_BG)
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        x = padding_x
        y = padding_y + row * line_height
        for ch, fg, bg, bold in line:
            cells = ansi_char_cells(ch)
            cell_width = char_width * cells
            if bg != ANSI_DEFAULT_BG:
                draw.rectangle((x, y, x + cell_width, y + line_height), fill=bg)
            color = ANSI_BOLD_FG if bold and fg == ANSI_DEFAULT_FG else fg
            draw.text((x, y), ch, font=font, fill=color)
            x += cell_width
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


def claude_settings_model() -> str:
    # 현재 모델은 settings SoT(~/.claude/settings.json "model")에서 동적으로 읽는다 —
    # 모델명 하드코딩 금지 (T-260702-14). 테스트/특수 배치는 CLB_CLAUDE_SETTINGS 로 경로 오버라이드.
    path = Path(os.environ.get("CLB_CLAUDE_SETTINGS") or (Path.home() / ".claude" / "settings.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        model = str(data.get("model") or "").strip()
        return model or "(설정에 model 없음 — 세션 디폴트)"
    except Exception:  # noqa: BLE001
        return "(settings 확인 불가)"


def model_menu_aliases() -> list[str]:
    raw = os.environ.get("CLB_MODEL_CHOICES", "")
    aliases = [alias.strip() for alias in raw.split(",") if alias.strip()]
    return aliases or list(DEFAULT_MODEL_MENU_ALIASES)


def model_alias_allowed(alias: str) -> bool:
    # ⚠️ 하드닝 (T-260703-23, PR#362 리뷰): /model 적용 경로(콜백·인자형)는 이 allowlist
    #   안의 alias 만 composer 로 주입한다. 위조 callback_data / 임의 인자가 `/model <임의문자열>`
    #   로 흘러 들어가는 것을 차단. 전체 모델 ID 등 목록 밖 값은 '!' escape 원문 주입으로만.
    return alias in model_menu_aliases()


def model_alias_rejection_text(alias: str) -> str:
    choices = " ".join(model_menu_aliases())
    return (
        f"⛔ 알 수 없는 모델 별칭: {alias}\n"
        f"가능한 값: {choices}\n"
        f"전체 모델 ID 를 그대로 적용하려면 앞에 {SLASH_ESCAPE_PREFIX} 를 붙여 원문 주입하세요 "
        f"(예: {SLASH_ESCAPE_PREFIX}/model <id>)."
    )


def escape_unsafe_slash(text: str) -> str:
    # ⚠️ 안전 가드 (제거/약화 금지 without 근거) — 옛 버전은 ALLOWED_SLASH_COMMANDS
    #   allowlist(/ping·/start·/status)만 통과시키고 나머지 슬래시를 전각 ／ 로 이스케이프해
    #   Claude Code TUI 의 슬래시 명령 실행을 원천 차단했다. T-260702-14(codex parity)에서
    #   allowlist 를 폐기하고 codex 브릿지 모델로 대체: 단일 라인의 유효 슬래시 토큰
    #   (slash_token != "")은 전부 Claude Code 로 통과시킨다(/exit·/clear 포함). 가드의
    #   본질은 유지된다 — 멀티라인/선행텍스트 섞인 위장 슬래시는 slash_token 이 "" 를 반환하고
    #   여기서 ／ 로 이스케이프되어 명령 오인젝션을 막는다. 세션파괴 명령(/exit·/quit)의
    #   안전망은 SESSION_LIFECYCLE_SLASH_COMMANDS → watchdog 자가복구로 이관됨.
    token = slash_token(text)
    if token:
        return text
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return text
    prefix = text[: len(text) - len(stripped)]
    return prefix + "／" + stripped[1:]


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return "\n".join(chunks).strip()


def attachment_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = [attachment_text(item) for item in value]
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    if not isinstance(value, dict):
        return ""
    chunks: list[str] = []
    for key in ("text", "content", "body", "value"):
        chunk = attachment_text(value.get(key))
        if chunk:
            chunks.append(chunk)
    return "\n".join(chunks).strip()


def content_thinking(content: Any) -> str:
    """Extract Claude extended-thinking blocks from an assistant message content.

    Sibling of ``content_text``. Used to mirror the turn's reasoning to Telegram
    as a separate 🧠 block right after the final answer. Returns "" when the turn
    has no thinking (trivial turns naturally produce no reasoning mirror)."""
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "thinking" and isinstance(item.get("thinking"), str):
            chunks.append(item["thinking"])
    return "\n".join(chunks).strip()


def format_reasoning_mirror(text: str) -> str:
    body = text.strip()[:REASONING_MIRROR_LIMIT].strip()
    return f"{REASONING_HEADER}\n{body}" if body else ""


def flow_mirror_enabled() -> bool:
    """⚙️ flow mirror toggle — flag-file gated so it can be turned on/off at
    runtime without restarting the bridge. Default OFF (flag absent)."""
    return os.path.exists(FLOW_MIRROR_FLAG)


def _tool_detail(name: str, inp: Any) -> str:
    """Pick the most meaningful single-line descriptor from a tool_use input."""
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "file_path", "path", "command", "query", "url", "pattern", "prompt", "skill"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().splitlines()[0][:120]
    return ""


def tool_label(name: str) -> str:
    """⚙️ flow mirror — Korean action label for a harness tool name; unmapped
    tools keep their original name."""
    return TOOL_LABEL_KO.get(name, name)


def content_tool_summary(content: Any) -> str:
    """Compact one-line-per-tool summary of tool_use blocks in an assistant
    message, for the ⚙️ flow mirror. Returns "" when no tool_use present
    (e.g. thinking-only intermediate records produce no flow message)."""
    if not isinstance(content, list):
        return ""
    lines: list[str] = []
    for item in content:
        if not (isinstance(item, dict) and item.get("type") == "tool_use"):
            continue
        name = str(item.get("name") or "tool")
        detail = _tool_detail(name, item.get("input"))
        lines.append(f"• {tool_label(name)}{' · ' + detail if detail else ''}")
    return "\n".join(lines).strip()


def format_flow_mirror(text: str) -> str:
    body = text.strip()[:FLOW_MIRROR_LIMIT].strip()
    return f"{FLOW_MIRROR_HEADER}\n{body}" if body else ""


def format_ambient_flow(text: str) -> str:
    # ⚙️ ambient flow mirror (v0.1.5) — distinct "(노드 자율)" marker so the user can
    # tell node-autonomous work apart from active-turn flow cards.
    body = text.strip()[:FLOW_MIRROR_LIMIT].strip()
    return f"{FLOW_MIRROR_HEADER} (노드 자율)\n{body}" if body else ""


def format_ambient_final(text: str) -> str:
    # ⚙️ ambient flow mirror — node-originated work 의 최종 답변(결론) 카드. flow 카드
    # (도구 단계, "작업 흐름")와 구분되는 "✅ 노드 결과" 헤더로 결론임을 표시한다.
    body = text.strip()[:FLOW_MIRROR_LIMIT].strip()
    return f"{AMBIENT_FINAL_HEADER}\n{body}" if body else ""


# ⚙️ 받은-지시 카드 gist 정제 (T-260630-33) — 보일러플레이트 라우팅 헤더를 gist 에서
# 빼고, 라우트 헤더의 from=<host>·task= 메타를 "🍎 본진 → 🖥 · T-…" 한 줄로 만든다.
_DIRECTIVE_BOILERPLATE_RE = re.compile(
    r"^\[(?:claude-skills HEAD|CLAUDE-REVIEW-ROUTE|NODE-ACK-)"
)
_DIRECTIVE_DEDUP_HEADER_RE = re.compile(r"^\[[^\]]*→[^\]]*\]\s+\[[^\]]*\]")
# T-260703-16 ①: 운반체가 주입 본문 앞에 붙이는 라우트 메타 1줄 'from=<host> | task=<T-id>'.
# 파서(_DIRECTIVE_FROM_RE/_DIRECTIVE_TASK_RE)가 라우트를 뽑은 뒤 이 원문줄은 gist 에서
# 뺀다 — 파이프('|')·공백 두 구분자 모두. [claude-skills HEAD] 류 라우팅 보일러플레이트.
_DIRECTIVE_ROUTE_META_RE = re.compile(r"^from=\S+\s*\|?\s*task=")
_DIRECTIVE_FROM_RE = re.compile(r"\bfrom=([^\s|\]]+)")
_DIRECTIVE_TASK_RE = re.compile(r"\btask=(T-[0-9A-Za-z\-]+)")
_DIRECTIVE_TITLE_RE = re.compile(r"\]\s*(?:디렉티브|directive)\s*[—:-]\s*(.+)$", re.IGNORECASE)
_MAC_REPORT_TITLE_RE = re.compile(r"^\[Mac report title:\s*(.+?)\]\s*$", re.IGNORECASE)
_BRIDGE_NONCE_RE = re.compile(r"<(?:claude-telegram-bridge|clb)\s+nonce=|<clb-[0-9a-f]{8,64}/>")
# T-260703-16 ②: 로컬 슬래시-명령(예: /model, /clear) 출력 레코드는 노드발 지시가 아니므로
# 받은지시 카드 X. Claude Code 가 주입하는 <command-name>/<command-message>/<command-args>/
# <local-command-stdout>/<local-command-caveat> 마커로 식별한다 (nonce 가드와 동형 방어).
# 실사고 2026-07-03 09:21 로컬 /model 이 받은지시 카드 2장으로 오카드화.
_LOCAL_COMMAND_RE = re.compile(
    r"<(?:command-name|command-message|command-args"
    r"|local-command-stdout|local-command-caveat)\b"
)
# T-260709-79: /context 로컬 명령의 마크다운 요약 레코드는 <command-*> 태그 없이
# "## Context Usage" 로 시작하는 별도 레코드로 도착해 위 게이트를 통과 → context-show
# fire 마다 "⌨️ 터미널 입력 ## Context Usage **Model:**…" 잡음 1통 중복(아니키 실사고
# 2026-07-09, 정식 2통과 겹침). 문서 헤더 앵커라 본문 중간에 언급된 지시는 영향 0.
_LOCAL_CONTEXT_SUMMARY_RE = re.compile(r"^\s*#{1,3}\s+Context Usage\b")


def _directive_is_boilerplate(line: str) -> bool:
    return bool(
        _DIRECTIVE_BOILERPLATE_RE.match(line)
        or _DIRECTIVE_DEDUP_HEADER_RE.match(line)
        or _DIRECTIVE_ROUTE_META_RE.match(line)
    )


def _ambient_title_from_lines(lines: list[str]) -> str:
    for line in lines:
        m = _MAC_REPORT_TITLE_RE.match(line)
        if m:
            return m.group(1).strip()
        m = _DIRECTIVE_TITLE_RE.search(line)
        if m:
            return m.group(1).strip()
    return ""


def _ambient_title_node_label(title: str, lines: list[str], route_line: str) -> str:
    # Personal node-name detection is stripped from the public export.
    return ""


def _ambient_title_summary_ko(title: str, lines: list[str], route_line: str) -> str:
    title = (title or "").strip()
    if not title:
        return ""
    low = title.lower()
    topic = ""
    if any(key in low for key in ("worktree contention", "live checkout", "feature branch", "worktree")):
        topic = "작업폴더 정리 필요"
    elif any(key in low for key in ("chrome remote desktop", "remote desktop", "crd", "chromoting")):
        topic = "원격데스크톱 점검"
    elif "bridge" in low or "브릿지" in title:
        topic = "브릿지 점검"
    elif "youtube" in low:
        topic = "유튜브 작업"
    elif "ebook" in low:
        topic = "전자책 작업"
    elif any(key in low for key in ("build", "test", "verify", "verification")):
        topic = "검증 결과"
    elif "status" in low or "state" in low:
        topic = "상태 확인"
    elif "deploy" in low or "release" in low:
        topic = "배포 확인"
    if not topic:
        return ""

    details: list[str] = []
    if "ack" in low or "approval" in low or "confirm" in low:
        details.append("정리 ack 요청" if topic == "작업폴더 정리 필요" else "ack 요청")
    if "restart" in low or "reboot" in low or "kickstart" in low:
        details.append("재시작 확인")
    label = _ambient_title_node_label(title, lines, route_line)
    summary = topic
    if details:
        summary = f"{summary}, {', '.join(dict.fromkeys(details))}"
    return f"{label}: {summary}" if label else summary


def _format_directive_card(
    text: str,
    *,
    header: str,
    from_alias: str | None = None,
    to_alias: str | None = None,
    self_emoji: str | None = None,
    self_alias: str | None = None,
    narrative: bool = False,
) -> str:
    # ⚙️ ambient flow mirror — node-originated work 의 트리거(받은 지시) 카드. 라우팅
    # 보일러플레이트 헤더([claude-skills HEAD]/[CLAUDE-REVIEW-ROUTE]/[NODE-ACK-]/발신수신
    # dedup)를 gist 에서 빼고, 라우트 헤더의 from=<host>·task= 를 "🍎 본진 → 🖥 · T-…"
    # 한 줄로 만든 뒤 남은 의미있는 1~2줄을 내용 gist 로 붙인다. 헤더밖에 없어 내용이
    # 비어도 from/task 줄만이라도 보여 누가-무슨 task 인지는 드러난다. (T-260630-33)
    #
    # T-260702-06: 같은 정제 로직을 송신카드(📤 보낸 지시)에도 재사용해 수신/송신 카드
    # drift 를 막는다. 송신카드는 from/to alias 가 명시되므로 라우트 라벨을 양쪽 모두 노드명으로
    # 렌더하고, 수신카드는 기존처럼 self_emoji 만 받는다.
    raw = text or ""
    # 텔레그램-origin(clb- nonce) 프롬프트는 노드발 지시가 아니므로 카드 X (caller 게이트
    # not-nonce 와 동형 방어).
    if _BRIDGE_NONCE_RE.search(raw):
        return ""
    # T-260703-16 ②: 로컬 슬래시-명령 출력(/model·/clear 등)은 노드발 지시가 아님 → 카드 X.
    if _LOCAL_COMMAND_RE.search(raw):
        return ""
    # T-260709-79: /context 마크다운 요약(태그 없는 별도 레코드)도 로컬 명령 출력 → 카드 X.
    if _LOCAL_CONTEXT_SUMMARY_RE.match(raw):
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    from_host: str | None = None
    task_id: str | None = None
    for ln in lines:
        if from_host is None:
            m = _DIRECTIVE_FROM_RE.search(ln)
            if m:
                from_host = m.group(1)
        if task_id is None:
            m = _DIRECTIVE_TASK_RE.search(ln)
            if m:
                task_id = m.group(1)
    has_boilerplate = any(_directive_is_boilerplate(ln) for ln in lines)
    content_lines = [ln for ln in lines if not _directive_is_boilerplate(ln)]
    route_line = ""
    route_from = from_alias or from_host
    if route_from:
        label, emoji = node_label_emoji(route_from)
        sender = f"{emoji} {label}".strip()
        if to_alias:
            recv_label, recv_emoji = node_label_emoji(to_alias)
            recv = f"{recv_emoji} {recv_label}".strip()
        else:
            recv = self_emoji if self_emoji is not None else node_defaults()[1]
        route_line = f"{sender} → {recv}"
        if task_id:
            route_line = f"{route_line} · {task_id}"
    title = _ambient_title_from_lines(lines)
    title_summary = _ambient_title_summary_ko(title, lines, route_line)
    if (
        header == AMBIENT_DIRECTIVE_HEADER
        and not route_from
        and not task_id
        and not has_boilerplate
        and not title
    ):
        gist = "\n".join(content_lines[:2])[:AMBIENT_DIRECTIVE_LIMIT].strip()
        return f"{TERMINAL_INPUT_HEADER}\n{gist}" if gist else ""
    # alt3 이야기체 (spec v0.2 매트릭스 directive_sent aniki_dm 동형, T-260702-37 PR-B 판단 (b)
    # 카드 유지+이야기체): 라우트 줄 "X → Y · T-…" 를 사람 문장으로 바꾼다. 발신·수신 라벨이
    # 둘 다 해석될 때만 — 못 읽으면 기존 카드 그대로 (fallback, 행동 보존).
    if narrative and route_from:
        s_label, s_emoji = node_label_emoji(route_from)
        recv_token = to_alias or self_alias or node_defaults()[0]
        r_label, r_emoji = node_label_emoji(recv_token)
        if s_label and r_label:
            sentence = (
                f"{s_emoji} {s_label}{subject_particle(s_label)} "
                f"{r_emoji} {r_label}에게 맡겼어요"
            )
            if title_summary:
                sentence = f"{sentence} — {title_summary}"
            if task_id:
                sentence = f"{sentence} ({task_id})"
            parts = [sentence, *content_lines[:2]]
            gist = "\n".join(parts)[:AMBIENT_DIRECTIVE_LIMIT].strip()
            return gist
    parts: list[str] = []
    if route_line:
        parts.append(route_line)
    if title_summary:
        parts.append(title_summary)
    parts.extend(content_lines[:2])
    if not parts and task_id:
        parts.append(task_id)
    gist = "\n".join(parts)[:AMBIENT_DIRECTIVE_LIMIT].strip()
    return f"{header}\n{gist}" if gist else ""


def format_ambient_directive(
    text: str,
    self_emoji: str | None = None,
    self_alias: str | None = None,
) -> str:
    # alt3 ON = 이야기체 (수신 노드 = 자기 자신, self_alias 미지정 시 hostname 해석).
    # 송신카드(format_sent_directive)는 node_dm 운영 카드라 v0.1 불변 — narrative 미적용.
    return _format_directive_card(
        text,
        header=AMBIENT_DIRECTIVE_HEADER,
        self_emoji=self_emoji,
        self_alias=self_alias,
        narrative=alt3_narrative_enabled(),
    )


def format_sent_directive(text: str, from_alias: str, to_alias: str) -> str:
    return _format_directive_card(
        text,
        header=SENT_DIRECTIVE_HEADER,
        from_alias=from_alias,
        to_alias=to_alias,
    )


def screen_status_region(screen: str, tail_lines: int = 16) -> str:
    """Return the current terminal status area, excluding stale scrollback.

    tmux capture-pane intentionally grabs more lines for diagnostics, but busy
    state should be inferred from the visible bottom of the pane. Otherwise an
    old approval prompt in scrollback can wedge the bridge as approval_wait.
    """
    lines = (screen or "").splitlines()
    return "\n".join(lines[-tail_lines:])


def screen_has_approval_wait(screen: str) -> bool:
    return bool(APPROVAL_WAIT_RE.search(screen_status_region(screen)))


def screen_has_hook_block(screen: str) -> bool:
    return bool(HOOK_BLOCK_RE.search(screen_status_region(screen)))


def screen_has_active_work(screen: str) -> bool:
    region = strip_ansi_control(screen_status_region(screen))
    # Claude's completed footer can wrap "1 shell still running" as a physical
    # line containing only "running" in very narrow panes. That is an idle prompt
    # with a background shell, not a running assistant turn.
    region = re.sub(r"\bstill\s*\n\s*running\b", "still running", region, flags=re.IGNORECASE)
    if ACTIVE_SPINNER_RE.search(region) or ACTIVE_INTERRUPT_RE.search(region):
        return True
    # Narrow panes can wrap "esc to interrupt" or spinner text across physical
    # rows. tmux capture-pane callers do not always use -J, so inspect a joined
    # logical view as a second pass.
    joined = " ".join(region.splitlines())
    if ACTIVE_SPINNER_RE.search(joined) or ACTIVE_INTERRUPT_RE.search(joined):
        return True
    # T-260709-73: current Claude Code foreground tools render a glyph-less
    # "Running…" plus a ctrl+b "run in background" affordance. S5's spinner
    # glyph requirement correctly rejected answer prose, but also hid this real
    # busy state after transcript mtime aged past the 1s freshness window.
    return bool(
        ACTIVE_FOREGROUND_TOOL_RE.search(region)
        and ACTIVE_BACKGROUND_HINT_RE.search(joined)
    )


def strip_inline_node_emoji_header(text: str) -> str:
    lines = (text or "").splitlines()
    if not lines:
        return text
    first = lines[0].lstrip()
    for emoji in NODE_EMOJI_LINES:
        if first.startswith(emoji):
            rest = first[len(emoji) :]
            if rest and rest[0].isspace():
                lines[0] = rest.lstrip()
                return "\n".join(lines).strip()
    return text


def strip_node_emoji_header(text: str) -> str:
    lines = (text or "").splitlines()
    if not lines:
        return text
    first = lines[0].strip()
    if first in NODE_EMOJI_LINES:
        return "\n".join(lines[1:]).strip()
    return strip_inline_node_emoji_header(text).strip()


def is_copy_payload_message(text: str) -> bool:
    body = strip_node_emoji_header(text).strip()
    if not body:
        return False
    first_line = body.splitlines()[0].strip()
    return (
        first_line == "/goal"
        or first_line.startswith("/goal ")
        or first_line.startswith("상세스펙:")
        or first_line.startswith("상세 스펙:")
        or first_line.startswith("상세설명:")
        or first_line.startswith("상세 설명:")
        or re.match(r"^제목\s*:", first_line) is not None
        or re.match(r"^(내용|본문)\s*:", first_line) is not None
    )


def split_copy_payload_messages(text: str) -> list[str]:
    body = strip_node_emoji_header(text).strip()
    if not is_copy_payload_message(body):
        return []
    split_re = re.compile(
        r"\n(?=(?:/goal(?:\s|$)|상세\s*스펙:|상세\s*설명:|제목\s*:|(?:내용|본문)\s*:))"
    )
    starts = [0, *[match.start() + 1 for match in split_re.finditer(body)]]
    starts = sorted(set(starts))
    parts: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        part = body[start:end].strip()
        if part and is_copy_payload_message(part):
            parts.append(part)
    return parts or [body]


def copy_payload_dedup_key(text: str) -> str:
    body = strip_node_emoji_header(text).strip()
    if not body or not is_copy_payload_message(body):
        return ""
    digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
    return f"copy_payload:{digest}"


def record_timestamp_seconds(record: dict[str, Any]) -> float | None:
    raw = record.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def content_has_tool(content: Any, name: str) -> bool:
    if not isinstance(content, list):
        return False
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == name:
            return True
    return False


def record_contains_nonce(record: dict[str, Any]) -> str | None:
    text = content_text((record.get("message") or {}).get("content"))
    match = NONCE_RE.search(text)
    return match.group(0) if match else None


def record_attachment_nonce(record: dict[str, Any]) -> str | None:
    text = attachment_text(record.get("attachment"))
    match = NONCE_RE.search(text)
    return match.group(0) if match else None


def terminal_retry_original_text(text: str) -> str:
    cleaned = sanitize_text(text)
    lines = cleaned.splitlines()
    marker_indexes = [index for index, line in enumerate(lines) if line.strip() == "원문:"]
    if marker_indexes and any("브릿지 재주입 재시도" in line for line in lines[: marker_indexes[-1]]):
        return sanitize_text("\n".join(lines[marker_indexes[-1] + 1 :]))
    return cleaned


def outbox_key(nonce: str, assistant_uuid: str, answer: str) -> str:
    digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    return f"{nonce}:{assistant_uuid}:{digest}"


def answer_outbox_key(nonce: str, assistant_uuid: str, answer: str) -> str:
    return copy_payload_dedup_key(answer) or outbox_key(nonce, assistant_uuid, answer)


def message_update_key(update: dict[str, Any], bot_token_hash: str) -> str:
    message = update.get("message") or update.get("edited_message") or {}
    if not isinstance(message, dict):
        message = {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    parts = [
        bot_token_hash,
        str(chat.get("id") or ""),
        str(update.get("update_id") or ""),
        str(message.get("message_id") or ""),
        str(message.get("media_group_id") or ""),
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


class TelegramHTTPError(RuntimeError):
    def __init__(self, method: str, code: int, body: str) -> None:
        super().__init__(f"{method} HTTP {code}: {body[:300]}")
        self.method = method
        self.code = code
        self.body = body

    @property
    def is_conflict(self) -> bool:
        return self.code == 409 or "Conflict:" in self.body


# F3 (T-260705-72): 429 flood control 대응 상수 — codex-repl-telegram-bridge.py 동형.
# 고정 2s×3(+외곽 5s×3) 예산이 통상 30s+ flood 대기를 못 넘겨 최종답이 영구 유실되던 갭.
TELEGRAM_FLOOD_MAX_WAITS = 3
TELEGRAM_FLOOD_WAIT_CAP_SECONDS = 61.0


def telegram_retry_after_seconds(body: str, headers: Any = None, default: float = 3.0) -> float:
    """429 응답에서 대기 초를 해석: body JSON parameters.retry_after → Retry-After 헤더 → default."""
    try:
        payload = json.loads(body)
        value = (payload.get("parameters") or {}).get("retry_after")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = headers.get("Retry-After") if headers is not None else None
        if raw is not None:
            return max(0.0, float(raw))
    except Exception:  # noqa: BLE001
        pass
    return default


class TelegramClient:
    def __init__(self, token: str, chat_id: str, emoji: str, chunk_size: int) -> None:
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.emoji = emoji
        self.chunk_size = chunk_size

    def call(self, method: str, **params: Any) -> dict[str, Any] | None:
        cutover_payload = mesh_cutover_call(method, params)
        if cutover_payload is not None:
            return cutover_payload
        data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(f"{self.api}/{method}", data=data)

        def give_up(detail: str) -> None:
            log("TGERR", f"{method} failed: {detail}")
            mesh_ledger_record(method, params.get("chat_id"), params.get("text"), None, message_id=params.get("message_id"))

        # F3 (T-260705-72): 429 는 retry_after 를 지켜 기다렸다 재시도 — flood 대기는
        # 일반 재시도 예산(3회)과 별도로 센다. 그 외 4xx 는 영구 오류라 즉시 단락.
        attempt = 0
        flood_waits = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                mesh_ledger_record(method, params.get("chat_id"), params.get("text"), payload, message_id=params.get("message_id"))
                return payload if isinstance(payload, dict) else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 409:
                    raise TelegramHTTPError(method, exc.code, body) from exc
                if exc.code == 429 and flood_waits < TELEGRAM_FLOOD_MAX_WAITS:
                    flood_waits += 1
                    wait = min(telegram_retry_after_seconds(body, exc.headers) + 1.0, TELEGRAM_FLOOD_WAIT_CAP_SECONDS)
                    log("TGERR", f"{method} 429 flood; waiting {wait:.0f}s ({flood_waits}/{TELEGRAM_FLOOD_MAX_WAITS})")
                    time.sleep(wait)
                    continue
                if 400 <= exc.code < 500:
                    # 4xx(flood 예산 소진 포함)는 재시도 무의미 — 즉시 단락.
                    give_up(f"HTTP {exc.code} {body[:200]}")
                    return None
                attempt += 1
                if attempt >= 3:
                    give_up(f"HTTP {exc.code} {body[:200]}")
                    return None
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt >= 3:
                    give_up(str(exc))
                    return None
            time.sleep(2)

    def call_multipart(
        self,
        method: str,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> dict[str, Any] | None:
        boundary = "----clb" + secrets.token_hex(16)
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        filename = file_path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode()
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(file_path.read_bytes())
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())

        request = urllib.request.Request(
            f"{self.api}/{method}",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        attempt = 0
        flood_waits = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                return payload if isinstance(payload, dict) else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and flood_waits < TELEGRAM_FLOOD_MAX_WAITS:
                    flood_waits += 1
                    wait = min(telegram_retry_after_seconds(body, exc.headers) + 1.0, TELEGRAM_FLOOD_WAIT_CAP_SECONDS)
                    log("TGERR", f"{method} upload 429 flood; waiting {wait:.0f}s ({flood_waits}/{TELEGRAM_FLOOD_MAX_WAITS})")
                    time.sleep(wait)
                    continue
                if 400 <= exc.code < 500:
                    log("TGERR", f"{method} upload failed: HTTP {exc.code} {body[:200]}")
                    return None
                attempt += 1
                if attempt >= 3:
                    log("TGERR", f"{method} upload failed: HTTP {exc.code} {body[:200]}")
                    return None
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt >= 3:
                    log("TGERR", f"{method} upload failed: {exc}")
                    return None
            time.sleep(2)

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        payload = self.call("getUpdates", offset=offset, timeout=timeout)
        if not payload or not payload.get("ok"):
            return []
        result = payload.get("result")
        return result if isinstance(result, list) else []

    def get_webhook_info(self) -> dict[str, Any]:
        payload = self.call("getWebhookInfo")
        result = payload.get("result") if isinstance(payload, dict) else None
        return result if isinstance(result, dict) else {}

    def delete_webhook(self) -> bool:
        payload = self.call("deleteWebhook", drop_pending_updates="false")
        return bool(payload and payload.get("ok"))

    def send_typing(self) -> None:
        self.call("sendChatAction", chat_id=self.chat_id, action="typing")

    def download_file(
        self,
        file_id: str,
        output_dir: Path,
        name_hint: str,
        default_suffix: str = ".bin",
        allowed_extensions: set[str] | None = None,
    ) -> Path:
        payload = self.call("getFile", file_id=file_id)
        if not payload or not payload.get("ok") or not isinstance(payload.get("result"), dict):
            raise RuntimeError("Telegram getFile failed")
        file_path = str(payload["result"].get("file_path") or "")
        if not file_path:
            raise RuntimeError("Telegram getFile returned empty file_path")

        suffix = Path(file_path).suffix.lower() or default_suffix
        if allowed_extensions is not None and suffix not in allowed_extensions:
            suffix = default_suffix
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{safe_filename_part(name_hint)}{suffix}"

        quoted_path = urllib.parse.quote(file_path, safe="/")
        request = urllib.request.Request(f"https://api.telegram.org/file/bot{self.token}/{quoted_path}")
        # T-260705-43: 단발 통짜 read()는 텔레그램 파일서버 지연 국면에
        # 'The read operation timed out' 으로 그대로 실패(2026-07-05 본진+라이덴 크로스노드 재현).
        # chunk read 는 소켓 타임아웃이 청크마다 갱신되고, transient 실패는 백오프 재시도.
        last_err: Exception | None = None
        # T-260705-56 (2): per-attempt 60s→30s — 최악 단일스레드 블록 ~3min → ~1.6min.
        try:
            attempt_timeout = float(os.environ.get("CLB_DOWNLOAD_ATTEMPT_TIMEOUT_SEC", "30"))
        except ValueError:
            attempt_timeout = 30.0
        for attempt in range(3):
            try:
                buf = bytearray()
                with urllib.request.urlopen(request, timeout=attempt_timeout) as response:
                    while True:
                        chunk = response.read(1 << 16)
                        if not chunk:
                            break
                        buf.extend(chunk)
                output_path.write_bytes(bytes(buf))
                return output_path
            except (OSError, http.client.HTTPException) as err:
                # T-260705-56 (1): 중간 끊김의 http.client.IncompleteRead 는 OSError 가 아니라
                # HTTPException 계열 — 기존 except OSError 는 이를 놓쳐 재시도 없이 즉사했다.
                last_err = err
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"file download failed after 3 attempts: {last_err}")

    def with_emoji_prefix(self, text: str) -> str:
        text = strip_bridge_nonce_markers(text)
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        if first_line == self.emoji:
            return text
        text = strip_node_emoji_header(text)
        return f"{self.emoji}\n{text}"

    def chunks(self, text: str) -> list[str]:
        text = strip_bridge_nonce_markers(text or "")
        text = self.with_emoji_prefix(text or "(empty response)")
        chunks = [text[: self.chunk_size]]
        rest = text[self.chunk_size :]
        while rest:
            chunks.append(rest[: self.chunk_size])
            rest = rest[self.chunk_size :]
        return chunks

    def send(self, text: str, reply_to_message_id: int | None = None, mono: bool = False) -> list[int] | None:
        message_ids: list[int] = []
        for idx, chunk in enumerate(self.chunks(text)):
            params: dict[str, Any] = {"chat_id": self.chat_id, "text": chunk}
            if mono:
                # T-260709-80: 정렬 의존 블록(동전 매트릭스)용 pre entity — offset/length 는
                # 텔레그램 규격상 UTF-16 code unit 기준.
                utf16_len = len(chunk.encode("utf-16-le")) // 2
                params["entities"] = json.dumps([{"type": "pre", "offset": 0, "length": utf16_len}])
            if reply_to_message_id and idx == 0:
                # alt3 타래 (spec v0.2 §5, T-260702-37 PR-B): 분할 시 첫 chunk 만 루트에 단다.
                # §5-4 — 루트 삭제 등 거부 케이스 포함 유실 0.
                params["reply_to_message_id"] = reply_to_message_id
                params["allow_sending_without_reply"] = "true"
            payload = self.call("sendMessage", **params)
            if not payload or not payload.get("ok"):
                return None
            result = payload.get("result")
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                message_ids.append(int(result["message_id"]))
        return message_ids

    def send_photo(self, path: Path, caption: str = "", reply_to_message_id: int | None = None) -> list[int] | None:
        fields: dict[str, str] = {"chat_id": str(self.chat_id)}
        if caption:
            fields["caption"] = self.with_emoji_prefix(caption)[:1024]
        if reply_to_message_id:
            fields["reply_to_message_id"] = str(reply_to_message_id)
            fields["allow_sending_without_reply"] = "true"
        payload = self.call_multipart("sendPhoto", fields, "photo", path)
        if not payload or not payload.get("ok"):
            payload = self.call_multipart("sendDocument", fields, "document", path)
        if not payload or not payload.get("ok"):
            return None
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return [int(result["message_id"])]
        return []

    def send_update_button(self, text: str, callback_data: str) -> None:
        reply_markup = json.dumps(
            {"inline_keyboard": [[{"text": "\U0001f504 지금 업데이트", "callback_data": callback_data}]]},
            ensure_ascii=False,
        )
        self.call("sendMessage", chat_id=self.chat_id, text=self.with_emoji_prefix(text), reply_markup=reply_markup)

    def edit(self, message_id: int, text: str) -> None:
        # ⚙️ flow mirror edit-in-place — update one card instead of sending many.
        self.call(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=message_id,
            text=strip_bridge_nonce_markers(text) or "(empty response)",
        )


@dataclass(frozen=True)
class SessionIdentity:
    path: Path
    dev: int
    ino: int
    size: int


def session_identity(path: Path) -> SessionIdentity:
    stat = path.stat()
    return SessionIdentity(path=path, dev=stat.st_dev, ino=stat.st_ino, size=stat.st_size)


def cursor_offset_for_state(state: dict[str, Any] | None, identity: SessionIdentity) -> int | None:
    if not state:
        return None
    try:
        dev = int(state.get("dev"))
        ino = int(state.get("ino"))
        offset = int(state.get("offset"))
    except (TypeError, ValueError):
        return None
    if dev != identity.dev or ino != identity.ino:
        return None
    if offset < 0 or offset > identity.size:
        return None
    return offset


@dataclass(frozen=True)
class Config:
    node: str
    emoji: str
    token_file: Path
    chat_id: str
    state_dir: Path
    tmux_bin: str
    tmux_socket: str
    tmux_session: str
    telegram_chunk: int
    poll_timeout: int
    typing_max_seconds: int
    audio_transcribe_cmd: str | None
    audio_transcribe_timeout: int
    start_at_end: bool
    state_path: Path
    offset_file: Path
    pid_file: Path
    queue_path: Path
    outbox_path: Path
    quarantine_path: Path
    session_sidecar_path: Path
    egress_sidecar_path: Path
    token_registry_path: Path
    claude_projects_dir: Path
    token_owner: str
    expected_consumer: str
    expected_host: str
    session_ttl_seconds: int
    egress_ttl_seconds: int
    pending_transcript_seconds: int
    turn_sequence_fallback_seconds: float
    active_turn_stale_seconds: float
    transcript_stable_seconds: float
    latest_transcript_fallback_seconds: float
    composer_clear_retries: int
    injection_verify_timeout: float
    send_retry_seconds: float
    send_max_attempts: int
    queue_compact_max_events: int
    outbox_max_entries: int
    envelope_sidecar_flag_path: Path = ENVELOPE_SIDECAR_FLAG
    envelope_sidecar_off_flag_path: Path = ENVELOPE_SIDECAR_OFF_FLAG
    envelope_sidecar_path: Path = ENVELOPE_SIDECAR_PATH
    envelope_sidecar_ttl_seconds: float = DEFAULT_ENVELOPE_SIDECAR_TTL_SECONDS

    @classmethod
    def from_env(cls) -> "Config":
        default_node, default_emoji = node_defaults()
        node = env("CLB_NODE", default_node) or default_node
        state_dir = Path(env("CLB_STATE_DIR", "~/.local/state/claude-telegram-bridge") or "").expanduser()
        default_name = f"claude-telegram-bridge-{node}"
        return cls(
            node=node,
            emoji=env("CLB_EMOJI", default_emoji) or default_emoji,
            token_file=Path(
                env("CLB_TOKEN_FILE", "~/.config/claude-telegram-bridge/token.json") or ""
            ).expanduser(),
            chat_id=env("CLB_CHAT_ID", "") or "",
            state_dir=state_dir,
            tmux_bin=env("CLB_TMUX_BIN", "tmux") or "tmux",
            tmux_socket=env("CLB_TMUX_SOCKET", "default") or "default",
            tmux_session=env("CLB_TMUX_SESSION", "claude") or "claude",
            telegram_chunk=int_env("CLB_TG_CHUNK", 4096, minimum=512),
            poll_timeout=int_env("CLB_TG_POLL_TIMEOUT", 2, minimum=1),
            typing_max_seconds=int_env("CLB_TYPING_MAX_SECONDS", 7200, minimum=30),
            audio_transcribe_cmd=env("CLB_AUDIO_TRANSCRIBE_CMD"),
            audio_transcribe_timeout=int_env("CLB_AUDIO_TRANSCRIBE_TIMEOUT", 600, minimum=10),
            start_at_end=bool_env("CLB_START_AT_END", True),
            state_path=Path(env("CLB_STATE_PATH", str(state_dir / f"{default_name}.state.json")) or "").expanduser(),
            offset_file=Path(env("CLB_OFFSET_FILE", str(state_dir / f"{default_name}.offset")) or "").expanduser(),
            pid_file=Path(env("CLB_PID_FILE", str(state_dir / f"{default_name}.pid")) or "").expanduser(),
            queue_path=Path(env("CLB_QUEUE_PATH", str(state_dir / f"{default_name}.queue.jsonl")) or "").expanduser(),
            outbox_path=Path(env("CLB_OUTBOX_PATH", str(state_dir / f"{default_name}.outbox.json")) or "").expanduser(),
            quarantine_path=Path(env("CLB_QUARANTINE_PATH", str(state_dir / f"{default_name}.quarantine.jsonl")) or "").expanduser(),
            session_sidecar_path=Path(
                env("CLB_SESSION_SIDECAR", str(state_dir / "claude-telegram-bridge-sessions.json")) or ""
            ).expanduser(),
            egress_sidecar_path=Path(
                env("CLB_EGRESS_SIDECAR", str(state_dir / "claude-telegram-bridge-egress.json")) or ""
            ).expanduser(),
            token_registry_path=Path(
                env("CLB_TOKEN_REGISTRY", "~/.config/claude-telegram-bridge/token-registry.json") or ""
            ).expanduser(),
            claude_projects_dir=Path(env("CLB_CLAUDE_PROJECTS_DIR", "~/.claude/projects") or "").expanduser(),
            token_owner=env("CLB_TOKEN_OWNER", BRIDGE_OWNER) or BRIDGE_OWNER,
            expected_consumer=env("CLB_EXPECTED_CONSUMER", node) or node,
            expected_host=env("CLB_EXPECTED_HOST", os.uname().nodename) or os.uname().nodename,
            session_ttl_seconds=int_env("CLB_SESSION_TTL_SECONDS", 86400, minimum=60),
            egress_ttl_seconds=int_env("CLB_EGRESS_TTL_SECONDS", 900, minimum=60),
            pending_transcript_seconds=int_env("CLB_PENDING_TRANSCRIPT_SECONDS", 300, minimum=10),
            turn_sequence_fallback_seconds=float(env("CLB_TURN_SEQUENCE_FALLBACK_SECONDS", "7200") or "7200"),
            active_turn_stale_seconds=float(env("CLB_ACTIVE_TURN_STALE_SECONDS", "900") or "900"),
            transcript_stable_seconds=float(env("CLB_TRANSCRIPT_STABLE_SECONDS", "1.0") or "1.0"),
            latest_transcript_fallback_seconds=float(
                env("CLB_LATEST_TRANSCRIPT_FALLBACK_SECONDS", "1800") or "1800"
            ),
            composer_clear_retries=int_env("CLB_COMPOSER_CLEAR_RETRIES", 2, minimum=1),
            injection_verify_timeout=float(env("CLB_INJECTION_VERIFY_TIMEOUT", "20") or "20"),
            send_retry_seconds=float(env("CLB_SEND_RETRY_SECONDS", "5") or "5"),
            send_max_attempts=int_env("CLB_SEND_MAX_ATTEMPTS", 3, minimum=1),
            queue_compact_max_events=int_env("CLB_QUEUE_COMPACT_MAX_EVENTS", 5000, minimum=100),
            outbox_max_entries=int_env("CLB_OUTBOX_MAX_ENTRIES", 2000, minimum=100),
            envelope_sidecar_flag_path=Path(
                env("CLB_ENVELOPE_SIDECAR_FLAG", str(ENVELOPE_SIDECAR_FLAG)) or ""
            ).expanduser(),
            envelope_sidecar_off_flag_path=Path(
                env("CLB_ENVELOPE_SIDECAR_OFF_FLAG", str(ENVELOPE_SIDECAR_OFF_FLAG)) or ""
            ).expanduser(),
            envelope_sidecar_path=Path(env("CLB_ENVELOPE_SIDECAR_PATH", str(ENVELOPE_SIDECAR_PATH)) or "").expanduser(),
            envelope_sidecar_ttl_seconds=float_env(
                "CLB_ENVELOPE_SIDECAR_TTL_SECONDS",
                DEFAULT_ENVELOPE_SIDECAR_TTL_SECONDS,
                minimum=1.0,
            ),
        )

    @property
    def session_target(self) -> str:
        target = self.tmux_session
        if target.startswith("%") or ":" in target or "." in target:
            return target
        return f"={target}"

    @property
    def pane_target(self) -> str:
        target = self.tmux_session
        if target.startswith("%") or ":" in target or "." in target:
            return target
        return f"={target}:"


@dataclass
class QueueItem:
    queue_id: str
    update_id: int
    message_id: int
    text: str
    nonce: str
    received_at: float = field(default_factory=time.time)
    # T-260705-67: 아니키 발신 시각(Telegram message.date, epoch sec). 0.0 = unknown(레거시/미상).
    # finish_active_turn 등이 QueueItem 을 위치인자 6개로 재구성하므로 기존 위치 필드 순서는 유지.
    sent_at: float = 0.0
    source: str = ""
    voice_reply_path: str = ""
    # T-260707-36: generating 중 이 항목을 Escape 없는 native 큐잉으로 이미 TUI 에 주입했는지
    # 표시. per-item 멱등성 플래그 — 옛 "active_turn 존재 여부로 재주입 차단"을 대체한다.
    # 다음 drain 이 재-paste 하지 않도록(그리고 브릿지 재기동 후에도 재주입 안 하도록) durable
    # queue 레코드에 함께 실려 복원된다.
    busy_injected: bool = False
    # T-260708-46: 다중 busy-inject 에서는 후속 pending 의 user JSONL nonce 가 active_turn
    # 승계 전에 먼저 보일 수 있다. 관측 정보를 pending item 에 보존해 promote 뒤 verify
    # timeout 으로 빠지지 않게 한다.
    user_uuid: str = ""
    user_seen_at: float = 0.0
    native_queue_seen_at: float = 0.0
    # T-260709-70: 음성 전사 에코를 아니키 채팅에 1회만 보내는 멱등 플래그. durable 레코드에
    # 실려 브릿지 재기동 복원 후에도 중복 에코를 막는다.
    voice_echo_sent: bool = False

    def to_json(self) -> dict[str, Any]:
        payload = {
            "queue_id": self.queue_id,
            "update_id": self.update_id,
            "message_id": self.message_id,
            "text": self.text,
            "nonce": self.nonce,
            "received_at": self.received_at,
            "sent_at": self.sent_at,
        }
        if self.source:
            payload["source"] = self.source
        if self.voice_reply_path:
            payload["voice_reply_path"] = self.voice_reply_path
        if self.busy_injected:
            payload["busy_injected"] = True
        if self.user_uuid:
            payload["user_uuid"] = self.user_uuid
            payload["user_seen_at"] = self.user_seen_at
        if self.native_queue_seen_at > 0:
            payload["native_queue_seen_at"] = self.native_queue_seen_at
        if self.voice_echo_sent:
            payload["voice_echo_sent"] = True
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "QueueItem":
        return cls(
            queue_id=str(payload["queue_id"]),
            update_id=int(payload["update_id"]),
            message_id=int(payload.get("message_id") or 0),
            text=str(payload["text"]),
            nonce=str(payload["nonce"]),
            received_at=float(payload.get("received_at") or time.time()),
            sent_at=float(payload.get("sent_at") or 0.0),
            source=str(payload.get("source") or ""),
            voice_reply_path=str(payload.get("voice_reply_path") or ""),
            busy_injected=bool(payload.get("busy_injected")),
            user_uuid=str(payload.get("user_uuid") or ""),
            user_seen_at=float(payload.get("user_seen_at") or 0.0),
            native_queue_seen_at=float(payload.get("native_queue_seen_at") or 0.0),
            voice_echo_sent=bool(payload.get("voice_echo_sent")),
        )


@dataclass
class ActiveTurn:
    queue_id: str
    update_id: int
    message_id: int
    nonce: str
    injected_at: float
    text: str
    user_uuid: str | None = None
    user_seen_at: float = 0.0
    assistant_uuid: str | None = None
    external_reply_seen: bool = False
    inject_attempts: int = 1
    pending_answer: str | None = None
    pending_assistant_uuid: str | None = None
    pending_outbox_key: str | None = None
    send_attempts: int = 0
    last_send_attempt_at: float = 0.0
    send_in_progress: bool = False
    pending_reasoning: str | None = None  # transient: 🧠 mirror text for this turn (not persisted)
    accumulated_reasoning: str = ""  # transient: thinking accrued across the turn's assistant messages
    flow_message_id: int = 0  # persisted across restart (anti-fragmentation): telegram message id of this turn's ⚙️ flow card (edit-in-place)
    flow_body: str = ""  # persisted across restart (anti-fragmentation): accumulated flow lines for this turn's single card
    sent_at: float = 0.0  # T-260705-67: 아니키 발신 시각 (QueueItem.sent_at 승계, 0.0=unknown)
    source: str = ""
    voice_reply_path: str = ""
    busy_injected: bool = False
    native_queue_seen_at: float = 0.0
    sidecar_consumed_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        payload = {
            "queue_id": self.queue_id,
            "update_id": self.update_id,
            "message_id": self.message_id,
            "nonce": self.nonce,
            "injected_at": self.injected_at,
            "sent_at": self.sent_at,
            "text": self.text,
            "user_uuid": self.user_uuid,
            "user_seen_at": self.user_seen_at,
            "assistant_uuid": self.assistant_uuid,
            "external_reply_seen": self.external_reply_seen,
            "inject_attempts": self.inject_attempts,
            "pending_answer": self.pending_answer,
            "pending_assistant_uuid": self.pending_assistant_uuid,
            "pending_outbox_key": self.pending_outbox_key,
            "send_attempts": self.send_attempts,
            "last_send_attempt_at": self.last_send_attempt_at,
            "flow_message_id": self.flow_message_id,
            "flow_body": self.flow_body,
            "source": self.source,
            "voice_reply_path": self.voice_reply_path,
        }
        if self.busy_injected:
            payload["busy_injected"] = True
        if self.native_queue_seen_at > 0:
            payload["native_queue_seen_at"] = self.native_queue_seen_at
        if self.sidecar_consumed_at > 0:
            payload["sidecar_consumed_at"] = self.sidecar_consumed_at
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ActiveTurn":
        return cls(
            queue_id=str(payload["queue_id"]),
            update_id=int(payload["update_id"]),
            message_id=int(payload.get("message_id") or 0),
            nonce=str(payload["nonce"]),
            injected_at=float(payload.get("injected_at") or time.time()),
            text=str(payload.get("text") or ""),
            user_uuid=payload.get("user_uuid") if isinstance(payload.get("user_uuid"), str) else None,
            user_seen_at=float(payload.get("user_seen_at") or 0.0),
            assistant_uuid=payload.get("assistant_uuid") if isinstance(payload.get("assistant_uuid"), str) else None,
            external_reply_seen=bool(payload.get("external_reply_seen")),
            inject_attempts=int(payload.get("inject_attempts") or 1),
            pending_answer=payload.get("pending_answer") if isinstance(payload.get("pending_answer"), str) else None,
            pending_assistant_uuid=(
                payload.get("pending_assistant_uuid")
                if isinstance(payload.get("pending_assistant_uuid"), str)
                else None
            ),
            pending_outbox_key=(
                payload.get("pending_outbox_key") if isinstance(payload.get("pending_outbox_key"), str) else None
            ),
            send_attempts=int(payload.get("send_attempts") or 0),
            last_send_attempt_at=float(payload.get("last_send_attempt_at") or 0.0),
            flow_message_id=int(payload.get("flow_message_id") or 0),
            flow_body=str(payload.get("flow_body") or ""),
            sent_at=float(payload.get("sent_at") or 0.0),
            source=str(payload.get("source") or ""),
            voice_reply_path=str(payload.get("voice_reply_path") or ""),
            busy_injected=bool(payload.get("busy_injected")),
            native_queue_seen_at=float(payload.get("native_queue_seen_at") or 0.0),
            sidecar_consumed_at=float(payload.get("sidecar_consumed_at") or 0.0),
        )


@dataclass(frozen=True)
class ClaudeSessionBinding:
    transcript_path: Path
    session_id: str
    pane_pid: int


class ClaudeRepl:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._session_target: str | None = None
        self._pane_target: str | None = None

    def tmux(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        cmd = [self.config.tmux_bin, "-L", self.config.tmux_socket, *args]
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 15}
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        proc = subprocess.run(cmd, **kwargs)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"tmux {' '.join(args)} failed: {detail}")
        return proc

    def resolve_session_target(self) -> str:
        if self._session_target:
            return self._session_target
        target = self.config.tmux_session
        if target.startswith("%") or ":" in target or "." in target:
            self._session_target = target
            self._pane_target = target
            return target

        out = self.tmux("list-sessions", "-F", "#{session_name}\t#{session_group}\t#{session_created}")
        sessions: list[tuple[str, str, int]] = []
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name, group, created_raw = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if not name:
                continue
            try:
                created = int(created_raw)
            except ValueError:
                created = 0
            sessions.append((name, group, created))

        exact = [item for item in sessions if item[0] == target]
        grouped = [item for item in sessions if item[1] == target]
        prefixed = [item for item in sessions if item[0].startswith(f"{target}-")]
        candidates = exact or grouped or prefixed
        if not candidates:
            raise RuntimeError(f"tmux session not found: socket={self.config.tmux_socket} session={target}")
        name = sorted(candidates, key=lambda item: (item[2], item[0]))[0][0]
        self._session_target = f"={name}"
        self._pane_target = f"={name}:"
        return self._session_target

    def resolve_pane_target(self) -> str:
        if self._pane_target:
            return self._pane_target
        self.resolve_session_target()
        return self._pane_target or self._session_target or self.config.pane_target

    def verify(self) -> None:
        self.tmux("has-session", "-t", self.resolve_session_target())

    def invalidate_target_cache(self) -> None:
        self._session_target = None
        self._pane_target = None

    def pane_pid(self) -> int:
        # tmux 세션 재생성 시 캐시 타깃이 영구 stale 로 남아 'could not resolve
        # pane pid' 가 브릿지 재시작 전까지 지속되던 갭(T-260705-09 ②, 2026-07-05
        # 01:58 2노드 동시 실측) — 실패 시 캐시를 비우고 1회 재해석 후 재시도.
        last_error: Exception | None = None
        for _attempt in range(2):
            target = self.resolve_pane_target()
            try:
                out = self.tmux("display-message", "-p", "-t", target, "#{pane_pid}")
            except RuntimeError as exc:
                last_error = exc
            else:
                raw = out.stdout.strip()
                if raw.isdigit():
                    return int(raw)
                last_error = RuntimeError(
                    f"could not resolve pane pid: {raw!r} "
                    f"(target={target}, socket={self.config.tmux_socket})"
                )
            # 최종 실패여도 캐시는 비워 둔다 — 다음 사이클이 신선 해석을 타게
            self.invalidate_target_cache()
        assert last_error is not None
        raise last_error

    def pane_tty(self) -> str:
        out = self.tmux("display-message", "-p", "-t", self.resolve_pane_target(), "#{pane_tty}")
        return out.stdout.strip()

    def pane_current_path(self) -> str:
        out = self.tmux("display-message", "-p", "-t", self.resolve_pane_target(), "#{pane_current_path}")
        return out.stdout.strip()

    def capture_pane(self, lines: int = 80, ansi: bool = False) -> str:
        args = ["capture-pane", "-p", "-J"]
        if ansi:
            args.append("-e")
        args.extend(["-S", f"-{max(1, lines)}", "-t", self.resolve_pane_target()])
        out = self.tmux(*args)
        return out.stdout

    @contextmanager
    def temporary_window_width(self, columns: int = STATUS_WIDE_CAPTURE_COLUMNS):
        # /context 등 폭이 넓은 TUI 시각화를 좁은 tmux 창에서 capture-pane 하면
        # 잘려 나온다. 캡처 동안만 창/pane 을 넓혔다가 원복한다 (codex 브릿지 포팅).
        pane = self.resolve_pane_target()
        try:
            out = self.tmux(
                "display-message", "-p", "-t", pane,
                "#{window_id} #{window_width} #{window_height} #{pane_width} #{pane_height}",
            )
            window_id, window_width, window_height, pane_width, pane_height = out.stdout.strip().split()
            original_window_width = int(window_width)
            original_window_height = int(window_height)
            original_pane_width = int(pane_width)
            original_pane_height = int(pane_height)
        except Exception as exc:  # noqa: BLE001
            log("REPL", f"wide context capture unavailable: {exc}")
            yield
            return

        target_width = max(columns, original_window_width)
        resized = False
        if target_width > original_window_width:
            try:
                self.tmux("resize-window", "-t", window_id, "-x", str(target_width), "-y", str(original_window_height))
                self.tmux("resize-pane", "-t", pane, "-x", str(target_width))
                resized = True
                time.sleep(0.15)
            except Exception as exc:  # noqa: BLE001
                log("REPL", f"wide context capture resize failed: {exc}")
        try:
            yield
        finally:
            if resized:
                try:
                    self.tmux("resize-pane", "-t", pane, "-x", str(original_pane_width), "-y", str(original_pane_height))
                    self.tmux("resize-window", "-t", window_id, "-x", str(original_window_width), "-y", str(original_window_height))
                except Exception as exc:  # noqa: BLE001
                    log("REPL", f"wide context capture restore failed: {exc}")

    @contextmanager
    def composer_lock(self):
        path = composer_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as lock_file:
            # ⚠️ 제거 금지 (DO NOT REMOVE) — composer single-writer guard. codex 브릿지
            # composer_lock (T-260628-35) 미러링 — busy-inject(진행 중 주입, T-260707-36)와
            # idle 주입/슬래시 핸들러가 같은 composer 에 동시 send-keys 해 입력이 섞이는
            # 경합(TOCTOU: apply_model_choice 가 busy 게이트 통과 직후 busy-inject 가 claim)을 차단.
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _clear_composer_unlocked(self, interrupt: bool = True) -> None:
        self.verify()
        # interrupt=True (기본, idle 주입): Escape 로 진행 중 턴을 끊고 composer 를 비운다.
        # interrupt=False (busy-inject, T-260707-36): Escape 를 빼고 줄편집 키만 보낸다 —
        #   generating 중 composer 잔여만 비우고 진행 중 턴은 절대 끊지 않는다. C-e/C-u/C-a/C-k
        #   는 composer 줄 편집일 뿐이라 generating 을 인터럽트하지 않는다(codex 동형).
        keys = ("Escape", "C-e", "C-u", "C-a", "C-k") if interrupt else ("C-e", "C-u", "C-a", "C-k")
        for key in keys:
            self.tmux("send-keys", "-t", self.resolve_pane_target(), key)
            time.sleep(0.05)

    def clear_composer(self, interrupt: bool = True) -> None:
        with self.composer_lock():
            self._clear_composer_unlocked(interrupt)

    def _paste_prompt_unlocked(self, prompt: str, submit_key: str = "Enter") -> None:
        if not prompt.strip():
            return
        if BRACKETED_PASTE_RE.search(prompt):
            raise RuntimeError("prompt contains bracketed paste control sequences")
        self.verify()
        buffer = prompt.rstrip("\n")
        # 붙여넣기 버퍼가 bare 백슬래시로 끝나면, 이어지는 send-keys 제출키를 Claude Code TUI 가
        # `\`+Enter = 줄바꿈(line-continuation)으로 먹어 봉투가 composer 에 고착되고 nonce 가 user
        # JSONL 에 안 떠 주입이 "nonce user JSONL not observed" 로 실패한다 (실측: "ㅎㅇ\\").
        # 개행 1개를 덧붙여 백슬래시 뒤가 리터럴 개행이 되게 한다 — bracketed paste 는 Enter '키'가
        # 아닌 '문자' 삽입이라 continuation 이 안 걸리고 커서가 빈 줄에 놓여, 뒤따르는 send-keys
        # 제출키는 escape 없이 제출된다. 사용자 백슬래시 원문은 보존.
        if buffer.endswith("\\"):
            buffer += "\n"
        self.tmux("load-buffer", "-", input_text=buffer)
        self.tmux("paste-buffer", "-p", "-t", self.resolve_pane_target())
        time.sleep(0.1)
        # submit_key 기본 Enter. codex TUI 는 진행 중 큐잉 제출키가 Tab 이라 별도 submit_key 를
        # 쓴다("Repeated Enter can leave text sitting in the composer"). Claude Code TUI 의
        # generating 중 큐잉 제출키가 Enter 가 맞는지는 미검증 — 아니면 CLB_BUSY_SUBMIT_KEY 로 교체.
        self.tmux("send-keys", "-t", self.resolve_pane_target(), submit_key)

    def _submit_prompt_unlocked(self, submit_key: str = "Enter") -> None:
        self.verify()
        self.tmux("send-keys", "-t", self.resolve_pane_target(), submit_key)

    def submit_prompt(self, submit_key: str = "Enter") -> None:
        with self.composer_lock():
            self._submit_prompt_unlocked(submit_key)

    def paste_prompt(self, prompt: str, submit_key: str = "Enter") -> None:
        with self.composer_lock():
            self._paste_prompt_unlocked(prompt, submit_key)


def proc_ppid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return int(stat.rsplit(") ", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def descendants(root_pid: int) -> set[int]:
    ppids: dict[int, int] = {}
    proc_root = Path("/proc")
    if not proc_root.exists():
        return {root_pid}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppid = proc_ppid(pid)
        if ppid is not None:
            ppids[pid] = ppid
    result = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in ppids.items():
            if pid not in result and ppid in result:
                result.add(pid)
                changed = True
    return result


def proc_cmdline_text(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")


def transcripts_from_process_fds(pids: set[int]) -> set[Path]:
    candidates: set[Path] = set()
    for pid in pids:
        fd_dir = Path(f"/proc/{pid}/fd")
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if "/.claude/projects/" in target and target.endswith(".jsonl"):
                candidates.add(Path(target).resolve())
    return candidates


def pids_attached_to_tty(tty: str) -> set[int]:
    if not tty or not Path("/proc").exists():
        return set()
    target_tty = str(Path(tty))
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = proc_cmdline_text(pid)
        if "claude" not in cmdline.lower():
            continue
        for fd_name in ("0", "1", "2"):
            try:
                fd_target = os.readlink(Path(f"/proc/{pid}/fd/{fd_name}"))
            except OSError:
                continue
            if fd_target == target_tty:
                pids.add(pid)
                break
    return pids


def session_id_from_transcript(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = record.get("sessionId") or record.get("session_id")
                if isinstance(value, str) and value:
                    return value
    except OSError:
        pass
    return path.stem


def claude_project_slug(path: Path) -> str:
    resolved = str(path.expanduser().resolve())
    if resolved == "/":
        return "-"
    return resolved.replace("/", "-")


class SessionBinder:
    def __init__(self, config: Config, repl: ClaudeRepl) -> None:
        self.config = config
        self.repl = repl
        # T-260705-05: busy 고착 유령 transcript 격리 (path str → quarantined_at).
        # 사이드카 스코어링이 mtime 최신 우선이라, 격리 없이 재해석하면 유령이
        # 매번 다시 이긴다. TTL 만료로 자연 복권 (in-memory, 재시작 시 초기화).
        self.quarantined: dict[str, float] = {}

    def _quarantine_ttl_seconds(self) -> float:
        try:
            return float(os.environ.get("CLB_BUSY_STUCK_QUARANTINE_TTL_SEC", "1800"))
        except ValueError:
            return 1800.0

    def quarantine_transcript(self, path: Path) -> None:
        self.quarantined[str(Path(path).resolve())] = time.time()

    def _is_quarantined(self, path: Path) -> bool:
        now = time.time()
        ttl = self._quarantine_ttl_seconds()
        expired = [key for key, at in self.quarantined.items() if now - at > ttl]
        for key in expired:
            del self.quarantined[key]
        return str(Path(path).resolve()) in self.quarantined

    def resolve(self) -> ClaudeSessionBinding:
        pane_pid = self.repl.pane_pid()
        sidecar_matches = self._resolve_from_sidecar(pane_pid)
        if len(sidecar_matches) == 1:
            return sidecar_matches[0]
        if len(sidecar_matches) > 1:
            raise RuntimeError("ambiguous Claude SessionStart sidecar entries")

        fallback = self._resolve_from_proc_fds(pane_pid)
        if len(fallback) == 1:
            self._record_sidecar(fallback[0], "proc-fd-fallback")
            return fallback[0]
        if not fallback:
            tty_fallback = self._resolve_from_pane_tty_fds(pane_pid)
            if len(tty_fallback) == 1:
                self._record_sidecar(tty_fallback[0], "pane-tty-fallback")
                return tty_fallback[0]
            if len(tty_fallback) > 1:
                raise RuntimeError("ambiguous transcript fallback for tmux pane tty; refusing latest-jsonl guess")
            latest_fallback = self._resolve_from_latest_project_transcript(pane_pid)
            if len(latest_fallback) == 1:
                self._record_sidecar(latest_fallback[0], "latest-project-jsonl-fallback")
                return latest_fallback[0]
            if len(latest_fallback) > 1:
                raise RuntimeError("ambiguous recent project transcript fallback for tmux pane; refusing latest-jsonl guess")
            raise RuntimeError(
                "no SessionStart sidecar entry for tmux pane; proc-fd, pane-tty, and latest-project-jsonl fallback found none"
            )
        raise RuntimeError("ambiguous transcript fallback for tmux pane; refusing latest-jsonl guess")

    def _resolve_from_sidecar(self, pane_pid: int) -> list[ClaudeSessionBinding]:
        payload = read_json(self.config.session_sidecar_path) or {}
        entries = payload.get("sessions")
        if isinstance(entries, dict):
            values = [item for item in entries.values() if isinstance(item, dict)]
        elif isinstance(entries, list):
            values = [item for item in entries if isinstance(item, dict)]
        else:
            values = []

        scored_matches: list[tuple[bool, float, float, ClaudeSessionBinding]] = []
        now = time.time()
        for item in values:
            transcript_raw = str(item.get("transcript_path") or "")
            if not transcript_raw:
                continue
            transcript = Path(transcript_raw).expanduser()
            if self._is_quarantined(transcript):
                continue
            try:
                item_pid = int(item.get("pane_pid") or 0)
            except (TypeError, ValueError):
                item_pid = 0
            if item_pid != pane_pid:
                continue
            updated_at = float(item.get("updated_at") or 0)
            session_id = str(item.get("sessionId") or item.get("session_id") or "")
            if not transcript.exists():
                continue
            try:
                transcript_mtime = transcript.stat().st_mtime
            except OSError:
                continue
            if not session_id:
                session_id = session_id_from_transcript(transcript)
            binding = ClaudeSessionBinding(transcript.resolve(), session_id, pane_pid)
            fresh = bool(updated_at and now - updated_at <= self.config.session_ttl_seconds)
            scored_matches.append((fresh, transcript_mtime, updated_at, binding))
        scored_matches.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        if scored_matches and scored_matches[0][2] > 0:
            return [scored_matches[0][3]]
        return [item[3] for item in scored_matches]

    def _resolve_fresh_sidecar_metadata(self, pane_pid: int) -> list[ClaudeSessionBinding]:
        payload = read_json(self.config.session_sidecar_path) or {}
        entries = payload.get("sessions")
        if isinstance(entries, dict):
            values = [item for item in entries.values() if isinstance(item, dict)]
        elif isinstance(entries, list):
            values = [item for item in entries if isinstance(item, dict)]
        else:
            values = []

        matches: list[tuple[float, ClaudeSessionBinding]] = []
        for item in values:
            try:
                item_pid = int(item.get("pane_pid") or 0)
            except (TypeError, ValueError):
                item_pid = 0
            if item_pid != pane_pid:
                continue
            updated_at = float(item.get("updated_at") or 0)
            if not updated_at or time.time() - updated_at > self.config.session_ttl_seconds:
                continue
            raw_transcript = str(item.get("transcript_path") or "")
            if not raw_transcript:
                continue
            transcript = Path(raw_transcript).expanduser()
            session_id = str(item.get("sessionId") or item.get("session_id") or transcript.stem)
            matches.append((updated_at, ClaudeSessionBinding(transcript, session_id, pane_pid)))
        matches.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in matches]

    def _resolve_from_proc_fds(self, pane_pid: int) -> list[ClaudeSessionBinding]:
        candidates = transcripts_from_process_fds(descendants(pane_pid))
        return self._bindings_from_transcript_candidates(candidates, pane_pid)

    def _resolve_from_pane_tty_fds(self, pane_pid: int) -> list[ClaudeSessionBinding]:
        pane_tty = ""
        try:
            pane_tty = self.repl.pane_tty()
        except Exception:
            pane_tty = ""
        if not pane_tty:
            return []
        candidates = transcripts_from_process_fds(pids_attached_to_tty(pane_tty))
        return self._bindings_from_transcript_candidates(candidates, pane_pid)

    def _resolve_from_latest_project_transcript(self, pane_pid: int) -> list[ClaudeSessionBinding]:
        try:
            pane_cwd = self.repl.pane_current_path()
        except Exception:
            return []
        if not pane_cwd:
            return []
        project_dir = self.config.claude_projects_dir / claude_project_slug(Path(pane_cwd))
        try:
            candidates = [path for path in project_dir.glob("*.jsonl") if path.is_file()]
        except OSError:
            return []
        cutoff = time.time() - max(1.0, self.config.latest_transcript_fallback_seconds)
        recent: list[Path] = []
        for path in candidates:
            if self._is_quarantined(path):
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    recent.append(path.resolve())
            except OSError:
                continue
        if len(recent) != 1:
            return self._bindings_from_transcript_candidates(set(recent), pane_pid) if len(recent) > 1 else []
        return self._bindings_from_transcript_candidates(set(recent), pane_pid)

    def _bindings_from_transcript_candidates(
        self,
        candidates: set[Path],
        pane_pid: int,
    ) -> list[ClaudeSessionBinding]:
        return [
            ClaudeSessionBinding(path, session_id_from_transcript(path), pane_pid)
            for path in sorted(candidates)
            if path.exists()
        ]

    def _record_sidecar(self, binding: ClaudeSessionBinding, source: str) -> None:
        payload = read_json(self.config.session_sidecar_path) or {}
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        key = f"{binding.transcript_path}|{binding.session_id}"
        sessions[key] = {
            "bridge": BRIDGE_OWNER,
            "host": os.uname().nodename,
            "updated_at": time.time(),
            "transcript_path": str(binding.transcript_path),
            "sessionId": binding.session_id,
            "pane_pid": binding.pane_pid,
            "tmux_socket": self.config.tmux_socket,
            "tmux_session": self.config.tmux_session,
            "source": source,
        }
        write_json_atomic(
            self.config.session_sidecar_path,
            {"updated_at": time.time(), "sessions": sessions},
        )


class TokenOwnership:
    def __init__(self, config: Config, telegram: TelegramClient, token: str) -> None:
        self.config = config
        self.telegram = telegram
        self.token_hash = token_fingerprint(token)

    def verify_or_die(self, offset: int) -> None:
        registry = read_json(self.config.token_registry_path)
        if not registry:
            raise RuntimeError(f"token registry missing: {self.config.token_registry_path}")
        entry = self._registry_entry(registry)
        token_id = str(entry.get("token_id") or "")
        mode = str(entry.get("mode") or "")
        owner = str(entry.get("owner") or "")
        expected_consumer = str(entry.get("expected_consumer") or "")
        owner_host = str(entry.get("owner_host") or entry.get("expected_host") or "")
        if token_id not in {self.token_hash, f"sha256:{self.token_hash}"}:
            raise RuntimeError(
                f"token_id mismatch: registry={token_id!r} expected sha256:{self.token_hash}"
            )
        if mode != "polling":
            raise RuntimeError(f"token registry mode={mode!r}; bridge requires polling")
        if owner != self.config.token_owner:
            raise RuntimeError(f"token owner mismatch: registry={owner!r} expected={self.config.token_owner!r}")
        if expected_consumer and expected_consumer not in {self.config.expected_consumer, self.config.node}:
            raise RuntimeError(
                "token expected_consumer mismatch: "
                f"registry={expected_consumer!r} node={self.config.node!r}"
            )
        if owner_host and owner_host not in {self.config.expected_host, os.uname().nodename}:
            raise RuntimeError(
                "token owner_host mismatch: "
                f"registry={owner_host!r} expected_host={self.config.expected_host!r}"
            )

        webhook = self.telegram.get_webhook_info()
        webhook_url = str(webhook.get("url") or "")
        if webhook_url:
            if entry.get("allow_delete_webhook") is True and self._webhook_is_owned(entry, webhook_url):
                log("TOKEN", "deleteWebhook allowed by registry cutover")
                if not self.telegram.delete_webhook():
                    raise RuntimeError("deleteWebhook failed during explicit polling cutover")
            else:
                raise RuntimeError(
                    "webhook exists but registry does not allow deleting this owned/expected URL; "
                    "fail-closed"
                )

        try:
            self.telegram.get_updates(offset=offset, timeout=0)
        except TelegramHTTPError as exc:
            if exc.is_conflict:
                raise RuntimeError("getUpdates 409 conflict: token already owned by another poller") from exc
            raise

    def _registry_entry(self, registry: dict[str, Any]) -> dict[str, Any]:
        tokens = registry.get("tokens")
        if isinstance(tokens, dict):
            for key in (self.token_hash, f"sha256:{self.token_hash}", "default"):
                entry = tokens.get(key)
                if isinstance(entry, dict):
                    return entry
        if {"token_id", "mode", "owner"} <= set(registry):
            return registry
        raise RuntimeError(f"token registry has no entry for token hash {self.token_hash}")

    def _webhook_is_owned(self, entry: dict[str, Any], webhook_url: str) -> bool:
        candidates: set[str] = set()
        for key in ("expected_webhook_url", "owned_webhook_url", "webhook_url"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                candidates.add(value)
        values = entry.get("owned_webhook_urls") or entry.get("expected_webhook_urls")
        if isinstance(values, list):
            candidates.update(str(value) for value in values if value)
        return webhook_url in candidates


class DurableQueue:
    terminal = {"sent", "answered", "failed", "dropped"}
    injectable = {"received", "enqueued"}

    def __init__(self, path: Path, max_events: int = 5000) -> None:
        self.path = path
        self.max_events = max_events
        self.lock = threading.Lock()

    def append_status(self, item: QueueItem, status: str, **extra: Any) -> None:
        payload = {"ts": time.time(), "status": status, **item.to_json(), **extra}
        with self.lock:
            append_jsonl(self.path, payload)
            self.compact_if_needed()

    def records_by_queue_id(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        last: dict[str, dict[str, Any]] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("queue_id"):
                last[str(record["queue_id"])] = record
        return last

    def records_for_queue_id(self, queue_id: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and str(record.get("queue_id") or "") == queue_id:
                records.append(record)
        return records

    def status(self, queue_id: str) -> str | None:
        record = self.records_by_queue_id().get(queue_id)
        if not record:
            return None
        status = record.get("status")
        return str(status) if status else None

    def compact_if_needed(self) -> None:
        if self.max_events <= 0 or not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= self.max_events:
            return
        last = self.records_by_queue_id()
        compacted = sorted(last.values(), key=lambda record: (float(record.get("ts") or 0), str(record.get("queue_id"))))
        tmp = self.path.with_name(self.path.name + ".compact.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for record in compacted:
                json.dump(record, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    def pending_items(self) -> list[QueueItem]:
        items: list[QueueItem] = []
        for record in self.records_by_queue_id().values():
            if str(record.get("status") or "") not in self.injectable:
                continue
            try:
                items.append(QueueItem.from_json(record))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(items, key=lambda item: (item.received_at, item.update_id))


def build_voice_prompt(question: str) -> str:
    cleaned = normalize_text(question)
    return f"{VOICE_PROMPT_HEADER} {VOICE_PROMPT_INSTRUCTION}\n\n질문: {cleaned}"


def voice_question_from_prompt(text: str) -> str:
    marker = "\n\n질문: "
    idx = (text or "").rfind(marker)
    if idx < 0:
        return ""
    return text[idx + len(marker):].strip()


def format_voice_question_echo(question: str) -> str:
    cleaned = normalize_text(question)
    if len(cleaned) > VOICE_ECHO_NOTICE_THRESHOLD:
        return (
            "🎤 긴 음성 질문입니다. 전문은 세션에 주입했고, "
            "아래 원문은 텔레그램 한도에 맞춰 이어 보냅니다.\n\n"
            f"{cleaned}"
        )
    return f"🎤 {cleaned}"


def bridge_nonce() -> str:
    return f"clb-{secrets.token_hex(4)}"


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


def append_jsonl_locked(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_envelope_sidecar_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and record.get("schema") == ENVELOPE_SIDECAR_SCHEMA:
                        records.append(record)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return []
    return records


def enqueue_voice_prompt(
    config: Config,
    *,
    question: str,
    reply_path: Path,
    request_id: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    queue_id = request_id.strip() if request_id.strip() else f"voice-{int(now * 1000)}-{secrets.token_hex(4)}"
    item = QueueItem(
        queue_id=queue_id,
        update_id=int(now * 1000) % 2147483647,
        message_id=0,
        text=build_voice_prompt(question),
        nonce=bridge_nonce(),
        received_at=now,
        source="voice",
        voice_reply_path=str(reply_path),
    )
    queue = DurableQueue(config.queue_path, config.queue_compact_max_events)
    existing = queue.status(queue_id)
    if existing:
        return {
            "schema": "claude-telegram-bridge-voice-enqueue/v1",
            "status": "duplicate",
            "queue_id": queue_id,
            "existing_status": existing,
            "queue_path": str(config.queue_path),
            "voice_reply_path": str(reply_path),
        }
    queue.append_status(item, "received")
    queue.append_status(item, "enqueued")
    return {
        "schema": "claude-telegram-bridge-voice-enqueue/v1",
        "status": "enqueued",
        "queue_id": queue_id,
        "queue_path": str(config.queue_path),
        "voice_reply_path": str(reply_path),
    }


def write_voice_answer(active: ActiveTurn, *, assistant_uuid: str, answer: str, status: str = "answered") -> None:
    if active.source != "voice" or not active.voice_reply_path:
        return
    payload = {
        "schema": "claude-telegram-bridge-voice-answer/v1",
        "status": status,
        "queue_id": active.queue_id,
        "assistant_uuid": assistant_uuid,
        "answer": answer,
        "answered_at": time.time(),
    }
    write_json_atomic(Path(active.voice_reply_path).expanduser(), payload)


class Outbox:
    def __init__(self, path: Path, max_entries: int = 2000) -> None:
        self.path = path
        self.max_entries = max_entries
        self.lock = threading.Lock()
        payload = read_json(path) or {}
        sent = payload.get("sent")
        self.sent: dict[str, Any] = sent if isinstance(sent, dict) else {}

    def contains(self, key: str) -> bool:
        with self.lock:
            return key in self.sent

    def trim_locked(self) -> None:
        if self.max_entries > 0 and len(self.sent) > self.max_entries:
            ordered = sorted(
                self.sent.items(),
                key=lambda pair: float(pair[1].get("ts") or 0) if isinstance(pair[1], dict) else 0,
            )
            self.sent = dict(ordered[-self.max_entries :])

    def mark_sending(self, key: str, attempts: int = 0) -> None:
        with self.lock:
            self.sent[key] = {"ts": time.time(), "status": "sending", "attempts": attempts}
            self.trim_locked()
            write_json_atomic(self.path, {"sent": self.sent})

    def mark_sent(self, key: str, sent_message_ids: list[int]) -> None:
        with self.lock:
            self.sent[key] = {"ts": time.time(), "status": "sent", "sent_message_ids": sent_message_ids}
            self.trim_locked()
            write_json_atomic(self.path, {"sent": self.sent})

    def forget(self, key: str) -> None:
        with self.lock:
            if key not in self.sent:
                return
            self.sent.pop(key, None)
            write_json_atomic(self.path, {"sent": self.sent})


class Bridge:
    def __init__(self, config: Config, telegram: TelegramClient, repl: ClaudeRepl, token: str) -> None:
        self.config = config
        self.telegram = telegram
        self.repl = repl
        self.binder = SessionBinder(config, repl)
        self.token = token
        self.token_hash = token_fingerprint(token)
        self.queue = DurableQueue(config.queue_path, config.queue_compact_max_events)
        self.outbox = Outbox(config.outbox_path, config.outbox_max_entries)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.typing_lock = threading.Lock()
        self.typing_stop: threading.Event | None = None
        self.session_binding: ClaudeSessionBinding | None = None
        self.session_identity: SessionIdentity | None = None
        self.session_pos = 0
        self.parent_map: dict[str, str | None] = {}
        self.native_queue_nonce_by_timestamp: dict[str, str] = {}
        self.pending: list[QueueItem] = []
        self.active_turn: ActiveTurn | None = None
        # ⚙️ ambient flow mirror (v0.1.5) — in-memory card for node-originated work
        # that has no active telegram turn (autonomous worker / cron / node-to-node).
        # Ephemeral (not persisted): on restart ambient starts fresh. Flag-gated OFF.
        self.ambient_flow_body: str = ""
        self.ambient_flow_message_id: int = 0
        # ⚙️ ambient flow mirror — 노드발 작업 최종답변 미러 dedup(같은 결론 재미러 방지).
        self.ambient_final_last_key: str = ""
        # ⚙️ ambient flow mirror — 받은지시 카드를 결과 카드의 앵커로 재사용 (T-260630-48):
        # 결과 도착 시 새 ✅ 카드를 또 보내지 않고 받은지시 카드를 in-place edit 해 노드 챗에
        # 받은지시→결과를 1장으로 통합한다(받은지시/노드결과 2장 중복 제거). 0 = 열린 앵커 없음.
        self.ambient_directive_message_id: int = 0
        self.ambient_directive_body: str = ""
        self.last_transcript_mtime = 0.0
        self.last_jsonl_read_at = 0.0
        self.last_jsonl_watch_error = ""
        self.last_jsonl_watch_error_log_at = 0.0
        # T-260705-56 (3): 미디어 다운로드 실패 auto-requeue 대기열 — queue_key → (update, 시도수, 재시도시각).
        # T-260709-50 M1: Telegram offset 은 enqueue_update 복귀 직후 전진하므로 메모리만
        # 쓰면 그 사이 재기동 시 update 가 영구 유실된다. durable queue 의 별도
        # media-retry 레코드에서 복원한다(정상 주입 queue_id 와 namespace 분리).
        self.media_retry: dict[str, tuple[dict[str, Any], int, float]] = {}
        self.load_media_retries()
        # T-260705-67 ③-b: pending 정체 1회성 알림 발송분 (queue_id). ephemeral —
        # 재시작 시 초기화돼 여전히 정체면 1회 재알림되는 쪽이 안전.
        self.stuck_alert_sent: set[str] = set()
        # T-260705-05: '기록파일 신선 + 화면 idle + pending 대기' 모순 시작 시각.
        self.busy_stuck_since = 0.0

    def acquire_lock(self) -> None:
        self.config.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_handle = self.config.pid_file.open("a+")
        try:
            fcntl.flock(self.pid_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"bridge already running for pid file {self.config.pid_file}") from exc
        self.pid_handle.seek(0)
        self.pid_handle.truncate()
        json.dump(daemon_identity(self.config.pid_file), self.pid_handle, ensure_ascii=False, sort_keys=True)
        self.pid_handle.write("\n")
        self.pid_handle.flush()
        os.fsync(self.pid_handle.fileno())

    def release_lock(self) -> None:
        handle = getattr(self, "pid_handle", None)
        if handle is not None:
            try:
                if not handle.closed:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            try:
                if not handle.closed:
                    handle.close()
            except OSError:
                pass
            self.pid_handle = None
        self.clear_egress_sidecar()
        pid_payload = read_json(self.config.pid_file)
        try:
            owns_pid_file = bool(pid_payload and int(pid_payload.get("pid") or 0) == os.getpid())
        except (TypeError, ValueError):
            owns_pid_file = False
        if owns_pid_file or read_text(self.config.pid_file) == str(os.getpid()):
            try:
                self.config.pid_file.unlink()
            except OSError:
                pass

    def persist_state(self) -> None:
        identity = self.session_identity
        payload = {
            "updated_at": time.time(),
            "binding": self.binding_payload(),
            "offset": self.session_pos,
            "active_turn": self.active_turn.to_json() if self.active_turn else None,
            "parent_map": list(self.parent_map.items())[-500:],
        }
        if identity:
            payload.update({"dev": identity.dev, "ino": identity.ino, "session_path": str(identity.path)})
        write_json_atomic(self.config.state_path, payload)

    def load_state_for_identity(self, identity: SessionIdentity) -> None:
        state = read_json(self.config.state_path)
        # T-260709-50 M9: offset 기본값을 고르기 전에 active_turn 을 먼저 복원해야 한다.
        # 옛 순서는 self.active_turn=None 을 보고 start_at_end 로 건너뛴 뒤 active 를
        # 복원해, 재기동 직전 주입된 턴의 user/assistant 레코드를 영구히 놓쳤다.
        # 바인딩 교체 중 이미 메모리에 잡힌 active turn 은 state 에 별도 active payload 가
        # 없더라도 유지한다(기존 new-binding 계약). persisted payload 가 있을 때만 대체한다.
        loaded_active: ActiveTurn | None = self.active_turn
        active = (state or {}).get("active_turn")
        if isinstance(active, dict):
            loaded_active = None
            try:
                candidate = ActiveTurn.from_json(active)
            except (KeyError, TypeError, ValueError):
                pass
            else:
                if self.active_turn_is_stale_unanswered(candidate):
                    item = self.queue_item_for_active(candidate)
                    self.queue.append_status(
                        item,
                        "stale_released",
                        release_reason="state_load_stale_unseen",
                        age_seconds=int(max(self.active_turn_age_seconds(candidate), 0)),
                    )
                    log(
                        "STALE",
                        f"dropped stale active_turn on state load queue={candidate.queue_id[:10]}",
                    )
                else:
                    loaded_active = candidate
        self.active_turn = loaded_active
        cursor = cursor_offset_for_state(state, identity)
        if cursor is None:
            self.session_pos = 0 if self.active_turn else (identity.size if self.config.start_at_end else 0)
        else:
            self.session_pos = cursor
        parent_items = (state or {}).get("parent_map")
        if isinstance(parent_items, list):
            self.parent_map = {
                str(k): (str(v) if v is not None else None)
                for k, v in parent_items
                if isinstance(k, str)
            }

    def binding_payload(self) -> dict[str, Any]:
        binding = self.session_binding
        if not binding:
            return {}
        return {
            "transcript_path": str(binding.transcript_path),
            "sessionId": binding.session_id,
            "pane_pid": binding.pane_pid,
        }

    def has_fresh_pending_sidecar_binding(self, binding: ClaudeSessionBinding) -> bool:
        if binding.transcript_path.exists():
            return False
        try:
            return any(item == binding for item in self.binder._resolve_fresh_sidecar_metadata(binding.pane_pid))
        except Exception:
            return False

    def ensure_session_binding(self) -> ClaudeSessionBinding:
        try:
            binding = self.binder.resolve()
        except RuntimeError as exc:
            if "no SessionStart sidecar entry" not in str(exc):
                raise
            pending = self.binder._resolve_fresh_sidecar_metadata(self.repl.pane_pid())
            if len(pending) != 1:
                raise
            binding = pending[0]

        identity = session_identity(binding.transcript_path) if binding.transcript_path.exists() else None
        if self.session_binding != binding or (identity is not None and self.transcript_identity_changed(identity)):
            self.session_binding = binding
            if identity is None:
                self.session_identity = None
                self.session_pos = 0
                log("SESSION", f"waiting for transcript {binding.transcript_path}")
            else:
                self.session_identity = identity
                self.load_state_for_identity(identity)
                log("SESSION", f"watching {binding.transcript_path} offset={self.session_pos}")
            self.persist_state()
            self.write_egress_sidecar()
        elif identity is None:
            self.write_egress_sidecar()
        else:
            self.session_identity = identity  # 동일 파일 성장 — size 캐시만 갱신, 재로드 없음
        return binding

    def transcript_identity_changed(self, identity: SessionIdentity) -> bool:
        # T-260704-25 F2: size 는 transcript 가 자랄 때마다 변한다 — size 성장만으로
        # "세션 변경" 판정하면 poll 마다 load_state_for_identity 재로드가 폭주(실측
        # 134회/2h40m)해 뒤처진 디스크 state 가 인메모리 active_turn/parent_map/
        # session_pos 를 덮어쓴다 (🧠 reasoning 미러 유실·상태훼손, T-260701-74 연계).
        # 변경으로 보는 것: 파일 교체(dev/ino/path) 또는 축소(truncation 이상신호)뿐.
        prev = self.session_identity
        if prev is None:
            return True
        if (prev.dev, prev.ino, prev.path) != (identity.dev, identity.ino, identity.path):
            return True
        return identity.size < prev.size

    def write_egress_sidecar(self) -> None:
        binding = self.session_binding
        if not binding:
            return
        identity = daemon_identity(self.config.pid_file)
        payload = read_json(self.config.egress_sidecar_path) or {}
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        key = f"{binding.transcript_path}|{binding.session_id}"
        active = self.active_turn
        sessions[key] = {
            "bridge": BRIDGE_OWNER,
            "node": self.config.node,
            "daemon_pid": os.getpid(),
            "daemon_pid_start_time": identity["pid_start_time"],
            "daemon_cmdline_sha256": identity["cmdline_sha256"],
            "daemon_pid_file": identity["pid_file"],
            "updated_at": time.time(),
            "ttl_seconds": self.config.egress_ttl_seconds,
            "transcript_path": str(binding.transcript_path),
            "sessionId": binding.session_id,
            "pane_pid": binding.pane_pid,
            "claimed_turn_nonce": active.nonce if active else "",
            "active_queue_id": active.queue_id if active else "",
        }
        write_json_atomic(
            self.config.egress_sidecar_path,
            {"updated_at": time.time(), "sessions": sessions},
        )

    def clear_egress_sidecar(self) -> None:
        binding = self.session_binding
        payload = read_json(self.config.egress_sidecar_path)
        if not binding or not payload:
            return
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return
        key = f"{binding.transcript_path}|{binding.session_id}"
        item = sessions.get(key)
        try:
            owns_sidecar = isinstance(item, dict) and int(item.get("daemon_pid") or 0) == os.getpid()
        except (TypeError, ValueError):
            owns_sidecar = False
        if owns_sidecar:
            sessions.pop(key, None)
            write_json_atomic(
                self.config.egress_sidecar_path,
                {"updated_at": time.time(), "sessions": sessions},
            )

    def heartbeat_loop(self) -> None:
        while not self.stop_event.wait(5.0):
            try:
                self.write_egress_sidecar()
            except Exception as exc:  # noqa: BLE001
                log("SIDE", f"egress heartbeat failed: {exc}")

    def start_typing_loop(self, max_seconds: int | None = None) -> threading.Event:
        stop_event = threading.Event()

        def loop() -> None:
            deadline = time.monotonic() + max_seconds if max_seconds else None
            pulse_count = 0
            try:
                while not stop_event.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    pulse_count += 1
                    # F9 (T-260705-04) self-liveness: N pulse 마다 세션이 실제로 일하는지
                    # 확인하고 유휴면 자가소등 — stop_typing 호출을 놓친 어떤 경로가 남아도
                    # 유령이 TYPING_MAX(기본 2h)까지 살지 못한다. active_turn 무락 읽기는
                    # 휴리스틱(최악 1 pulse 지연). probe 예외 = 소등하지 않음(fail-open).
                    if (
                        pulse_count >= TYPING_LIVENESS_GRACE_PULSES
                        and pulse_count % TYPING_LIVENESS_CHECK_EVERY == 0
                    ):
                        try:
                            if not self.has_typing_tracked_work() and not self.session_occupied_excluding_active(
                                missing_transcript_busy=False
                            ):
                                log("TYPE", f"self-exit: session idle at pulse={pulse_count}")
                                break
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        self.telegram.send_typing()
                    except Exception as exc:  # noqa: BLE001
                        log("TYPE", f"sendChatAction failed: {exc}")
                    wait_seconds = TYPING_PULSE_FIRST_WAIT if pulse_count == 1 else TYPING_PULSE_WAIT
                    if deadline is not None:
                        wait_seconds = min(wait_seconds, max(0.0, deadline - time.monotonic()))
                    if wait_seconds <= 0:
                        break
                    stop_event.wait(wait_seconds)
            finally:
                with self.typing_lock:
                    if self.typing_stop is stop_event:
                        self.typing_stop = None

        threading.Thread(target=loop, daemon=True, name="clb-typing").start()
        return stop_event

    def has_typing_tracked_work(self) -> bool:
        with self.lock:
            return self.active_turn is not None or bool(self.pending)

    def begin_typing(self) -> None:
        with self.typing_lock:
            if self.typing_stop:
                self.typing_stop.set()
            self.typing_stop = self.start_typing_loop(self.config.typing_max_seconds)

    def ensure_typing(self) -> None:
        with self.typing_lock:
            if self.typing_stop:
                return
            self.typing_stop = self.start_typing_loop(self.config.typing_max_seconds)

    def stop_typing(self) -> None:
        with self.typing_lock:
            if self.typing_stop:
                self.typing_stop.set()
                self.typing_stop = None

    def busy_state(self) -> str:
        self.release_completed_active_turn_if_recorded()
        self.release_stale_active_turn_if_idle()
        with self.lock:
            if self.active_turn:
                return "generating"
        binding = self.session_binding
        if binding:
            try:
                if time.time() - binding.transcript_path.stat().st_mtime < self.config.transcript_stable_seconds:
                    return "generating"
            except OSError:
                with self.lock:
                    if self.session_binding == binding and (
                        self.session_identity is not None or not self.has_fresh_pending_sidecar_binding(binding)
                    ):
                        self.session_binding = None
                        self.session_identity = None
                        self.session_pos = 0
        try:
            screen = self.repl.capture_pane(80)
        except Exception:
            return "hook_blocked"
        if screen_has_approval_wait(screen):
            return "approval_wait"
        if screen_has_hook_block(screen):
            return "hook_blocked"
        if screen_has_active_work(screen):
            return "generating"
        return "idle"

    def session_clear_pending(self) -> bool:
        clearing = self.config.state_dir / "clearing"
        if not clearing.exists():
            return False
        try:
            ttl = float(os.environ.get("CLB_CLEARING_HOLD_TTL_SEC", "300"))
        except ValueError:
            ttl = 300.0
        if ttl > 0:
            try:
                if time.time() - clearing.stat().st_mtime >= ttl:
                    log("BUSY", "stale clearing flag ignored")
                    return False
            except OSError:
                return False
        return True

    def session_occupied_excluding_active(self, *, missing_transcript_busy: bool = True) -> bool:
        """Busy signal that ignores our own active_turn.

        busy_state() short-circuits to "generating" whenever active_turn is set,
        which it always is during the injection-verify window — so it can't tell
        a stuck inject from a session that is simply still finishing a prior turn.
        This mirrors busy_state()'s transcript-mtime + pane heuristics MINUS the
        active_turn check, so check_injection_timeout can wait instead of falsely
        failing while the session is genuinely busy.
        (2026-06-23 노트북, 아니키 ack: busy-aware delivery, 가역 패치.)
        """
        binding = self.session_binding
        if binding:
            try:
                if time.time() - binding.transcript_path.stat().st_mtime < self.config.transcript_stable_seconds:
                    return True
            except OSError:
                if missing_transcript_busy:
                    return True
        try:
            screen = self.repl.capture_pane(80)
        except Exception as exc:  # noqa: BLE001
            if is_tmux_session_lost_error(exc):
                self.release_active_turn_due_to_tmux_session_lost(str(exc))
                return False
            return True
        if screen_has_approval_wait(screen):
            return True
        if screen_has_hook_block(screen):
            return True
        if screen_has_active_work(screen):
            return True
        return False

    def flush_reasoning_mirror(self, active: ActiveTurn) -> None:
        # ⚠️ 제거 금지 (DO NOT REMOVE) — 🧠 reasoning 미러 유실 방지 (T-260701-63)
        # release_completed_active_turn_if_recorded 의 조기 active_turn=None 과 send 경로가
        # reasoning emit 을 두고 race → busy_state 폴링이 emit 전에 active 를 날려 🧠 사고
        # 미러가 통째로 유실되던 회귀(PR#267 / 5916501 부작용) 차단. send 경로·release 경로
        # 양쪽에서 호출, pending_reasoning None 마킹으로 idempotent(어느 경로든 정확히 1회).
        reasoning = active.pending_reasoning
        active.pending_reasoning = None
        if not reasoning:
            return
        mirror = format_reasoning_mirror(reasoning)
        if not mirror:
            return
        try:
            self.telegram.send(mirror)
            log("SEND", f"sent reasoning mirror nonce={active.nonce} len={len(mirror)}")
        except Exception as exc:  # noqa: BLE001
            log("SEND", f"reasoning mirror send failed (non-fatal): {exc}")

    def release_completed_active_turn_if_recorded(self) -> bool:
        with self.lock:
            active = self.active_turn
        if not active:
            return False

        queue_status = self.queue.status(active.queue_id)
        completed_by_queue = queue_status in DurableQueue.terminal
        completed_by_outbox = bool(active.pending_outbox_key and self.outbox.contains(active.pending_outbox_key))
        if not completed_by_queue and not completed_by_outbox:
            return False

        item = self.queue_item_for_active(active)
        with self.lock:
            if self.active_turn is not active:
                return False
            self.active_turn = None
        self.flush_reasoning_mirror(active)  # ⚠️ 제거 금지 (DO NOT REMOVE) — release 전 🧠 reasoning 보장 (T-260701-63)
        self.stop_typing()
        if completed_by_outbox and not completed_by_queue:
            self.queue.append_status(
                item,
                "sent",
                assistant_uuid=active.pending_assistant_uuid or active.assistant_uuid or "",
                recovered_from="outbox_sent",
            )
        self.persist_state()
        self.write_egress_sidecar()
        log(
            "TURN",
            f"released completed active_turn queue={active.queue_id[:10]} status={queue_status or '-'} "
            f"outbox_sent={int(completed_by_outbox)}",
        )
        return True

    def release_active_turn_due_to_tmux_session_lost(self, detail: str) -> bool:
        with self.lock:
            active = self.active_turn
        if not active:
            return False

        item = self.queue_item_for_active(active)
        with self.lock:
            if self.active_turn is not active:
                return False
            self.active_turn = None
            self.session_binding = None
            self.session_identity = None
            self.session_pos = 0
        self.flush_reasoning_mirror(active)  # ⚠️ 제거 금지 (DO NOT REMOVE) — release 전 🧠 reasoning 보장 (T-260701-63)
        self.stop_typing()
        self.queue.append_status(item, "stale_released", release_reason="tmux_session_lost")
        self.mark_directive_terminal(item, "failed", error="tmux_session_lost")
        self.persist_state()
        self.write_egress_sidecar()
        if hasattr(self.repl, "invalidate_target_cache"):
            self.repl.invalidate_target_cache()
        log("TURN", f"released active_turn queue={active.queue_id[:10]}: tmux session lost ({detail})")
        return True

    def active_turn_age_seconds(self, active: ActiveTurn) -> float:
        reference_at = max(active.user_seen_at or 0.0, active.sidecar_consumed_at or 0.0, active.injected_at or 0.0)
        return time.time() - reference_at

    def active_turn_can_stale_release(self, active: ActiveTurn) -> bool:
        return not (
            active.assistant_uuid
            or active.pending_answer
            or active.pending_assistant_uuid
            or active.pending_outbox_key
            or active.send_in_progress
        )

    def active_turn_is_stale_unanswered(self, active: ActiveTurn) -> bool:
        ttl = self.config.active_turn_stale_seconds
        if ttl <= 0 or not self.active_turn_can_stale_release(active):
            return False
        return self.active_turn_age_seconds(active) >= ttl

    def active_turn_unconfirmed_submission(self, active: ActiveTurn) -> bool:
        return not (
            active.user_uuid
            or active.user_seen_at > 0
            or active.sidecar_consumed_at > 0
            or active.native_queue_seen_at > 0
        )

    def active_turn_session_transcript_lost(self) -> bool:
        # T-260709-72: 확정 배달 stale release 의 재큐 여부 판별 — 바인딩된 세션
        # transcript 파일이 실제로 사라졌을 때만 "세션 증발" 로 보고 재큐를 허용한다.
        # 바인딩 없음/판별 불가는 이중실행 쪽이 더 위험하므로 소실로 치지 않는다.
        binding = self.session_binding
        if not binding:
            return False
        try:
            binding.transcript_path.stat()
        except OSError:
            return True
        return False

    def release_stale_active_turn_if_idle(self) -> bool:
        ttl = self.config.active_turn_stale_seconds
        if ttl <= 0:
            return False
        with self.lock:
            active = self.active_turn
        if not active or not self.active_turn_can_stale_release(active):
            return False
        age = self.active_turn_age_seconds(active)
        unconfirmed_submission = self.active_turn_unconfirmed_submission(active)
        release_reason = "active_turn_submit_unconfirmed_timeout" if unconfirmed_submission else "active_turn_idle_timeout"
        if age < ttl:
            # T-260710-27: confirmed busy-inject promotions can fail to start while
            # the pane is already idle; release that slot without waiting 900s.
            promoted_idle_ttl = busy_inject_promote_idle_stale_seconds()
            if (
                not active.busy_injected
                or unconfirmed_submission
                or promoted_idle_ttl <= 0
                or age < promoted_idle_ttl
            ):
                return False
            if self.session_occupied_excluding_active(missing_transcript_busy=False):
                return False
            if self.active_turn_session_transcript_lost():
                return False
            release_reason = "busy_inject_promote_idle_timeout"
        elif not unconfirmed_submission and self.session_occupied_excluding_active(missing_transcript_busy=False):
            return False

        item = self.queue_item_for_active(active)
        with self.lock:
            if self.active_turn is not active:
                return False
            self.active_turn = None
        self.stop_typing()
        self.queue.append_status(
            item,
            "stale_released",
            age_seconds=int(max(age, 0)),
            release_reason=release_reason,
        )
        if unconfirmed_submission or self.active_turn_session_transcript_lost():
            # 미확인 배달, 또는 확정 배달이라도 바인딩된 세션 transcript 가 소실된 경우
            # (세션 증발 = 답이 영영 안 옴)는 기존대로 failed 재큐 (T-260707-16 유실 방지).
            self.mark_directive_terminal(item, "failed", error=release_reason)
        else:
            # T-260709-72: 배달 확인(user record/sidecar/native queue 관측)까지 끝났고
            # 세션 transcript 도 살아 있으면, 롱턴(900s+)이어도 '전달 실패'가 아니다 —
            # failed 재큐가 같은 지시를 다시 주입해 이중실행 위험(실사고 2026-07-09
            # update=986031644: 21:55 배달확인 → 22:10 idle_timeout 오탐 재큐 →
            # 재주입 2회, 원 턴은 22:12 정상 완료·outbox 발송). 슬롯만 해제(신규 지시
            # 배달 가능)하고 재큐·전달실패 경보 없이 종착 — stale_released 는
            # injectable({received,enqueued}) 밖이라 재시작 replay 에도 안 실린다
            # (stuck_busy_idle / state_load_stale_unseen 해제 경로와 동일 패턴).
            log(
                "STALE",
                f"idle release without requeue queue={active.queue_id[:10]}: delivery confirmed, transcript alive",
            )
        self.persist_state()
        self.write_egress_sidecar()
        log(
            "STALE",
            f"released active_turn queue={active.queue_id[:10]} age={int(max(age, 0))}s reason={release_reason}",
        )
        return True

    def format_metadata(self, metadata: dict[str, Any]) -> str:
        parts: list[str] = []
        for key, value in metadata.items():
            if value in (None, "", [], {}):
                continue
            parts.append(f"{key}={value}")
        return "; ".join(parts)

    def image_prompt_text(self, caption_text: str, image_path: Path, metadata: dict[str, Any]) -> str:
        lines = [
            "[Telegram image received]",
            f"local_path: {image_path}",
        ]
        if caption_text:
            lines.append(f"caption: {caption_text}")
        metadata_line = self.format_metadata(metadata)
        if metadata_line:
            lines.append(f"metadata: {metadata_line}")
        lines.extend(
            [
                "",
                "Open the local image path, inspect it, and answer the Telegram user in Korean. "
                "Keep the answer concise and useful.",
            ]
        )
        return "\n".join(lines)

    def transcribe_audio(self, media_path: Path) -> tuple[str, str]:
        template = self.config.audio_transcribe_cmd
        if not template:
            return "", "not_available: set CLB_AUDIO_TRANSCRIBE_CMD to enable audio transcription"

        quoted_path = shlex.quote(str(media_path))
        cmd = template.replace("{path}", quoted_path) if "{path}" in template else f"{template} {quoted_path}"
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.config.audio_transcribe_timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "", f"failed: transcription timed out after {self.config.audio_transcribe_timeout}s"
        except OSError as exc:
            return "", f"failed: {exc}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            suffix = f": {detail[-1][:200]}" if detail else ""
            return "", f"failed: transcription command rc={proc.returncode}{suffix}"
        transcript = sanitize_text(proc.stdout or "", limit=12000)
        if not transcript:
            return "", "failed: transcription command returned empty stdout"
        return transcript, "ok"

    def audio_prompt_text(
        self,
        media_kind: str,
        caption_text: str,
        media_path: Path,
        metadata: dict[str, Any],
        transcript: str,
        transcript_status: str,
    ) -> str:
        lines = [
            "[Telegram audio received]",
            f"local_path: {media_path}",
            f"media_kind: {media_kind}",
        ]
        if caption_text:
            lines.append(f"caption: {caption_text}")
        metadata_line = self.format_metadata(metadata)
        if metadata_line:
            lines.append(f"metadata: {metadata_line}")
        lines.append("")
        if transcript:
            lines.extend(["transcript:", transcript])
        else:
            lines.append(f"transcript_status: {transcript_status}")
        lines.extend(
            [
                "",
                "Answer the Telegram user in Korean. If transcript is unavailable, say the audio file "
                "was received and ask for text or CLB_AUDIO_TRANSCRIBE_CMD setup when needed.",
            ]
        )
        return "\n".join(lines)

    def download_thumbnail(self, media: dict[str, Any], media_dir: Path, update_id: int) -> Path | None:
        thumbnail = media.get("thumbnail") or media.get("thumb")
        if not isinstance(thumbnail, dict) or not thumbnail.get("file_id"):
            return None
        name_hint = f"telegram-video-thumb-{update_id}-{thumbnail.get('file_unique_id') or thumbnail.get('file_id')}"
        try:
            return self.telegram.download_file(
                str(thumbnail["file_id"]),
                media_dir,
                name_hint,
                default_suffix=".jpg",
                allowed_extensions=IMAGE_EXTENSIONS,
            )
        except Exception as exc:  # noqa: BLE001
            log("TG", f"thumbnail download failed: {exc}")
            return None

    def video_prompt_text(
        self,
        media_kind: str,
        caption_text: str,
        media_path: Path,
        metadata: dict[str, Any],
        thumbnail_path: Path | None,
        transcript: str,
        transcript_status: str,
    ) -> str:
        lines = [
            "[Telegram video received]",
            f"local_path: {media_path}",
            f"media_kind: {media_kind}",
        ]
        if thumbnail_path:
            lines.append(f"thumbnail_path: {thumbnail_path}")
        if caption_text:
            lines.append(f"caption: {caption_text}")
        metadata_line = self.format_metadata(metadata)
        if metadata_line:
            lines.append(f"metadata: {metadata_line}")
        lines.append("")
        if transcript:
            lines.extend(["audio_transcript:", transcript])
        else:
            lines.append(f"audio_transcript_status: {transcript_status}")
        lines.extend(
            [
                "",
                "Open thumbnail_path with the local image tool if present. Answer the Telegram user "
                "in Korean based on the local video path, thumbnail, caption, metadata, and transcript. "
                "If the video cannot be inspected directly, state that limitation briefly.",
            ]
        )
        return "\n".join(lines)

    def document_prompt_text(self, caption_text: str, media_path: Path, metadata: dict[str, Any]) -> str:
        lines = [
            "[Telegram file received]",
            f"local_path: {media_path}",
            "media_kind: document",
        ]
        if caption_text:
            lines.append(f"caption: {caption_text}")
        metadata_line = self.format_metadata(metadata)
        if metadata_line:
            lines.append(f"metadata: {metadata_line}")
        lines.extend(
            [
                "",
                "Answer the Telegram user in Korean. Use the local_path and metadata above. "
                "If the file cannot be inspected directly, say that the file was received and "
                "ask for the specific action needed.",
            ]
        )
        return "\n".join(lines)

    def prompt_from_telegram_message(self, message: dict[str, Any], update_id: int) -> str:
        raw_text = message.get("text")
        if isinstance(raw_text, str) and raw_text.strip():
            return raw_text

        caption = message.get("caption")
        caption_text = caption.strip() if isinstance(caption, str) else ""
        media_dir = self.config.state_dir / "claude-telegram-bridge-media" / self.config.node

        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            candidates = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
            if candidates:
                photo = max(
                    candidates,
                    key=lambda item: (
                        int(item.get("file_size") or 0),
                        int(item.get("width") or 0) * int(item.get("height") or 0),
                    ),
                )
                name_hint = f"telegram-{update_id}-{photo.get('file_unique_id') or photo.get('file_id')}"
                image_path = self.telegram.download_file(
                    str(photo["file_id"]),
                    media_dir,
                    name_hint,
                    default_suffix=".jpg",
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return self.image_prompt_text(
                    caption_text,
                    image_path,
                    {
                        "width": photo.get("width"),
                        "height": photo.get("height"),
                        "file_size": photo.get("file_size"),
                    },
                )

        document = message.get("document")
        if isinstance(document, dict) and str(document.get("mime_type") or "").startswith("image/"):
            file_id = str(document.get("file_id") or "")
            if file_id:
                name_hint = f"telegram-{update_id}-{document.get('file_unique_id') or file_id}"
                default_suffix = suffix_from_metadata(
                    str(document.get("file_name") or ""),
                    str(document.get("mime_type") or ""),
                    ".jpg",
                )
                image_path = self.telegram.download_file(
                    file_id,
                    media_dir,
                    name_hint,
                    default_suffix=default_suffix,
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return self.image_prompt_text(
                    caption_text,
                    image_path,
                    {
                        "mime_type": document.get("mime_type"),
                        "file_name": document.get("file_name"),
                        "file_size": document.get("file_size"),
                    },
                )

        audio: dict[str, Any] | None = None
        audio_kind = ""
        for key, kind in (("voice", "voice"), ("audio", "audio")):
            candidate = message.get(key)
            if isinstance(candidate, dict) and candidate.get("file_id"):
                audio = candidate
                audio_kind = kind
                break
        if audio is None and isinstance(document, dict) and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "")
            file_name = str(document.get("file_name") or "")
            if mime_type.startswith("audio/") or Path(file_name).suffix.lower() in AUDIO_EXTENSIONS:
                audio = document
                audio_kind = "audio_document"
        if audio is not None:
            file_id = str(audio.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{audio.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(audio.get("file_name") or ""),
                str(audio.get("mime_type") or ""),
                ".ogg" if audio_kind == "voice" else ".mp3",
            )
            media_path = self.telegram.download_file(
                file_id,
                media_dir,
                name_hint,
                default_suffix=default_suffix,
                allowed_extensions=AUDIO_EXTENSIONS,
            )
            transcript, transcript_status = self.transcribe_audio(media_path)
            return self.audio_prompt_text(
                audio_kind,
                caption_text,
                media_path,
                {
                    "duration": audio.get("duration"),
                    "mime_type": audio.get("mime_type"),
                    "file_name": audio.get("file_name"),
                    "title": audio.get("title"),
                    "performer": audio.get("performer"),
                    "file_size": audio.get("file_size"),
                },
                transcript,
                transcript_status,
            )

        video: dict[str, Any] | None = None
        video_kind = ""
        for key, kind in (("video", "video"), ("video_note", "video_note"), ("animation", "animation")):
            candidate = message.get(key)
            if isinstance(candidate, dict) and candidate.get("file_id"):
                video = candidate
                video_kind = kind
                break
        if video is None and isinstance(document, dict) and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "")
            file_name = str(document.get("file_name") or "")
            if mime_type.startswith("video/") or Path(file_name).suffix.lower() in VIDEO_EXTENSIONS:
                video = document
                video_kind = "video_document"
        if video is not None:
            file_id = str(video.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{video.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(video.get("file_name") or ""),
                str(video.get("mime_type") or ""),
                ".mp4",
            )
            media_path = self.telegram.download_file(
                file_id,
                media_dir,
                name_hint,
                default_suffix=default_suffix,
                allowed_extensions=VIDEO_EXTENSIONS,
            )
            thumbnail_path = self.download_thumbnail(video, media_dir, update_id)
            transcript, transcript_status = self.transcribe_audio(media_path)
            return self.video_prompt_text(
                video_kind,
                caption_text,
                media_path,
                {
                    "duration": video.get("duration"),
                    "mime_type": video.get("mime_type"),
                    "file_name": video.get("file_name"),
                    "width": video.get("width") or video.get("length"),
                    "height": video.get("height") or video.get("length"),
                    "file_size": video.get("file_size"),
                },
                thumbnail_path,
                transcript,
                transcript_status,
            )

        if isinstance(document, dict) and document.get("file_id"):
            file_id = str(document.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{document.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(document.get("file_name") or ""),
                str(document.get("mime_type") or ""),
                ".bin",
            )
            media_path = self.telegram.download_file(
                file_id,
                media_dir,
                name_hint,
                default_suffix=default_suffix,
            )
            return self.document_prompt_text(
                caption_text,
                media_path,
                {
                    "mime_type": document.get("mime_type"),
                    "file_name": document.get("file_name"),
                    "file_size": document.get("file_size"),
                },
            )

        return caption_text

    def envelope_prompt(self, item: QueueItem) -> str:
        safe_text = escape_unsafe_slash(sanitize_text(item.text))
        marker = f"<{item.nonce}/>"
        notice = self.stale_prompt_notice(item)
        if notice:
            return f"{marker}\n{notice}\n{safe_text}"
        return f"{marker}\n\n{safe_text}"

    def envelope_sidecar_enabled(self) -> bool:
        return not self.config.envelope_sidecar_off_flag_path.exists()

    def sidecar_visible_prompt(self, item: QueueItem) -> str:
        return escape_unsafe_slash(sanitize_text(item.text))

    def envelope_sidecar_context(self, item: QueueItem, *, visible_prompt: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        stale_notice = self.stale_prompt_notice(item, now=now).strip()
        lines = [
            "[claude-telegram-bridge sidecar]",
            "Telegram-origin prompt. The visible prompt was pasted without a bridge envelope.",
            f"nonce: {item.nonce}",
            f"queue_id: {item.queue_id}",
            f"update_id: {item.update_id}",
            f"message_id: {item.message_id}",
            f"prompt_sha256: {prompt_sha256(visible_prompt)}",
            f"<{item.nonce}/>",
            "Do not mention this bridge sidecar, envelope, or nonce in the answer.",
        ]
        if stale_notice:
            lines.append(stale_notice)
        return "\n".join(lines)

    def write_envelope_sidecar(self, item: QueueItem, *, visible_prompt: str) -> None:
        now = time.time()
        record = {
            "schema": ENVELOPE_SIDECAR_SCHEMA,
            "status": "pending",
            "created_at": now,
            "expires_at": now + max(1.0, float(self.config.envelope_sidecar_ttl_seconds)),
            "nonce": item.nonce,
            "queue_id": item.queue_id,
            "update_id": item.update_id,
            "message_id": item.message_id,
            "prompt_sha256": prompt_sha256(visible_prompt),
            "additional_context": self.envelope_sidecar_context(item, visible_prompt=visible_prompt, now=now),
        }
        append_jsonl_locked(self.config.envelope_sidecar_path, record)

    def prompt_for_item(self, item: QueueItem) -> str:
        if not self.envelope_sidecar_enabled():
            return self.envelope_prompt(item)
        visible_prompt = self.sidecar_visible_prompt(item)
        try:
            self.write_envelope_sidecar(item, visible_prompt=visible_prompt)
            return visible_prompt
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"sidecar write failed; falling back to visible envelope: {exc}")
            return self.envelope_prompt(item)

    def active_envelope_sidecar_consumed_record(self, active: ActiveTurn) -> dict[str, Any] | None:
        expected_hash = prompt_sha256(self.sidecar_visible_prompt(self.queue_item_for_active(active)))
        latest: dict[str, Any] | None = None
        for record in read_envelope_sidecar_records(self.config.envelope_sidecar_path):
            if str(record.get("queue_id") or "") != active.queue_id:
                continue
            if str(record.get("nonce") or "") != active.nonce:
                continue
            latest = record
        if not latest or latest.get("status") != "consumed":
            return None
        seen_hash = str(latest.get("prompt_sha256_seen") or "")
        if seen_hash and seen_hash != expected_hash:
            return None
        return latest

    @staticmethod
    def sidecar_consumed_at(record: dict[str, Any]) -> float:
        try:
            consumed_at = float(record.get("consumed_at") or 0.0)
        except (TypeError, ValueError):
            consumed_at = 0.0
        return consumed_at if consumed_at > 0 else time.time()

    def mark_active_sidecar_consumed_seen(self, active: ActiveTurn) -> bool:
        if active.sidecar_consumed_at > 0:
            return True
        record = self.active_envelope_sidecar_consumed_record(active)
        if not record:
            return False
        consumed_at = self.sidecar_consumed_at(record)
        item: QueueItem | None = None
        with self.lock:
            if self.active_turn is not active:
                return False
            if self.active_turn.sidecar_consumed_at <= 0:
                self.active_turn.sidecar_consumed_at = consumed_at
                item = self.queue_item_for_active(self.active_turn)
        if item:
            self.queue.append_status(item, "sidecar_consumed_seen", consumed_at=consumed_at)
            self.persist_state()
            self.write_egress_sidecar()
            log("JSONL", f"sidecar consumed seen {active.nonce}")
        return True

    def mark_active_sidecar_body_user_seen(self, record: dict[str, Any]) -> bool:
        user_uuid = str(record.get("uuid") or "")
        if not user_uuid:
            return False
        with self.lock:
            active = self.active_turn
        if not active:
            return False
        body = content_text((record.get("message") or {}).get("content"))
        if not body:
            return False
        expected_hash = prompt_sha256(self.sidecar_visible_prompt(self.queue_item_for_active(active)))
        if prompt_sha256(body) != expected_hash:
            return False
        user_seen_at = record_timestamp_seconds(record) or time.time()
        sidecar_consumed = self.mark_active_sidecar_consumed_seen(active)
        if not sidecar_consumed:
            # T-260710-15: busy 큐잉으로 진행 턴에 병합된 프롬프트는 UserPromptSubmit
            # 사이드카가 늦거나 생략돼 consumed 기록이 없을 수 있다. 주입 이후 생성된
            # fresh user record 의 본문 해시 일치는 배달 실증으로 인정 — 가짜
            # "nonce user JSONL not observed" 실패 → 재주입 중복 사고 차단
            # (실사고 2026-07-10 update=568752431: 배달됐는데 failed → 배너 재주입 2회).
            # 주입 이전 timestamp 의 동일 본문(과거 중복 발화) 오매칭은 시각 가드로 배제.
            if user_seen_at + 2.0 < active.injected_at:
                return False
        item: QueueItem | None = None
        with self.lock:
            if self.active_turn is not active:
                return False
            self.active_turn.user_uuid = user_uuid
            self.active_turn.user_seen_at = user_seen_at
            item = self.queue_item_for_active(self.active_turn)
        self.queue.append_status(
            item,
            "user_jsonl_seen",
            user_uuid=user_uuid,
            user_seen_at=user_seen_at,
            sidecar_consumed=sidecar_consumed,
        )
        self.persist_state()
        self.write_egress_sidecar()
        if sidecar_consumed:
            log("JSONL", f"body-only sidecar user seen {active.nonce}")
        else:
            log("JSONL", f"body-only user seen without sidecar {active.nonce}")
        return True

    def stale_prompt_notice(self, item: QueueItem, now: float | None = None) -> str:
        # T-260705-67 ②: 주입 시점에 늙은 메시지(배달 지연/큐 대기)면 봉투 안에 경고를 박아
        # 지연수신 stale 메시지가 진행중 R3 작업의 중지/반전 지시로 오해석되는 사고 클래스 차단.
        # 차단이 아니라 advisory — fresh 재확인 지시문만 얹는다. 0 이하로 끄기 가능.
        try:
            threshold = float(os.environ.get("CLB_STALE_PROMPT_SEC", "300"))
        except ValueError:
            threshold = 300.0
        if threshold <= 0:
            return ""
        now = time.time() if now is None else now
        origin = item.sent_at if item.sent_at > 0 else item.received_at
        age = now - origin
        if age < threshold:
            return ""
        basis = "발신된" if item.sent_at > 0 else "수신된"
        clock = time.strftime("%H:%M", time.localtime(origin))
        minutes = max(1, int(age // 60))
        return (
            f"⚠️ 지연 배달 경고: 이 메시지는 약 {minutes}분 전({clock} KST) {basis} 것이 지금에야 주입됐다. "
            "그 사이 상황이 바뀌었을 수 있다. 이 메시지를 진행중인 R3 작업(공개 발행·대외 발신·스토어 제출 등)의 "
            "중지/반전/승인 지시로 해석해야 한다면, 실행 전 반드시 아니키에게 fresh 재확인을 받은 뒤에만 움직여라. "
            "일반 작업 지시라도 최신 상태(tasks.md·직전 대화)를 먼저 점검하고 착수하라.\n"
        )

    def maybe_alert_late_delivery(self, item: QueueItem) -> None:
        # T-260705-67 ③-a: 발신→수신 갭(브릿지 다운/폴링 정체 구간)이 큰 메시지는 enqueue 즉시
        # 아니키 폰에 표면화. enqueue_update 가 queue_id 로 dedup 하므로 메시지당 최대 1회.
        try:
            threshold = float(os.environ.get("CLB_STALE_PROMPT_SEC", "300"))
        except ValueError:
            threshold = 300.0
        if threshold <= 0 or item.sent_at <= 0:
            return
        gap = item.received_at - item.sent_at
        if gap < threshold:
            return
        minutes = max(1, int(gap // 60))
        clock = time.strftime("%H:%M", time.localtime(item.sent_at))
        log("QUEUE", f"late delivery gap={int(gap)}s update={item.update_id}")
        try:
            self.telegram.send(
                f"⚠️ 지연 수신: 약 {minutes}분 전({clock} KST)에 보내신 메시지를 지금 받았어요. "
                "그 사이 브릿지가 밀려 있었을 수 있어요. 오래된 지시라면 최신 상황 기준으로 다시 한 번 보내주세요."
            )
        except Exception as exc:  # noqa: BLE001
            log("QUEUE", f"late delivery alert failed: {exc}")

    def check_busy_stuck_rebind(self) -> None:
        # T-260705-05: 이중(유령) 세션 busy 고착 self-heal. 유령 세션이 bound
        # transcript 를 계속 갱신하면 busy_state 가 영구 "generating" 이라 인바운드
        # 주입이 죽는다 (2026-07-05 01:0x 본진 실사고 — 아니키 수동 재기동으로 복구).
        # '기록파일 신선 + 화면 idle + pending 대기 + active_turn 없음' 모순이
        # threshold(기본 300s) 연속 지속되면 해당 transcript 를 격리하고 바인딩을
        # 리셋한다 — 다음 ensure_session_binding 이 화면 세션으로 재바인딩.
        try:
            threshold = float(os.environ.get("CLB_BUSY_STUCK_REBIND_SEC", "300"))
        except ValueError:
            threshold = 300.0
        if threshold <= 0:
            return
        with self.lock:
            has_pending = bool(self.pending)
            active = self.active_turn
        binding = self.session_binding
        if not has_pending or binding is None:
            self.busy_stuck_since = 0.0
            return
        if active is not None:
            if not self.active_turn_is_stale_unanswered(active):
                self.busy_stuck_since = 0.0
                return
            try:
                transcript_age = time.time() - binding.transcript_path.stat().st_mtime
            except OSError:
                transcript_age = 0.0
            if transcript_age < threshold:
                self.busy_stuck_since = 0.0
                return
            try:
                screen = self.repl.capture_pane(80)
            except Exception:  # noqa: BLE001
                self.busy_stuck_since = 0.0
                return
            if (
                screen_has_approval_wait(screen)
                or screen_has_hook_block(screen)
                or screen_has_active_work(screen)
            ):
                self.busy_stuck_since = 0.0
                return
            now = time.time()
            if not self.busy_stuck_since:
                self.busy_stuck_since = now
                return
            if now - self.busy_stuck_since < threshold:
                return
            item = self.queue_item_for_active(active)
            with self.lock:
                if self.active_turn is not active:
                    return
                self.active_turn = None
            self.busy_stuck_since = now
            self.stop_typing()
            self.queue.append_status(
                item,
                "stale_released",
                release_reason="stuck_busy_idle",
                age_seconds=int(max(self.active_turn_age_seconds(active), 0)),
            )
            self.persist_state()
            self.write_egress_sidecar()
            log(
                "BUSY",
                f"stuck self-heal: released active_turn {active.queue_id[:10]} "
                f"(idle screen + stale transcript for {int(transcript_age)}s)",
            )
            return
        try:
            transcript_fresh = (
                time.time() - binding.transcript_path.stat().st_mtime
                < self.config.transcript_stable_seconds
            )
        except OSError:
            transcript_fresh = False
        if not transcript_fresh:
            self.busy_stuck_since = 0.0
            return
        try:
            screen = self.repl.capture_pane(80)
        except Exception:  # noqa: BLE001
            self.busy_stuck_since = 0.0
            return
        if (
            screen_has_approval_wait(screen)
            or screen_has_hook_block(screen)
            or screen_has_active_work(screen)
        ):
            # 진짜 작업 중 — 정상 큐 대기
            self.busy_stuck_since = 0.0
            return
        now = time.time()
        if not self.busy_stuck_since:
            self.busy_stuck_since = now
            return
        if now - self.busy_stuck_since < threshold:
            return
        self.busy_stuck_since = now  # 재발동도 threshold 간격 — 스팸 방지
        self.binder.quarantine_transcript(binding.transcript_path)
        with self.lock:
            self.session_binding = None
            self.session_identity = None
            self.session_pos = 0
        self.persist_state()
        log(
            "BUSY",
            f"stuck self-heal: quarantined {binding.transcript_path} "
            f"(fresh transcript + idle screen for {int(now - (self.busy_stuck_since - threshold))}s)",
        )
        try:
            self.telegram.send(
                "⚠️ 유령 세션 감지: 화면은 쉬는데 뒤에서 다른 세션이 기록을 계속 써서 "
                "메시지 주입이 막혀 있었어요. 화면 세션으로 다시 연결합니다 — "
                "밀린 메시지는 곧 전달됩니다."
            )
        except Exception as exc:  # noqa: BLE001
            log("BUSY", f"stuck self-heal notice failed: {exc}")

    def check_queue_stuck_alert(self) -> None:
        # T-260705-67 ③-b: 수신→주입 정체(세션 busy/브릿지 내부 문제로 pending 이 안 빠지는 상태)
        # 표면화. telegram_loop 틱(기본 2s)마다 불리므로 queue_id 별 1회성 set 로 스팸 차단.
        try:
            threshold = float(os.environ.get("CLB_QUEUE_STUCK_ALERT_SEC", "180"))
        except ValueError:
            threshold = 180.0
        if threshold <= 0:
            return
        now = time.time()
        with self.lock:
            pending_ids = {item.queue_id for item in self.pending}
            # 큐를 떠난 항목 키는 정리해 set 무한 증가 방지 (재enqueue 는 dedup 이 막는다).
            self.stuck_alert_sent &= pending_ids
            stuck = [
                item
                for item in self.pending
                if now - item.received_at >= threshold and item.queue_id not in self.stuck_alert_sent
            ]
            for item in stuck:
                self.stuck_alert_sent.add(item.queue_id)
        for item in stuck:
            age = int(now - item.received_at)
            preview = sanitize_text(item.text)[:40]
            if item.busy_injected:
                log("QUEUE", f"busy-injected pending wait age={age}s update={item.update_id}")
                try:
                    self.telegram.send(
                        f"⏳ 대기 안내: 메시지는 이미 세션 입력큐에 실렸고, "
                        f"진행 중인 턴이 끝나면 처리돼요 ({age}초째, “{preview}…”)."
                    )
                except Exception as exc:  # noqa: BLE001
                    log("QUEUE", f"busy-injected wait notice send failed: {exc}")
                continue
            log("QUEUE", f"stuck pending age={age}s update={item.update_id}")
            try:
                self.telegram.send(
                    f"⚠️ 큐 정체: 받은 메시지가 {age}초째 노드에 주입되지 못하고 대기중이에요 "
                    f"(“{preview}…”). 세션이 풀리면 자동 전달되지만, 급한 지시면 상태를 확인해 주세요."
                )
            except Exception as exc:  # noqa: BLE001
                log("QUEUE", f"stuck alert send failed: {exc}")

    def directive_retry_max(self) -> int:
        try:
            value = int(os.environ.get("CLB_DIRECTIVE_RETRY_MAX", "2"))
        except ValueError:
            value = 2
        return max(0, min(value, 5))

    def terminal_retry_count(self, item: QueueItem) -> int:
        counts: list[int] = []
        for record in self.queue.records_for_queue_id(item.queue_id):
            try:
                counts.append(max(0, int(record.get("terminal_retry_count") or 0)))
            except (TypeError, ValueError):
                continue
        return max(counts) if counts else 0

    def terminal_original_text(self, item: QueueItem) -> str:
        for record in reversed(self.queue.records_for_queue_id(item.queue_id)):
            original = record.get("terminal_original_text")
            if isinstance(original, str) and original.strip():
                return terminal_retry_original_text(original)
        return terminal_retry_original_text(item.text)

    def terminal_retry_status_extra(self, item: QueueItem) -> dict[str, Any]:
        retry_count = self.terminal_retry_count(item)
        if retry_count <= 0:
            return {}
        extra: dict[str, Any] = {"terminal_retry_count": retry_count}
        original_text = self.terminal_original_text(item)
        if original_text:
            extra["terminal_original_text"] = original_text
        return extra

    def terminal_retry_prompt_text(
        self,
        original_text: str,
        *,
        status: str,
        error: str = "",
        retry_count: int,
        retry_max: int,
    ) -> str:
        label = "전달 실패" if status == "failed" else "차단"
        lines = [
            f"⚠️ 브릿지 재주입 재시도 {retry_count}/{retry_max}: 이전 전달이 {label} 상태로 중단됐다.",
            "이 원문은 지연/재시도된 지시다. R3 작업(머지·배포·외부발신 등)은 실행 전 최신 상태와 fresh 확인을 먼저 보라.",
        ]
        detail = sanitize_text(error, limit=500)
        if detail:
            lines.append(f"중단 사유: {detail}")
        lines.extend(["", "원문:", sanitize_text(original_text)])
        return "\n".join(lines)

    def mark_directive_terminal(
        self,
        item: QueueItem,
        status: str,
        *,
        error: str = "",
        slash_command: str = "",
    ) -> QueueItem | None:
        # T-260707-16: failed/blocked directive가 큐에서 무소음 유실되지 않도록 sender 에
        # 즉시 표면화하고, 같은 queue_id 를 유한 횟수만 재-enqueue 한다.
        retry_count = self.terminal_retry_count(item)
        retry_max = self.directive_retry_max()
        if retry_max <= 0:
            terminal_retry_count = 0
        else:
            terminal_retry_count = min(retry_count + 1, retry_max)
        original_text = self.terminal_original_text(item)
        extra: dict[str, Any] = {"terminal_retry_count": terminal_retry_count}
        if original_text:
            extra["terminal_original_text"] = original_text
        if error:
            extra["error"] = error
        if slash_command:
            extra["slash_command"] = slash_command
        self.queue.append_status(item, status, **extra)

        label = "전달 실패" if status == "failed" else "차단"
        if retry_count >= retry_max:
            try:
                self.telegram.send(
                    f"⚠️ 브릿지 {label}: 지시를 노드에 전달하지 못했고 자동 재시도 소진 "
                    f"({retry_count}/{retry_max}) 상태예요. 최신 상황 기준으로 다시 보내주세요."
                )
            except Exception as exc:  # noqa: BLE001
                log("QUEUE", f"terminal exhaustion notice failed: {exc}")
            log("QUEUE", f"directive {status} exhausted queue={item.queue_id} retry={retry_count}/{retry_max}")
            return None

        next_retry = retry_count + 1
        retry_item = QueueItem(
            queue_id=item.queue_id,
            update_id=item.update_id,
            message_id=item.message_id,
            text=self.terminal_retry_prompt_text(
                original_text,
                status=status,
                error=error,
                retry_count=next_retry,
                retry_max=retry_max,
            ),
            nonce=bridge_nonce(),
            received_at=item.received_at,
            sent_at=item.sent_at,
            source=item.source,
            voice_reply_path=item.voice_reply_path,
            busy_injected=False,
        )
        self.queue.append_status(
            retry_item,
            "enqueued",
            terminal_retry_count=next_retry,
            terminal_original_text=original_text,
            retry_from_status=status,
            retry_reason=sanitize_text(error, limit=500),
            retry_slash_command=slash_command,
        )
        with self.lock:
            if not (self.active_turn and self.active_turn.queue_id == retry_item.queue_id):
                self.pending = [pending for pending in self.pending if pending.queue_id != retry_item.queue_id]
                self.pending.append(retry_item)
        try:
            self.telegram.send(
                f"⚠️ 브릿지 {label}: 지시 전달이 중단돼 유실 방지로 자동 재시도 {next_retry}/{retry_max} 회차를 큐에 넣었어요."
            )
        except Exception as exc:  # noqa: BLE001
            log("QUEUE", f"terminal retry notice failed: {exc}")
        log("QUEUE", f"directive {status} requeued queue={item.queue_id} retry={next_retry}/{retry_max}")
        return retry_item

    def enqueue_update(self, update: dict[str, Any]) -> None:
        if "edited_message" in update:
            log("QUEUE", f"ignore edited_message update={update.get('update_id')}")
            return
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if str(chat.get("id")) != self.config.chat_id:
            return
        update_id = int(update["update_id"])
        retry_key = message_update_key(update, self.token_hash)
        media_retry_completion = ""
        try:
            text = self.prompt_from_telegram_message(message, update_id)
            self.media_retry.pop(retry_key, None)
            media_retry_completion = "media_retry_resolved"
        except Exception as exc:  # noqa: BLE001
            caption = message.get("caption")
            caption_text = caption.strip() if isinstance(caption, str) else ""
            detail = str(exc).replace(self.token, "<redacted-token>")
            if caption_text:
                self.media_retry.pop(retry_key, None)
                media_retry_completion = "media_retry_caption_fallback"
                self.telegram.send(f"media 처리 실패: {detail}. caption만 전달합니다.")
                text = caption_text
            else:
                # T-260705-56 (3): 재전송 요구 전에 1회 자동 재시도 — transient 다운로드
                # 실패(타임아웃/중간끊김)에서 아니키 손(재전송) 빌리는 UX 제거 (원칙 1 손0).
                attempts = self.media_retry.get(retry_key, (None, 0, 0.0))[1]
                if attempts < 1:
                    try:
                        delay = float(os.environ.get("CLB_MEDIA_REQUEUE_DELAY_SEC", "30"))
                    except ValueError:
                        delay = 30.0
                    retry_at = time.time() + delay
                    self.media_retry[retry_key] = (update, attempts + 1, retry_at)
                    # offset 전진보다 먼저 fsync 되는 durable queue 에 원 update 를 기록한다.
                    self.persist_media_retry(update, retry_key, attempts + 1, retry_at)
                    log("QUEUE", f"media download failed; auto-requeue in {int(delay)}s update={update_id}")
                    self.telegram.send(f"media 내려받기 실패: {detail}. {int(delay)}초 뒤 자동 재시도할게요.")
                    return
                self.media_retry.pop(retry_key, None)
                self.finish_media_retry(update, retry_key, "media_retry_failed")
                self.telegram.send(f"media 처리 실패: {detail}. 다시 보내주시면 재시도합니다.")
                return
        text = sanitize_text(text)
        if not text:
            self.telegram.send("처리할 텍스트나 media caption이 없습니다.")
            return
        hold_response = release_hold_response(text)
        if hold_response:
            self.telegram.send(hold_response)
            return
        command = slash_token(text)
        if command in BRIDGE_HEALTH_SLASH_COMMANDS:
            self.telegram.send("claude-telegram-bridge running")
            return
        if command == BRIDGE_STATUS_SLASH_COMMAND:
            self.handle_status_command()
            return

        message_id = int(message.get("message_id") or 0)
        queue_id = message_update_key(update, self.token_hash)
        nonce = bridge_nonce()
        # T-260705-67: Telegram message.date = 아니키 발신 시각(epoch sec). 사고(1895s 발신→수신 갭)의
        # 원인 이분(발신→수신 vs 수신→주입)을 durable queue 에 남기는 계측 기반.
        try:
            sent_at = float(message.get("date") or 0.0)
        except (TypeError, ValueError):
            sent_at = 0.0
        item = QueueItem(
            queue_id=queue_id, update_id=update_id, message_id=message_id, text=text, nonce=nonce, sent_at=sent_at
        )
        with self.lock:
            active_queue_id = self.active_turn.queue_id if self.active_turn else ""
            pending_queue_ids = {existing.queue_id for existing in self.pending}
        existing_status = self.queue.status(queue_id)
        if queue_id == active_queue_id or queue_id in pending_queue_ids or existing_status:
            if media_retry_completion:
                self.finish_media_retry(update, retry_key, media_retry_completion)
            log("QUEUE", f"skip duplicate update={update_id} queue={queue_id[:10]} status={existing_status or 'live'}")
            return
        self.queue.append_status(item, "received")
        # 정상 durable queue 가 fsync 된 뒤에만 media-retry 장부를 닫는다. 이 순서가
        # 다운로드 성공 직후 재기동 창에서도 update 를 최소 한 장부에 남긴다.
        if media_retry_completion:
            self.finish_media_retry(update, retry_key, media_retry_completion)
        with self.lock:
            active_queue_id = self.active_turn.queue_id if self.active_turn else ""
            if item.queue_id == active_queue_id or any(existing.queue_id == item.queue_id for existing in self.pending):
                return
            self.pending.append(item)
            self.queue.append_status(item, "enqueued")
        log("QUEUE", f"enqueued update={update_id} queue={queue_id[:10]}")
        self.ensure_typing()
        self.maybe_alert_late_delivery(item)

    def handle_status_command(self) -> None:
        # codex /status 패리티 (T-260703-01 확장): bridge 상태 한 줄 → 상태 + 컨텍스트 요약 바.
        # busy(generating) 중엔 composer 오염 방지를 위해 캡처 생략 — 기존 한 줄 응답 유지.
        state = self.busy_state()
        if state != "idle":
            self.telegram.send(f"claude bridge status: {state} (턴 진행 중 — 컨텍스트 캡처 생략)")
            return
        try:
            self.telegram.send_typing()
        except Exception:  # noqa: BLE001
            pass
        settle = float(os.environ.get("CLB_CONTEXT_SETTLE_SEC", "1.2"))
        try:
            with self.repl.temporary_window_width(STATUS_WIDE_CAPTURE_COLUMNS):
                for _ in range(self.config.composer_clear_retries):
                    self.repl.clear_composer()
                self.repl.paste_prompt(CONTEXT_SLASH_COMMAND)
                time.sleep(settle)
                screen = self.repl.capture_pane(context_capture_lines())
            source = extract_slash_command_block(screen, CONTEXT_SLASH_COMMAND) or screen
            body = extract_context_screen(source)
            if body.startswith("Claude context"):
                body = body[len("Claude context"):].lstrip("\n")
            header = "Claude status\nBridge: idle"
            self.telegram.send(f"{header}\n{body}" if body else header)
            log("INJECT", "/status rich mirrored")
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"/status capture failed: {exc}")
            self.telegram.send(f"claude bridge status: idle (컨텍스트 캡처 실패: {exc})")

    def handle_capture_command(self, item: "QueueItem", command_token: str) -> None:
        # /context /usage /cost — read-only 정보 명령. 시각화가 넓은 폭을 요구해 좁은
        # tmux 창에선 capture 가 잘리므로, 캡처 동안만 창을 넓혀 실행·캡처하고
        # 터미널 화면 그대로 폰에 미러한다 (codex /status 동형, T-260702-14/T-260703-01).
        try:
            self.telegram.send_typing()
        except Exception:  # noqa: BLE001
            pass
        prompt = escape_unsafe_slash(sanitize_text(item.text))
        settle = float(os.environ.get("CLB_CONTEXT_SETTLE_SEC", "1.2"))
        # T-260703-36: 고정 settle 후 1회 캡처는 렌더가 settle 안에 안 끝나면(예: /context
        #   "✦ Forging..." 지연) 스피너 프레임을 찍어 빈 화면만 미러했다. settle 을 바닥값으로
        #   두되, 활성 작업 스피너(screen_has_active_work — esc-to-interrupt/spinner)가 사라질
        #   때까지 재캡처 폴링해 렌더 완료 프레임을 잡는다. /usage·/cost 도 동일 경로라 함께 수혜.
        capture_timeout = float(os.environ.get("CLB_CONTEXT_CAPTURE_TIMEOUT_SEC", "8.0"))
        poll = float(os.environ.get("CLB_CONTEXT_POLL_SEC", "0.4"))
        capture_ansi = command_token == CONTEXT_SLASH_COMMAND
        try:
            with self.repl.temporary_window_width(STATUS_WIDE_CAPTURE_COLUMNS):
                for _ in range(self.config.composer_clear_retries):
                    self.repl.clear_composer()
                self.repl.paste_prompt(prompt)
                time.sleep(settle)
                screen = self.repl.capture_pane(context_capture_lines(), ansi=capture_ansi)
                deadline = time.monotonic() + max(capture_timeout, 0.0)
                while screen_has_active_work(screen) and time.monotonic() < deadline:
                    try:
                        self.telegram.send_typing()
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(poll)
                    screen = self.repl.capture_pane(context_capture_lines(), ansi=capture_ansi)
            if screen_has_active_work(screen):
                # T-260704-38 F6-c: 세션이 긴 턴 작업 중이라 타임아웃 — 작업중 프레임
                # 덤프(라이덴 'Frolicking...' 케이스) 대신 1줄 안내만 보낸다.
                self.telegram.send(
                    f"⏳ claude 세션이 다른 작업을 실행 중이라 {command_token} 화면을 못 잡았어요 — 유휴 때 다시 시도해주세요."
                )
                self.queue.append_status(
                    item,
                    "injected",
                    slash_command=command_token,
                    **self.terminal_retry_status_extra(item),
                )
                self.queue.append_status(item, "sent", slash_command=command_token)
                log("INJECT", f"{command_token} busy-timeout notice update={item.update_id}")
                return
            # codex /status 톤: 도형 차트 그리드를 걷어낸 깔끔 텍스트 우선, 정리 결과가 비면
            # 원본 캡처(글리프 포함)로 폴백 (T-260703-36).
            # F6-a: '❯ <명령>' 에코~다음 프롬프트 마커 블록만 추출 (없으면 전체 화면 폴백).
            source = extract_slash_command_block(screen, command_token) or screen
            if command_token == CONTEXT_SLASH_COMMAND:
                # T-260709-80: 이미지(render_ansi_png+sendPhoto) → 동전 매트릭스+% 텍스트(mono).
                # 아니키 2026-07-09 23:30 "이미지말고 텍스트로" + "이 네모부분이 %와 함께".
                reply_to = item.message_id if item.message_id > 0 else None
                answer = context_grid_text(source or screen)
                if answer:
                    answer = append_context_rate_limit_footer(answer, source or screen)
                    self.telegram.send(answer, reply_to_message_id=reply_to, mono=True)
                    log("INJECT", f"{command_token} grid text mirrored update={item.update_id}")
                else:
                    answer = clean_context_screen(source) or extract_context_screen(source)
                    self.telegram.send(answer or f"claude bridge {command_token}: 매트릭스 파싱 실패, 캡처된 화면도 비어 있습니다.")
            else:
                answer = clean_context_screen(source) or extract_context_screen(source)
                self.telegram.send(answer or f"claude bridge {command_token}: 캡처된 화면이 비어 있습니다.")
            self.queue.append_status(
                item,
                "injected",
                slash_command=command_token,
                **self.terminal_retry_status_extra(item),
            )
            self.queue.append_status(item, "sent", slash_command=command_token)
            log("INJECT", f"{command_token} wide-capture mirrored update={item.update_id}")
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"{command_token} capture failed: {exc}")
            self.queue.append_status(item, "failed", error=str(exc))
            self.telegram.send(f"claude bridge {command_token} failed: {exc}")

    def handle_model_command(self, item: "QueueItem") -> None:
        # /model 인터셉트 (T-260703-17 프리즈 실사고 재발방지): bare 는 선택지 키보드,
        # 인자형은 TUI 를 열지 않으므로 그대로 주입(비대화형 적용) + 적용 확인 회신.
        text = sanitize_text(item.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            alias = parts[1].strip()
            # ⚠️ 하드닝 (T-260703-23): 인자형도 allowlist 밖 alias 는 주입 거부 — 콜백 경로와 동형.
            if not model_alias_allowed(alias):
                self.telegram.send(model_alias_rejection_text(alias))
                self.queue.append_status(item, "blocked", slash_command=MODEL_SLASH_COMMAND)
                log("INJECT", f"/model arg rejected (not allowlisted): {alias!r} update={item.update_id}")
                return
            try:
                for _ in range(self.config.composer_clear_retries):
                    self.repl.clear_composer()
                self.repl.paste_prompt(f"/model {alias}")
            except Exception as exc:  # noqa: BLE001
                log("INJECT", f"/model arg apply failed: {exc}")
                self.queue.append_status(item, "failed", error=str(exc))
                self.telegram.send(f"claude bridge /model 적용 실패: {exc}")
                return
            time.sleep(float(os.environ.get("CLB_MODEL_SETTLE_SEC", "1.0")))
            applied = claude_settings_model()
            self.telegram.send(f"모델 적용을 보냈어요: {alias}\n현재 설정: {applied}")
            self.queue.append_status(
                item,
                "injected",
                slash_command=MODEL_SLASH_COMMAND,
                **self.terminal_retry_status_extra(item),
            )
            self.queue.append_status(item, "sent", slash_command=MODEL_SLASH_COMMAND)
            log("INJECT", f"/model arg={alias} applied update={item.update_id}")
            return
        current = claude_settings_model()
        buttons = [
            [
                {
                    "text": ("✅ " if alias and alias in current else "") + alias,
                    "callback_data": f"{MODEL_CALLBACK}::{alias}",
                }
            ]
            for alias in model_menu_aliases()
        ]
        prefix = getattr(self.telegram, "with_emoji_prefix", lambda value: value)
        self.telegram.call(
            "sendMessage",
            chat_id=self.config.chat_id,
            text=prefix(f"현재 모델: {current}\n바꿀 모델을 골라주세요 — 선택 즉시 적용돼요 (선택창 없이):"),
            reply_markup=json.dumps({"inline_keyboard": buttons}, ensure_ascii=False),
        )
        self.queue.append_status(item, "sent", slash_command=MODEL_SLASH_COMMAND)
        log("INJECT", f"/model menu sent update={item.update_id}")

    def handle_blocked_slash(self, item: "QueueItem", command_token: str) -> None:
        # 프리즈 가드 fail-safe (T-260702-14): 지원 목록 밖 슬래시는 인터랙티브
        # 대화상자로 세션이 얼 수 있어 명령으로는 주입하지 않고, 원문 프롬프트로 재시도한다.
        self.mark_directive_terminal(
            item,
            "blocked",
            slash_command=command_token,
            error=f"unsupported slash command {command_token}",
        )
        log("INJECT", f"slash blocked token={command_token} update={item.update_id}")

    def _emit_model_notice(self, text: str, menu_message_id: int | None) -> None:
        # 메뉴 메시지가 있으면 그 자리를 edit(적용/거부/대기 확인), 없으면 새 메시지로 회신.
        if menu_message_id is not None:
            self.telegram.call(
                "editMessageText",
                chat_id=self.config.chat_id,
                message_id=menu_message_id,
                text=text,
            )
        else:
            self.telegram.send(text)

    def apply_model_choice(self, alias: str, menu_message_id: int | None = None) -> None:
        # inline keyboard 선택 콜백 → 비대화형 인자형 주입 + 메뉴 메시지를 적용 확인으로 edit.
        # ⚠️ 하드닝 (T-260703-23, PR#362 리뷰 YELLOW):
        #   ① allowlist — model_menu_aliases() 밖 alias(위조 callback_data 포함)는 주입 거부.
        #   ② idle 게이트 — 진행 중 턴이 있으면 주입하지 않는다. clear_composer()의 첫 키가
        #      Escape 라, busy 일 때 주입하면 진행 중 턴을 끊어버린다. 대기 안내만 하고 반환.
        #   두 실패 모두 composer 를 건드리지 않는다(턴 무영향).
        prefix = getattr(self.telegram, "with_emoji_prefix", lambda value: value)
        if not model_alias_allowed(alias):
            self._emit_model_notice(prefix(model_alias_rejection_text(alias)), menu_message_id)
            log("INJECT", f"/model choice rejected (not allowlisted): {alias!r}")
            return
        state = self.busy_state()
        if state != "idle":
            self._emit_model_notice(prefix(MODEL_BUSY_DEFER_TEXT), menu_message_id)
            log("INJECT", f"/model choice deferred (busy state={state}): {alias}")
            return
        try:
            for _ in range(self.config.composer_clear_retries):
                self.repl.clear_composer()
            self.repl.paste_prompt(f"/model {alias}")
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"/model choice apply failed: {exc}")
            self.telegram.send(f"claude bridge /model 적용 실패: {exc}")
            return
        time.sleep(float(os.environ.get("CLB_MODEL_SETTLE_SEC", "1.0")))
        applied = claude_settings_model()
        text = prefix(f"✅ 모델 선택 적용: {alias}\n현재 설정: {applied}")
        self._emit_model_notice(text, menu_message_id)
        log("INJECT", f"/model choice={alias} applied")

    def trigger_lifecycle_recovery(self, command_token: str) -> None:
        # /exit·/quit 통과 후 Claude Code 세션이 종료되면 브릿지는 inject 대상을 잃는다.
        # watchdog 정주기 tick 을 기다리지 않고 정착 지연 후 자가복구를 앞당겨 트리거한다
        # (claude-bridge-watchdog 이 죽은 세션을 재생성 → graceful 재기동).
        watchdog = Path(os.environ.get("CLB_WATCHDOG_SCRIPT", "~/.config/claude-telegram-bridge/watchdog.sh")).expanduser()
        if not watchdog.exists():
            log("LIFECYCLE", f"watchdog missing for {command_token}: {watchdog}")
            return
        delay = float(os.environ.get("CLB_LIFECYCLE_RECOVERY_DELAY_SEC", "3"))

        def _heal() -> None:
            time.sleep(delay)
            try:
                subprocess.run(
                    ["bash", str(watchdog), self.config.node],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=90, check=False,
                )
                log("LIFECYCLE", f"watchdog self-heal spawned after {command_token}")
            except Exception as exc:  # noqa: BLE001
                log("LIFECYCLE", f"watchdog spawn failed after {command_token}: {exc}")

        threading.Thread(target=_heal, daemon=True).start()

    def busy_inject_guarded_clear(self) -> None:
        # T-260707-36 안전 가드: 주입 전 capture-pane 으로 composer 잔여 텍스트 확인 →
        # Escape 없는 clear 키로 정리해, 사용자가 이미 타이핑해 둔 입력에 우리 prompt 가
        # 이어붙어 오염되는 걸 막는다. 잔여가 있으면 방어적으로 한 번 더 비운다.
        # 호출부(try_busy_inject)가 composer_lock 을 잡고 있으므로 unlocked primitive 사용.
        try:
            screen = self.repl.capture_pane(80)
        except Exception:  # noqa: BLE001
            screen = ""
        residual = composer_residual_text(screen)
        passes = self.config.composer_clear_retries
        if residual:
            log("INJECT", f"busy-inject composer residual detected (len={len(residual)}) — Escape-less clear")
            passes += 1
        for _ in range(passes):
            self.repl._clear_composer_unlocked(interrupt=False)

    def try_busy_inject(self) -> bool:
        # T-260707-36: generating 중 Escape 없는 native 큐잉 주입. 주입하면 True,
        # 대기로 넘기면 False(호출부가 기존 대기 경로로). 슬래시 명령은 프리즈 가드(idle
        # 게이트)를 지켜 busy 중 주입하지 않는다.
        #
        # 라이브 버그 픽스: busy-inject 가 필요한 순간이 바로 "진행 중 텔레그램 턴 A 위에
        # 새 메시지 B 가 온" 그 순간이므로, 옛 `if self.active_turn` 가드는 주 시나리오를
        # 스스로 막았다. 이제 active_turn(A) 이 set 이어도 주입한다. 단 A 의 추적을
        # clobber 하지 않도록, A 가 진행 중일 땐 active_turn 을 B 로 덮지 않고 B 를 pending
        # 에 남겨 busy_injected 로 표시한다(A 완료 후 idle drain 이 재-paste 없이 승계).
        # 재진입 중복주입/enqueued 고착(2026-06-28 회귀)은 이 per-item busy_injected 표시로
        # 막는다 — active_turn 존재 여부 가드를 대체.
        #
        # T-260708-46: pending[0] 이 이미 busy_injected 인 상태에서 뒤 텍스트가 쌓이면,
        # 앞 항목은 이미 TUI native 큐에 올라간 것이므로 그 뒤 텍스트도 순서대로 추가
        # 제출한다. 단 head 가 slash/media 처럼 아직 올라가지 않은 보류 항목이면 뒤를
        # 먼저 태우지 않는다(순서 보존).
        with self.lock:
            if not self.pending:
                return False
            selected_index = -1
            for idx, candidate in enumerate(self.pending):
                if candidate.busy_injected:
                    # 이미 native 큐잉됨 — 재-paste 금지(멱등성). 뒤 텍스트는 같은 native
                    # 큐 뒤에 추가 제출할 수 있다.
                    continue
                stripped_text = (candidate.text or "").strip()
                escape_slash = stripped_text.startswith(SLASH_ESCAPE_PREFIX) and bool(
                    slash_token(stripped_text[len(SLASH_ESCAPE_PREFIX):].lstrip())
                )
                if bool(slash_token(candidate.text)) or escape_slash:
                    # 슬래시는 busy 중 주입 금지 — 기존 대기 경로가 idle 까지 보류(프리즈 가드).
                    # head 보류 항목을 건너뛰고 뒤 텍스트를 먼저 태우면 순서가 뒤집힌다.
                    return False
                if is_telegram_media_prompt(candidate.text) and not busy_inject_media_enabled():
                    # T-260708-22: media prompts are multi-line local-path envelopes.
                    # During generation Claude can leave them in the composer while the
                    # bridge marks busy_injected and stops retrying.
                    # T-260710-15: 전면 제외가 긴 턴 중 첨부 3~27분 정체(+순서보존으로 뒤
                    # 텍스트 연쇄 지연)의 근인이라 기본 참여로 전환. composer 잔류는 이후
                    # 도입된 가드(residual retry·부착 관측·promote-idle 해제)가 회수한다.
                    # CLB_BUSY_INJECT_MEDIA=0 이면 옛 idle-only 보류 경로.
                    return False
                selected_index = idx
                break
            if selected_index < 0:
                # pending 전부 이미 native 큐잉됨 — 재-paste 금지. idle drain 이 승계 처리.
                return False
            item = self.pending[selected_index]
            # paste 전에 표시(재진입/롤백 안전). 실패 시 아래에서 False 로 되돌린다.
            item.busy_injected = True
            adopt = self.active_turn is None and selected_index == 0
            if adopt:
                # 진행 중 텔레그램 턴이 없다 — B 를 즉시 active_turn 으로 claim 해
                # nonce/최종답변이 정상 턴 추적으로 미러되게 한다(기존 동작 보존).
                self.pending.pop(selected_index)
                self.active_turn = ActiveTurn(
                    queue_id=item.queue_id,
                    update_id=item.update_id,
                    message_id=item.message_id,
                    nonce=item.nonce,
                    injected_at=time.time(),
                    text=item.text,
                    sent_at=item.sent_at,
                    source=item.source,
                    voice_reply_path=item.voice_reply_path,
                    busy_injected=True,
                    user_uuid=item.user_uuid or None,
                    user_seen_at=item.user_seen_at,
                    native_queue_seen_at=item.native_queue_seen_at,
                )
                self.reset_ambient_flow()
            # else: A 가 진행 중 — active_turn(A) 를 덮지 않는다. B 는 pending 에 남아
            # busy_injected 로 표시된 채 A 완료 후 idle drain 에서 승계된다.
        self.persist_state()
        self.write_egress_sidecar()
        prompt = self.prompt_for_item(item)
        try:
            # codex clear_and_paste_prompt 미러: composer_lock 을 1회 잡고 clear+paste 를
            # 원자적으로(단일쓰기) 수행 — 진행 중 slash 핸들러/idle 주입과의 경합 차단.
            with self.repl.composer_lock():
                self.busy_inject_guarded_clear()
                self.repl._paste_prompt_unlocked(prompt, submit_key=busy_submit_key())
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"busy-inject failed: {exc}")
            with self.lock:
                item.busy_injected = False  # 롤백 — 다음 사이클 재시도 허용
                if adopt and self.active_turn and self.active_turn.queue_id == item.queue_id:
                    self.active_turn = None
            if adopt:
                # adopt 경로는 item 을 pending 에서 pop 했으므로 유실 방지 위해 failed 통지.
                self.mark_directive_terminal(item, "failed", error=str(exc))
            # else: B 는 pending 에 그대로(enqueued) — 조용히 다음 drain 에서 재시도.
            self.persist_state()
            self.write_egress_sidecar()
            return True
        self.maybe_echo_voice_question(item)
        if adopt:
            with self.lock:
                if self.active_turn and self.active_turn.queue_id == item.queue_id:
                    self.active_turn.injected_at = time.time()
            self.queue.append_status(item, "injected", busy_inject=True, **self.terminal_retry_status_extra(item))
        else:
            # B 는 여전히 injectable(enqueued) 로 두되 busy_injected 를 durable 레코드에 실어
            # 브릿지 재기동 후에도 중복 주입되지 않게 한다(item.to_json() 이 플래그 포함).
            self.queue.append_status(item, "enqueued", busy_inject=True, **self.terminal_retry_status_extra(item))
        self.persist_state()
        self.write_egress_sidecar()
        if adopt:
            self.begin_typing()
        else:
            # A 의 typing 루프가 이미 돌고 있으면 유지(끊지 않음), 없으면 시작.
            self.ensure_typing()
        log(
            "INJECT",
            f"busy-inject nonce={item.nonce} update={item.update_id} state=generating "
            f"adopt={int(adopt)}",
        )
        return True

    def drain_queue(self) -> None:
        if self.session_clear_pending():
            log("BUSY", "skip inject state=clearing")
            return
        state = self.busy_state()
        if state != "idle":
            # T-260707-36 busy-inject: generating(진행 중 턴) + CLB_BUSY_INJECT 켜졌을 때만,
            # Escape 없는 clear + paste + Enter 로 주입해 Claude Code TUI native 큐잉에 실어
            # 다음 턴으로 반영시킨다. 진행 중 턴은 절대 끊지 않는다. approval_wait/hook_blocked/
            # clearing 등 다른 non-idle 은 아래 기존 대기 경로 그대로(주입 안 함).
            if state == "generating" and busy_inject_enabled() and self.try_busy_inject():
                return
            # 세션이 busy 라 아직 inject 못 하는 구간에도 입력중 유지. 기존엔 inject 후에야
            # begin_typing 이 떠서, 직전 턴/백그라운드 작업으로 busy 인 동안(보낸 직후·백그라운드
            # 재진입·완료 정착) 사용자 폰엔 무표시였다(2026-06-27 아니키 "백그라운드 작업일 때도
            # 타이핑"). active typing 루프(begin_typing)가 이미 돌면 중복 안 쏨.
            self.ensure_typing()
            log("BUSY", f"skip inject state={state}")
            return
        promoted_busy_injected = False
        busy_inject_demoted = False
        with self.lock:
            if self.active_turn or not self.pending:
                return
            item = self.pending.pop(0)
            stripped_text = (item.text or "").strip()
            escape_slash = stripped_text.startswith(SLASH_ESCAPE_PREFIX) and bool(
                slash_token(stripped_text[len(SLASH_ESCAPE_PREFIX):].lstrip())
            )
            slash_command = bool(slash_token(item.text)) or escape_slash
            if slash_command:
                self.reset_ambient_flow()
            else:
                self.active_turn = ActiveTurn(
                    queue_id=item.queue_id,
                    update_id=item.update_id,
                    message_id=item.message_id,
                    nonce=item.nonce,
                    injected_at=time.time(),
                    text=item.text,
                    sent_at=item.sent_at,
                    source=item.source,
                    voice_reply_path=item.voice_reply_path,
                    busy_injected=item.busy_injected,
                    user_uuid=item.user_uuid or None,
                    user_seen_at=item.user_seen_at,
                    native_queue_seen_at=item.native_queue_seen_at,
                )
                # ⚙️ ambient flow mirror (v0.1.5) — incoming-message boundary reset.
                self.reset_ambient_flow()
                # T-260707-36: busy 창에서 이미 native 큐잉된 항목이면 active_turn 으로
                # 승계만 하고 재-paste 는 건너뛴다(중복 주입 방지). idle 이 됐다는 건 A 가
                # 끝났고 TUI 가 B 를 다음 턴으로 처리한다는 뜻이므로 추적만 재개하면 된다.
                #
                # T-260710-14: 단 busy_injected 는 paste "전에" 낙관 마크되므로 그 자체가
                # 부착 증거가 아니다. native enqueue/attachment/user record 증거가 하나도
                # 없으면 busy paste 가 TUI 에 안 붙은 채 유실된 케이스다(2026-07-10 00:2x
                # update=568752401 실사고 — 무증거 승계가 status=injected 로 전이해 지시
                # 통째 침묵 유실). 이때는 승계 대신 busy_injected 를 내리고 정상 idle
                # 재-paste 경로로 강등한다. busy_injected 를 내려야 T-260707-68 no-retry
                # 계약 대신 일반 verify 사다리(clear/retry)가 적용된다. session_binding
                # 유무는 게이트에 안 쓴다 — busy_state() 가 수시로 binding 을 리셋하고,
                # binding 이 깨진 순간이야말로 증거 관측이 불가능해 유실이 조용해진다.
                # 트레이드오프: 진짜 native 큐잉됐는데 enqueue 레코드 tail 이 늦은 극단
                # race 에선 중복 배달 가능 — 중복(가시적·경미)이 침묵 유실(치명)보다 낫다.
                if item.busy_injected and not (
                    item.native_queue_seen_at > 0
                    or bool(item.user_uuid)
                    or item.user_seen_at > 0
                ):
                    item.busy_injected = False
                    self.active_turn.busy_injected = False
                    busy_inject_demoted = True
                promoted_busy_injected = item.busy_injected
        if busy_inject_demoted:
            log(
                "INJECT",
                f"busy-inject promote without native-queue evidence — demote to re-paste "
                f"nonce={item.nonce} update={item.update_id}",
            )
        if promoted_busy_injected:
            with self.lock:
                if self.active_turn and self.active_turn.queue_id == item.queue_id:
                    self.active_turn.injected_at = time.time()
            self.queue.append_status(item, "injected", busy_inject=True, **self.terminal_retry_status_extra(item))
            self.persist_state()
            self.write_egress_sidecar()
            self.begin_typing()
            log(
                "INJECT",
                f"busy-inject promote (no re-paste) nonce={item.nonce} update={item.update_id}",
            )
            return
        if slash_command:
            # ── T-260702-14 슬래시 인터셉트 레이어 (codex 브릿지 동형) ──
            # escape hatch: '!' prefix 는 프리즈 가드를 명시적으로 우회해 원문 그대로 주입.
            inject_text = item.text
            if escape_slash:
                inject_text = stripped_text[len(SLASH_ESCAPE_PREFIX):].lstrip()
                command_token = slash_token(inject_text)
            else:
                command_token = slash_token(item.text)
                # read-only 정보 명령(/context /usage /cost) = 넓힌 창 캡처 미러.
                if command_token in CAPTURE_MIRROR_SLASH_COMMANDS:
                    self.handle_capture_command(item, command_token)
                    self.persist_state()
                    self.write_egress_sidecar()
                    return
                # /model = 인터셉트 (원문 주입 시 선택창 프리즈 — T-260703-17 실사고).
                if command_token == MODEL_SLASH_COMMAND:
                    self.handle_model_command(item)
                    self.persist_state()
                    self.write_egress_sidecar()
                    return
                # 프리즈 가드 fail-safe: 지원 목록 밖 슬래시 = 주입 차단 + 안내.
                if command_token not in SAFE_PASSTHROUGH_SLASH_COMMANDS:
                    self.handle_blocked_slash(item, command_token)
                    self.persist_state()
                    self.write_egress_sidecar()
                    return
            prompt = escape_unsafe_slash(sanitize_text(inject_text))
            try:
                for _ in range(self.config.composer_clear_retries):
                    self.repl.clear_composer()
                self.repl.paste_prompt(prompt)
            except Exception as exc:  # noqa: BLE001
                log("INJECT", f"slash failed: {exc}")
                self.mark_directive_terminal(item, "failed", error=str(exc), slash_command=command_token)
                self.persist_state()
                self.write_egress_sidecar()
                return
            self.queue.append_status(
                item,
                "injected",
                slash_command=command_token,
                **self.terminal_retry_status_extra(item),
            )
            self.queue.append_status(item, "sent", slash_command=command_token)
            self.persist_state()
            self.write_egress_sidecar()
            # /exit·/quit 는 세션을 종료시키므로 watchdog 자가복구를 앞당겨 트리거 (graceful 재기동).
            if command_token in SESSION_LIFECYCLE_SLASH_COMMANDS:
                self.trigger_lifecycle_recovery(command_token)
            log("INJECT", f"slash={command_token} update={item.update_id}")
            return
        self.persist_state()
        self.write_egress_sidecar()
        prompt = self.prompt_for_item(item)
        try:
            for _ in range(self.config.composer_clear_retries):
                self.repl.clear_composer()
            self.repl.paste_prompt(prompt)
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"failed: {exc}")
            with self.lock:
                if self.active_turn and self.active_turn.queue_id == item.queue_id:
                    self.active_turn = None
            self.mark_directive_terminal(item, "failed", error=str(exc))
            self.persist_state()
            self.write_egress_sidecar()
            return

        self.maybe_echo_voice_question(item)
        with self.lock:
            if self.active_turn and self.active_turn.queue_id == item.queue_id:
                self.active_turn.injected_at = time.time()
        self.queue.append_status(item, "injected", **self.terminal_retry_status_extra(item))
        self.persist_state()
        self.write_egress_sidecar()
        self.begin_typing()
        log("INJECT", f"nonce={item.nonce} update={item.update_id}")

    def maybe_echo_voice_question(self, item: "QueueItem") -> None:
        # T-260709-70: 자비스가 들은 음성 전사를 아니키 채팅방에 미러 — 인식 오류를
        # 아니키가 즉시 볼 수 있게 한다. 에코 실패는 주입을 막지 않는다(best effort).
        if item.source != "voice" or item.voice_echo_sent:
            return
        question = voice_question_from_prompt(item.text)
        if not question:
            return
        item.voice_echo_sent = True
        try:
            self.telegram.send(format_voice_question_echo(question))
        except Exception as exc:  # noqa: BLE001
            log("SEND", f"voice echo failed: {exc}")

    def active_turn_submit_key(self, active: ActiveTurn) -> str:
        return busy_submit_key() if active.busy_injected else "Enter"

    def active_composer_residual_text(self, active: ActiveTurn) -> str:
        try:
            screen = self.repl.capture_pane(80)
        except Exception as exc:  # noqa: BLE001
            if is_tmux_session_lost_error(exc):
                self.release_active_turn_due_to_tmux_session_lost(str(exc))
            else:
                log("INJECT", f"submit verify capture failed queue={active.queue_id[:10]}: {exc}")
            return ""
        return composer_residual_text(screen)

    def fail_active_submit_confirmation(self, active: ActiveTurn, item: QueueItem, error: str) -> None:
        with self.lock:
            if self.active_turn is not active:
                return
            self.active_turn = None
            self.pending = [pending for pending in self.pending if pending.queue_id != item.queue_id]
        self.stop_typing()
        self.queue.append_status(item, "failed", error=error, submit_confirm_failed=True)
        try:
            preview = sanitize_text(item.text, limit=80)
            self.telegram.send(
                "⚠️ 브릿지 제출 실패: 지시를 Claude Code composer에 붙였지만 제출 확인이 안 돼 "
                f"자동 재시도를 중단했어요. 최신 상황 기준으로 다시 보내주세요. (“{preview}…”)"
            )
        except Exception as exc:  # noqa: BLE001
            log("QUEUE", f"submit confirmation failure notice failed: {exc}")
        self.persist_state()
        self.write_egress_sidecar()
        log("QUEUE", f"directive submit confirmation failed queue={item.queue_id} error={error}")

    def retry_active_submit_if_composer_residual(self, active: ActiveTurn) -> bool:
        residual = self.active_composer_residual_text(active)
        if not residual:
            return False
        item = self.queue_item_for_active(active)
        if active.inject_attempts >= 2:
            self.fail_active_submit_confirmation(
                active,
                item,
                "prompt remained in composer after submit retry",
            )
            return True
        submit_key = self.active_turn_submit_key(active)
        try:
            self.repl.submit_prompt(submit_key)
        except Exception as exc:  # noqa: BLE001
            self.fail_active_submit_confirmation(active, item, f"submit retry failed: {exc}")
            return True
        with self.lock:
            if self.active_turn is not active:
                return True
            active.inject_attempts += 1
            active.injected_at = time.time()
            attempt = active.inject_attempts
        self.queue.append_status(
            item,
            "submit_retry",
            attempt=attempt,
            residual_len=len(residual),
            submit_key=submit_key,
        )
        self.persist_state()
        self.write_egress_sidecar()
        log("INJECT", f"composer still held prompt; submit retry queue={item.queue_id[:10]} attempt={attempt}")
        return True

    def media_retry_item(self, update: dict[str, Any], retry_key: str) -> QueueItem:
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        try:
            sent_at = float(message.get("date") or 0.0)
        except (TypeError, ValueError):
            sent_at = 0.0
        return QueueItem(
            queue_id=f"media-retry:{retry_key}",
            update_id=int(update.get("update_id") or 0),
            message_id=int(message.get("message_id") or 0),
            text="",
            nonce="",
            sent_at=sent_at,
        )

    def persist_media_retry(
        self,
        update: dict[str, Any],
        retry_key: str,
        attempts: int,
        retry_at: float,
    ) -> None:
        self.queue.append_status(
            self.media_retry_item(update, retry_key),
            "media_retry_pending",
            telegram_update=update,
            media_retry_attempts=attempts,
            media_retry_at=retry_at,
        )

    def finish_media_retry(self, update: dict[str, Any], retry_key: str, status: str) -> None:
        item = self.media_retry_item(update, retry_key)
        if self.queue.status(item.queue_id) == "media_retry_pending":
            self.queue.append_status(item, status)

    def load_media_retries(self) -> None:
        for record in self.queue.records_by_queue_id().values():
            if record.get("status") != "media_retry_pending":
                continue
            update = record.get("telegram_update")
            if not isinstance(update, dict) or "update_id" not in update:
                continue
            retry_key = message_update_key(update, self.token_hash)
            try:
                attempts = max(1, int(record.get("media_retry_attempts") or 1))
                retry_at = float(record.get("media_retry_at") or 0.0)
            except (TypeError, ValueError):
                continue
            self.media_retry.setdefault(retry_key, (update, attempts, retry_at))

    def retry_media_downloads(self) -> None:
        # T-260705-56 (3): 만기 도래한 미디어 auto-requeue 를 재주입. telegram_loop 단일
        # 스레드에서만 불려 media_retry 동시성 이슈 없음. 성공/최종실패 시 enqueue_update 가 pop.
        now = time.time()
        for key, (update, _attempts, retry_at) in list(self.media_retry.items()):
            if retry_at > now:
                continue
            log("QUEUE", f"media auto-requeue retry update={update.get('update_id')} key={key[:10]}")
            self.enqueue_update(update)

    def check_injection_timeout(self) -> None:
        with self.lock:
            active = self.active_turn
        if not active or active.user_uuid:
            return
        if time.time() - active.injected_at < self.config.injection_verify_timeout:
            return

        if self.retry_active_submit_if_composer_residual(active):
            return

        if active.busy_injected:
            try:
                busy_timeout = float(os.environ.get("CLB_BUSY_INJECT_VERIFY_TIMEOUT_SEC", "60"))
            except ValueError:
                busy_timeout = 60.0
            busy_timeout = max(busy_timeout, self.config.injection_verify_timeout)
            if active.native_queue_seen_at > 0:
                reference_at = max(active.native_queue_seen_at, active.injected_at)
                if time.time() - reference_at < busy_timeout:
                    log("INJECT", f"busy-inject nonce {active.nonce} awaiting native queue attachment")
                    return
                item = self.queue_item_for_active(active)
                log("INJECT", f"busy-inject nonce {active.nonce} native queue attachment timed out")
                with self.lock:
                    if self.active_turn is not active:
                        return
                    self.active_turn = None
                self.stop_typing()
                self.mark_directive_terminal(item, "failed", error="busy-inject native queue attachment not observed")
                self.persist_state()
                self.write_egress_sidecar()
                return
            if time.time() - active.injected_at < busy_timeout:
                log("INJECT", f"busy-inject nonce {active.nonce} awaiting JSONL user record")
                return

        # Do not fail or clear-retry while the session is still busy. A long prior
        # turn keeps the injected prompt waiting in the composer until the session
        # is free; clearing it here loses the user's message and shows a false
        # "delivery failed". Extend the grace window so only idle time counts
        # toward the timeout, and re-check next tick.
        # (2026-06-23 노트북, 아니키 ack — busy 중 가짜 통신끊김 경보 차단.)
        if self.session_occupied_excluding_active():
            with self.lock:
                if self.active_turn and self.active_turn.queue_id == active.queue_id:
                    self.active_turn.injected_at = time.time()
            return

        if self.mark_active_sidecar_consumed_seen(active):
            with self.lock:
                if self.active_turn and self.active_turn.queue_id == active.queue_id:
                    self.active_turn.injected_at = time.time()
            return

        item = QueueItem(
            active.queue_id,
            active.update_id,
            active.message_id,
            active.text,
            active.nonce,
            active.injected_at,
            sent_at=active.sent_at,
            source=active.source,
            voice_reply_path=active.voice_reply_path,
            busy_injected=active.busy_injected,
            user_uuid=active.user_uuid or "",
            user_seen_at=active.user_seen_at,
            native_queue_seen_at=active.native_queue_seen_at,
        )
        if active.inject_attempts >= 2:
            log("INJECT", f"nonce {active.nonce} not observed in JSONL after retry")
            with self.lock:
                self.active_turn = None
            self.stop_typing()
            self.mark_directive_terminal(item, "failed", error="nonce user JSONL not observed")
            self.persist_state()
            self.write_egress_sidecar()
            return

        log("INJECT", f"nonce {active.nonce} not observed; composer clear/retry")
        try:
            self.repl.clear_composer()
            self.repl.paste_prompt(self.prompt_for_item(item))
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"retry failed: {exc}")
            with self.lock:
                self.active_turn = None
            self.stop_typing()
            self.mark_directive_terminal(item, "failed", error=f"retry failed: {exc}")
            self.persist_state()
            self.write_egress_sidecar()
            return

        active.inject_attempts += 1
        active.injected_at = time.time()
        self.queue.append_status(
            item,
            "injected_retry",
            attempt=active.inject_attempts,
            **self.terminal_retry_status_extra(item),
        )
        self.persist_state()
        self.write_egress_sidecar()

    def load_pending_queue(self) -> None:
        with self.lock:
            known = {item.queue_id for item in self.pending}
            if self.active_turn:
                known.add(self.active_turn.queue_id)
            for item in self.queue.pending_items():
                if item.queue_id not in known:
                    self.pending.append(item)
                    known.add(item.queue_id)
            self.pending.sort(key=lambda item: (item.received_at, item.update_id))

    def update_parent_map(self, record: dict[str, Any]) -> None:
        uuid = record.get("uuid")
        if isinstance(uuid, str) and uuid:
            parent = record.get("parentUuid")
            self.parent_map[uuid] = parent if isinstance(parent, str) else None
            if len(self.parent_map) > 1000:
                self.parent_map = dict(list(self.parent_map.items())[-700:])

    def ancestor_matches_active_turn(self, assistant_parent_uuid: str | None) -> ActiveTurn | None:
        active = self.active_turn
        if not active or not active.user_uuid:
            return None
        seen: set[str] = set()
        cursor = assistant_parent_uuid
        while isinstance(cursor, str) and cursor and cursor not in seen:
            if cursor == active.user_uuid:
                return active
            seen.add(cursor)
            cursor = self.parent_map.get(cursor)
        return None

    def sequence_matches_active_turn(self, record: dict[str, Any]) -> ActiveTurn | None:
        """Fallback for Claude transcript records whose parentUuid chain is broken.

        The primary guard remains the parent graph. This fallback opens only after
        the bridge-injected nonce user is observed in the same transcript, then
        accepts assistant records in a bounded timestamp window. That handles
        Claude Code transcript compaction/attachment gaps without letting terminal
        origin answers leak into Telegram.
        """
        active = self.active_turn
        if not active or not active.user_uuid or active.user_seen_at <= 0:
            return None
        if record.get("isSidechain") is not False:
            return None
        ts = record_timestamp_seconds(record)
        if ts is None:
            return None
        if ts + 0.001 < active.user_seen_at:
            return None
        if ts - active.user_seen_at > self.config.turn_sequence_fallback_seconds:
            return None
        return active

    @staticmethod
    def append_reasoning(active: ActiveTurn, text: str) -> None:
        text = sanitize_text(text, limit=REASONING_MIRROR_LIMIT)
        if not text:
            return
        active.accumulated_reasoning = (
            f"{active.accumulated_reasoning}\n{text}".strip()
            if active.accumulated_reasoning
            else text
        )

    def queue_item_for_active(self, active: ActiveTurn) -> QueueItem:
        return QueueItem(
            active.queue_id,
            active.update_id,
            active.message_id,
            active.text,
            active.nonce,
            active.injected_at,
            sent_at=active.sent_at,
            source=active.source,
            voice_reply_path=active.voice_reply_path,
            busy_injected=active.busy_injected,
            user_uuid=active.user_uuid or "",
            user_seen_at=active.user_seen_at,
            native_queue_seen_at=active.native_queue_seen_at,
        )

    def mark_pending_user_nonce_seen(self, nonce: str, record: dict[str, Any]) -> bool:
        user_uuid = str(record.get("uuid") or "")
        if not user_uuid:
            return False
        user_seen_at = record_timestamp_seconds(record) or time.time()
        matched: QueueItem | None = None
        with self.lock:
            for item in self.pending:
                if item.busy_injected and item.nonce == nonce:
                    item.user_uuid = user_uuid
                    item.user_seen_at = user_seen_at
                    matched = item
                    break
        if not matched:
            return False
        self.queue.append_status(
            matched,
            "enqueued",
            busy_inject=True,
            jsonl_seen=True,
            user_uuid=user_uuid,
            user_seen_at=user_seen_at,
        )
        self.persist_state()
        self.write_egress_sidecar()
        log("JSONL", f"pending user nonce seen {nonce}")
        return True

    def mark_attachment_nonce_seen(self, record: dict[str, Any]) -> bool:
        nonce = record_contains_nonce(record) or record_attachment_nonce(record)
        if not nonce:
            return False
        user_uuid = str(record.get("uuid") or record.get("parentUuid") or f"attachment:{nonce}")
        user_seen_at = record_timestamp_seconds(record) or time.time()
        active_item: QueueItem | None = None
        pending_item: QueueItem | None = None
        with self.lock:
            if self.active_turn and self.active_turn.nonce == nonce:
                self.active_turn.user_uuid = user_uuid
                self.active_turn.user_seen_at = user_seen_at
                active_item = self.queue_item_for_active(self.active_turn)
            else:
                for item in self.pending:
                    if item.busy_injected and item.nonce == nonce:
                        item.user_uuid = user_uuid
                        item.user_seen_at = user_seen_at
                        pending_item = item
                        break
        if active_item:
            self.queue.append_status(
                active_item,
                "user_jsonl_seen",
                user_uuid=user_uuid,
                user_seen_at=user_seen_at,
                sidecar_attachment=True,
            )
        elif pending_item:
            self.queue.append_status(
                pending_item,
                "enqueued",
                busy_inject=True,
                jsonl_seen=True,
                sidecar_attachment=True,
                user_uuid=user_uuid,
                user_seen_at=user_seen_at,
            )
        else:
            return False
        self.persist_state()
        self.write_egress_sidecar()
        log("JSONL", f"attachment nonce seen {nonce}")
        return True

    def remember_native_queue_enqueue(self, record: dict[str, Any]) -> bool:
        if record.get("operation") != "enqueue":
            return False
        content = record.get("content")
        if not isinstance(content, str):
            return False
        match = NONCE_RE.search(content)
        if not match:
            return False
        timestamp = str(record.get("timestamp") or "")
        if not timestamp:
            return False
        nonce = match.group(0)
        seen_at = record_timestamp_seconds(record) or time.time()
        active_item: QueueItem | None = None
        matched_item: QueueItem | None = None
        with self.lock:
            self.native_queue_nonce_by_timestamp[timestamp] = nonce
            if len(self.native_queue_nonce_by_timestamp) > 200:
                self.native_queue_nonce_by_timestamp = dict(
                    list(self.native_queue_nonce_by_timestamp.items())[-120:]
                )
            if self.active_turn and self.active_turn.nonce == nonce:
                self.active_turn.native_queue_seen_at = seen_at
                active_item = self.queue_item_for_active(self.active_turn)
            else:
                for item in self.pending:
                    if item.busy_injected and item.nonce == nonce:
                        item.native_queue_seen_at = seen_at
                        matched_item = item
                        break
        if active_item:
            self.queue.append_status(active_item, "injected", busy_inject=True, native_queue_seen=True)
        elif matched_item:
            self.queue.append_status(matched_item, "enqueued", busy_inject=True, native_queue_seen=True)
        if active_item or matched_item:
            self.persist_state()
            self.write_egress_sidecar()
        log("JSONL", f"native queue nonce enqueued {nonce}")
        return True

    def mark_native_queue_attachment_seen(self, record: dict[str, Any]) -> bool:
        timestamp = str(record.get("timestamp") or "")
        user_uuid = str(record.get("uuid") or "")
        if not timestamp or not user_uuid:
            return False
        with self.lock:
            nonce = self.native_queue_nonce_by_timestamp.get(timestamp)
        if not nonce:
            return False
        user_seen_at = record_timestamp_seconds(record) or time.time()
        active_item: QueueItem | None = None
        pending_item: QueueItem | None = None
        with self.lock:
            if self.active_turn and self.active_turn.nonce == nonce:
                self.active_turn.user_uuid = user_uuid
                self.active_turn.user_seen_at = user_seen_at
                if self.active_turn.native_queue_seen_at <= 0:
                    self.active_turn.native_queue_seen_at = user_seen_at
                active_item = self.queue_item_for_active(self.active_turn)
            else:
                for item in self.pending:
                    if item.busy_injected and item.nonce == nonce:
                        item.user_uuid = user_uuid
                        item.user_seen_at = user_seen_at
                        if item.native_queue_seen_at <= 0:
                            item.native_queue_seen_at = user_seen_at
                        pending_item = item
                        break
        if active_item:
            self.queue.append_status(
                active_item,
                "user_jsonl_seen",
                user_uuid=user_uuid,
                user_seen_at=user_seen_at,
                native_queue_attachment=True,
            )
        elif pending_item:
            self.queue.append_status(
                pending_item,
                "enqueued",
                busy_inject=True,
                jsonl_seen=True,
                native_queue_attachment=True,
                user_uuid=user_uuid,
                user_seen_at=user_seen_at,
            )
        else:
            return False
        self.persist_state()
        self.write_egress_sidecar()
        log("JSONL", f"native queue attachment seen {nonce}")
        return True

    def release_batched_busy_pending(self, active: ActiveTurn, status: str) -> None:
        if status not in {"sent", "answered"} or not active.busy_injected or active.user_seen_at <= 0:
            return
        released: list[QueueItem] = []
        with self.lock:
            remaining: list[QueueItem] = []
            releasing_prefix = True
            for item in self.pending:
                if (
                    releasing_prefix
                    and item.busy_injected
                    and item.user_uuid
                    and item.user_seen_at > 0
                    and item.user_seen_at + 0.001 >= active.user_seen_at
                ):
                    released.append(item)
                    continue
                releasing_prefix = False
                remaining.append(item)
            if released:
                self.pending = remaining
        for item in released:
            self.queue.append_status(item, status, busy_inject=True, batched_with=active.queue_id)
        if released:
            log(
                "TURN",
                f"released {len(released)} busy-inject batched pending item(s) with {active.queue_id[:10]}",
            )

    def send_active_answer(self, active: ActiveTurn, assistant_uuid: str, answer: str) -> None:
        claim = self.claim_send_attempt(active, assistant_uuid, answer)
        if claim == "outbox_sent":
            log("SEND", "skip duplicate outbox key")
            mesh_ledger_record("sendMessage", self.config.chat_id, answer, result="suppressed")
            self.finish_active_turn("sent", active)
            return
        if not claim:
            return
        self.send_claimed_active_answer(active, assistant_uuid, answer, claim)

    def claim_send_attempt(self, active: ActiveTurn, assistant_uuid: str, answer: str) -> str | None:
        with self.lock:
            if self.active_turn is not active:
                return None
            if active.send_in_progress:
                return None
            key = answer_outbox_key(active.nonce, assistant_uuid, answer)
            if self.outbox.contains(key):
                return "outbox_sent"
            active.assistant_uuid = assistant_uuid
            active.pending_answer = answer
            active.pending_assistant_uuid = assistant_uuid
            active.pending_outbox_key = key
            active.send_attempts += 1
            active.last_send_attempt_at = time.time()
            active.send_in_progress = True
            return key

    def claim_retry_send_attempt(self) -> tuple[ActiveTurn, str, str, str] | tuple[str, ActiveTurn, str] | None:
        with self.lock:
            active = self.active_turn
            if not active or not active.pending_answer or not active.pending_assistant_uuid:
                return None
            if active.send_in_progress:
                return None
            now = time.time()
            if now - active.last_send_attempt_at < self.config.send_retry_seconds:
                return None
            answer = active.pending_answer
            assistant_uuid = active.pending_assistant_uuid
            key = answer_outbox_key(active.nonce, assistant_uuid, answer)
            if self.outbox.contains(key):
                return "outbox_sent", active, answer
            active.assistant_uuid = assistant_uuid
            active.pending_outbox_key = key
            active.send_attempts += 1
            active.last_send_attempt_at = now
            active.send_in_progress = True
            return active, assistant_uuid, answer, key

    def send_claimed_active_answer(
        self,
        active: ActiveTurn,
        assistant_uuid: str,
        answer: str,
        key: str,
    ) -> None:
        send_error = "telegram send failed"
        copy_payload_messages = split_copy_payload_messages(answer)
        reply_to_message_id = active.message_id if active.message_id > 0 and active.source != "voice" else None
        self.outbox.mark_sending(key, active.send_attempts)
        try:
            if copy_payload_messages:
                sent_ids = []
                used_reply = False
                for message in copy_payload_messages:
                    part_ids = self.telegram.send(
                        message,
                        reply_to_message_id=reply_to_message_id if not used_reply else None,
                    )
                    used_reply = True
                    if part_ids is None:
                        sent_ids = None
                        break
                    sent_ids.extend(part_ids)
            else:
                sent_ids = self.telegram.send(answer, reply_to_message_id=reply_to_message_id)
        except Exception as exc:  # noqa: BLE001
            send_error = str(exc)
            sent_ids = None
        item = self.queue_item_for_active(active)
        if sent_ids is None:
            self.outbox.forget(key)
            with self.lock:
                attempts = active.send_attempts
                maxed = attempts >= self.config.send_max_attempts
                if self.active_turn is active and maxed:
                    active.send_in_progress = False
                    self.active_turn = None
                elif self.active_turn is active:
                    active.send_in_progress = False
            if maxed:
                log("SEND", f"telegram send failed after {attempts} attempts; releasing active turn")
                self.queue.append_status(
                    item,
                    "failed",
                    error=send_error,
                    assistant_uuid=assistant_uuid,
                    attempts=attempts,
                )
                self.stop_typing()
                self.persist_state()
                self.write_egress_sidecar()
                self.drain_queue()
                return
            log("SEND", f"telegram send failed; retry pending attempt={attempts}")
            self.queue.append_status(
                item,
                "send_retry_pending",
                assistant_uuid=assistant_uuid,
                attempts=attempts,
            )
            self.persist_state()
            self.write_egress_sidecar()
            return

        self.outbox.mark_sent(key, sent_ids)
        try:
            write_voice_answer(active, assistant_uuid=assistant_uuid, answer=answer)
        except Exception as exc:  # noqa: BLE001
            log("SEND", f"voice answer sidecar write failed (non-fatal): {exc}")
        # 🧠 reasoning mirror — sent once, right after the deduped final answer
        # (sibling of codex-repl-telegram-bridge's 🧠 코덱스 사고). Empty/no-thinking
        # turns produce no block. Failures here never affect answer delivery.
        if copy_payload_messages:
            active.pending_reasoning = None  # 복붙 콘텐츠 turn 은 🧠 미러 skip
        else:
            self.flush_reasoning_mirror(active)
        with self.lock:
            if self.active_turn is active:
                active.send_in_progress = False
        self.queue.append_status(
            item,
            "sent",
            assistant_uuid=assistant_uuid,
            sent_message_ids=sent_ids,
            attempts=active.send_attempts,
        )
        log("SEND", f"sent final nonce={active.nonce} assistant={assistant_uuid}")
        self.finish_active_turn("sent", active)

    def retry_pending_send(self) -> None:
        claim = self.claim_retry_send_attempt()
        if isinstance(claim, tuple) and claim and claim[0] == "outbox_sent":
            log("SEND", "skip duplicate outbox key")
            _, active, answer = claim
            mesh_ledger_record("sendMessage", self.config.chat_id, answer, result="suppressed")
            self.finish_active_turn("sent", active)
            return
        if not claim:
            return
        active, assistant_uuid, answer, key = claim
        self.send_claimed_active_answer(active, assistant_uuid, answer, key)

    def process_record(self, record: dict[str, Any]) -> None:
        binding = self.session_binding
        if not binding:
            return
        if record.get("sessionId") not in {binding.session_id, None}:
            return
        self.update_parent_map(record)
        record_type = record.get("type")
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")

        if record_type == "queue-operation":
            self.remember_native_queue_enqueue(record)
            return

        if record_type == "attachment":
            if self.mark_native_queue_attachment_seen(record):
                return
            if self.mark_attachment_nonce_seen(record):
                return

        if record_type == "user" and message.get("role") == "user":
            nonce = record_contains_nonce(record)
            if nonce and self.active_turn and nonce == self.active_turn.nonce:
                self.active_turn.user_uuid = str(record.get("uuid") or "")
                self.active_turn.user_seen_at = record_timestamp_seconds(record) or time.time()
                self.queue.append_status(
                    self.queue_item_for_active(self.active_turn),
                    "user_jsonl_seen",
                    user_uuid=self.active_turn.user_uuid,
                    user_seen_at=self.active_turn.user_seen_at,
                )
                self.persist_state()
                self.write_egress_sidecar()
                log("JSONL", f"user nonce seen {nonce}")
                return
            if nonce and self.mark_pending_user_nonce_seen(nonce, record):
                return
            if not nonce and self.mark_active_sidecar_body_user_seen(record):
                return
            # ⚙️ ambient flow mirror — node-originated incoming directive (다른 노드/오케가
            # 주입한 트리거 프롬프트). 텔레그램 active turn 도 nonce 도 없는 user 레코드 =
            # 노드발 지시. 결과("✅ 노드 결과")만 떠서 맥락이 끊기던 문제 보완으로 받은
            # 지시를 1장 미러한다. 텔레그램-origin 은 clb- nonce 를 달고 들어오므로
            # not nonce 로 배제(노드 디렉티브는 nonce 無). tool_result(도구결과)는
            # content_text 가 ""라 자동 제외, sub-agent sidechain 은 isSidechain 가드로
            # 제외. flow-mirror 토글 ON 한정.
            if (
                flow_mirror_enabled()
                and not nonce
                and not self.active_turn
                and not record.get("isSidechain")
            ):
                self.mirror_ambient_directive(content)
            return

        if record_type != "assistant" or message.get("role") != "assistant":
            return
        active = self.ancestor_matches_active_turn(record.get("parentUuid")) or self.sequence_matches_active_turn(record)
        if not active:
            # ⚙️ ambient flow mirror (v0.1.5) — node-originated work (autonomous worker /
            # cron / node-to-node) has no active telegram turn, so this assistant record
            # was dropped here and the work was invisible. When flow mirror is on,
            # accumulate the tool_use steps into an ambient card. The card boundary is
            # reset whenever an incoming telegram message opens a new active turn.
            if flow_mirror_enabled():
                if message.get("stop_reason") != "end_turn":
                    self.mirror_ambient_flow(content)
                else:
                    # ⚠️ 제거 금지 (DO NOT REMOVE) — 비-텔레그램-origin(노드발/cron/노드간)
                    # 작업의 최종 답변을 노드 챗에 미러. 작업흐름 카드(도구 단계)만 뜨고
                    # 결론이 노드 챗에서 사라지던 사각 차단.
                    # issue: 2026-06-27-bridge-flow-mirror-final-report-missed
                    self.mirror_ambient_final(content)
            return

        if content_has_tool(content, MCP_TELEGRAM_REPLY_TOOL):
            active.external_reply_seen = True
            self.persist_state()
            log("EGRESS", "MCP telegram reply tool_use seen; suppress bridge duplicate")
            mesh_ledger_record("sendMessage", self.config.chat_id, content_text(content), result="suppressed")
            return

        # Accumulate thinking across the whole turn. This Claude Code version emits
        # thinking in SEPARATE non-end_turn assistant messages, so the final
        # end_turn message content is text-only — reading thinking only there always
        # misses it. (fc8024b 후속 fix, 2026-06-23 노트북 카나리 SPLIT 판정 근거.)
        stop_reason = message.get("stop_reason")
        turn_thinking = content_thinking(content)
        if turn_thinking:
            self.append_reasoning(active, turn_thinking)
        elif stop_reason != "end_turn":
            self.append_reasoning(active, content_text(content))

        if stop_reason != "end_turn":
            # ⚙️ flow mirror — relay intermediate tool_use steps in real time when
            # the flag is on. Scoped to the active turn (nonce chain) already, and
            # fully non-fatal: failures here never affect final answer delivery.
            # Edit-in-place: accumulate this turn's steps into ONE growing card
            # (edit the same message) instead of one card per tool-use message.
            if flow_mirror_enabled():
                summary = content_tool_summary(content)
                if summary:
                    candidate = f"{active.flow_body}\n{summary}".strip() if active.flow_body else summary
                    if not active.flow_message_id or len(candidate) > FLOW_MIRROR_LIMIT:
                        # first card of the turn, or overflow -> start a fresh card
                        active.flow_body = summary
                        try:
                            ids = self.telegram.send(format_flow_mirror(active.flow_body))
                            active.flow_message_id = ids[0] if ids else 0
                            log("SEND", f"sent flow mirror nonce={active.nonce} mid={active.flow_message_id}")
                        except Exception as exc:  # noqa: BLE001
                            log("SEND", f"flow mirror send failed (non-fatal): {exc}")
                    else:
                        # same turn -> grow the existing card in place
                        active.flow_body = candidate
                        try:
                            self.telegram.edit(active.flow_message_id, format_flow_mirror(active.flow_body))
                            log("SEND", f"edited flow mirror nonce={active.nonce} mid={active.flow_message_id}")
                        except Exception as exc:  # noqa: BLE001
                            log("SEND", f"flow mirror edit failed (non-fatal): {exc}")
            return
        if record.get("isSidechain") is not False:
            return
        answer = sanitize_text(content_text(content), limit=16000)
        if not answer:
            return
        if active.external_reply_seen:
            log("SEND", "skip final because external reply tool was seen")
            mesh_ledger_record("sendMessage", self.config.chat_id, answer, result="suppressed")
            self.finish_active_turn("answered", active)
            return
        assistant_uuid = str(record.get("uuid") or "")
        reasoning = active.accumulated_reasoning or content_thinking(content)
        active.pending_reasoning = sanitize_text(reasoning, limit=REASONING_MIRROR_LIMIT) or None
        self.send_active_answer(active, assistant_uuid, answer)

    def reset_ambient_flow(self) -> None:
        # ⚙️ ambient flow mirror (v0.1.5) — incoming-message boundary reset: a new active
        # telegram turn closes the current ambient card so the next bout of
        # node-autonomous work starts a fresh card instead of growing the old one.
        self.ambient_flow_body = ""
        self.ambient_flow_message_id = 0

    def mirror_ambient_flow(self, content: Any) -> None:
        # ⚙️ ambient flow mirror (v0.1.5) — see call site in process_record. Non-fatal;
        # never affects message delivery. Only emits when no active turn exists.
        if self.active_turn:
            return
        summary = content_tool_summary(content)
        if not summary:
            return
        candidate = f"{self.ambient_flow_body}\n{summary}".strip() if self.ambient_flow_body else summary
        if not self.ambient_flow_message_id or len(candidate) > FLOW_MIRROR_LIMIT:
            # first card of this ambient bout, or overflow -> start a fresh card
            self.ambient_flow_body = summary
            try:
                ids = self.telegram.send(format_ambient_flow(self.ambient_flow_body))
                self.ambient_flow_message_id = ids[0] if ids else 0
                log("SEND", f"sent ambient flow mid={self.ambient_flow_message_id}")
            except Exception as exc:  # noqa: BLE001
                log("SEND", f"ambient flow send failed (non-fatal): {exc}")
        else:
            # same ambient bout -> grow the existing card in place
            self.ambient_flow_body = candidate
            try:
                self.telegram.edit(self.ambient_flow_message_id, format_ambient_flow(self.ambient_flow_body))
                log("SEND", f"edited ambient flow mid={self.ambient_flow_message_id}")
            except Exception as exc:  # noqa: BLE001
                log("SEND", f"ambient flow edit failed (non-fatal): {exc}")

    def mirror_ambient_directive(self, content: Any) -> None:
        # ⚙️ ambient flow mirror — node-originated work 의 받은 지시(트리거) 카드를 노드
        # 챗에 1장 미러. 결과("✅ 노드 결과")만 떠서 맥락이 끊기던 문제 보완. tool_result
        # (도구 결과) user 레코드는 content_text 가 ""를 반환해 자동 제외된다. Non-fatal;
        # never affects message delivery. Only emits when no active telegram turn.
        if self.active_turn:
            return
        body = format_ambient_directive(content_text(content))
        if not body:
            return
        # 새 bout 시작 → flow 카드 묶음 경계 리셋(받은지시→작업흐름→노드결과 한 묶음).
        self.reset_ambient_flow()
        try:
            ids = self.telegram.send(body)
            # ⚙️ T-260630-48 — 이 받은지시 카드를 결과의 앵커로 보관. mirror_ambient_final 이
            # 새 ✅ 카드 대신 이 카드를 edit 해 받은지시→결과를 1장으로 통합한다.
            self.ambient_directive_message_id = ids[0] if ids else 0
            self.ambient_directive_body = body
            log("SEND", f"sent ambient directive mid={self.ambient_directive_message_id}")
        except Exception as exc:  # noqa: BLE001
            self.ambient_directive_message_id = 0
            self.ambient_directive_body = ""
            log("SEND", f"ambient directive send failed (non-fatal): {exc}")

    def mirror_ambient_final(self, content: Any) -> None:
        # ⚠️ 제거 금지 (DO NOT REMOVE) — 비-텔레그램-origin(노드발/cron/노드간) 작업의
        # 최종 답변을 노드 챗에 1장 미러. flow 카드(도구 단계)만 뜨고 결론이 사라지던
        # 사각 차단. issue: 2026-06-27-bridge-flow-mirror-final-report-missed.
        # Non-fatal; never affects message delivery. Only emits when no active turn.
        if self.active_turn:
            return
        # F9 (T-260705-04): ambient(디렉티브/mac-report) 턴 종료 지점 — typing 명시 소등.
        # 기존엔 finish_active_turn(브릿지가 주입한 턴)만 stop_typing 을 불러, drain_queue
        # busy 분기의 ensure_typing 이 켠 '입력중'이 보고 턴 뒤 TYPING_MAX(2h)까지 남는
        # 유령이 됐다(2026-07-05 새벽 5노드 실측). suppress/dedupe/전송실패 분기 포함
        # 모든 종료 경로보다 앞에서 소등한다.
        self.stop_typing()
        # ⚠️ 제거 금지 (DO NOT REMOVE) — 이중송신 가드 (T-260628-10): mac-report.sh 가 같은 노드
        # 봇챗에 노드보고를 이미 보낸 경우(suppress 플래그 90초 내) skip → mac-report self-chat
        # 미러와 ambient_final 의 노드 봇챗 교차중복 차단.
        # issue: 2026-06-27-bridge-flow-mirror-final-report-missed
        suppress = os.path.expanduser(f"~/.config/claude-telegram-bridge/ambient-suppress-{self.config.node}")
        try:
            if os.path.exists(suppress) and (time.time() - os.path.getmtime(suppress)) < 90:
                # ⚙️ T-260630-48 — mac-report 가 노드 챗을 소유(suppress)하면 bridge 는 받은지시
                # 앵커를 놓아준다(다음 final 이 옛 받은지시 카드를 잘못 edit 하지 않게 정리).
                self.ambient_directive_message_id = 0
                self.ambient_directive_body = ""
                log("SEND", "skip ambient final (mac-report suppress active)")
                mesh_ledger_record("sendMessage", self.config.chat_id, content_text(content), result="suppressed")
                return
        except OSError:
            pass
        text = sanitize_text(content_text(content), limit=FLOW_MIRROR_LIMIT)
        if not text:
            return
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key == self.ambient_final_last_key:
            mesh_ledger_record("sendMessage", self.config.chat_id, format_ambient_final(text), result="suppressed")
            return
        self.ambient_final_last_key = key
        anchor = self.ambient_directive_message_id
        if anchor and alt3_narrative_enabled():
            # alt3 (spec v0.2 §6, T-260702-37 PR-B): 받은지시 카드 edit-통합 모델 폐기 →
            # 결과는 받은지시 루트에 native reply (같은 chat·같은 봇이라 §5-3 충족).
            # 본문은 R-C1 자연어 그대로(✅ chrome 없음) — 연결은 reply 인용이 표현한다.
            # reply send 실패 시 폴백 = 기존 ✅ 카드 send (결과 1장 보장).
            try:
                ids = self.telegram.send(text, reply_to_message_id=anchor)
                if ids:
                    log("SEND", f"sent ambient final as reply to directive root mid={anchor}")
                else:
                    self.telegram.send(format_ambient_final(text))
                    log("SEND", "ambient final reply failed → fallback card send")
            except Exception as exc:  # noqa: BLE001
                log("SEND", f"ambient final reply failed → fallback send (non-fatal): {exc}")
                try:
                    self.telegram.send(format_ambient_final(text))
                except Exception as exc2:  # noqa: BLE001
                    log("SEND", f"ambient final fallback send failed (non-fatal): {exc2}")
            self.ambient_directive_message_id = 0
            self.ambient_directive_body = ""
        elif anchor:
            # ⚙️ T-260630-48 — 받은지시 앵커 카드를 결과까지 포함한 1장으로 in-place 통합
            # (새 ✅ 카드 X). 받은지시→노드결과 2장 중복 제거. edit 실패 시 폴백으로 새 카드 send
            # (앵커 매칭/edit 실패해도 결과는 1장 보장 — 0장도 2장폭발도 아님).
            unified = f"{self.ambient_directive_body}\n\n{format_ambient_final(text)}"
            try:
                self.telegram.edit(anchor, unified)
                log("SEND", f"edited ambient final into directive anchor mid={anchor}")
            except Exception as exc:  # noqa: BLE001
                log("SEND", f"ambient final anchor edit failed → fallback send (non-fatal): {exc}")
                try:
                    self.telegram.send(format_ambient_final(text))
                except Exception as exc2:  # noqa: BLE001
                    log("SEND", f"ambient final fallback send failed (non-fatal): {exc2}")
            self.ambient_directive_message_id = 0
            self.ambient_directive_body = ""
        else:
            try:
                self.telegram.send(format_ambient_final(text))
                log("SEND", "sent ambient final")
            except Exception as exc:  # noqa: BLE001
                log("SEND", f"ambient final send failed (non-fatal): {exc}")
        # 최종 답변 미러 후 flow 카드 bout 종료 -> 다음 노드 작업은 새 카드로.
        self.ambient_flow_body = ""
        self.ambient_flow_message_id = 0

    def finish_active_turn(self, status: str, active: ActiveTurn) -> bool:
        with self.lock:
            if self.active_turn is not active:
                current = self.active_turn
                current_queue = current.queue_id[:10] if current else "-"
                log(
                    "TURN",
                    f"skip stale finish expected={active.queue_id[:10]} current={current_queue} status={status}",
                )
                return False
            self.active_turn = None
        self.stop_typing()
        self.queue.append_status(self.queue_item_for_active(active), status)
        self.release_batched_busy_pending(active, status)
        self.persist_state()
        self.write_egress_sidecar()
        self.drain_queue()
        return True

    def quarantine_line(self, line: bytes, error: str, start: int, end: int) -> None:
        append_jsonl(
            self.config.quarantine_path,
            {
                "ts": time.time(),
                "start": start,
                "end": end,
                "error": error,
                "line_sha256": hashlib.sha256(line).hexdigest(),
                "preview": line[:300].decode("utf-8", errors="replace"),
            },
        )

    def jsonl_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                binding = self.ensure_session_binding()
                path = binding.transcript_path
                if not path.exists():
                    self.retry_pending_send()
                    self.write_egress_sidecar()
                    self.stop_event.wait(0.5)
                    continue
                with path.open("rb") as handle:
                    handle.seek(self.session_pos)
                    data = handle.read()
                if not data:
                    self.retry_pending_send()
                    self.stop_event.wait(0.5)
                    continue
                cursor = self.session_pos
                parts = data.split(b"\n")
                complete = parts[:-1]
                for raw in complete:
                    line_start = cursor
                    line_end = cursor + len(raw) + 1
                    cursor = line_end
                    if not raw:
                        self.session_pos = line_end
                        continue
                    try:
                        record = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError as exc:
                        self.quarantine_line(raw, str(exc), line_start, line_end)
                        self.session_pos = line_end
                        self.persist_state()
                        continue
                    if isinstance(record, dict):
                        self.process_record(record)
                    self.session_pos = line_end
                    self.persist_state()
                self.last_transcript_mtime = path.stat().st_mtime
                self.last_jsonl_read_at = time.time()
                self.retry_pending_send()
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                now = time.time()
                if message != self.last_jsonl_watch_error or now - self.last_jsonl_watch_error_log_at >= 30.0:
                    log("JSONL", f"watch error: {message}")
                    self.last_jsonl_watch_error = message
                    self.last_jsonl_watch_error_log_at = now
                if is_tmux_session_lost_error(exc):
                    self.release_active_turn_due_to_tmux_session_lost(message)
                if isinstance(exc, FileNotFoundError):
                    with self.lock:
                        if self.session_identity is not None or (
                            self.session_binding is not None
                            and not self.has_fresh_pending_sidecar_binding(self.session_binding)
                        ):
                            self.session_binding = None
                            self.session_identity = None
                            self.session_pos = 0
            self.stop_event.wait(0.5)

    def offer_update_if_available(self) -> None:
        latest = self_update_available()
        if not latest:
            return
        if bool_env(f"{SELF_UPDATE_PREFIX}_AUTO_UPDATE", False):
            perform_self_update(latest)
            return
        current = _self_update_installed_version() or "?"
        try:
            self.telegram.send_update_button(
                f"\U0001f195 새 버전 v{latest} 가 출시됐어요! (현재 v{current})\n업데이트하려면 아래 버튼을 누르세요.",
                f"{SELF_UPDATE_CALLBACK}::{latest}",
            )
        except Exception as exc:  # noqa: BLE001
            log("UPDATE", f"update offer failed: {exc}")

    def service_external_queue_once(self) -> None:
        self.load_pending_queue()
        self.drain_queue()

    def telegram_loop(self) -> None:
        offset_raw = read_text(self.config.offset_file)
        offset = int(offset_raw) if offset_raw.isdigit() else 0
        TokenOwnership(self.config, self.telegram, self.token).verify_or_die(offset)
        self.offer_update_if_available()
        while not self.stop_event.is_set():
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=self.config.poll_timeout)
            except TelegramHTTPError as exc:
                if exc.is_conflict:
                    append_jsonl(
                        self.config.state_dir / "claude-telegram-bridge-watchdog.jsonl",
                        {"ts": time.time(), "event": "telegram_409", "error": str(exc)},
                    )
                    raise RuntimeError("Telegram getUpdates 409; token ownership lost, fail-closed") from exc
                raise
            for update in updates:
                if not isinstance(update, dict) or "update_id" not in update:
                    continue
                update_id = int(update["update_id"])
                cb = update.get("callback_query")
                if isinstance(cb, dict):
                    data = str(cb.get("data") or "")
                    offset = update_id + 1
                    write_text_atomic(self.config.offset_file, offset)
                    if data.startswith(f"{SELF_UPDATE_CALLBACK}::"):
                        cb_chat = (cb.get("message") or {}).get("chat") or {}
                        if str(cb_chat.get("id")) == str(self.config.chat_id):
                            self.telegram.call("answerCallbackQuery", callback_query_id=cb.get("id"), text="업데이트를 시작합니다…")
                            perform_self_update(data.split("::", 1)[1])
                    elif data.startswith(f"{MODEL_CALLBACK}::"):
                        # /model inline keyboard 선택 → 비대화형 인자형 적용 (T-260702-14)
                        cb_chat = (cb.get("message") or {}).get("chat") or {}
                        if str(cb_chat.get("id")) == str(self.config.chat_id):
                            # 토스트는 결과(적용/거부/대기) 확정 전에 뜨므로 중립 문구 — 실제 결과는
                            # apply_model_choice 가 메뉴 메시지를 edit 해서 durable 하게 알린다 (T-260703-23).
                            self.telegram.call("answerCallbackQuery", callback_query_id=cb.get("id"), text="확인 중…")
                            menu_message_id = (cb.get("message") or {}).get("message_id")
                            self.apply_model_choice(data.split("::", 1)[1], menu_message_id=menu_message_id)
                    continue
                self.enqueue_update(update)
                offset = update_id + 1
                write_text_atomic(self.config.offset_file, offset)
                self.drain_queue()
            self.check_injection_timeout()
            self.check_queue_stuck_alert()
            self.check_busy_stuck_rebind()
            self.retry_media_downloads()
            self.retry_pending_send()
            self.service_external_queue_once()

    def run(self) -> None:
        self.repl.verify()
        self.load_pending_queue()
        self.acquire_lock()
        threads = [
            threading.Thread(target=self.jsonl_loop, daemon=True, name="clb-jsonl"),
            threading.Thread(target=self.heartbeat_loop, daemon=True, name="clb-egress-heartbeat"),
        ]
        for thread in threads:
            thread.start()
        try:
            self.telegram_loop()
        finally:
            self.stop_event.set()
            self.stop_typing()
            self.release_lock()


def handle_stop_signal(bridge: Bridge, signum: int) -> None:
    bridge.stop_event.set()
    bridge.release_lock()
    raise SystemExit(0)


def health_check_main() -> int:
    try:
        config = Config.from_env()
        repl = ClaudeRepl(config)
        repl.verify()
        binder = SessionBinder(config, repl)
        transcript_pending = False
        try:
            binding = binder.resolve()
        except RuntimeError:
            pending = binder._resolve_fresh_sidecar_metadata(repl.pane_pid())
            if len(pending) != 1:
                raise
            binding = pending[0]
            transcript_pending = not binding.transcript_path.exists()
        payload = {
            "ok": True,
            "node": config.node,
            "transcript_path": str(binding.transcript_path),
            "transcript_exists": binding.transcript_path.exists(),
            "transcript_pending": transcript_pending,
            "sessionId": binding.session_id,
            "pane_pid": binding.pane_pid,
            "session_sidecar": str(config.session_sidecar_path),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "node": node_defaults()[0],
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 20


def validate_startup_config(config: "Config") -> None:
    # T-260702-42: 공개 export 의 chat_id 기본값은 "" — 검증 없이 기동하면 어떤
    # 채팅도 매칭 못 하는 데몬이 조용히 떠서 영원히 폴링한다 (silent fail).
    chat = str(config.chat_id or "").strip()
    if not re.fullmatch(r"-?\d+", chat):
        raise ValueError(
            "CLB_CHAT_ID is required and must be a numeric Telegram chat id "
            f"(got {chat!r}). Message your bot once, read the chat id from "
            "getUpdates, then set CLB_CHAT_ID before starting the bridge."
        )


def enqueue_voice_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Enqueue a Jarvis voice question into the Claude Telegram bridge queue.")
    parser.add_argument("--question", required=True)
    parser.add_argument("--voice-reply-path", required=True)
    parser.add_argument("--voice-request-id", default="")
    args = parser.parse_args(argv)
    config = Config.from_env()
    result = enqueue_voice_prompt(
        config,
        question=args.question,
        reply_path=Path(args.voice_reply_path).expanduser(),
        request_id=args.voice_request_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"enqueued", "duplicate"} else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--health-check":
        return health_check_main()
    if len(sys.argv) > 1 and sys.argv[1] == "--enqueue-voice":
        return enqueue_voice_main(sys.argv[2:])
    try:
        config = Config.from_env()
        validate_startup_config(config)
        token = load_token(config.token_file)
        bridge = Bridge(
            config,
            TelegramClient(token, config.chat_id, config.emoji, config.telegram_chunk),
            ClaudeRepl(config),
            token,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    def stop(signum: int, _frame: Any) -> None:
        handle_stop_signal(bridge, signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)

    log(
        "START",
        f"node={config.node} chat={config.chat_id} tmux={config.tmux_socket}/{config.tmux_session}",
    )
    try:
        bridge.run()
    except Exception as exc:  # noqa: BLE001
        bridge.release_lock()
        print(f"runtime error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
