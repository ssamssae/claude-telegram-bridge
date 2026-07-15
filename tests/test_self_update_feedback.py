#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


BRIDGE_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = BRIDGE_ROOT / "claude-telegram-bridge.py"
if not BRIDGE.exists():
    BRIDGE = BRIDGE_ROOT / "claude_telegram_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("claude_self_update_feedback_under_test", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.buttons: list[tuple[str, str, str]] = []
        self.calls: list[tuple[str, dict]] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def send_update_button(
        self,
        text: str,
        callback_data: str,
        button_text: str = "🔄 지금 업데이트",
    ) -> None:
        self.buttons.append((text, callback_data, button_text))

    def call(self, method: str, **params):
        self.calls.append((method, params))
        return {"ok": True}


class ClaudeSelfUpdateFeedbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load_bridge()

    def test_pep668_failure_requires_separate_break_consent(self):
        notices: list[str] = []
        proc = subprocess.CompletedProcess(
            ["python", "-m", "pip"],
            1,
            "",
            "error: externally-managed-environment\nSee PEP 668",
        )
        with mock.patch.object(self.bridge.subprocess, "run", return_value=proc) as run, \
             mock.patch.object(self.bridge.os, "execv") as execv:
            result = self.bridge.perform_self_update("0.9.0", notify=notices.append)

        execv.assert_not_called()
        self.assertEqual(result.status, "pep668_consent_required")
        self.assertIn("PEP 668", result.message)
        self.assertIn("--break-system-packages", result.message)
        self.assertEqual(notices, [])
        self.assertNotIn("--break-system-packages", run.call_args.args[0])

    def test_explicit_break_consent_adds_flag_and_reports_restart(self):
        notices: list[str] = []
        proc = subprocess.CompletedProcess(["python", "-m", "pip"], 0, "", "")
        with mock.patch.object(self.bridge.subprocess, "run", return_value=proc) as run, \
             mock.patch.object(self.bridge.os, "execv") as execv, \
             mock.patch.dict(os.environ, {}, clear=False):
            result = self.bridge.perform_self_update(
                "0.9.0",
                notify=notices.append,
                allow_break_system_packages=True,
            )

        self.assertEqual(result.status, "updated")
        self.assertIn("--break-system-packages", run.call_args.args[0])
        self.assertIn("v0.9.0", notices[-1])
        self.assertIn("재시작", notices[-1])
        execv.assert_called_once()

    def test_generic_pip_failure_gets_actionable_chat_feedback(self):
        notices: list[str] = []
        proc = subprocess.CompletedProcess(["python", "-m", "pip"], 2, "", "network unavailable")
        with mock.patch.object(self.bridge.subprocess, "run", return_value=proc), \
             mock.patch.object(self.bridge.os, "execv") as execv:
            result = self.bridge.perform_self_update("0.9.0", notify=notices.append)

        execv.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertIn("업데이트 실패", notices[-1])
        self.assertIn("pipx install --force claude-telegram-bridge", notices[-1])

    def test_pip26_user_failure_is_named_in_chat(self):
        notices: list[str] = []
        proc = subprocess.CompletedProcess(
            ["python", "-m", "pip"],
            1,
            "",
            "ERROR: --user installs are not supported by pip 26",
        )
        with mock.patch.object(self.bridge.subprocess, "run", return_value=proc):
            result = self.bridge.perform_self_update("0.9.0", notify=notices.append)

        self.assertEqual(result.status, "failed")
        self.assertIn("pip 26", notices[-1])
        self.assertIn("--user", notices[-1])

    def test_update_callback_turns_pep668_failure_into_consent_button(self):
        telegram = FakeTelegram()
        bridge = self.bridge.Bridge.__new__(self.bridge.Bridge)
        bridge.config = SimpleNamespace(chat_id="123")
        bridge.telegram = telegram
        callback = {
            "id": "callback-1",
            "data": f"{self.bridge.SELF_UPDATE_CALLBACK}::0.9.0",
            "message": {"chat": {"id": 123}},
        }
        seen: dict[str, object] = {}

        def fake_update(latest: str, *, notify=None, allow_break_system_packages=False):
            seen["latest"] = latest
            seen["notify"] = notify
            seen["allow_break"] = allow_break_system_packages
            return self.bridge.SelfUpdateResult("pep668_consent_required", "PEP 668 consent")

        with mock.patch.object(self.bridge, "perform_self_update", side_effect=fake_update):
            self.assertTrue(bridge.handle_self_update_callback(callback))

        self.assertEqual(seen["latest"], "0.9.0")
        self.assertFalse(seen["allow_break"])
        self.assertIs(seen["notify"].__self__, telegram)
        self.assertEqual(telegram.calls[0][0], "answerCallbackQuery")
        self.assertEqual(
            telegram.buttons,
            [
                (
                    "PEP 668 consent",
                    f"{self.bridge.SELF_UPDATE_BREAK_CALLBACK}::0.9.0",
                    "⚠️ 보호 설정 우회 동의",
                )
            ],
        )

    def test_break_consent_callback_is_the_only_flagged_path(self):
        telegram = FakeTelegram()
        bridge = self.bridge.Bridge.__new__(self.bridge.Bridge)
        bridge.config = SimpleNamespace(chat_id="123")
        bridge.telegram = telegram
        callback = {
            "id": "callback-2",
            "data": f"{self.bridge.SELF_UPDATE_BREAK_CALLBACK}::0.9.0",
            "message": {"chat": {"id": 123}},
        }
        seen: dict[str, object] = {}

        def fake_update(latest: str, *, notify=None, allow_break_system_packages=False):
            seen["latest"] = latest
            seen["notify"] = notify
            seen["allow_break"] = allow_break_system_packages
            return self.bridge.SelfUpdateResult("failed", "failed")

        with mock.patch.object(self.bridge, "perform_self_update", side_effect=fake_update):
            self.assertTrue(bridge.handle_self_update_callback(callback))

        self.assertEqual(seen["latest"], "0.9.0")
        self.assertTrue(seen["allow_break"])
        self.assertIs(seen["notify"].__self__, telegram)
        self.assertIn("동의", telegram.calls[0][1]["text"])

    def test_break_consent_from_another_chat_is_ignored(self):
        telegram = FakeTelegram()
        bridge = self.bridge.Bridge.__new__(self.bridge.Bridge)
        bridge.config = SimpleNamespace(chat_id="123")
        bridge.telegram = telegram
        callback = {
            "id": "callback-3",
            "data": f"{self.bridge.SELF_UPDATE_BREAK_CALLBACK}::0.9.0",
            "message": {"chat": {"id": 999}},
        }

        with mock.patch.object(self.bridge, "perform_self_update") as update:
            self.assertTrue(bridge.handle_self_update_callback(callback))

        update.assert_not_called()
        self.assertEqual(telegram.calls, [])

    def test_auto_update_pep668_result_offers_consent_in_chat(self):
        telegram = FakeTelegram()
        bridge = self.bridge.Bridge.__new__(self.bridge.Bridge)
        bridge.telegram = telegram
        result = self.bridge.SelfUpdateResult("pep668_consent_required", "PEP 668 consent")

        with mock.patch.object(self.bridge, "self_update_available", return_value="0.9.0"), \
             mock.patch.object(self.bridge, "bool_env", return_value=True), \
             mock.patch.object(self.bridge, "perform_self_update", return_value=result):
            bridge.offer_update_if_available()

        self.assertEqual(telegram.buttons[0][1], f"{self.bridge.SELF_UPDATE_BREAK_CALLBACK}::0.9.0")


if __name__ == "__main__":
    unittest.main()
