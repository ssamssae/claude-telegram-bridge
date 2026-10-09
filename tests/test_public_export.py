import dataclasses
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class PublicExportTest(unittest.TestCase):
    def load_delivery_bridge(self):
        path = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"
        spec = importlib.util.spec_from_file_location("claude_public_delivery", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_app_permission_cards_bind_context_and_use_separate_callbacks(self):
        mod = self.load_delivery_bridge()
        screen = "\n".join([
            "────────────────────────────────────────",
            "Computer Use wants to control these apps", "Inspect the test page.",
            "● Browser", "● Mail", "4 other apps will be hidden while Claude works.",
            "❯ Deny, and tell Claude what to do differently", "(esc)",
            "  Allow for this session (2 apps)", "Enter to confirm · Esc to cancel",
        ])
        parsed = mod.parse_pane_choice(screen)
        self.assertEqual(parsed["kind"], "computer_use")
        self.assertEqual(len(parsed["options"]), 2)
        for row in mod.choice_keyboard(parsed):
            self.assertTrue(row[0]["callback_data"].startswith("clb-computer-use::"))
        moved = screen.replace("❯ Deny", "  Deny").replace("  Allow", "❯ Allow")
        self.assertEqual(parsed["signature"], mod.parse_pane_choice(moved)["signature"])
        changed = screen.replace("● Browser", "● Calendar")
        self.assertNotEqual(parsed["signature"], mod.parse_pane_choice(changed)["signature"])

    def test_send_requires_a_positive_confirmed_message_id(self):
        mod = self.load_delivery_bridge()
        client = mod.TelegramClient("token", "1234", "", 4096)
        for message_id in (0, -1, True, None):
            client.call = mock.Mock(return_value={"ok": True, "result": {"message_id": message_id}})
            self.assertIsNone(client.send("Test reply"))
            self.assertIsNone(client.send_copy_content("Test content", code=True))

    def test_approval_card_completes_after_late_record_without_private_speaker(self):
        mod = self.load_delivery_bridge()
        title = "Publish the prepared release?"
        screen = "\n".join([
            "────────────────────────────────────────", "☐ Approval", "", title, "",
            "❯ 1. Publish", "  2. Cancel", "  3. Type something.",
            "────────────────────────────────────────", "  4. Chat about this", "",
            "Enter to select · ↑/↓ to navigate · Esc to cancel",
        ])
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.config = SimpleNamespace(chat_id="1234")
        bridge.session_binding = SimpleNamespace(session_id="test-session")
        bridge.repl = SimpleNamespace(supports_pane_features=True,
                                      capture_pane=mock.Mock(return_value=screen))
        bridge.telegram = SimpleNamespace(
            call=mock.Mock(return_value={"ok": True, "result": {"message_id": 42}}),
            with_emoji_prefix=lambda text, **kwargs: text,
        )
        bridge.persist_state = mock.Mock()
        bridge.question_card = None
        bridge.question_tool = None
        bridge.question_receipts = []
        bridge.question_pending_sig = ""
        bridge.question_checked_at = 0
        with mock.patch.object(mod.subprocess, "Popen") as spawn:
            bridge.maybe_send_question_card(min_interval=0)
            bridge.maybe_send_question_card(min_interval=0)
            self.assertEqual(bridge.question_card["message_id"], 42)
            self.assertIsNone(bridge.question_card.get("completion"))
            bridge.observe_question_record({
                "type": "assistant", "sessionId": "test-session",
                "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion",
                    "id": "approval-tool", "input": {"questions": [{
                        "question": title, "header": "Approval", "multiSelect": False,
                    }]}}]},
            })
            result = {
                "type": "user", "sessionId": "test-session",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "approval-tool"}]},
                "toolUseResult": {"answers": {title: "Publish"}},
            }
            bridge.observe_question_record(result)
            bridge.observe_question_record(result)
            edits = [call for call in bridge.telegram.call.call_args_list
                     if call.args[0] == "editMessageText"]
            self.assertEqual(len(edits), 1)
            self.assertEqual(edits[0].kwargs["message_id"], 42)
            self.assertIn("✅", edits[0].kwargs["text"])
            self.assertIn("Publish", edits[0].kwargs["text"])
            self.assertEqual(edits[0].kwargs["reply_markup"], '{"inline_keyboard": []}')
            spawn.assert_not_called()

    def test_stop_hook_final_keeps_reply_origin_and_is_not_replayed(self):
        mod = self.load_delivery_bridge()
        with tempfile.TemporaryDirectory() as td:
            bridge = mod.Bridge.__new__(mod.Bridge)
            bridge.config = SimpleNamespace(chat_id="1234")
            bridge.session_binding = SimpleNamespace(session_id="test-session")
            bridge.active_turn = None
            bridge.completed_reply = {"session_id": "test-session", "message_id": 42,
                                      "last_final_uuid": "first-final", "answer_sha": "previous"}
            bridge.parent_map = {"hook": "first-final"}
            bridge.outbox = mod.Outbox(Path(td) / "outbox.json")
            bridge.telegram = SimpleNamespace(send=mock.Mock(return_value=[123]))
            for method in ("persist_state", "stop_typing", "finish_ambient_response", "close_ambient_flow_card"):
                setattr(bridge, method, mock.Mock())
            bridge.observe_reply_continuation({"type": "user", "uuid": "hook", "parentUuid": "first-final",
                "isMeta": True, "isSidechain": False,
                "message": {"role": "user", "content": "Stop hook feedback:\nFinish cleanup."}})
            final = {"uuid": "next-final", "parentUuid": "hook", "isSidechain": False,
                     "message": {"stop_reason": "end_turn", "content": "Cleanup finished."}}
            self.assertTrue(bridge.deliver_reply_continuation(final))
            bridge.telegram.send.assert_called_once_with("Cleanup finished.", reply_to_message_id=42)
            self.assertTrue(bridge.deliver_reply_continuation(final))
            self.assertEqual(bridge.telegram.send.call_count, 1)
            bridge.observe_reply_continuation({"type": "user", "isMeta": False,
                                              "message": {"role": "user", "content": "A new request"}})
            self.assertFalse(bridge.deliver_reply_continuation(final))

    def test_imports_public_bridge(self):
        path = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"
        spec = importlib.util.spec_from_file_location("claude_telegram_bridge", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.assertEqual(mod.node_defaults()[0], "claude")
        self.assertEqual(mod.BRIDGE_OWNER, "claude-telegram-bridge")
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("asc-release-hold", source)
        self.assertNotIn("claude-" "automations", source)  # adjacent-literal split keeps the leak sweep itself clean
        self.assertIsNone(mod.release_hold_response("출시 멈춰 memoyo"))
        # T-260701-68: stripped mesh layer must leave working no-op stubs
        self.assertIsNone(mod.mesh_cutover_call("sendMessage", {}))
        self.assertFalse(mod.mesh_cutover_applies("sendMessage", {}))
        # T-260718-031: internal call sites pass bot_token=... — the stub must
        # accept it, and the surviving except/raise sites need the exception type.
        self.assertIsNone(mod.mesh_cutover_call("sendMessage", {}, bot_token="tok"))
        self.assertTrue(issubclass(mod.MeshRouteRetiredError, RuntimeError))
        self.assertIsNone(mod.mesh_ledger_record())
        cfg = dataclasses.replace(mod.Config.from_env(), chat_id="")
        with self.assertRaises(ValueError):
            mod.validate_startup_config(cfg)

    def test_flow_mirror_supports_bridge_env_and_flag_fallback(self):
        path = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"
        spec = importlib.util.spec_from_file_location("claude_flow_mirror_behavior", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as td:
            flag = Path(td) / "flow-mirror.on"
            flag.touch()
            with mock.patch.object(mod, "FLOW_MIRROR_FLAG", str(flag)), \
                 mock.patch.dict(os.environ, {"CLB_FLOW_MIRROR": "0"}, clear=False):
                self.assertFalse(mod.flow_mirror_enabled())
            with mock.patch.object(mod, "FLOW_MIRROR_FLAG", str(flag)), \
                 mock.patch.dict(os.environ, {"CLB_FLOW_MIRROR": "1"}, clear=False):
                self.assertTrue(mod.flow_mirror_enabled())
            with mock.patch.object(mod, "FLOW_MIRROR_FLAG", str(flag)), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLB_FLOW_MIRROR", None)
                self.assertTrue(mod.flow_mirror_enabled())

    def test_private_dm_removes_leading_decoration_and_group_keeps_context(self):
        path = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"
        spec = importlib.util.spec_from_file_location("claude_telegram_bridge_behavior", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        private = mod.TelegramClient("token", "1234", "🤖", 4096)
        private_calls = []
        private.call = lambda method, **params: private_calls.append((method, params)) or {
            "ok": True,
            "result": {"message_id": 1},
        }
        self.assertEqual(private.with_emoji_prefix("🙂😄👋 hello"), "hello")
        self.assertEqual(private.with_emoji_prefix("🙂"), "🙂")
        private.send("answer", reply_to_message_id=42)
        self.assertEqual(private_calls[-1][1]["reply_to_message_id"], 42)

        group = mod.TelegramClient("token", "-1234", "🤖", 4096)
        group_calls = []
        group.call = lambda method, **params: group_calls.append((method, params)) or {
            "ok": True,
            "result": {"message_id": 1},
        }
        group.send("answer", reply_to_message_id=42)
        self.assertEqual(group_calls[-1][1]["text"], "🤖\nanswer")
        self.assertEqual(group_calls[-1][1]["reply_to_message_id"], 42)


if __name__ == "__main__":
    unittest.main()
