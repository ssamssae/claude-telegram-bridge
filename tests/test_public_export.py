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


if __name__ == "__main__":
    unittest.main()
