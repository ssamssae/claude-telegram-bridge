"""Human-only replies to an explicit GO request in a current final answer."""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path


PREFIX = "clb-go::"
TTL = 3600
GO_REQUEST = re.compile(r"(?i)(?<!\w)(?:go|고)(?:를|라고)?\s*(?:주시면|주세요|해\s*주시면|라고\s*답해)")


def go_request(answer):
    if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 3200:
        return ""
    text = answer.strip()
    tail = text.split("\n\n")[-1]
    if ("```" in text or any(line.lstrip().startswith((">", "※ recap:")) for line in tail.splitlines())
            or not GO_REQUEST.search(tail)):
        return ""
    return text


def current_source(bridge, assistant_uuid, question=None):
    """Read the latest native record; system summaries never create a request."""
    binding = getattr(bridge, "session_binding", None)
    if not binding or not isinstance(assistant_uuid, str) or not assistant_uuid:
        return False
    try:
        path = Path(binding.transcript_path)
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 131072))
            if handle.tell():
                handle.readline()
            raw = handle.read(131072)
        if not raw.endswith(b"\n"):
            return False
        for line in reversed(raw.splitlines()):
            record = json.loads(line)
            if not isinstance(record, dict):
                return False
            if record.get("type") not in {"user", "assistant"}:
                continue
            content = (record.get("message") or {}).get("content")
            native = "\n".join(block.get("text", "") for block in content
                               if isinstance(block, dict) and block.get("type") == "text") if isinstance(content, list) else ""
            return bool(record.get("type") == "assistant"
                        and record.get("sessionId") == binding.session_id
                        and record.get("uuid") == assistant_uuid
                        and not record.get("isSidechain")
                        and isinstance(content, list)
                        and not any(block.get("type") == "tool_use" for block in content if isinstance(block, dict))
                        and go_request(native)
                        and (question is None or question == native.strip()))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return False


def clear_buttons(bridge, card):
    try:
        bridge.telegram.call("editMessageReplyMarkup", chat_id=bridge.config.chat_id,
                             message_id=card["message_id"], reply_markup=json.dumps({"inline_keyboard": []}))
    except Exception:
        pass  # A visible stale button remains inert; never replay the reply.


def expire_if_changed(bridge):
    card = getattr(bridge, "go_question_card", None)
    if not isinstance(card, dict) or card.get("status") != "open":
        return
    binding = getattr(bridge, "session_binding", None)
    if (not binding or card.get("session_id") != binding.session_id
            or not 0 <= time.time() - card.get("created_at", 0) < TTL
            or not current_source(bridge, card.get("assistant_uuid"), card.get("question"))):
        card["status"] = "expired"
        bridge.persist_state()
        clear_buttons(bridge, card)


def send_card(bridge, answer, assistant_uuid, reply_to):
    question = go_request(answer)
    if not question or type(reply_to) is not int or reply_to <= 0 or not str(bridge.config.chat_id).isdigit():
        return False
    if (getattr(bridge, "active_turn", None) or getattr(bridge, "pending", [])
            or not current_source(bridge, assistant_uuid, question)):
        return False
    previous = getattr(bridge, "go_question_card", None)
    if isinstance(previous, dict) and previous.get("assistant_uuid") == assistant_uuid:
        return False
    expire_if_changed(bridge)
    token = secrets.token_hex(8)
    response = bridge.telegram.call(
        "sendMessage", chat_id=bridge.config.chat_id, reply_to_message_id=reply_to,
        text=bridge.tr('위 요청의 진행 여부를 선택해 주세요. 직접 답변을 보내셔도 됩니다.'),
        reply_markup=json.dumps({"inline_keyboard": [[
            {"text": "고", "callback_data": PREFIX + token + "::go"},
            {"text": "보류", "callback_data": PREFIX + token + "::hold"},
        ]]}, ensure_ascii=False),
    )
    mid = (response.get("result") or {}).get("message_id") if isinstance(response, dict) else None
    if not isinstance(response, dict) or response.get("ok") is not True or type(mid) is not int or mid <= 0:
        return False
    bridge.go_question_card = {"token": token, "status": "open", "message_id": mid,
                               "session_id": bridge.session_binding.session_id,
                               "assistant_uuid": assistant_uuid, "question": question,
                               "created_at": time.time()}
    bridge.persist_state()
    return True


def callback(bridge, query, update_id):
    data = str(query.get("data") or "")
    if not data.startswith(PREFIX):
        return False
    toast = '만료되었거나 이미 답한 요청입니다.'
    try:
        with bridge.lock:
            expire_if_changed(bridge)
            card = getattr(bridge, "go_question_card", None)
            message = query.get("message") or {}
            binding = getattr(bridge, "session_binding", None)
            action = data.rsplit("::", 1)[-1]
            valid = bool(
                isinstance(card, dict) and card.get("status") == "open"
                and action in {"go", "hold"} and data == PREFIX + card.get("token", "") + "::" + action
                and str((message.get("chat") or {}).get("id")) == str(bridge.config.chat_id)
                and str((query.get("from") or {}).get("id")) == str(bridge.config.chat_id)
                and message.get("message_id") == card.get("message_id")
                and binding and card.get("session_id") == binding.session_id
                and 0 <= time.time() - card.get("created_at", 0) < TTL
                and not getattr(bridge, "active_turn", None) and not getattr(bridge, "pending", [])
                and getattr(bridge, "latest_human_input_at", 0) < card["created_at"]
                and current_source(bridge, card.get("assistant_uuid"), card.get("question"))
            )
            if valid:
                # Persist the one-shot claim before passing through the normal queue.
                # A crash after this point must not turn a second tap into another GO.
                card["status"] = "claimed"
                bridge.persist_state()
                answer = "고. 이 요청을 진행해 주세요." if action == "go" else "이 요청을 보류해 주세요."
                prompt = answer + "\n\n제가 답한 요청:\n" + card["question"]
                queued = bridge.enqueue_update({"update_id": update_id, "message": {
                    "message_id": card["message_id"], "chat": {"id": bridge.config.chat_id},
                    "from": query["from"], "date": int(time.time()), "text": prompt,
                }})
                if not queued or getattr(queued, "update_id", None) != update_id:
                    raise RuntimeError("reply queue receipt missing")
                card["status"] = "answered"
                bridge.persist_state()
                clear_buttons(bridge, card)
                toast = '선택한 답변을 대기열에 넣었어요.'
        if valid:
            bridge.drain_queue()
    except Exception:
        toast = '답변 전달을 확인하지 못했어요. 현재 대화를 확인해 주세요.'
    try:
        bridge.telegram.call("answerCallbackQuery", callback_query_id=query.get("id"), text=bridge.tr(toast))
    except Exception:
        pass
    return True
