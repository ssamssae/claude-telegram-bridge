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

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


HOME = Path.home()
NODE_EMOJI_LINES = {"\U0001f34e", "\U0001f3ed", "\U0001fa9f", "\U0001f5a5", "\U0001f4bb", "\U0001f916"}
BRIDGE_OWNER = "claude-telegram-bridge"
ALLOWED_SLASH_COMMANDS = {"/ping", "/start", "/status"}
MCP_TELEGRAM_REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
BRACKETED_PASTE_RE = re.compile(r"\x1b\[(?:200|201)~")
NONCE_RE = re.compile(r"clb-[0-9a-f]{24,64}")
REASONING_HEADER = "\U0001f9e0 클로드 사고"
REASONING_MIRROR_LIMIT = 3500
APPROVAL_WAIT_RE = re.compile(
    r"\b(approval|do you want|would you like|allow(?:\s+this|\s+command)?|"
    r"approve|permission prompt)\b",
    re.IGNORECASE,
)
HOOK_BLOCK_RE = re.compile(
    r"\b(hook\s+(?:blocked|denied|failed)|blocked\s+by\s+hook|"
    r"permission\s+denied\s+by\s+hook|pretooluse\s+(?:blocked|denied|failed))\b",
    re.IGNORECASE,
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".weba"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def release_hold_response(text: str) -> str | None:
    if not re.match(r"^\s*출시\s*멈춰\s+\S+", text or ""):
        return None
    helper = Path(__file__).resolve().parent / "asc-release-hold.sh"
    try:
        proc = subprocess.run(
            [str(helper), text],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"출시 보류 신호 기록 실패: {exc}"
    if proc.returncode == 0:
        return (proc.stdout or "출시 보류 신호 기록됨").strip()
    if proc.returncode == 2:
        return None
    detail = (proc.stderr or proc.stdout or "").strip()
    return f"출시 보류 신호 기록 실패: {detail[:200]}"


def int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def bool_env(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    return (env(name, fallback) or fallback).lower() in {"1", "true", "yes", "on"}


def now_ts() -> str:
    return time.strftime("%H:%M:%S")


def log(label: str, message: str) -> None:
    print(f"[{now_ts()}] {label:<6} {message}", flush=True)


def node_defaults() -> tuple[str, str]:
    return "claude", "\U0001f916"


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


def write_text_atomic(path: Path, value: str | int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(value), encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
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


def sanitize_text(text: str, limit: int = 12000) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = BRACKETED_PASTE_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + "\n\n[truncated by claude-telegram-bridge]"
    return text


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
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return ""
    return stripped.split(maxsplit=1)[0].split("@", 1)[0].lower()


def escape_unsafe_slash(text: str) -> str:
    token = slash_token(text)
    if not token or token in ALLOWED_SLASH_COMMANDS:
        return text
    stripped = text.lstrip()
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
    if not body:
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


class TelegramClient:
    def __init__(self, token: str, chat_id: str, emoji: str, chunk_size: int) -> None:
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.emoji = emoji
        self.chunk_size = chunk_size

    def call(self, method: str, **params: Any) -> dict[str, Any] | None:
        data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(f"{self.api}/{method}", data=data)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                return payload if isinstance(payload, dict) else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 409:
                    raise TelegramHTTPError(method, exc.code, body) from exc
                if attempt == 2:
                    log("TGERR", f"{method} failed: HTTP {exc.code} {body[:200]}")
                    return None
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log("TGERR", f"{method} failed: {exc}")
                    return None
            time.sleep(2)
        return None

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
        with urllib.request.urlopen(request, timeout=60) as response:
            output_path.write_bytes(response.read())
        return output_path

    def with_emoji_prefix(self, text: str) -> str:
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        if first_line in NODE_EMOJI_LINES:
            return text
        text = strip_inline_node_emoji_header(text)
        return f"{self.emoji}\n{text}"

    def chunks(self, text: str) -> list[str]:
        text = self.with_emoji_prefix(text or "(empty response)")
        chunks = [text[: self.chunk_size]]
        rest = text[self.chunk_size :]
        while rest:
            chunks.append(rest[: self.chunk_size])
            rest = rest[self.chunk_size :]
        return chunks

    def send(self, text: str) -> list[int] | None:
        message_ids: list[int] = []
        for chunk in self.chunks(text):
            payload = self.call("sendMessage", chat_id=self.chat_id, text=chunk)
            if not payload or not payload.get("ok"):
                return None
            result = payload.get("result")
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                message_ids.append(int(result["message_id"]))
        return message_ids


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
    token_owner: str
    expected_consumer: str
    expected_host: str
    session_ttl_seconds: int
    egress_ttl_seconds: int
    turn_sequence_fallback_seconds: float
    transcript_stable_seconds: float
    composer_clear_retries: int
    injection_verify_timeout: float
    send_retry_seconds: float
    send_max_attempts: int
    queue_compact_max_events: int
    outbox_max_entries: int

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
            token_owner=env("CLB_TOKEN_OWNER", BRIDGE_OWNER) or BRIDGE_OWNER,
            expected_consumer=env("CLB_EXPECTED_CONSUMER", node) or node,
            expected_host=env("CLB_EXPECTED_HOST", os.uname().nodename) or os.uname().nodename,
            session_ttl_seconds=int_env("CLB_SESSION_TTL_SECONDS", 86400, minimum=60),
            egress_ttl_seconds=int_env("CLB_EGRESS_TTL_SECONDS", 900, minimum=60),
            turn_sequence_fallback_seconds=float(env("CLB_TURN_SEQUENCE_FALLBACK_SECONDS", "7200") or "7200"),
            transcript_stable_seconds=float(env("CLB_TRANSCRIPT_STABLE_SECONDS", "1.0") or "1.0"),
            composer_clear_retries=int_env("CLB_COMPOSER_CLEAR_RETRIES", 2, minimum=1),
            injection_verify_timeout=float(env("CLB_INJECTION_VERIFY_TIMEOUT", "20") or "20"),
            send_retry_seconds=float(env("CLB_SEND_RETRY_SECONDS", "5") or "5"),
            send_max_attempts=int_env("CLB_SEND_MAX_ATTEMPTS", 3, minimum=1),
            queue_compact_max_events=int_env("CLB_QUEUE_COMPACT_MAX_EVENTS", 5000, minimum=100),
            outbox_max_entries=int_env("CLB_OUTBOX_MAX_ENTRIES", 2000, minimum=100),
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

    def to_json(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "update_id": self.update_id,
            "message_id": self.message_id,
            "text": self.text,
            "nonce": self.nonce,
            "received_at": self.received_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "QueueItem":
        return cls(
            queue_id=str(payload["queue_id"]),
            update_id=int(payload["update_id"]),
            message_id=int(payload.get("message_id") or 0),
            text=str(payload["text"]),
            nonce=str(payload["nonce"]),
            received_at=float(payload.get("received_at") or time.time()),
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

    def to_json(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "update_id": self.update_id,
            "message_id": self.message_id,
            "nonce": self.nonce,
            "injected_at": self.injected_at,
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
        }

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

    def pane_pid(self) -> int:
        out = self.tmux("display-message", "-p", "-t", self.resolve_pane_target(), "#{pane_pid}")
        raw = out.stdout.strip()
        if not raw.isdigit():
            raise RuntimeError(f"could not resolve pane pid: {raw!r}")
        return int(raw)

    def pane_tty(self) -> str:
        out = self.tmux("display-message", "-p", "-t", self.resolve_pane_target(), "#{pane_tty}")
        return out.stdout.strip()

    def capture_pane(self, lines: int = 80) -> str:
        out = self.tmux(
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{max(1, lines)}",
            "-t",
            self.resolve_pane_target(),
        )
        return out.stdout

    def clear_composer(self) -> None:
        self.verify()
        for key in ("Escape", "C-e", "C-u", "C-a", "C-k"):
            self.tmux("send-keys", "-t", self.resolve_pane_target(), key)
            time.sleep(0.05)

    def paste_prompt(self, prompt: str) -> None:
        if not prompt.strip():
            return
        if BRACKETED_PASTE_RE.search(prompt):
            raise RuntimeError("prompt contains bracketed paste control sequences")
        self.verify()
        self.tmux("load-buffer", "-", input_text=prompt.rstrip("\n"))
        self.tmux("paste-buffer", "-p", "-t", self.resolve_pane_target())
        time.sleep(0.1)
        self.tmux("send-keys", "-t", self.resolve_pane_target(), "Enter")


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


class SessionBinder:
    def __init__(self, config: Config, repl: ClaudeRepl) -> None:
        self.config = config
        self.repl = repl

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
            raise RuntimeError("no SessionStart sidecar entry for tmux pane; proc-fd and pane-tty fallback found none")
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

        scored_matches: list[tuple[bool, float, ClaudeSessionBinding]] = []
        for item in values:
            transcript_raw = str(item.get("transcript_path") or "")
            if not transcript_raw:
                continue
            transcript = Path(transcript_raw).expanduser()
            try:
                item_pid = int(item.get("pane_pid") or 0)
            except (TypeError, ValueError):
                item_pid = 0
            if item_pid != pane_pid:
                continue
            updated_at = float(item.get("updated_at") or 0)
            session_id = str(item.get("sessionId") or item.get("session_id") or "")
            if not session_id:
                if not transcript.exists():
                    continue
                session_id = session_id_from_transcript(transcript)
            binding = ClaudeSessionBinding(transcript.resolve(), session_id, pane_pid)
            fresh = bool(updated_at and time.time() - updated_at <= self.config.session_ttl_seconds)
            scored_matches.append((fresh, updated_at, binding))
        scored_matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if scored_matches and scored_matches[0][1] > 0:
            return [scored_matches[0][2]]
        return [item[2] for item in scored_matches]

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

    def mark_sent(self, key: str, sent_message_ids: list[int]) -> None:
        with self.lock:
            self.sent[key] = {"ts": time.time(), "sent_message_ids": sent_message_ids}
            if self.max_entries > 0 and len(self.sent) > self.max_entries:
                ordered = sorted(
                    self.sent.items(),
                    key=lambda pair: float(pair[1].get("ts") or 0) if isinstance(pair[1], dict) else 0,
                )
                self.sent = dict(ordered[-self.max_entries :])
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
        self.pending: list[QueueItem] = []
        self.active_turn: ActiveTurn | None = None
        self.last_transcript_mtime = 0.0
        self.last_jsonl_read_at = 0.0
        self.last_jsonl_watch_error = ""
        self.last_jsonl_watch_error_log_at = 0.0

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
        active = (state or {}).get("active_turn")
        if isinstance(active, dict):
            try:
                self.active_turn = ActiveTurn.from_json(active)
            except (KeyError, TypeError, ValueError):
                self.active_turn = None

    def binding_payload(self) -> dict[str, Any]:
        binding = self.session_binding
        if not binding:
            return {}
        return {
            "transcript_path": str(binding.transcript_path),
            "sessionId": binding.session_id,
            "pane_pid": binding.pane_pid,
        }

    def ensure_session_binding(self) -> ClaudeSessionBinding:
        binding = self.binder.resolve()
        if self.session_binding != binding:
            identity = session_identity(binding.transcript_path)
            self.session_binding = binding
            self.session_identity = identity
            self.load_state_for_identity(identity)
            log("SESSION", f"watching {binding.transcript_path} offset={self.session_pos}")
            self.persist_state()
            self.write_egress_sidecar()
        return binding

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
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self.telegram.send_typing()
                wait_seconds = 4.0
                if deadline is not None:
                    wait_seconds = min(wait_seconds, max(0.0, deadline - time.monotonic()))
                if wait_seconds <= 0:
                    break
                stop_event.wait(wait_seconds)

        threading.Thread(target=loop, daemon=True, name="clb-typing").start()
        return stop_event

    def begin_typing(self) -> None:
        with self.typing_lock:
            if self.typing_stop:
                self.typing_stop.set()
            self.typing_stop = self.start_typing_loop(self.config.typing_max_seconds)

    def stop_typing(self) -> None:
        with self.typing_lock:
            if self.typing_stop:
                self.typing_stop.set()
                self.typing_stop = None

    def busy_state(self) -> str:
        with self.lock:
            if self.active_turn:
                return "generating"
        binding = self.session_binding
        if binding:
            try:
                if time.time() - binding.transcript_path.stat().st_mtime < self.config.transcript_stable_seconds:
                    return "generating"
            except OSError:
                return "hook_blocked"
        try:
            screen = self.repl.capture_pane(80)
        except Exception:
            return "hook_blocked"
        if screen_has_approval_wait(screen):
            return "approval_wait"
        if screen_has_hook_block(screen):
            return "hook_blocked"
        return "idle"

    def session_occupied_excluding_active(self) -> bool:
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
                return True
        try:
            screen = self.repl.capture_pane(80)
        except Exception:  # noqa: BLE001
            return True
        if screen_has_approval_wait(screen):
            return True
        if screen_has_hook_block(screen):
            return True
        return False

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
        return (
            f"<claude-telegram-bridge nonce=\"{item.nonce}\" "
            f"update_id=\"{item.update_id}\" message_id=\"{item.message_id}\">\n"
            "Telegram-origin prompt. Do not mention this bridge envelope or nonce in the answer.\n"
            "</claude-telegram-bridge>\n\n"
            f"{safe_text}"
        )

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
        try:
            text = self.prompt_from_telegram_message(message, update_id)
        except Exception as exc:  # noqa: BLE001
            caption = message.get("caption")
            caption_text = caption.strip() if isinstance(caption, str) else ""
            detail = str(exc).replace(self.token, "<redacted-token>")
            if caption_text:
                self.telegram.send(f"media 처리 실패: {detail}. caption만 전달합니다.")
                text = caption_text
            else:
                self.telegram.send(f"media 처리 실패: {detail}")
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
        if command in {"/start", "/ping"}:
            self.telegram.send("claude-telegram-bridge running")
            return
        if command == "/status":
            self.telegram.send(f"claude bridge status: {self.busy_state()}")
            return

        message_id = int(message.get("message_id") or 0)
        queue_id = message_update_key(update, self.token_hash)
        nonce = f"clb-{secrets.token_hex(16)}"
        item = QueueItem(queue_id=queue_id, update_id=update_id, message_id=message_id, text=text, nonce=nonce)
        with self.lock:
            active_queue_id = self.active_turn.queue_id if self.active_turn else ""
            pending_queue_ids = {existing.queue_id for existing in self.pending}
        existing_status = self.queue.status(queue_id)
        if queue_id == active_queue_id or queue_id in pending_queue_ids or existing_status:
            log("QUEUE", f"skip duplicate update={update_id} queue={queue_id[:10]} status={existing_status or 'live'}")
            return
        self.queue.append_status(item, "received")
        with self.lock:
            active_queue_id = self.active_turn.queue_id if self.active_turn else ""
            if item.queue_id == active_queue_id or any(existing.queue_id == item.queue_id for existing in self.pending):
                return
            self.pending.append(item)
            self.queue.append_status(item, "enqueued")
        log("QUEUE", f"enqueued update={update_id} queue={queue_id[:10]}")

    def drain_queue(self) -> None:
        state = self.busy_state()
        if state != "idle":
            log("BUSY", f"skip inject state={state}")
            return
        with self.lock:
            if self.active_turn or not self.pending:
                return
            item = self.pending.pop(0)
            self.active_turn = ActiveTurn(
                queue_id=item.queue_id,
                update_id=item.update_id,
                message_id=item.message_id,
                nonce=item.nonce,
                injected_at=time.time(),
                text=item.text,
            )
        self.persist_state()
        self.write_egress_sidecar()
        prompt = self.envelope_prompt(item)
        try:
            for _ in range(self.config.composer_clear_retries):
                self.repl.clear_composer()
            self.repl.paste_prompt(prompt)
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"failed: {exc}")
            self.queue.append_status(item, "failed", error=str(exc))
            with self.lock:
                if self.active_turn and self.active_turn.queue_id == item.queue_id:
                    self.active_turn = None
            self.telegram.send(f"claude bridge delivery failed: {exc}")
            self.persist_state()
            self.write_egress_sidecar()
            return

        with self.lock:
            if self.active_turn and self.active_turn.queue_id == item.queue_id:
                self.active_turn.injected_at = time.time()
        self.queue.append_status(item, "injected")
        self.persist_state()
        self.write_egress_sidecar()
        self.begin_typing()
        log("INJECT", f"nonce={item.nonce} update={item.update_id}")

    def check_injection_timeout(self) -> None:
        with self.lock:
            active = self.active_turn
        if not active or active.user_uuid:
            return
        if time.time() - active.injected_at < self.config.injection_verify_timeout:
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

        item = QueueItem(
            active.queue_id,
            active.update_id,
            active.message_id,
            active.text,
            active.nonce,
            active.injected_at,
        )
        if active.inject_attempts >= 2:
            log("INJECT", f"nonce {active.nonce} not observed in JSONL after retry")
            self.queue.append_status(item, "failed", error="nonce user JSONL not observed")
            with self.lock:
                self.active_turn = None
            self.stop_typing()
            self.telegram.send("메시지를 노드에 전달하지 못했어요. 한 번 더 보내주세요. (세션이 응답 중이면 끝난 뒤 자동 전달됩니다)")
            self.persist_state()
            self.write_egress_sidecar()
            return

        log("INJECT", f"nonce {active.nonce} not observed; composer clear/retry")
        try:
            self.repl.clear_composer()
            self.repl.paste_prompt(self.envelope_prompt(item))
        except Exception as exc:  # noqa: BLE001
            log("INJECT", f"retry failed: {exc}")
            self.queue.append_status(item, "failed", error=f"retry failed: {exc}")
            with self.lock:
                self.active_turn = None
            self.stop_typing()
            self.telegram.send(f"claude bridge delivery retry failed: {exc}")
            self.persist_state()
            self.write_egress_sidecar()
            return

        active.inject_attempts += 1
        active.injected_at = time.time()
        self.queue.append_status(item, "injected_retry", attempt=active.inject_attempts)
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
        )

    def send_active_answer(self, active: ActiveTurn, assistant_uuid: str, answer: str) -> None:
        claim = self.claim_send_attempt(active, assistant_uuid, answer)
        if claim == "outbox_sent":
            log("SEND", "skip duplicate outbox key")
            self.finish_active_turn("sent")
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

    def claim_retry_send_attempt(self) -> tuple[ActiveTurn, str, str, str] | str | None:
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
                return "outbox_sent"
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
        try:
            if copy_payload_messages:
                sent_ids = []
                for message in copy_payload_messages:
                    part_ids = self.telegram.send(message)
                    if part_ids is None:
                        sent_ids = None
                        break
                    sent_ids.extend(part_ids)
            else:
                sent_ids = self.telegram.send(answer)
        except Exception as exc:  # noqa: BLE001
            send_error = str(exc)
            sent_ids = None
        item = self.queue_item_for_active(active)
        if sent_ids is None:
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
        # 🧠 reasoning mirror — sent once, right after the deduped final answer
        # (sibling of codex-repl-telegram-bridge's 🧠 코덱스 사고). Empty/no-thinking
        # turns produce no block. Failures here never affect answer delivery.
        reasoning = None if copy_payload_messages else active.pending_reasoning
        active.pending_reasoning = None
        if reasoning:
            mirror = format_reasoning_mirror(reasoning)
            if mirror:
                try:
                    self.telegram.send(mirror)
                    log("SEND", f"sent reasoning mirror nonce={active.nonce} len={len(mirror)}")
                except Exception as exc:  # noqa: BLE001
                    log("SEND", f"reasoning mirror send failed (non-fatal): {exc}")
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
        self.finish_active_turn("sent")

    def retry_pending_send(self) -> None:
        claim = self.claim_retry_send_attempt()
        if claim == "outbox_sent":
            log("SEND", "skip duplicate outbox key")
            self.finish_active_turn("sent")
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

        if record_type == "user" and message.get("role") == "user":
            nonce = record_contains_nonce(record)
            if nonce and self.active_turn and nonce == self.active_turn.nonce:
                self.active_turn.user_uuid = str(record.get("uuid") or "")
                self.active_turn.user_seen_at = record_timestamp_seconds(record) or time.time()
                self.queue.append_status(
                    QueueItem(
                        self.active_turn.queue_id,
                        self.active_turn.update_id,
                        self.active_turn.message_id,
                        self.active_turn.text,
                        self.active_turn.nonce,
                        self.active_turn.injected_at,
                    ),
                    "user_jsonl_seen",
                    user_uuid=self.active_turn.user_uuid,
                )
                self.persist_state()
                self.write_egress_sidecar()
                log("JSONL", f"user nonce seen {nonce}")
            return

        if record_type != "assistant" or message.get("role") != "assistant":
            return
        active = self.ancestor_matches_active_turn(record.get("parentUuid")) or self.sequence_matches_active_turn(record)
        if not active:
            return

        if content_has_tool(content, MCP_TELEGRAM_REPLY_TOOL):
            active.external_reply_seen = True
            self.persist_state()
            log("EGRESS", "MCP telegram reply tool_use seen; suppress bridge duplicate")
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
            return
        if record.get("isSidechain") is not False:
            return
        answer = sanitize_text(content_text(content), limit=16000)
        if not answer:
            return
        if active.external_reply_seen:
            log("SEND", "skip final because external reply tool was seen")
            self.finish_active_turn("answered")
            return
        assistant_uuid = str(record.get("uuid") or "")
        reasoning = active.accumulated_reasoning or content_thinking(content)
        active.pending_reasoning = sanitize_text(reasoning, limit=REASONING_MIRROR_LIMIT) or None
        self.send_active_answer(active, assistant_uuid, answer)

    def finish_active_turn(self, status: str) -> None:
        with self.lock:
            active = self.active_turn
            self.active_turn = None
        self.stop_typing()
        if active:
            self.queue.append_status(
                QueueItem(
                    active.queue_id,
                    active.update_id,
                    active.message_id,
                    active.text,
                    active.nonce,
                    active.injected_at,
                ),
                status,
            )
        self.persist_state()
        self.write_egress_sidecar()
        self.drain_queue()

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
            self.stop_event.wait(0.5)

    def telegram_loop(self) -> None:
        offset_raw = read_text(self.config.offset_file)
        offset = int(offset_raw) if offset_raw.isdigit() else 0
        TokenOwnership(self.config, self.telegram, self.token).verify_or_die(offset)
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
                self.enqueue_update(update)
                offset = update_id + 1
                write_text_atomic(self.config.offset_file, offset)
                self.drain_queue()
            self.check_injection_timeout()
            self.retry_pending_send()
            self.drain_queue()

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


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--health-check":
        return health_check_main()
    try:
        config = Config.from_env()
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
