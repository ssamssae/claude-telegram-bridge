"""Telegram notice when the bound Claude session's model changes (T-261021-001).

Two triggers, both read from the session transcript the bridge already tails:
1. ``system`` row ``subtype=model_refusal_fallback`` — Fable's safeguards refused a
   message and Claude Code retried it on a fallback model, latching the whole
   session (``scope: session``) to that model.
2. Consecutive main-chain ``assistant`` rows whose ``message.model`` differ —
   covers any other switch, including the owner switching back.

The bridge never restores the model by itself (2026-10-21 사용자 결정: 「내가
판단하고 바꾸게」). The notice carries the ``/model`` command that returns to the
model written in ``~/.claude/settings.json`` so a restore keeps that exact value
(e.g. ``claude-fable-5-1[1m]`` — ``/model fable`` would overwrite the 1M default).

Notice text must stay clear of the node-room outbound drop filter words
(메일·오류·✅·❌·PASS·노드보고·오픈챗); ``test_claude_model_notice`` pins that.
"""
import json
from pathlib import Path

REFUSAL_SUBTYPE = "model_refusal_fallback"
SETTINGS_PATH = Path.home() / ".claude/settings.json"
NOTIFIED_CAP = 50
DEFAULT_RESTORE = "fable"


def load(payload):
    """Sanitize the persisted state (dict or None) into the in-memory shape."""
    last_model = payload.get("last_model") if isinstance(payload, dict) else None
    notified = payload.get("notified") if isinstance(payload, dict) else None
    if not isinstance(last_model, str) or not last_model:
        last_model = None
    if not isinstance(notified, list):
        notified = []
    notified = [item for item in notified if isinstance(item, str) and item][-NOTIFIED_CAP:]
    return {"last_model": last_model, "notified": notified}


def state(bridge):
    current = getattr(bridge, "model_notice", None)
    if not isinstance(current, dict) or set(current) != {"last_model", "notified"}:
        current = load(current)
        bridge.model_notice = current
    return current


def pretty(model):
    """``claude-fable-5-1[1m]`` → ``Fable 5.1 (1M)``; unknown ids are returned as-is."""
    if not isinstance(model, str) or not model:
        return "?"
    name = model
    suffix = ""
    if name.endswith("[1m]"):
        name = name[:-4]
        suffix = " (1M)"
    if name.startswith("claude-"):
        name = name[len("claude-"):]
    parts = name.split("-")
    family = parts[0]
    if family not in {"fable", "opus", "sonnet", "haiku"}:
        return model
    digits = [part for part in parts[1:] if part.isdigit()]
    version = ".".join(digits)
    return family.capitalize() + (" " + version if version else "") + suffix


def restore_target():
    """Model id to put after ``/model`` — the configured default, else ``fable``."""
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError, AttributeError):
        value = None
    return value if isinstance(value, str) and value.strip() else DEFAULT_RESTORE


def _main_chain_row(bridge, record):
    binding = getattr(bridge, "session_binding", None)
    if not isinstance(record, dict) or binding is None:
        return False
    if record.get("sessionId") != getattr(binding, "session_id", None):
        return False
    if record.get("isSidechain") or record.get("agentId") or record.get("agent_id"):
        return False
    return True


def _send(bridge, text, log, what):
    ids = bridge.telegram.send(bridge.tr(text))
    if ids:
        if log:
            log("MODEL", f"{what} notified message_id={ids[0]}")
        return True
    if log:
        log("MODEL", f"{what} notice send failed: {getattr(bridge.telegram, 'last_send_error', '') or '-'}")
    return False


def _observe(bridge, record, log):
    if not _main_chain_row(bridge, record):
        return False
    current = state(bridge)
    if record.get("type") == "system" and record.get("subtype") == REFUSAL_SUBTYPE:
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or not uuid or uuid in current["notified"]:
            return False
        original = record.get("originalModel")
        fallback = record.get("fallbackModel")
        category = record.get("apiRefusalCategory") or "?"
        text = (
            f"⚠️ 모델 자동 전환: {pretty(original)} → {pretty(fallback)}\n"
            f"사유: {pretty(original)} 안전장치가 메시지를 거부 [{category}] — 넓은 안전장치라 정상 작업도 걸립니다.\n"
            f"이 세션은 앞으로 {pretty(fallback)} 로 답합니다. 자동 복구는 하지 않습니다.\n"
            f"되돌리기: /model {restore_target()}"
        )
        if not _send(bridge, text, log, f"refusal fallback {original}->{fallback} [{category}]"):
            return False
        current["notified"] = (current["notified"] + [uuid])[-NOTIFIED_CAP:]
        if isinstance(fallback, str) and fallback:
            current["last_model"] = fallback
        bridge.persist_state()
        return True
    if record.get("type") != "assistant":
        return False
    message = record.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    if not isinstance(model, str) or not model or model.startswith("<"):
        return False
    previous = current["last_model"]
    if previous == model:
        return False
    current["last_model"] = model
    bridge.persist_state()
    if previous is None:
        return False
    text = (
        f"ℹ️ 모델 변경 감지: {pretty(previous)} → {pretty(model)}\n"
        f"설정 기본 모델: {pretty(restore_target())} · 되돌리기: /model {restore_target()}"
    )
    return _send(bridge, text, log, f"model change {previous}->{model}")


def observe(bridge, record, log=None):
    """Never raises: a notice must not stall the transcript loop."""
    try:
        return _observe(bridge, record, log)
    except Exception as exc:  # noqa: BLE001
        if log:
            log("MODEL", f"notice skipped: {type(exc).__name__}: {exc}")
        return False
