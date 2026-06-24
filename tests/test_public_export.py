import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path


class PublicExportTest(unittest.TestCase):
    def load_module(self):
        path = Path(__file__).resolve().parents[1] / "claude_telegram_bridge.py"
        spec = importlib.util.spec_from_file_location("claude_telegram_bridge", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_imports_public_bridge(self):
        mod = self.load_module()
        self.assertEqual(mod.node_defaults()[0], "claude")
        self.assertEqual(mod.BRIDGE_OWNER, "claude-telegram-bridge")

    def test_title_content_copy_payload_splits(self):
        mod = self.load_module()
        title = "제목: 클로드를 텔레그램에 연결했다 | 폰에서 Claude Code 자동화 실행하기"
        content = "내용: Claude Telegram Bridge로 Claude Code 세션을 텔레그램에서 직접 호출합니다."

        self.assertTrue(mod.is_copy_payload_message(title))
        self.assertTrue(mod.is_copy_payload_message(content))
        self.assertEqual(mod.split_copy_payload_messages(f"{title}\n\n{content}"), [title, content])

    def test_title_content_copy_payload_send_path_suppresses_reasoning_mirror(self):
        mod = self.load_module()

        class FakeTelegram:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                return [100 + len(self.sent)]

            def send_typing(self):
                return None

        class FakeRepl:
            def capture_pane(self, lines=80):
                return ""

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = mod.Config(
                node="test",
                emoji="🤖",
                token_file=root / "token.json",
                chat_id="1234",
                state_dir=root,
                tmux_bin="tmux",
                tmux_socket="claude",
                tmux_session="claude",
                telegram_chunk=4096,
                poll_timeout=1,
                typing_max_seconds=30,
                audio_transcribe_cmd=None,
                audio_transcribe_timeout=10,
                start_at_end=False,
                state_path=root / "state.json",
                offset_file=root / "offset",
                pid_file=root / "pid",
                queue_path=root / "queue.jsonl",
                outbox_path=root / "outbox.json",
                quarantine_path=root / "quarantine.jsonl",
                session_sidecar_path=root / "sessions.json",
                egress_sidecar_path=root / "egress.json",
                token_registry_path=root / "registry.json",
                token_owner="claude-telegram-bridge",
                expected_consumer="test",
                expected_host="test-host",
                session_ttl_seconds=3600,
                egress_ttl_seconds=900,
                turn_sequence_fallback_seconds=3600,
                active_turn_stale_seconds=900,
                transcript_stable_seconds=0.1,
                composer_clear_retries=1,
                injection_verify_timeout=1.0,
                send_retry_seconds=0.0,
                send_max_attempts=3,
                queue_compact_max_events=1000,
                outbox_max_entries=1000,
            )
            tg = FakeTelegram()
            bridge = mod.Bridge(cfg, tg, repl=FakeRepl(), token="123:abc")
            active = mod.ActiveTurn(
                queue_id="q1",
                update_id=1,
                message_id=1,
                nonce="clb-" + "a" * 32,
                injected_at=time.time(),
                text="제목 내용 따로",
                pending_reasoning="중간 사고 요약",
                send_attempts=1,
                send_in_progress=True,
            )
            bridge.active_turn = active
            title = "제목: 클로드를 텔레그램에 연결했다 | 폰에서 Claude Code 자동화 실행하기"
            content = "내용: Claude Telegram Bridge로 Claude Code 세션을 텔레그램에서 직접 호출합니다."

            bridge.send_claimed_active_answer(active, "a-final", f"{title}\n\n{content}", "outbox-key")

        self.assertEqual(tg.sent, [title, content])
        self.assertTrue(all(not message.startswith(mod.REASONING_HEADER) for message in tg.sent))

    def test_stale_active_turn_releases_idle_missing_transcript_and_drains_queue(self):
        mod = self.load_module()

        class FakeTelegram:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)
                return [100 + len(self.sent)]

            def send_typing(self):
                return None

        class FakeRepl:
            def __init__(self):
                self.pasted = []
                self.cleared = 0

            def capture_pane(self, lines=80):
                return "Claude is idle\n> "

            def clear_composer(self):
                self.cleared += 1

            def paste_prompt(self, prompt):
                self.pasted.append(prompt)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = mod.Config(
                node="test",
                emoji="🤖",
                token_file=root / "token.json",
                chat_id="1234",
                state_dir=root,
                tmux_bin="tmux",
                tmux_socket="claude",
                tmux_session="claude",
                telegram_chunk=4096,
                poll_timeout=1,
                typing_max_seconds=30,
                audio_transcribe_cmd=None,
                audio_transcribe_timeout=10,
                start_at_end=False,
                state_path=root / "state.json",
                offset_file=root / "offset",
                pid_file=root / "pid",
                queue_path=root / "queue.jsonl",
                outbox_path=root / "outbox.json",
                quarantine_path=root / "quarantine.jsonl",
                session_sidecar_path=root / "sessions.json",
                egress_sidecar_path=root / "egress.json",
                token_registry_path=root / "registry.json",
                token_owner="claude-telegram-bridge",
                expected_consumer="test",
                expected_host="test-host",
                session_ttl_seconds=3600,
                egress_ttl_seconds=900,
                turn_sequence_fallback_seconds=3600,
                active_turn_stale_seconds=1,
                transcript_stable_seconds=0.1,
                composer_clear_retries=1,
                injection_verify_timeout=1.0,
                send_retry_seconds=0.0,
                send_max_attempts=3,
                queue_compact_max_events=1000,
                outbox_max_entries=1000,
            )
            repl = FakeRepl()
            bridge = mod.Bridge(cfg, FakeTelegram(), repl=repl, token="123:abc")
            bridge.session_binding = mod.ClaudeSessionBinding(root / "missing.jsonl", "missing", 123)
            old = time.time() - 120
            bridge.active_turn = mod.ActiveTurn(
                queue_id="q-old",
                update_id=1,
                message_id=2,
                nonce="clb-" + "a" * 32,
                injected_at=old,
                text="old message",
                user_uuid="u-old",
                user_seen_at=old,
            )
            bridge.pending.append(
                mod.QueueItem(
                    queue_id="q-new",
                    update_id=3,
                    message_id=4,
                    text="new message",
                    nonce="clb-" + "b" * 32,
                    received_at=old + 1,
                )
            )

            bridge.drain_queue()

            self.assertEqual(bridge.queue.status("q-old"), "stale_released")
            self.assertEqual(bridge.active_turn.queue_id, "q-new")
            self.assertEqual(len(repl.pasted), 1)
            self.assertIn("new message", repl.pasted[0])


if __name__ == "__main__":
    unittest.main()
