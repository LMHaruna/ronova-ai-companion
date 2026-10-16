"""Provider routing, old vault migration and real Qt placement, entirely offline."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from chat_client import ChatClient, ChatConfig, ChatError
from client_credentials import crypt
from client_models import ClientModels, ModelProfile
from client_controller import DesktopController
from client_store import HistoryStore, Preferences
from client_calls import CallPlanner
from client_companion import SemanticAnalyzer
from client_fixtures import FakeCall, FakeVoice, FakeAnalyzer, fake_router
from local_voice import LocalVoice

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))


class Response:
    def __init__(self, text='凡人，先歇一会儿。', status=200, complete=True):
        self.text=text; self.status_code=status; self.complete=complete
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def close(self): pass
    def iter_lines(self,**kwargs):
        yield 'data: '+json.dumps({'choices':[{'delta':{'reasoning_content':'不应念出来'},'finish_reason':None}]})
        yield 'data: '+json.dumps({'choices':[{'delta':{'content':self.text},'finish_reason':'stop' if self.complete else None}]})
        if self.complete: yield 'data: [DONE]'


class ProviderProfiles(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.env=patch('client_models.environment',side_effect=lambda name:{'DASHSCOPE_API_KEY':'legacy-qwen','ALI_API_Key':'legacy-gpt'}.get(name,''))
        self.env.start(); self.models=ClientModels(self.directory.name)
    def tearDown(self): self.env.stop(); self.directory.cleanup()

    def test_provider_defaults_never_copy_legacy_keys(self):
        for name,model,url in [('openai','gpt-6-sol','https://api.openai.com/v1/chat/completions'),
                               ('deepseek','deepseek-flash','https://api.deepseek.com/chat/completions')]:
            profile=self.models.snapshot()[name]
            self.assertEqual((profile.model,profile.url,profile.api_key),(model,url,''))
            with self.assertRaises(ChatError): self.models.config(name).validate()

    def test_v1_vault_migrates_without_losing_customer_profiles(self):
        profiles={'qwen':ModelProfile('qwen-audio-3.1-realtime-plus','wss://customer.example/realtime','customer-qwen'),
                  'gpt':ModelProfile('customer-llm','https://customer.example/v1','customer-gpt')}
        payload=json.dumps({'version':1,'profiles':{k:asdict(v) for k,v in profiles.items()}}).encode()
        self.models.path.parent.mkdir(parents=True,exist_ok=True); self.models.path.write_bytes(crypt(payload))
        restored=ClientModels(self.directory.name)
        self.assertFalse(restored.warning)
        for name,profile in profiles.items(): self.assertEqual(restored.snapshot()[name],profile)
        self.assertFalse(restored.snapshot()['openai'].api_key); self.assertFalse(restored.snapshot()['deepseek'].api_key)
        restored.commit(restored.prepare('openai',replace(restored.snapshot()['openai'],api_key='customer-openai')))
        self.assertEqual(json.loads(crypt(restored.path.read_bytes(),decrypt=True))['version'],2)
        reloaded=ClientModels(self.directory.name)
        for name,profile in profiles.items(): self.assertEqual(reloaded.snapshot()[name],profile)
        self.assertEqual(reloaded.snapshot()['openai'].api_key,'customer-openai')
        self.assertFalse(reloaded.snapshot()['deepseek'].api_key)

    def test_new_keys_are_separate_encrypted_and_survive_restart(self):
        for name in ('openai','deepseek'):
            self.models.commit(self.models.prepare(name,replace(self.models.snapshot()[name],api_key='fixture-'+name)))
        reloaded=ClientModels(self.directory.name)
        for name in ('openai','deepseek'):
            self.assertEqual(reloaded.config(name).key(),'fixture-'+name)
            self.assertNotIn(('fixture-'+name).encode(),self.models.path.read_bytes())
            self.assertNotIn('fixture-'+name,repr(reloaded.snapshot()))

    def test_built_in_endpoint_cannot_send_provider_key_to_custom_host(self):
        for name in ('openai','deepseek'):
            with self.assertRaises(ValueError):
                self.models.prepare(name,replace(self.models.snapshot()[name],url='https://wrong.example/chat/completions',api_key='fixture-key'))

    def test_incomplete_v2_vault_clears_all_keys(self):
        payload=json.dumps({'version':2,'profiles':{'qwen':asdict(self.models.snapshot()['qwen'])}}).encode()
        self.models.path.parent.mkdir(parents=True,exist_ok=True); self.models.path.write_bytes(crypt(payload))
        reloaded=ClientModels(self.directory.name)
        self.assertTrue(reloaded.warning); self.assertTrue(all(not p.api_key for p in reloaded.snapshot().values()))


class ProviderTransport(unittest.TestCase):
    def test_qwen_flash_requests_fast_mode_without_injecting_options_into_other_custom_models(self):
        for model in ('qwen3.8-flash','customer-llm'):
            post=Mock(return_value=Response())
            chat=ChatClient(post=post,config=ChatConfig(model,'https://example.test/v1','fixture-key'))
            try:
                chat.reply('测试')
                body=post.call_args.kwargs['json']
                if model=='qwen3.8-flash':
                    self.assertIs(body['enable_thinking'],False)
                    self.assertTrue(chat.metrics['thinking_disabled'])
                else:
                    self.assertNotIn('enable_thinking',body)
                    self.assertFalse(chat.metrics['thinking_disabled'])
            finally: chat.close()

    def test_actual_stream_payload_uses_each_endpoint_key_and_options(self):
        for provider,model,url in [('openai','gpt-6-sol','https://api.openai.com/v1/chat/completions'),
                                   ('deepseek','deepseek-flash','https://api.deepseek.com/chat/completions')]:
            with self.subTest(provider=provider):
                post=Mock(return_value=Response()); config=ChatConfig(model,url,'fixture-'+provider,provider)
                chat=ChatClient(post=post,config=config); tokens=[]
                self.assertEqual(chat.reply('你好',tokens.append,max_tokens=600),'凡人，先歇一会儿。')
                args,kwargs=post.call_args
                self.assertEqual(args[0],url); self.assertEqual(kwargs['headers']['Authorization'],'Bearer fixture-'+provider)
                self.assertEqual(kwargs['json']['model'],model); self.assertFalse(kwargs['allow_redirects'])
                self.assertTrue(kwargs['json']['stream']); self.assertNotIn('audio',kwargs['json'])
                if provider=='openai':
                    self.assertEqual(kwargs['json']['max_completion_tokens'],600); self.assertNotIn('max_tokens',kwargs['json'])
                    self.assertEqual(kwargs['json']['reasoning_effort'],'none'); self.assertNotIn('temperature',kwargs['json'])
                else:
                    self.assertEqual(kwargs['json']['max_tokens'],600); self.assertEqual(kwargs['json']['thinking'],{'type':'disabled'})
                self.assertEqual(tokens,['凡人，先歇一会儿。']); self.assertNotIn('fixture-'+provider,json.dumps(chat.metrics))
                chat.close()

    def test_unset_provider_key_never_falls_back_to_ali_key(self):
        with patch.dict(os.environ,{'ALI_API_Key':'legacy-only','OPENAI_API_KEY':'','DEEPSEEK_API_KEY':''}):
            for name in ('openai','deepseek'):
                with self.assertRaises(ChatError): ChatConfig('m','https://example.test/chat/completions',provider=name).validate()

    def test_truncation_and_unauthorized_response_do_not_write_history(self):
        for name in ('openai','deepseek'):
            for response in (Response(complete=False),Response(status=401)):
                chat=ChatClient(post=lambda *a,**k:response,config=ChatConfig('m','https://example.test/chat/completions','fixture-secret',name))
                with self.assertRaises(ChatError) as caught: chat.reply('你好')
                self.assertEqual(len(chat.history),1); self.assertNotIn('fixture-secret',str(caught.exception)); chat.close()

    def test_background_analysis_and_call_planning_use_same_provider(self):
        for name in ('openai','deepseek'):
            config=ChatConfig('m','https://example.test/chat/completions','fixture-key',name)
            for service in (SemanticAnalyzer(name,config),CallPlanner(name,config)):
                self.assertIsInstance(service.client,ChatClient); self.assertIs(service.client.config,config)
                self.assertFalse(getattr(service.client,'outputs_audio',False)); service.close()


class SynthFixture(FakeVoice):
    def __init__(self): super().__init__(); self.spoken=[]
    def append(self,job,text): self.spoken.append(text)


class PipelineCompletion(unittest.TestCase):
    def test_opening_done_is_set_after_history_callback_returns(self):
        from reply_pipeline import ReplyPipeline
        started=threading.Event(); release=threading.Event()
        chat=ChatClient(post=lambda *a,**k:Response()); voice=SynthFixture()
        def event(data):
            if data['kind']=='completed': started.set(); release.wait(3)
        pipeline=ReplyPipeline(chat,voice,output=lambda text:None,status=lambda text:None,on_event=event)
        try:
            turn=pipeline.submit_opening('是我打来的。')
            self.assertTrue(started.wait(2))
            self.assertFalse(turn.done.is_set(),'completion must wait for the persisted history callback')
            release.set(); self.assertTrue(pipeline.wait_finished(3))
        finally: release.set(); pipeline.close(); chat.close()


class ProviderDesktop(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.synth=SynthFixture()
        self.env=patch('client_models.environment',side_effect=lambda name:'legacy-fixture' if name in ('DASHSCOPE_API_KEY','ALI_API_Key') else '')
        self.env.start(); self.constructor=patch('client_controller.LocalVoice',return_value=self.synth); self.voice_constructor=self.constructor.start()
        self.post=patch('requests.Session.post',return_value=Response()); self.requests=self.post.start()
        self.controller=DesktopController(self.directory.name,call_factory=FakeCall,analysis_factory=lambda *args:FakeAnalyzer())
    def tearDown(self):
        self.controller.close().result(5); self.post.stop(); self.constructor.stop(); self.env.stop(); self.directory.cleanup()
    def select(self,name):
        profile=self.controller.models.snapshot()[name]
        self.assertTrue(self.controller.configure_model(name,profile.model,profile.url,'fixture-'+name).result(4))
    def send(self,text):
        turn=self.controller.submit(text).result(3)
        self.assertTrue(self.controller.pipeline.wait_finished(4)); self.assertTrue(turn.done.is_set()); return turn

    def test_text_providers_share_local_synthesis_and_keep_backend_identity(self):
        for name in ('openai','deepseek','gpt'):
            self.select(name); chat,voice=self.controller.router.backends[name]
            self.assertIs(voice,self.synth); self.assertEqual(chat.backend,name)
            turn=self.send('来自 '+name)
            self.assertEqual(turn.backend,name); self.assertEqual(turn.metrics['backend'],name)
            self.assertEqual(self.controller.records()[-1]['backend'],name)
            self.assertIn('凡人，先歇一会儿。',''.join(self.synth.spoken))
        self.assertEqual(self.voice_constructor.call_count,1)
        self.assertIsNot(self.controller.router.backends['qwen'][1],self.synth)

    def test_real_local_voice_factory_is_shared_and_prepares_on_selection(self):
        # Default Qwen stays lazy; local selections share one prepared wrapper.
        with tempfile.TemporaryDirectory() as directory, patch('client_controller.LocalVoice',LocalVoice), \
                patch.object(LocalVoice,'prepare',return_value=threading.Event()) as prepare, \
                patch('subprocess.Popen',side_effect=AssertionError('worker start forbidden')):
            controller=DesktopController(directory,call_factory=FakeCall,analysis_factory=lambda *args:FakeAnalyzer())
            try:
                prepare.assert_not_called()
                voices=[]
                for name in ('openai','deepseek','gpt'):
                    profile=controller.models.snapshot()[name]
                    self.assertTrue(controller.configure_model(name,profile.model,profile.url,'fixture-'+name).result(4))
                    voices.append(controller.router.voice.snapshot())
                self.assertTrue(all(isinstance(v,LocalVoice) for v in voices)); self.assertIs(voices[0],voices[1]); self.assertIs(voices[1],voices[2])
                self.assertEqual(prepare.call_count,3)
                self.assertIsNone(voices[0]._process)
            finally: controller.close().result(5)

    def test_call_origin_reaches_each_provider_after_opening(self):
        for name in ('openai','deepseek'):
            self.select(name)
            identifier=self.controller.incoming_calls.ring({'reason':'关心答辩准备','opening':'是我，想问问你的答辩准备。'})
            self.assertFalse(self.controller.call.active)
            self.assertTrue(self.controller.answer_call(identifier).result(3)); self.assertTrue(self.controller.pipeline.wait_finished(4))
            self.send('你为什么打来？')
            messages=self.requests.call_args.kwargs['json']['messages']
            self.assertIn('【客户端来电事实】',messages[0]['content']); self.assertIn('关心答辩准备',messages[0]['content'])
            self.assertIn('若娜瓦',messages[0]['content']); self.assertEqual(self.controller.records()[-1]['backend'],name)
            self.controller.end_call().result(3)

    def test_history_restores_only_selected_provider_context(self):
        self.select('deepseek'); self.send('DeepSeek 独有话题')
        self.select('gpt'); self.send('千问文字模型独有话题')
        self.select('deepseek')
        text=''.join(m['content'] for m in self.controller.router.chat.snapshot().history[1:])
        self.assertIn('DeepSeek 独有话题',text); self.assertNotIn('千问文字模型独有话题',text)
        self.controller.close().result(5)
        restored=DesktopController(self.directory.name,call_factory=FakeCall,analysis_factory=lambda *args:FakeAnalyzer())
        try:
            self.assertEqual(restored.preferences.engine,'deepseek')
            self.assertEqual(restored.router.selected,'deepseek')
            self.assertIn('DeepSeek 独有话题',''.join(m['content'] for m in restored.router.chat.snapshot().history[1:]))
            self.assertNotIn('千问文字模型独有话题',''.join(m['content'] for m in restored.router.chat.snapshot().history[1:]))
        finally: restored.close().result(5)

    def test_invalid_key_leaves_active_call_and_old_model_untouched(self):
        self.select('openai'); self.controller.start_call().result(3)
        before=self.controller.models.snapshot()
        profile=before['deepseek']
        self.assertIsNone(self.controller.configure_model('deepseek',profile.model,profile.url,'').result(3))
        self.assertTrue(self.controller.call.active); self.assertEqual(self.controller.router.selected,'openai')
        self.assertEqual(self.controller.models.snapshot(),before)


class ProviderUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([]); cls.app.setQuitOnLastWindowClosed(False)
    def setUp(self):
        from client_app import ClientWindow
        self.env=patch('client_models.environment',return_value='ui-fixture-key'); self.env.start()
        self.directory=tempfile.TemporaryDirectory(); self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        self.window=ClientWindow(self.controller,ROOT); self.window.show(); self.app.processEvents()
    def tearDown(self):
        self.controller.close().result(5); self.window.close(); self.app.processEvents(); self.env.stop(); self.directory.cleanup()
    def seed(self,score):
        with self.controller.memory.connection:
            self.controller.memory.connection.execute('UPDATE profile SET affinity=?,stage=?',(score,'close' if score==100 else 'angry' if score<0 else 'acquaintance'))
        self.controller.memory.revision+=1; self.window.refresh(); self.app.processEvents()

    def test_home_meter_follows_real_score_and_does_not_belong_to_settings(self):
        from client_affinity import AffinityBar
        self.assertEqual(len(self.window.findChildren(AffinityBar)),1)
        self.assertIs(self.window.affinity_bar.parentWidget(),self.window)
        self.assertFalse(self.window.settings_pane.findChildren(AffinityBar))
        for value in (-20,-5,0,30,100):
            self.seed(value); self.assertEqual(self.window.affinity_bar.value(),value)
            self.assertIn(str(value),self.window.affinity_bar.toolTip())

    def test_meter_stays_above_heart_at_three_sizes_and_all_pages(self):
        self.seed(100)
        for width,height in ((1280,802),(960,602),(1440,960)):
            self.window.resize(width,height); self.app.processEvents()
            for page in ('home','call','settings'):
                self.window.screen=page; self.window.place_controls(); self.app.processEvents()
                meter=self.window.affinity_bar.geometry(); heart=self.window.oath_button.geometry()
                rail,_,_=self.window.affinity_bar.layout_geometry()
                center_x=meter.x()+rail.center().x()
                self.assertLess(meter.bottom(),heart.top()); self.assertLessEqual(abs(center_x-heart.center().x()),1)
                self.assertTrue(self.window.affinity_bar.isVisible()); self.assertEqual(self.window.oath_button.base_geometry,(700,665,68,64))

    def test_builtin_rows_use_real_model_names_and_only_key_fields(self):
        from PySide6.QtWidgets import QLineEdit
        pane=self.window.settings_pane
        rows={row.property('choice'):row for row in pane.model_rows}
        self.assertEqual(set(rows),{'qwen','deepseek','gpt'})
        for name in ('qwen','deepseek','gpt'):
            self.assertEqual(rows[name].title.text(),self.controller.models.snapshot()[name].model)
            self.assertEqual(set(pane.model_fields[name]),{'api_key'})
            self.assertEqual(pane.model_fields[name]['api_key'].echoMode(),QLineEdit.EchoMode.Password)
        self.assertEqual(rows['gpt'].title.text(),'qwen3.8-flash')
        self.assertIn(self.controller.models.snapshot()['qwen'].model,pane.engine.title.text())

    def test_render_home_call_and_model_choices(self):
        from PySide6.QtTest import QTest
        directory=Path(os.environ.get('RONOVA_PREVIEW_DIR',ROOT/'runtime/verification-records/model-providers'))
        directory.mkdir(parents=True,exist_ok=True)
        self.seed(100); QTest.qWait(400); self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(directory/'affinity-home.png')))
        self.window.open_call(); QTest.qWait(450); self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(directory/'affinity-call.png')))
        self.window.settings(); QTest.qWait(450)
        self.window.settings_pane.choose_models(); QTest.qWait(200); self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(directory/'provider-options.png')))

    def test_key_only_save_preserves_previously_saved_model_and_url(self):
        self.assertTrue(self.controller.configure_model('gpt','legacy-model','https://customer.example/v1','legacy-key').result(3))
        pane=self.window.settings_pane; pane.choose_models()
        self.assertEqual(pane.model_rows[-1].title.text(),'legacy-model')
        self.assertEqual(set(pane.model_fields['gpt']),{'api_key'})
        pane.model_fields['gpt']['api_key'].setText('updated-fixture-key'); pane.save_model('gpt')
        self.assertIsNotNone(pane.pending); self.assertTrue(pane.pending.result(3))
        profile=self.controller.models.snapshot()['gpt']
        self.assertEqual((profile.model,profile.url,profile.api_key),('legacy-model','https://customer.example/v1','updated-fixture-key'))


if __name__=='__main__': unittest.main()
