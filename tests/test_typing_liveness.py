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
        bridge.eye_activity_context = lambda: ([], 0)
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
        bridge.repl = SimpleNamespace(
            supports_pane_features=True,
            capture_pane=lambda _lines: "✻ Working…\nesc to interrupt",
        )

        self.assertTrue(bridge.has_live_typing_work())
        self.assertTrue(bridge.ambient_response_active)

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
