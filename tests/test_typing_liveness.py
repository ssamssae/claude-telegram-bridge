"""T-260720-033: typing-liveness 신세대 test (공개 배포본 정본).

공개 repo의 구세대 test_typing_liveness.py를 대체한다. 구세대 test는 Bridge를
__new__로 만들고 속성을 수동 세팅하는데, 신세대 drain_queue가 service_exhaust_parked_items
(T-260718-046 exhausted-park)를 거치며 self.exhaust_parked를 참조한다. 구세대 test는 그
속성을 세팅하지 않아 신 코드에서 AttributeError로 죽었다(세대 불일치). 이 정본은 동일 계약을
검증하되 신세대 인스턴스 상태(exhaust_parked)를 함께 갖춘다.

export가 dist/tests로 내보낼 때 브릿지 파일명만 언더스코어로 재작성한다(로드 경로 표준화).
"""
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def load_bridge_module():
    path = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"
    spec = importlib.util.spec_from_file_location("claude_typing_liveness", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TypingLivenessTest(unittest.TestCase):
    def test_turn_end_stops_typing_when_only_background_monitor_remains(self):
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.telegram = mock.Mock()
        bridge.lock = threading.RLock()
        bridge.typing_lock = threading.Lock()
        bridge.typing_stop = None
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.ambient_final_direct_deliver = True  # T-260829-029: 착지 예정 턴만 typing 대상
        # (frames, reply_to, initial_delay) — 3번째는 T-260730-002 지속 국면에서 추가됐다.
        bridge.eye_activity_context = lambda: ([], 0, 0.0)
        bridge.session_occupied_excluding_active = mock.Mock(return_value=True)
        bridge.repl = SimpleNamespace(
            supports_pane_features=True,
            capture_pane=lambda _lines: "Background command polling every 60s\n1 shell still running\n❯",
        )

        with mock.patch.object(mod, "TYPING_LIVENESS_GRACE_PULSES", 1), \
             mock.patch.object(mod, "TYPING_LIVENESS_CHECK_EVERY", 1), \
             mock.patch.object(mod, "TYPING_PULSE_FIRST_WAIT", 0.01), \
             mock.patch.object(mod, "TYPING_PULSE_WAIT", 0.01):
            stop_event = bridge.start_typing_loop(max_seconds=1)
            self.addCleanup(stop_event.set)
            self.assertTrue(
                stop_event.wait(0.2),
                "typing must self-extinguish after the response turn ends",
            )

    def test_passive_background_monitor_does_not_restart_typing(self):
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = False
        bridge.suggested_loop_runtime_enabled = True
        bridge.config = SimpleNamespace(suggested_loop_kill_path=Path("/nonexistent"))
        bridge.repl = SimpleNamespace(supports_pane_features=True)
        bridge.drop_pending_suggested_auto = mock.Mock()
        bridge.session_clear_pending = mock.Mock(return_value=False)
        bridge.dismiss_feedback_survey_if_pending = mock.Mock(return_value="none")
        bridge.busy_state = mock.Mock(return_value="generating")
        bridge.try_busy_inject = mock.Mock(return_value=False)
        bridge.ensure_typing = mock.Mock()
        bridge.stop_typing = mock.Mock()
        # 신세대 상태: exhausted-park 서비스 경로(T-260718-046)가 drain_queue에서 참조.
        bridge.exhaust_parked = []
        bridge.last_nonidle_seen_at = 0.0

        bridge.drain_queue()

        bridge.ensure_typing.assert_not_called()
        bridge.stop_typing.assert_called_once_with()

    def test_foreground_response_remains_typing_tracked(self):
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.ambient_final_direct_deliver = True  # T-260829-029
        bridge.repl = SimpleNamespace(
            supports_pane_features=True,
            capture_pane=lambda _lines: "✻ Working…\nesc to interrupt",
        )

        self.assertTrue(bridge.has_live_typing_work())
        self.assertTrue(bridge.ambient_response_active)

    def test_nondeliverable_ambient_turn_is_not_typing_tracked(self):
        """T-260829-029: 폰에 최종답변이 안 가는 ambient 턴(마커 없는 재진입 —
        순수 자율작업·cron·워크플로우·백그라운드 에이전트)은 '입력중'을 켜지 않는다.

        ★변이 프로브 = has_typing_tracked_work/has_live_typing_work 의
        ambient_typing_eligible() 게이트를 옛 `self.ambient_response_active` 단독으로
        되돌리면 이 테스트가 FAIL 한다 (2026-08-29 유령 입력중 실사고 — drain 3s 가
        typing 을 상시 재점화, 착지 0통에 폰엔 '입력중'만 하루 종일).
        capture_pane 이 호출되면 즉시 FAIL — 판정은 pane 이전에 끝나야 한다."""
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.ambient_final_direct_deliver = False

        def _fail_capture(_lines):
            raise AssertionError("non-deliverable ambient must not reach pane capture")

        bridge.repl = SimpleNamespace(
            supports_pane_features=True,
            capture_pane=_fail_capture,
        )

        self.assertFalse(bridge.has_typing_tracked_work())
        self.assertFalse(bridge.has_live_typing_work())

    def test_drain_busy_does_not_relight_typing_for_nondeliverable_ambient(self):
        """T-260829-029 점화 경로 픽스처: busy(generating) 중 drain_queue 는
        비착지 ambient 만 있으면 ensure_typing 대신 stop_typing 을 불러야 한다."""
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.ambient_final_direct_deliver = False
        bridge.suggested_loop_runtime_enabled = True
        bridge.config = SimpleNamespace(suggested_loop_kill_path=Path("/nonexistent"))
        bridge.repl = SimpleNamespace(supports_pane_features=True)
        bridge.drop_pending_suggested_auto = mock.Mock()
        bridge.session_clear_pending = mock.Mock(return_value=False)
        bridge.dismiss_feedback_survey_if_pending = mock.Mock(return_value="none")
        bridge.busy_state = mock.Mock(return_value="generating")
        bridge.try_busy_inject = mock.Mock(return_value=False)
        bridge.ensure_typing = mock.Mock()
        bridge.stop_typing = mock.Mock()
        bridge.exhaust_parked = []
        bridge.last_nonidle_seen_at = 0.0

        bridge.drain_queue()

        bridge.ensure_typing.assert_not_called()
        bridge.stop_typing.assert_called_once_with()

    def test_drain_busy_keeps_typing_for_deliverable_ambient(self):
        """T-260829-029 무회귀 짝: 착지 예정(ambient_final_direct_deliver=True) 턴은
        종전대로 busy 구간에서 ensure_typing 이 유지된다 (2026-06-27 사용자 요청 계보)."""
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.ambient_final_direct_deliver = True
        bridge.suggested_loop_runtime_enabled = True
        bridge.config = SimpleNamespace(suggested_loop_kill_path=Path("/nonexistent"))
        bridge.repl = SimpleNamespace(supports_pane_features=True)
        bridge.drop_pending_suggested_auto = mock.Mock()
        bridge.session_clear_pending = mock.Mock(return_value=False)
        bridge.dismiss_feedback_survey_if_pending = mock.Mock(return_value="none")
        bridge.busy_state = mock.Mock(return_value="generating")
        bridge.try_busy_inject = mock.Mock(return_value=False)
        bridge.ensure_typing = mock.Mock()
        bridge.stop_typing = mock.Mock()
        bridge.exhaust_parked = []
        bridge.last_nonidle_seen_at = 0.0

        bridge.drain_queue()

        bridge.ensure_typing.assert_called_once_with()
        bridge.stop_typing.assert_not_called()

    def _drain_bridge(self, mod, *, direct_deliver, card_visible):
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.ambient_final_direct_deliver = direct_deliver
        bridge.ambient_directive_card_visible = card_visible
        bridge.suggested_loop_runtime_enabled = True
        bridge.config = SimpleNamespace(suggested_loop_kill_path=Path("/nonexistent"))
        bridge.repl = SimpleNamespace(supports_pane_features=True)
        bridge.drop_pending_suggested_auto = mock.Mock()
        bridge.session_clear_pending = mock.Mock(return_value=False)
        bridge.dismiss_feedback_survey_if_pending = mock.Mock(return_value="none")
        bridge.busy_state = mock.Mock(return_value="generating")
        bridge.try_busy_inject = mock.Mock(return_value=False)
        bridge.ensure_typing = mock.Mock()
        bridge.stop_typing = mock.Mock()
        bridge.exhaust_parked = []
        bridge.last_nonidle_seen_at = 0.0
        return bridge

    def test_drain_busy_keeps_typing_for_ambient_with_visible_directive_card(self):
        """T-261007-022: 받은지시 카드가 텔레그램에 올라간 ambient 턴은 최종답변
        직접발신 대상이 아니어도 busy 구간에서 입력중을 켠다 (실측 2026-10-07 macOS 노드 —
        다른 엔진이 주입한 지시 카드는 떴는데 18분 작업 내내 입력중 무표시).

        ★변이 프로브 = ambient_typing_eligible() 에서 ambient_directive_card_visible
        갈래를 빼면 이 테스트가 FAIL 한다."""
        mod = load_bridge_module()
        bridge = self._drain_bridge(mod, direct_deliver=False, card_visible=True)

        bridge.drain_queue()

        bridge.ensure_typing.assert_called_once_with()
        bridge.stop_typing.assert_not_called()

    def test_card_visible_ambient_turn_self_exits_when_screen_is_idle(self):
        """T-261007-022 무회귀: 카드가 보여도 화면이 쉬고 있으면 입력중 생존 판정은
        False — T-260829-029 유령 입력중 방지 장치가 그대로 걸린다."""
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.ambient_final_direct_deliver = False
        bridge.ambient_directive_card_visible = True
        bridge.session_has_foreground_response = mock.Mock(return_value=False)
        bridge.has_passive_background_work = mock.Mock(return_value=False)

        self.assertTrue(bridge.has_typing_tracked_work())
        self.assertFalse(bridge.has_live_typing_work())

    def test_directive_card_send_marks_typing_visibility(self):
        """T-261007-022: 카드 발신 성공 때만 True, 실패면 False."""
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.ambient_directive_card_visible = False
        bridge.reset_ambient_flow = mock.Mock()
        bridge.telegram = SimpleNamespace(send=mock.Mock(return_value=[123]))
        with mock.patch.object(mod, "format_ambient_directive", return_value="📥 지시"):
            bridge.mirror_ambient_directive("작업해")
        self.assertTrue(bridge.ambient_directive_card_visible)

        failing = mod.Bridge.__new__(mod.Bridge)
        failing.ambient_directive_card_visible = False
        failing.reset_ambient_flow = mock.Mock()
        failing.telegram = SimpleNamespace(send=mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch.object(mod, "format_ambient_directive", return_value="📥 지시"):
            failing.mirror_ambient_directive("작업해")
        self.assertFalse(failing.ambient_directive_card_visible)

    def test_finish_ambient_response_clears_card_visibility(self):
        """T-261007-022: 턴 종료 뒤 카드 표시가 남아 다음 무카드 재진입을 켜지 않게."""
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.typing_lock = threading.Lock()
        bridge.typing_stop = None
        bridge.ambient_response_active = True
        bridge.ambient_directive_card_visible = True

        bridge.finish_ambient_response()

        self.assertFalse(bridge.ambient_directive_card_visible)
        self.assertFalse(bridge.ambient_typing_eligible())

    def test_top_level_end_turn_explicitly_stops_ambient_typing(self):
        mod = load_bridge_module()
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.lock = threading.RLock()
        bridge.active_turn = None
        bridge.pending = []
        bridge.ambient_response_active = True
        bridge.session_binding = SimpleNamespace(session_id="fixture-session")
        bridge.update_parent_map = mock.Mock()
        bridge.ancestor_matches_active_turn = mock.Mock(return_value=None)
        bridge.sequence_matches_active_turn = mock.Mock(return_value=None)
        bridge.stop_typing = mock.Mock()
        record = {
            "sessionId": "fixture-session",
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
            },
        }

        with mock.patch.object(mod, "flow_mirror_enabled", return_value=False):
            bridge.process_record(record)

        self.assertFalse(bridge.ambient_response_active)
        bridge.stop_typing.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
