"""Language command boundaries for Claude, Cursor and Grok; no live services."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge_i18n import Language


def load(engine):
    path = ROOT / (engine + '-telegram-bridge.py')
    if not path.exists():
        path = ROOT / (engine + '_telegram_bridge.py')
    spec = importlib.util.spec_from_file_location('i18n_' + engine, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LanguageIsolationTests(unittest.TestCase):
    def test_engine_env_and_saved_selection_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CRB_LANGUAGE':'ko','CUB_LANGUAGE':'en'}, clear=True):
            cursor = Language(Path(tmp), default='ko', env_prefix='CUB', state_name='cursor-language.json')
            grok = Language(Path(tmp), default='ko', env_prefix='GRB', state_name='grok-language.json')
            self.assertEqual(cursor.code, 'en')
            self.assertEqual(grok.code, 'ko')
            cursor.command('/language ko')
            self.assertEqual(Language(Path(tmp), env_prefix='CUB', state_name='cursor-language.json').code, 'ko')
            self.assertFalse((Path(tmp)/'grok-language.json').exists())


class EngineLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selected = os.environ.get('BRIDGE_I18N_TEST_ENGINE')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        env = {prefix + key:value for prefix in ['CLB','CUB','GRB']
               for key,value in [('_DRY_RUN','1'),('_CHAT_ID','123'),('_STATE_DIR',cls.tmp.name)]}
        with patch.dict(os.environ, env):
            engines = [selected] if selected else [e for e in ['claude','cursor','grok']
                if (ROOT/(e+'-telegram-bridge.py')).exists() or (ROOT/(e+'_telegram_bridge.py')).exists()]
            cls.modules = {e:load(e) for e in engines}

    def test_cursor_and_grok_commands_never_reach_engine_or_queue(self):
        for engine in ['cursor','grok']:
            if engine not in self.modules:
                continue
            m = self.modules[engine]
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                language = Language(Path(tmp), default='ko')
                with patch.object(m,'LANGUAGE',language), patch.object(m,'deliver_mesh_event') as send, patch.object(m,'JOBS') as jobs:
                    with patch.object(m,'maybe_busy_inject_telegram') as inject:
                        m.handle_message_text('/language en')
                        self.assertEqual(language.code,'en')
                        self.assertIn('English',send.call_args.args[1])
                        self.assertEqual(send.call_args.args[0],'report')
                        jobs.put.assert_not_called(); inject.assert_not_called()
                        m.handle_message_text('/language ja')
                        self.assertEqual(language.code,'en')
                    self.assertEqual(m.telegram_prompt_from_update({'message':{'chat':{'id':'not-allowed'},'text':'/language ko'}}),'')

    def test_cursor_language_command_precedes_acp_control(self):
        if 'cursor' not in self.modules:
            self.skipTest('not a Cursor export')
        m=self.modules['cursor']
        with patch.object(m,'LANGUAGE',Language(default='ko')), patch.object(m,'deliver_mesh_event'), patch.object(m,'ide_enabled',side_effect=AssertionError('IDE must not receive language commands')), patch.object(m,'acp_enabled',return_value=True), patch.object(m,'acp_bridge') as acp:
            m.handle_message_text('/language en')
            acp.assert_not_called()

    def test_language_command_is_not_persisted_as_an_engine_job(self):
        for engine in ['cursor','grok']:
            if engine not in self.modules: continue
            m=self.modules[engine]
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                language=Language(Path(tmp),default='ko')
                update={'update_id':7,'message':{'chat':{'id':m.CHAT_ID},'text':'/language en'}}
                with patch.object(m,'LANGUAGE',language), patch.object(m,'tg',side_effect=[{'ok':True,'result':[update]},KeyboardInterrupt]), patch.object(m,'_read',return_value=''), patch.object(m,'_write'), patch.object(m,'health_mark'), patch.object(m,'deliver_mesh_event'), patch.object(m,'inbox_spool') as spool, patch.object(m,'handle_message_text') as handle:
                    with self.assertRaises(KeyboardInterrupt): m.telegram_poller()
                    spool.assert_not_called();handle.assert_not_called()
                    self.assertEqual(language.code,'en')

    def test_other_engine_answer_is_not_translated(self):
        raw='선택할 수 없는 모델입니다. /model 목록을 다시 확인하세요.'
        for engine in ['cursor','grok']:
            if engine not in self.modules: continue
            m=self.modules[engine]
            with self.subTest(engine=engine), patch.object(m,'LANGUAGE',Language(default='en')), patch.object(m,'deliver_mesh_event') as send:
                if engine=='cursor': m.deliver_cursor_answer(raw)
                else:
                    with patch.object(m,'CHAT_LANE','headless'): m.mirror_answer('telegram',raw)
                self.assertEqual(send.call_args.args[1],raw)

    def test_claude_allowlist_command_and_raw_answer(self):
        if 'claude' not in self.modules:
            self.skipTest('not a Claude export')
        m=self.modules['claude']
        with tempfile.TemporaryDirectory() as tmp:
            b=object.__new__(m.Bridge)
            b.config=SimpleNamespace(chat_id='123')
            b.language=Language(Path(tmp),default='ko')
            b.telegram=Mock(); b.repl=Mock(); b.queue=Mock()
            b.maybe_answer_approval_history=Mock(return_value=False)
            b.note_unknown_chat=Mock()
            b.enqueue_update({'message':{'chat':{'id':'999'},'text':'/language en'}})
            self.assertEqual(b.language.code,'ko')
            b.enqueue_update({'message':{'chat':{'id':'123'},'text':'/language en'}})
            self.assertEqual(b.language.code,'en')
            b.repl.inject.assert_not_called(); b.queue.append_status.assert_not_called()
            self.assertIn('English',b.telegram.send.call_args.args[0])
            client=m.TelegramClient('test','123','',4000,state_dir=Path(tmp))
            client.language=b.language
            client.call=Mock(return_value={'ok':True,'result':{'message_id':7}})
            raw='claude-telegram-bridge running'
            client.send(raw)
            self.assertEqual(client.call.call_args.kwargs['text'],raw)

    def test_claude_cards_translate_instructions_not_options(self):
        if 'claude' not in self.modules:
            self.skipTest('not a Claude export')
        m=self.modules['claude']
        language=Language(default='en')
        parsed={'title':'원문 질문','options':[(1,'원문 선택')], 'selected':1,'signature':'fixed','freeform':True,'kind':'menu'}
        text=m.choice_card_text(parsed, translate=language.text)
        self.assertIn('원문 질문',text);self.assertIn('원문 선택',text)
        self.assertIn('terminal',text)
        self.assertEqual(m.choice_keyboard(parsed)[0][0]['callback_data'],m.CHOICE_CALLBACK+'::fixed::1')
        self.assertIn('typed answer',m.ask_question_card_text(parsed,translate=language.text))

    def test_claude_setup_language_and_plain_buttons(self):
        if 'claude' not in self.modules:
            self.skipTest('not a Claude export')
        package=ROOT.parent/'packaging/claude-telegram-bridge/bridge_setup.py'
        if not package.exists(): package=ROOT/'bridge_setup.py'
        spec=importlib.util.spec_from_file_location('engine_language_setup',package)
        setup=importlib.util.module_from_spec(spec);sys.modules[spec.name]=setup;spec.loader.exec_module(setup)
        self.assertEqual(setup.build_parser().parse_args(['setup','--language','ko']).language,'ko')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            setup.write_private_config(config_file=root/'bridge.env',token_file=root/'token.json',registry_file=root/'registry.json',state_dir=root,token='TESTONLY-token',chat_id='123',language='ko')
            self.assertEqual(setup.load_env_file(root/'bridge.env')['CLB_LANGUAGE'],'ko')
            client=self.modules['claude'].TelegramClient('test','123','',4000,state_dir=root)
            client.language=Language(default='en');client.call=Mock()
            client.send_update_button('Original notice','unchanged-callback')
            button=json.loads(client.call.call_args.kwargs['reply_markup'])['inline_keyboard'][0][0]
            self.assertEqual(button,{'text':'Update now','callback_data':'unchanged-callback'})
            api=Mock(return_value={'ok':True})
            self.assertTrue(setup.send_test_message('test','123',api_call=api,language='ko'))
            self.assertIn('설정',api.call_args.kwargs['text'])

    def test_claude_model_controls_keep_identifiers_and_translate_notices(self):
        if 'claude' not in self.modules:
            self.skipTest('not a Claude export')
        m=self.modules['claude'];b=object.__new__(m.Bridge)
        b.language=Language(default='en');b.config=SimpleNamespace(chat_id='123')
        b.telegram=SimpleNamespace(call=Mock(),with_emoji_prefix=lambda text:text)
        b.queue=Mock();b.repl=Mock();b.current_session_model=Mock(return_value='')
        b.current_session_effort=Mock(return_value='high')
        with patch.object(m,'repl_supports_pane_features',return_value=True):
            b.handle_model_command(SimpleNamespace(text='/model',update_id=7))
            self.assertIn('Current session model:',b.telegram.call.call_args.kwargs['text'])
            b.handle_effort_command(SimpleNamespace(text='/effort',update_id=8))
            self.assertIn('Current session reasoning effort: high',b.telegram.call.call_args.kwargs['text'])
        self.assertIn('model-id',m.Bridge.model_apply_notice('model-id',True,'original-id',language='en'))
        self.assertIn('unconfirmed',m.Bridge.effort_apply_notice('high',False,'high',language='en'))

    def test_owned_wait_and_update_notices_are_english(self):
        if 'cursor' in self.modules:
            m=self.modules['cursor']
            with patch.object(m,'LANGUAGE',Language(default='en')):
                self.assertEqual(m._elapsed_words(3660),'1h 1m')
        if 'claude' in self.modules:
            m=self.modules['claude'];b=object.__new__(m.Bridge)
            b.language=Language(default='en');b.telegram=Mock()
            item=SimpleNamespace(busy_injected=True,received_at=0,text='원문 그대로',update_id=7)
            b.send_queue_stuck_notice([item],60)
            notice=b.telegram.send.call_args.args[0]
            self.assertIn('session input queue',notice);self.assertIn('원문 그대로',notice)
            result=m._self_update_failure_result('1.2.3','externally-managed-environment',1,allow_break_system_packages=False,language='en')
            self.assertEqual(result.status,'pep668_consent_required')
            self.assertIn('consent button',result.message)
            self.assertIn('--break-system-packages',result.message)


if __name__ == '__main__':
    unittest.main()
