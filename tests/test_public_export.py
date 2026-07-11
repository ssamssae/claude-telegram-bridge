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

    def test_private_dm_removes_leading_decoration_and_keeps_reply_quote(self):
        mod = self._load_bridge()
        telegram = mod.TelegramClient("token", "1234", "🤖", 4096)
        calls = []
        telegram.call = lambda method, **params: calls.append((method, params)) or {
            "ok": True,
            "result": {"message_id": 1},
        }

        self.assertEqual(telegram.with_emoji_prefix("🙂😄👋 안녕하세요"), "안녕하세요")
        telegram.send("🤖\n답변", reply_to_message_id=42)

        self.assertEqual(calls[-1][1]["text"], "답변")
        self.assertEqual(calls[-1][1]["reply_to_message_id"], 42)

    def test_group_keeps_node_emoji_and_reply_quote(self):
        mod = self._load_bridge()
        telegram = mod.TelegramClient("token", "-1234", "🤖", 4096)
        calls = []
        telegram.call = lambda method, **params: calls.append((method, params)) or {
            "ok": True,
            "result": {"message_id": 1},
        }

        telegram.send("답변", reply_to_message_id=42)

        self.assertEqual(calls[-1][1]["text"], "🤖\n답변")
        self.assertEqual(calls[-1][1]["reply_to_message_id"], 42)

    @staticmethod
    def _load_bridge():
        path = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"
        spec = importlib.util.spec_from_file_location("claude_telegram_bridge_behavior", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod


if __name__ == "__main__":
    unittest.main()
