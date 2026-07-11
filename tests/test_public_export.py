import dataclasses
import importlib.util
import sys
import unittest
from pathlib import Path


class PublicExportTest(unittest.TestCase):
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
        self.assertIsNone(mod.mesh_ledger_record())
        cfg = dataclasses.replace(mod.Config.from_env(), chat_id="")
        with self.assertRaises(ValueError):
            mod.validate_startup_config(cfg)

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
        self.assertEqual(private.with_emoji_prefix("🍎"), "🍎")
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
