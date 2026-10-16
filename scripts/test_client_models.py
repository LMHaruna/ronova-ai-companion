"""Customer settings, real Windows DPAPI and mocked requests; no cloud/mic."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chat_client import ChatClient, ChatConfig, ChatError
from client_credentials import crypt
from client_models import ClientModels, ModelProfile
from client_controller import DesktopController
from qwen_realtime import MODEL, RealtimeConfig, connect
from client_fixtures import FakeCall, fake_router


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch('client_models.environment',return_value='environment-fixture-key'); self.env.start()
        self.models = ClientModels(self.directory.name)
    def tearDown(self): self.env.stop(); self.directory.cleanup()

    def test_defaults_prefill_current_endpoints_and_maskable_environment_credentials(self):
        profiles = self.models.snapshot()
        self.assertEqual(profiles['qwen'].model,MODEL)
        self.assertEqual(profiles['gpt'].model,'qwen3.8-flash')
        self.assertTrue(profiles['gpt'].url.endswith('/chat/completions'))
        self.assertEqual(profiles['qwen'].api_key,'environment-fixture-key')
        self.assertNotIn('environment-fixture-key',repr(profiles))

    def test_real_dpapi_roundtrip_is_atomic_and_contains_no_plaintext_key(self):
        profile = ModelProfile('customer-llm','https://customer.example/v1','customer-fixture-key')
        self.models.commit(self.models.prepare('gpt',profile))
        raw = self.models.path.read_bytes()
        self.assertNotIn(b'customer-fixture-key',raw)
        self.assertEqual(json.loads(crypt(raw,decrypt=True))['profiles']['gpt']['api_key'],'customer-fixture-key')
        restored = ClientModels(self.directory.name)
        self.assertEqual(restored.snapshot()['gpt'],profile)
        self.assertFalse(self.models.path.with_name(self.models.path.name+'.tmp').exists())

    def test_customer_key_overrides_owner_environment_after_restart(self):
        profile = replace(self.models.snapshot()['qwen'],api_key='customer-qwen-key')
        self.models.commit(self.models.prepare('qwen',profile))
        restored = ClientModels(self.directory.name)
        with patch('qwen_realtime.environment',return_value='owner-environment-key'):
            self.assertEqual(restored.config('qwen').key(),'customer-qwen-key')
            restored.config('qwen').validate()

    def test_encryption_and_write_failure_preserve_previous_settings(self):
        before = self.models.snapshot()
        draft = replace(before['gpt'],api_key='new-fixture-key')
        with patch('client_models.crypt',side_effect=RuntimeError('encryption failed')):
            with self.assertRaises(RuntimeError): self.models.prepare('gpt',draft)
        self.assertEqual(self.models.snapshot(),before)
        prepared = self.models.prepare('gpt',draft)
        with patch('client_models.write_encrypted',side_effect=OSError('disk failed')):
            with self.assertRaises(OSError): self.models.commit(prepared)
        self.assertEqual(self.models.snapshot(),before)

    def test_unreadable_vault_blocks_silent_owner_key_fallback(self):
        self.models.path.parent.mkdir(parents=True,exist_ok=True)
        self.models.path.write_bytes(b'corrupt fixture')
        restored = ClientModels(self.directory.name)
        self.assertTrue(restored.warning)
        self.assertTrue(all(not p.api_key for p in restored.snapshot().values()))
        with self.assertRaises(ChatError): restored.config('qwen').validate()

    def test_query_model_is_normalized_and_bound_voice_model_is_checked(self):
        profile = self.models.snapshot()['qwen']
        config = self.models.config('qwen',replace(profile,url=profile.url+'?model=old-value'))
        self.assertTrue(config.endpoint().endswith('?model='+MODEL))
        with self.assertRaises(ChatError): self.models.config('qwen',replace(profile,model='incompatible-model')).validate()
        for url in ('wss://example.test?api_key=private','wss://example.test#fragment','wss://user:pass@example.test'):
            with self.assertRaises((ValueError,ChatError)):
                self.models.config('qwen',replace(profile,url=url)).endpoint()

    def test_invalid_profiles_and_empty_keys_never_save(self):
        profile = self.models.snapshot()['gpt']
        for draft in (replace(profile,model=''),replace(profile,model='a b'),replace(profile,api_key=''),
                      replace(profile,api_key='a\nb'),replace(profile,url='https://example.test?api_key=x')):
            with self.assertRaises((ValueError,ChatError)): self.models.prepare('gpt',draft)
        self.assertFalse(self.models.path.exists())


class TransportTests(unittest.TestCase):
    def test_llm_uses_customer_model_url_and_key_and_keeps_local_synthesis_route(self):
        response = Mock(); response.status_code = 200; response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.iter_lines.return_value = ['data: '+json.dumps({'choices':[{'delta':{'content':'凡人，休息。'}}]}),'data: [DONE]']
        post = Mock(return_value=response)
        config = ChatConfig('customer-llm','https://customer.example/v1','customer-key')
        chat = ChatClient(post=post,config=config)
        with patch.dict(os.environ,{'ALI_API_Key':'owner-key'}): self.assertEqual(chat.reply('测试'),'凡人，休息。')
        args,options = post.call_args
        self.assertEqual(args[0],'https://customer.example/v1/chat/completions')
        self.assertEqual(options['json']['model'],'customer-llm')
        self.assertEqual(options['headers']['Authorization'],'Bearer customer-key')
        self.assertFalse(options['allow_redirects'])
        self.assertFalse(getattr(chat,'outputs_audio',False))
        self.assertNotIn('customer-key',json.dumps(chat.metrics))

    def test_websocket_uses_customer_key_and_model_without_environment_fallback(self):
        ws = Mock(); module = types.SimpleNamespace(enableTrace=Mock(),create_connection=Mock(return_value=ws))
        config = RealtimeConfig('wss://customer.example/realtime',model='customer-realtime',api_key='customer-ws-key')
        with patch.dict(sys.modules,{'websocket':module}),patch('qwen_realtime.environment',side_effect=AssertionError('fallback forbidden')):
            config.validate(); self.assertIs(connect(config),ws)
        args,options = module.create_connection.call_args
        self.assertEqual(args[0],'wss://customer.example/realtime?model=customer-realtime')
        self.assertEqual(options['header'],{'Authorization':'Bearer customer-ws-key'})
        self.assertEqual(options['redirect_limit'],0)
        self.assertNotIn('customer-ws-key',repr(config))

    def test_urls_reject_embedded_credentials_and_remote_cleartext_but_allow_local_llm(self):
        for url in ('http://remote.example/v1','https://user:pass@example.test/v1','https://example.test/v1?key=x'):
            with self.assertRaises(ChatError): ChatConfig('m',url,'key').validate()
        self.assertEqual(ChatConfig('m','http://127.0.0.1:1234/v1','key').endpoint(),'http://127.0.0.1:1234/v1/chat/completions')


class DesktopModelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch('client_models.environment',return_value='fixture-env-key'); self.env.start()
        self.router,self.chat,self.voice = fake_router()
        self.controller = DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
    def tearDown(self):
        self.controller.close().result(5); self.env.stop(); self.directory.cleanup()

    def test_gpt_configuration_updates_text_transport_and_retains_existing_local_voice(self):
        local_voice = self.router.backends['gpt'][1]
        self.assertTrue(self.controller.configure_model('gpt','customer-llm','https://customer.example/v1','customer-gpt-key').result(3))
        chat,voice = self.router.backends['gpt']
        self.assertEqual(chat.config.model,'customer-llm'); self.assertIs(voice,local_voice)
        self.assertFalse(chat.outputs_audio); self.assertEqual(self.router.selected,'gpt')
        self.assertEqual(self.controller.preferences.engine,'gpt')
        self.assertEqual(chat.calls,0); self.assertFalse(self.controller.call.active)
        for path in Path(self.directory.name).rglob('*.json'):
            self.assertNotIn('customer-gpt-key',path.read_text())

    def test_qwen_settings_apply_to_dialogue_and_replay_without_requests(self):
        profile = self.controller.models.snapshot()['qwen']
        self.assertTrue(self.controller.configure_model('qwen',profile.model,'wss://customer.example/realtime','customer-qwen-key').result(3))
        self.assertIs(self.chat.config,self.voice.config)
        self.assertEqual(self.chat.config.key(),'customer-qwen-key')
        self.assertEqual(self.chat.config.voice,self.controller.models.realtime.voice)
        self.assertEqual(self.chat.calls,0); self.assertEqual(self.voice.replayed,[])

    def test_invalid_and_encryption_failure_leave_running_call_intact(self):
        self.controller.start_call().result(2)
        stops = len(self.controller.call.stops)
        self.assertIsNone(self.controller.configure_model('gpt','customer','https://customer.example/v1','').result(3))
        with patch('client_models.crypt',side_effect=RuntimeError('fixture encryption failure')):
            self.assertIsNone(self.controller.configure_model('gpt','customer','https://customer.example/v1','fixture-key').result(3))
        self.assertEqual(len(self.controller.call.stops),stops); self.assertTrue(self.controller.call.active)
        self.assertEqual(self.router.selected,'qwen')

    def test_changing_credentials_stops_current_call_and_does_not_restart_capture(self):
        self.controller.start_call().result(2)
        profile = self.controller.models.snapshot()['qwen']
        self.assertTrue(self.controller.configure_model('qwen',profile.model,profile.url,'new-customer-key').result(3))
        self.assertFalse(self.controller.call.active); self.assertFalse(self.controller.call_requested)
        self.assertEqual(len(self.controller.call.starts),1); self.assertEqual(self.chat.calls,0)

    def test_saving_identical_settings_preserves_running_call_and_context_revision(self):
        self.controller.start_call().result(2)
        before = self.chat._revision; stops = len(self.controller.call.stops)
        profile = self.controller.models.snapshot()['qwen']
        self.assertTrue(self.controller.configure_model('qwen',profile.model,profile.url,profile.api_key).result(3))
        self.assertEqual(self.chat._revision,before); self.assertEqual(len(self.controller.call.stops),stops)
        self.assertTrue(self.controller.call.active)


if __name__=='__main__': unittest.main()
