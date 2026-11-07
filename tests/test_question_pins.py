#!/usr/bin/env python3
# @mutate claude_telegram_bridge.py | self.sync_question_pins() | pass
# @mutate-why 질문 화면 폴링에서 빠지면 새 기록이 없는 동안 고정·만료 해제가 실행되지 않는다.
# @mutate claude_telegram_bridge.py | open_card = card is not None and not card.get("completion")  # pin follows the native answer | open_card = card is not None  # pin follows the native answer
# @mutate-why 응답한 질문의 고정을 유지하면 미응답 질문이 남았다는 표시가 흐려진다.
"""T-261107-009 — 미응답 Claude 질문 카드를 카드 중복 없이 상단 고정으로 닿게 한다.

코덱스 브릿지 PR #2745(T-261107-007)와 같은 의미: 열린 카드의 원본 메시지를 소유자
개인 대화에서 조용히 고정하고, 답·만료·세션 변경이면 해제한다. 고정은 표시일 뿐
답변·권한이 아니다. 틱당 네트워크 호출 1회, 행위별 재시도 60초 간격 최대 3회, 소유
필드는 호출 전에 저장한다. 고정 장부(question_pins)는 카드 목록과 따로 산다.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("claude_bridge_question_pins_test", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge_mod = load_bridge()
Bridge = bridge_mod.Bridge
TTL = bridge_mod.QUESTION_PIN_TTL_SECONDS
RETRY = bridge_mod.QUESTION_PIN_RETRY_SECONDS
MAX_ATTEMPTS = bridge_mod.QUESTION_PIN_MAX_ATTEMPTS

NOW = 1789220000.0
SESSION = "session-a"
PIN_FIELDS = ("pin_managed", "pin_confirmed", "pin_action", "pin_attempts", "pin_retry_at")


class Telegram:
    """Records every Bot API call; pin state mirrors what Telegram would hold."""

    chat_id = "123"

    def __init__(self):
        self.calls = []
        self.budgets = []
        self.pinned = {999}  # A user's unrelated pinned message.
        self.fail = None          # method name that raises after the operation
        self.reject = None        # method name that answers ok:false
        self.unconfirmed = None   # method name that answers ok:true, result:false
        self.on_call = None       # hook(method, kwargs) run before the response
        self.last_send_error = ""

    def call(self, method, *, _attempts=3, _request_timeout=60.0, _wait_on_flood=True, **kwargs):
        self.calls.append((method, kwargs))
        self.budgets.append((method, _attempts, _request_timeout, _wait_on_flood))
        if self.on_call:
            self.on_call(method, kwargs)
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": kwargs.get("message_id")}}
        if method == self.reject:
            return {"ok": False, "error_code": 400, "description": "rejected"}
        if method == "pinChatMessage":
            self.pinned.add(kwargs["message_id"])
        if method == "unpinChatMessage":
            self.pinned.discard(kwargs["message_id"])
        if method == self.fail:
            raise TimeoutError("response lost after the operation")
        if method == self.unconfirmed:
            return {"ok": True, "result": False}
        return {"ok": True, "result": True}

    def send(self, text):
        return self.call("sendMessage", text=text)

    def calls_for(self, method):
        return [kwargs for name, kwargs in self.calls if name == method]


def card(message_id, *, session=SESSION, title="승인 대상", **extra):
    item = {
        "episode": "☐ 승인|" + title, "signature": "sig-" + str(message_id), "freeform": None,
        "multi": False, "title": title, "option_labels": ["승인", "거절"],
        "message_id": message_id, "session_id": session, "tool_id": "tool-" + str(message_id),
    }
    item.update(extra)
    return item


def entry(message_id, *, session=SESSION, chat="123", created=NOW, **extra):
    item = {"message_id": message_id, "chat_id": chat, "session_id": session}
    if created is not None:
        item["created_at"] = created
    item.update(extra)
    return item


class QuestionPinsTest(unittest.TestCase):
    def setUp(self):
        self.telegram = Telegram()
        self.now = NOW
        self.saves = 0
        self.b = self.make_bridge()

    def make_bridge(self, receipts=None, pins=None, *, chat_id="123", session=SESSION):
        b = Bridge.__new__(Bridge)
        b.config = types.SimpleNamespace(chat_id=chat_id)
        b.telegram = self.telegram
        b.session_binding = types.SimpleNamespace(session_id=session) if session else None
        b.question_receipts = receipts if receipts is not None else []
        b.question_pins = pins if pins is not None else []
        b.question_pin_lock = threading.RLock()
        b.question_tool = None
        b.question_card = None
        b.question_pending_sig = ""
        b.askq_done_tool_ids = frozenset()
        b.tr = lambda text, **kwargs: text.format(**kwargs) if kwargs else text
        b.release_approval_light = lambda item: None
        b.retire_askq_sidecar = lambda *args, **kwargs: None

        def persist():
            self.saves += 1
        b.persist_state = persist
        return b

    def open(self, message_id, *, session=SESSION, chat="123", created=NOW, **card_extra):
        item, pin = card(message_id, session=session, **card_extra), entry(message_id, session=session, chat=chat, created=created)
        self.b.question_receipts.append(item)
        self.b.question_pins.append(pin)
        return item, pin

    def tick(self, bridge=None):
        (bridge or self.b).sync_question_pins(now=self.now)

    def restart(self):
        """A fresh process sees only what persist_state wrote (JSON round trip)."""
        receipts = json.loads(json.dumps(self.b.question_receipts))
        pins = json.loads(json.dumps(self.b.question_pins))
        self.b = self.make_bridge(receipts, pins, chat_id=self.b.config.chat_id,
                                  session=getattr(self.b.session_binding, "session_id", None))
        return self.b

    # ── 고정·해제 기본 ───────────────────────────────────────────────

    def test_new_progress_does_not_bury_two_questions_or_duplicate_cards(self):
        first, second = self.open(55)[1], self.open(56, title="두 번째 승인")[1]
        self.tick()
        self.assertEqual(self.telegram.calls_for("pinChatMessage"),
                         [{"chat_id": "123", "message_id": 55, "disable_notification": True}])
        self.tick()
        self.assertEqual(self.telegram.pinned, {999, 55, 56})
        self.tick()
        self.assertEqual(len(self.telegram.calls), 2)  # nothing to do once both are confirmed
        self.assertEqual(self.telegram.calls_for("sendMessage"), [])
        self.assertTrue(first["pin_confirmed"] and second["pin_confirmed"])
        self.assertEqual(self.telegram.budgets[0][1:], (1, 5, False))
        self.assertEqual(len(self.b.question_receipts), 2)

    def test_answer_removes_only_that_pin_and_keeps_the_other_question(self):
        (first_card, first), (_, second) = self.open(55), self.open(56, title="두 번째 승인")
        self.tick(); self.tick()
        first_card["completion"] = "✅ 선택: 승인"
        self.tick()
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": "123", "message_id": 55}])
        self.assertEqual(self.telegram.pinned, {999, 56})
        self.assertEqual(self.b.question_pins, [second])
        self.assertTrue(second["pin_confirmed"])
        self.tick()
        self.assertEqual(len(self.telegram.calls), 3)

    def test_native_receipt_unpins_without_resubmitting(self):
        item, _ = self.open(55)
        self.b.question_tool = {"tool_id": "tool-55", "session_id": SESSION,
                                "questions": [{"question": "승인 대상", "header": "승인"}]}
        self.tick()
        self.assertEqual(self.telegram.pinned, {999, 55})
        record = {"type": "user", "sessionId": SESSION,
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-55"}]},
                  "toolUseResult": {"answers": {"승인 대상": "승인"}}}
        self.b.observe_question_record(record)
        self.assertEqual(item["completion"], "✅ 선택: 승인")
        self.assertEqual(len(self.telegram.calls_for("editMessageText")), 1)
        self.assertEqual(self.b.question_receipts, [])  # the card leaves as before
        self.tick()
        self.assertEqual(self.telegram.pinned, {999})
        self.assertEqual(self.b.question_pins, [])
        self.assertEqual(self.telegram.calls_for("sendMessage"), [])
        self.assertEqual(len(self.telegram.calls_for("editMessageText")), 1)

    def test_unconfirmed_submission_keeps_visible_question_without_resending(self):
        self.open(55, submitted=True)  # typed answer not yet confirmed by the native record
        self.tick(); self.tick()
        self.assertEqual(self.telegram.pinned, {999, 55})
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.telegram.calls_for("sendMessage"), [])

    def test_session_rotation_clears_owned_pin_but_missing_session_preserves_it(self):
        _, pin = self.open(55)
        self.tick()
        self.b.session_binding = None
        self.tick()
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertTrue(pin["pin_confirmed"])
        self.b.session_binding = types.SimpleNamespace(session_id="session-b")
        self.tick()
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": "123", "message_id": 55}])
        self.assertEqual(self.b.question_pins, [])

    def test_expiry_unpins_even_with_no_new_messages(self):
        self.open(55)
        self.tick()
        self.now = NOW + TTL
        self.tick()
        self.assertEqual(self.telegram.pinned, {999})
        self.assertEqual(self.b.question_pins, [])
        self.open(57, created=NOW - TTL)
        self.tick()
        self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), 1)
        self.assertEqual(self.b.question_pins, [])  # too old to pin, nothing to release

    # ── 재시작·실패 상한 ─────────────────────────────────────────────

    def test_restart_keeps_confirmed_pin_without_repinning_or_resending(self):
        self.open(55)
        self.tick()
        self.restart()
        self.tick(); self.tick()
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.telegram.pinned, {999, 55})

    def test_ambiguous_pin_is_owned_before_call_and_cleared_after_restart(self):
        _, pin = self.open(55)
        self.telegram.fail = "pinChatMessage"
        self.tick()
        self.assertEqual(self.telegram.pinned, {999, 55})  # Telegram did pin, the response was lost
        self.assertEqual({key: pin.get(key) for key in PIN_FIELDS},
                         {"pin_managed": True, "pin_confirmed": None, "pin_action": "pinChatMessage",
                          "pin_attempts": 1, "pin_retry_at": NOW + RETRY})
        self.assertGreaterEqual(self.saves, 1)
        self.telegram.fail = None
        self.restart()
        self.tick()
        self.assertEqual(len(self.telegram.calls), 1)  # retry waits for the 60s spacing
        self.now = NOW + RETRY
        self.tick()
        self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), 2)
        self.assertTrue(self.b.question_pins[0]["pin_confirmed"])
        self.b.question_receipts[0]["completion"] = "그 선택창이 이미 닫혔어요"
        self.tick()
        self.assertEqual(self.telegram.pinned, {999})
        self.assertEqual(self.b.question_pins, [])

    def test_pin_failures_are_bounded_across_restart_and_never_block_answer(self):
        item, pin = self.open(55)
        self.telegram.reject = "pinChatMessage"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.tick(); self.tick()
            self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), attempt)
            self.assertEqual(pin["pin_attempts"], attempt)
            self.now += RETRY
        self.tick()
        self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), MAX_ATTEMPTS)
        self.restart()
        self.now += RETRY
        self.tick()
        self.assertEqual(len(self.telegram.calls_for("pinChatMessage")), MAX_ATTEMPTS)
        self.telegram.reject = None
        self.b.question_receipts[0]["completion"] = "✅ 선택: 승인"
        self.tick()
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": "123", "message_id": 55}])
        self.assertEqual(self.b.question_pins, [])

    def test_unpin_failure_retries_without_reopening_or_resubmitting(self):
        item, pin = self.open(55)
        self.tick()
        item["completion"] = "✅ 선택: 승인"
        self.telegram.reject = "unpinChatMessage"
        self.tick(); self.tick()
        self.assertEqual(len(self.telegram.calls_for("unpinChatMessage")), 1)
        self.assertEqual(item["completion"], "✅ 선택: 승인")
        self.assertEqual(pin["pin_attempts"], 1)
        self.telegram.reject = None
        self.now += RETRY
        self.tick()
        self.assertEqual(len(self.telegram.calls_for("unpinChatMessage")), 2)
        self.assertEqual(self.b.question_pins, [])
        self.assertEqual(self.telegram.calls_for("sendMessage"), [])
        self.assertEqual(self.telegram.calls_for("editMessageText"), [])

    def test_unpin_is_abandoned_after_bounded_failures(self):
        item, _ = self.open(55)
        self.tick()
        item["completion"] = "✅ 선택: 승인"
        self.telegram.reject = "unpinChatMessage"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.tick()
            self.assertEqual(len(self.telegram.calls_for("unpinChatMessage")), attempt)
            self.now += RETRY
        self.tick(); self.tick()
        self.assertEqual(len(self.telegram.calls_for("unpinChatMessage")), MAX_ATTEMPTS)
        self.assertEqual(self.b.question_pins, [])  # ledger entry dropped, answer untouched
        self.assertEqual(item["completion"], "✅ 선택: 승인")

    # ── 대상 제한 ───────────────────────────────────────────────────

    def test_no_pin_for_unaccepted_invalid_id_expired_foreign_or_group_cards(self):
        self.b.question_receipts[:] = [card(55), card(56), card(57), card(58), card(59), card(60, session="session-b")]
        self.b.question_pins[:] = [
            entry(None), entry(0), entry(-5), entry(55, chat="999"), entry(56, chat=None),
            entry(57, created=None), entry(58, created=NOW - TTL), entry(59, created=NOW + 5),
            entry(60), entry(61, created=True),
        ]
        self.tick(); self.tick()
        self.assertEqual(self.telegram.calls, [])
        self.b = self.make_bridge([card(62)], [entry(62)], chat_id="-100123")
        self.tick()
        self.assertEqual(self.telegram.calls, [])

    def test_card_without_ledger_entry_is_never_pinned(self):
        self.b.question_receipts[:] = [card(55)]  # a card from before the ledger existed
        self.tick()
        self.assertEqual(self.telegram.calls, [])

    def test_one_network_operation_per_tick_and_no_global_unpin(self):
        self.open(55); self.open(56); self.open(57)
        for expected in (1, 2, 3, 3):
            self.tick()
            self.assertEqual(len(self.telegram.calls), expected)
        for item in self.b.question_receipts:
            item["completion"] = "그 선택창이 이미 닫혔어요"
        for expected in (4, 5, 6, 6):
            self.tick()
            self.assertEqual(len(self.telegram.calls), expected)
        self.assertEqual(self.telegram.calls_for("unpinAllChatMessages"), [])
        self.assertEqual(self.telegram.pinned, {999})

    def test_chat_binding_is_required_and_config_change_does_not_touch_other_chat(self):
        item, pin = self.open(55)
        self.tick()
        self.assertTrue(pin["pin_confirmed"])
        self.b.config.chat_id = "456"
        item["completion"] = "✅ 선택: 승인"
        self.tick()
        self.assertEqual(len(self.telegram.calls), 1)  # the other chat's message is never touched
        self.assertEqual(self.telegram.pinned, {999, 55})
        self.assertEqual(self.b.question_pins, [pin])

    def test_answer_during_pin_request_is_unpinned_on_next_tick(self):
        item, pin = self.open(55)

        def answer_while_pinning(method, kwargs):
            if method == "pinChatMessage":
                item["completion"] = "✅ 선택: 승인"
        self.telegram.on_call = answer_while_pinning
        self.tick()
        self.assertTrue(pin["pin_confirmed"])
        self.telegram.on_call = None
        self.tick()
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": "123", "message_id": 55}])
        self.assertEqual(self.telegram.pinned, {999})

    def test_unsuccessful_telegram_response_is_not_recorded_as_pinned(self):
        _, pin = self.open(55)
        self.telegram.unconfirmed = "pinChatMessage"
        self.tick()
        self.assertIsNone(pin["pin_confirmed"])
        self.assertEqual(pin["pin_attempts"], 1)
        self.tick()
        self.assertEqual(len(self.telegram.calls), 1)
        self.telegram.unconfirmed = None
        self.now += RETRY
        self.tick()
        self.assertTrue(pin["pin_confirmed"])

    # ── 틱 연결·장부 복원 ──────────────────────────────────────────

    def test_idle_question_poll_pins_without_new_transcript_events(self):
        self.open(55, created=time.time())  # the poll uses the wall clock
        self.b.repl = types.SimpleNamespace(supports_pane_features=True)
        self.b.question_checked_at = float("-inf")
        self.b.poll_approval_speech = lambda: None
        self.b.recover_late_question_cards = lambda: None
        self.b.settle_open_question_from_transcript = lambda: None
        self.b.refresh_question_cards = lambda: None
        self.b.capture_question_screen = Mock(side_effect=RuntimeError("no screen yet"))
        self.b.maybe_send_question_card()
        self.assertEqual(self.telegram.pinned, {999, 55})
        self.assertEqual(self.telegram.calls_for("sendMessage"), [])

    def test_restored_ledger_keeps_valid_entries_only(self):
        owned = dict(pin_managed=True, pin_confirmed=True, pin_action="pinChatMessage",
                     pin_attempts=1, pin_retry_at=NOW + RETRY)
        state = {
            "question_receipts": [card(55), card(56, session="session-b")],
            "question_pins": [entry(55, **owned), entry(56, **owned), entry(57),
                              entry(0), {"message_id": 58}, "junk"],
        }
        self.b.restore_question_state(state)
        self.assertEqual([item["message_id"] for item in self.b.question_pins], [55, 56, 57])
        self.assertEqual(self.b.question_receipts[1]["completion"], "그 선택창이 이미 닫혔어요")
        self.tick(); self.tick()
        # 55 stays pinned without a call; 56 was pinned but its session closed → unpin;
        # 57 was never pinned by us and has no card → dropped silently.
        self.assertEqual(self.telegram.calls_for("unpinChatMessage"), [{"chat_id": "123", "message_id": 56}])
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual([item["message_id"] for item in self.b.question_pins], [55])


if __name__ == "__main__":
    unittest.main()
