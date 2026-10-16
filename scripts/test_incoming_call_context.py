"""Real GPT/Qwen request assembly with offline transports and incoming-call facts."""
from pathlib import Path
import base64
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
CODE=Path(os.environ.get('RONOVA_CODE_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(CODE),str(CODE/'scripts'),str(ROOT),str(ROOT/'scripts')]
from chat_client import ChatClient,ChatConfig
from qwen_realtime import QwenRealtime,RealtimeConfig,MODEL
from voice_backends import BackendRouter
from client_calls import CallStore
from client_controller import DesktopController
from client_fixtures import FakeCall,FakeVoice,FakeAnalyzer,fake_router
from local_voice import SpeechJob

OPENING='昨天你说要准备答辩，今天准备得怎么样了？'
REASON='回访昨天约定的答辩准备'


class Response:
    status_code=200
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def iter_lines(self,**kwargs):
        yield 'data: '+json.dumps({'choices':[{'delta':{'content':'是我打来的，想问问你的准备情况。'},'finish_reason':'stop'}]})
        yield 'data: [DONE]'


class Socket:
    def __init__(self):
        self.sent=[]
        self.events=iter([{'type':'session.updated'},
            {'type':'response.audio.delta','delta':base64.b64encode(b'\x10\x00'*2400).decode()},
            {'type':'response.audio_transcript.done','transcript':'是我打来的，想问问你的准备情况。'},
            {'type':'response.done','response':{'status':'completed'}}])
    def send(self,raw): self.sent.append(json.loads(raw))
    def recv(self): return json.dumps(next(self.events))
    def close(self,**kwargs): pass
    def shutdown(self): pass


class CallContextBase(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        self.calls=self.controller.incoming_calls
    def tearDown(self):
        self.controller.close().result(5); self.directory.cleanup()
    def ring(self):
        return self.calls.ring({'reason':REASON,'opening':OPENING})
    def answer(self):
        identifier=self.ring(); self.assertTrue(self.controller.answer_call(identifier).result(3))
        self.assertTrue(self.controller.pipeline.wait_finished(3)); return identifier
    def messages(self,text='你怎么想起来打给我？'):
        messages,_=self.chat._messages(text); return messages
    def fact(self,messages=None):
        content=(messages or self.messages())[0]['content']
        if '【客户端来电事实】' not in content: return None
        payload=content.split('【客户端来电事实】',1)[1].split('\n',1)[1]
        return json.JSONDecoder().raw_decode(payload)[0]
    def wait_opening_job(self,job):
        deadline=time.monotonic()+3
        while job not in self.voice.jobs and time.monotonic()<deadline: time.sleep(.005)
        self.assertIn(job,self.voice.jobs)


class CallContextTests(CallContextBase):
    def test_completed_opening_supplies_initiator_reason_and_words(self):
        identifier=self.answer(); data=self.fact()
        self.assertEqual(data['发起者'],'若娜瓦'); self.assertEqual(data['接听者'],'用户')
        self.assertEqual(data['来电缘由'],REASON); self.assertEqual(data['范围'],'当前已接听来电')
        self.assertEqual(data['开场播放状态'],'completed'); self.assertEqual(data['已完整说过的开场'],OPENING)
        self.assertEqual(self.calls.store.find(identifier)['opening_state'],'completed')
        self.assertEqual([m['role'] for m in self.messages()],[ 'system','assistant','user'])
        self.assertEqual(self.controller.records()[0]['user'],'')
    def test_context_is_present_before_microphone_starts(self):
        identifier=self.ring(); captured=[]; original=self.controller.call.start
        def start(*args,**kwargs):
            captured.append(self.fact()); return original(*args,**kwargs)
        with patch.object(self.controller.call,'start',side_effect=start):
            self.controller.answer_call(identifier).result(3); self.controller.pipeline.wait_finished(3)
        self.assertEqual(captured[0]['发起者'],'若娜瓦'); self.assertEqual(captured[0]['状态'],'connecting')
        self.assertEqual(captured[0]['开场播放状态'],'pending')
        self.assertNotIn('已完整说过的开场',captured[0])
    def test_early_voice_interrupt_keeps_event_without_claiming_unheard_opening(self):
        identifier=self.ring(); job=SpeechJob()
        def speak(text): self.voice.jobs.append(job); return job
        with patch.object(self.voice,'speak',side_effect=speak):
            self.controller.answer_call(identifier).result(3); self.wait_opening_job(job)
            self.controller.pipeline.submit('是你给我打的吗？',input_metrics={})
            self.assertTrue(self.controller.pipeline.wait_finished(3))
        data=self.fact(); self.assertEqual(data['开场播放状态'],'interrupted')
        self.assertEqual(data['计划开场（未确认全部说完）'],OPENING)
        self.assertEqual(data['发起者'],'若娜瓦'); self.assertNotIn('已完整说过的开场',data)
        self.assertEqual(self.controller.records()[0]['state'],'cancelled')
        self.assertFalse(any(m['role']=='assistant' and m['content']==OPENING for m in self.chat.history))
    def test_call_event_survives_many_turns_and_history_budget_omission(self):
        self.answer()
        with self.chat._lock:
            self.chat.history.extend([m for n in range(30) for m in
                ({'role':'user','content':'继续聊'+str(n)},{'role':'assistant','content':'下一件事'+str(n)})])
        messages=self.messages(); self.assertFalse(any(m['content']==OPENING for m in messages[1:]))
        self.assertEqual(self.fact(messages)['已完整说过的开场'],OPENING)
    def test_ended_call_is_recalled_as_past_without_reviving_it(self):
        self.answer(); self.controller.end_call().result(3)
        data=self.fact(); self.assertEqual(data['范围'],'最近一次已接听来电（已经结束）')
        self.assertEqual(data['状态'],'ended'); self.assertFalse(self.controller.call.active)
        self.controller.start_call().result(3)
        self.assertEqual(self.fact()['范围'],'最近一次已接听来电（已经结束）')
    def test_restart_restores_call_fact_without_replaying_or_starting_capture(self):
        self.answer(); self.controller.close().result(5)
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        self.calls=self.controller.incoming_calls
        self.assertEqual(self.fact()['范围'],'最近一次已接听来电（已经结束）')
        self.assertEqual(self.fact()['已完整说过的开场'],OPENING)
        self.assertEqual(self.voice.replayed,[]); self.assertEqual(self.controller.call.starts,[])
    def test_declined_and_missed_calls_do_not_become_accepted_events(self):
        identifier=self.ring(); self.controller.decline_call(identifier).result(3)
        self.assertIsNone(self.fact())
        identifier=self.ring(); self.calls.dismiss('missed',identifier)
        self.assertIsNone(self.fact()); self.assertEqual(self.controller.records(),[])
    def test_failed_synthesis_does_not_claim_spoken_words(self):
        identifier=self.ring(); job=SpeechJob(); job.error='offline failure'; job.finished.set()
        with patch.object(self.voice,'speak',return_value=job):
            self.controller.answer_call(identifier).result(3); self.controller.pipeline.wait_finished(3)
        self.assertEqual(self.fact()['开场播放状态'],'failed')
        self.assertNotIn('已完整说过的开场',self.fact())
    def test_late_opening_completion_cannot_overwrite_interruption(self):
        identifier=self.ring(); self.calls.claim(identifier)
        self.calls.store.opening_result(identifier,'interrupted')
        self.calls.store.opening_result(identifier,'completed')
        self.assertEqual(self.calls.store.find(identifier)['opening_state'],'interrupted')
    def test_clear_history_removes_call_facts_and_late_events_do_not_restore_them(self):
        identifier=self.answer(); self.controller.clear_history().result(3)
        self.calls.opening_event({'kind':'completed','call_id':identifier})
        self.assertIsNone(self.fact()); self.assertEqual(self.calls.store.recent(),[])
    def test_memory_edit_excludes_old_call_topics_from_future_context(self):
        self.answer(); self.controller.configure_memory(enabled=False).result(3)
        self.assertIsNone(self.fact())
        self.assertNotIn(REASON,self.messages()[0]['content']); self.assertNotIn(OPENING,self.messages()[0]['content'])
    def test_test_calls_are_identified_without_inventing_real_world_reasons(self):
        identifier=self.controller.test_incoming_call().result(3)
        self.controller.answer_call(identifier).result(3); self.controller.pipeline.wait_finished(3)
        self.assertTrue(self.fact()['测试来电']); self.assertEqual(self.fact()['来电缘由'],'测试来电')
    def test_call_ledger_clear_removes_model_fact_even_if_history_is_retained(self):
        self.answer(); self.calls.store.clear(); self.assertIsNone(self.fact())


class RequestWireTests(CallContextBase):
    """Use the production encoders; only their network endpoints are fixtures."""
    def test_qwen_wire_sends_call_fact_and_opening_on_each_reply(self):
        sockets=[]
        def connector(config): ws=Socket(); sockets.append(ws); return ws
        chat=QwenRealtime(RealtimeConfig('wss://example.invalid/realtime',api_key='offline-call-context'),connector=connector)
        self.router.backends['qwen']=(chat,self.voice); self.chat=chat; self.controller.restore_contexts()
        self.answer()
        self.controller.pipeline.submit('你为什么主动来电？',input_metrics={}); self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.assertEqual(len(sockets),1)
        wire=sockets[0].sent
        data=self.fact([{'content':wire[0]['session']['instructions']}])
        self.assertEqual(data['发起者'],'若娜瓦'); self.assertEqual(data['来电缘由'],REASON)
        items=[event['item'] for event in wire if event['type']=='conversation.item.create']
        self.assertEqual([(item['role'],item['content'][0]['text']) for item in items],
                         [('assistant',OPENING),('user','你为什么主动来电？')])
        self.controller.pipeline.submit('刚才那通电话想问什么？',input_metrics={}); self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.assertEqual(self.fact([{'content':sockets[1].sent[0]['session']['instructions']}])['来电缘由'],REASON)
    def test_gpt_wire_preserves_call_fact_after_opening_interrupt(self):
        received=[]
        def post(*args,**kwargs): received.append(kwargs['json']); return Response()
        chat=ChatClient(config=ChatConfig(api_key='offline-call-context'),post=post)
        self.controller.close().result(5)
        self.voice=FakeVoice()
        self.router=BackendRouter(chat,self.voice,gpt_factory=lambda:(chat,self.voice))
        self.router.analysis_factory=lambda *args:FakeAnalyzer()
        # Keep a Qwen fixture for startup, then select GPT through normal preferences.
        fixture,_,_=fake_router(); self.router.qwen_factory=lambda:fixture.backends.get('qwen') or fixture.qwen_factory()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        self.calls=self.controller.incoming_calls
        from dataclasses import replace
        self.controller.apply_preferences(replace(self.controller.preferences,engine='gpt')).result(3)
        self.chat=chat; self.controller.restore_contexts()
        identifier=self.ring(); job=SpeechJob()
        def speak(text): self.voice.jobs.append(job); return job
        with patch.object(self.voice,'speak',side_effect=speak):
            self.controller.answer_call(identifier).result(3); self.wait_opening_job(job)
            self.controller.pipeline.submit('是你主动打来的吗？',input_metrics={}); self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.assertEqual(len(received),1)
        data=self.fact(received[0]['messages']); self.assertEqual(data['发起者'],'若娜瓦')
        self.assertEqual(data['开场播放状态'],'interrupted')
        self.assertEqual([m['role'] for m in received[0]['messages']],['system','user'])


class MigrationTests(unittest.TestCase):
    def test_old_ledger_keeps_accepted_call_without_assuming_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.sqlite3'; db=sqlite3.connect(path)
            db.execute('''CREATE TABLE calls (id TEXT PRIMARY KEY, backend TEXT NOT NULL, reason TEXT NOT NULL,
                opening TEXT NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,test INTEGER NOT NULL,
                created REAL NOT NULL,updated REAL NOT NULL,due REAL NOT NULL DEFAULT 0,
                expires REAL NOT NULL DEFAULT 0,reminded INTEGER NOT NULL DEFAULT 0)''')
            for identifier,state in (('ended','ended'),('declined','declined'),('connecting','connecting')):
                db.execute('INSERT INTO calls(id,backend,reason,opening,fingerprint,state,test,created,updated) VALUES (?,?,?,?,?,?,?,?,?)',
                    (identifier,'qwen',REASON,OPENING,'topic',state,0,1000,1100))
            db.commit(); db.close(); store=CallStore(path)
            try:
                self.assertEqual(store.find('ended')['opening_state'],'unknown')
                self.assertEqual(store.find('ended')['accepted_at'],1000)
                self.assertEqual(store.find('declined')['accepted_at'],0)
                self.assertEqual(store.find('connecting')['state'],'interrupted')
                self.assertEqual(store.latest_accepted()['id'],'connecting')
            finally: store.close()


if __name__=='__main__':
    suite=unittest.TestSuite(); suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(CallContextTests))
    for name in RequestWireTests.__dict__:
        if name.startswith('test_'): suite.addTest(RequestWireTests(name))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MigrationTests))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    directory=Path(os.environ.get('RONOVA_PREVIEW_DIR',CODE/'verification')); directory.mkdir(parents=True,exist_ok=True)
    report={'tests':result.testsRun,'passed':result.wasSuccessful(),'cloud_requests':0,'physical_microphone_started':False,
            'failures':[(str(t),text) for t,text in result.failures], 'errors':[(str(t),text) for t,text in result.errors]}
    (directory/'call-context-tests.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    sys.exit(not result.wasSuccessful())
