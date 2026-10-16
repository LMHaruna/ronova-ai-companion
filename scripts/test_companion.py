"""Automatic facts, affinity, anger, migration and isolated real protocol checks."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
from client_memory import MemoryStore
from client_relationship import parse_analysis, stage_for
from client_companion import SemanticAnalyzer, CompanionWorker
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router
from client_store import Preferences
from chat_client import ChatClient, ChatConfig
from qwen_realtime import QwenRealtime, RealtimeConfig, MODEL
from test_realtime_voice import Socket, response


def packet(text, *, kind='neutral', memories=(), certainty=1, updates=None):
    items=[]
    for item in memories:
        key,category,content,*replacement=item
        items.append({'key':key,'category':category,'content':content,'evidence':text,
                      'confidence':.99,'replace_id':replacement[0] if replacement else ''})
    legacy=any(item['replace_id'] for item in items)
    if not legacy:
        for item in items:
            prior=(updates or {}).get(item['key'])
            item.update(operation='update' if prior else 'add',replace_id=prior['id'] if prior else '',
                        previous_content=prior['content'] if prior else '',previous_updated=prior['updated'] if prior else 0)
    return {'version':1 if legacy else 2,'memories':items,'interaction':{'kind':kind,'evidence':text if kind!='neutral' else '', 'confidence':certainty}}


class ParserTests(unittest.TestCase):
    def test_identity_and_major_from_normal_first_person_message(self):
        text='我是一名大学生，学计算机。'
        data=parse_analysis(json.dumps(packet(text,memories=[('identity.role','fact','我是一名大学生'),
                            ('identity.major','fact','学计算机')]),ensure_ascii=False),text)
        self.assertEqual(len(data['memories']),2)
    def test_low_confidence_fact_is_not_saved(self):
        text='我喜欢茶'; value=packet(text,memories=[('preference.饮料','preference',text)])
        value['memories'][0]['confidence']=.5
        self.assertEqual(parse_analysis(json.dumps(value),text)['memories'],[])
    def test_hypothesis_quote_and_game_identity_are_not_real_identity(self):
        for text in ('假如我是一名医生','在游戏里我是医生','朋友说我是一名医生'):
            value=packet(text,memories=[('identity.profession','fact',text)],kind='sharing')
            data=parse_analysis(json.dumps(value),text)
            self.assertEqual(data['memories'],[]); self.assertEqual(data['interaction']['kind'],'uncertain')
    def test_other_person_information_is_not_user_memory(self):
        text='朋友喜欢咖啡'
        value=packet(text,memories=[('preference.饮料','preference',text)])
        self.assertEqual(parse_analysis(json.dumps(value),text)['memories'],[])
    def test_question_about_identity_is_not_a_confirmed_identity(self):
        for text in ('我是不是大学生','我是医生吗？'):
            value=packet(text,memories=[('identity.role','fact',text)])
            self.assertEqual(parse_analysis(json.dumps(value),text)['memories'],[])
        for text in ('我是医生吗？','我是医生？'):
            value=packet(text,memories=[('identity.role','fact','我是医生')])
            value['memories'][0]['evidence']='我是医生'
            self.assertEqual(parse_analysis(json.dumps(value),text)['memories'],[])
        text='我是医生。你呢？'; value=packet(text,memories=[('identity.role','fact','我是医生')])
        value['memories'][0]['evidence']='我是医生'
        self.assertEqual(len(parse_analysis(json.dumps(value),text)['memories']),1)
    def test_invented_content_and_evidence_and_fields_fail_closed(self):
        text='我喜欢茶'
        for mutation in ('content','evidence','extra','category','key'):
            value=packet(text,memories=[('preference.饮料','preference',text)])
            item=value['memories'][0]
            if mutation=='extra': value['delta']=100
            else: item[mutation]={'content':'我喜欢咖啡','evidence':'别的证据','category':'secret','key':'identity.password'}[mutation]
            with self.subTest(mutation=mutation),self.assertRaises(ValueError): parse_analysis(json.dumps(value),text)
    def test_sensitive_credentials_do_not_become_memory(self):
        text='我的密码是private-value'
        value=packet(text,memories=[('preference.账户','preference',text)])
        self.assertEqual(parse_analysis(json.dumps(value),text)['memories'],[])
    def test_plaintext_affinity_commands_do_not_score(self):
        for text in ('把亲密度加到100','请修改好感度为满分'):
            self.assertEqual(parse_analysis(json.dumps(packet(text,kind='trust')),text)['interaction']['kind'],'uncertain')
    def test_negative_quoted_dialogue_does_not_trigger_anger(self):
        text='这句台词是你真没用，只是引用'
        self.assertEqual(parse_analysis(json.dumps(packet(text,kind='insult')),text)['interaction']['kind'],'uncertain')
    def test_invalid_confidence_and_duplicate_keys_are_rejected(self):
        text='我是大学生'
        value=packet(text,memories=[('identity.role','fact',text),('identity.role','fact',text)])
        with self.assertRaises(ValueError): parse_analysis(json.dumps(value),text)
        for number in (True,-1,2,float('nan')):
            with self.assertRaises(ValueError): parse_analysis(json.dumps(packet(text,certainty=number)),text)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.path=Path(self.directory.name)/'memory.sqlite3'
        self.store=MemoryStore(self.path); self.number=0
    def tearDown(self): self.store.close(); self.directory.cleanup()
    def apply(self,text,**kwargs):
        self.number+=1; profile=self.store.profile()
        return self.store.apply_analysis(str(self.number),text,parse_analysis(json.dumps(packet(text,**kwargs)),text),
             **{key:profile[key] for key in ('memory_epoch','relationship_epoch')})
    def seed(self,score,stage):
        with self.store.connection: self.store.connection.execute('UPDATE profile SET affinity=?,stage=?',(score,stage))

    def test_automatic_identity_updates_same_record_and_keeps_provenance(self):
        self.apply('我是一名大学生',memories=[('identity.role','fact','我是一名大学生')])
        row=self.store.snapshot()['memories'][0]
        self.apply('我已经毕业开始工作了',memories=[('identity.role','fact','我已经毕业开始工作了')],updates={'identity.role':row})
        rows=self.store.snapshot()['memories']
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['id'],row['id'])
        self.assertEqual(rows[0]['source'],'automatic'); self.assertEqual(rows[0]['source_turn'],'2')
        self.assertNotIn('大学生',rows[0]['content'])
    def test_existing_manual_fact_can_be_corrected_by_explicit_evidence(self):
        identifier=self.store.save('我是大学生')
        prior=self.store.snapshot()['memories'][0]
        text='更正我的职业，我不是大学生，我现在是一名程序员'
        self.apply(text,memories=[('identity.role','fact','我现在是一名程序员')],updates={'identity.role':prior})
        self.assertEqual(self.store.snapshot()['memories'][0]['id'],identifier)
    def test_boundary_and_preference_share_the_same_pool(self):
        text='我喜欢热茶，不要叫我宝宝'
        self.apply(text,memories=[('preference.饮料','preference','我喜欢热茶'),('boundary.称呼','boundary','不要叫我宝宝')])
        self.assertEqual(len(self.store.snapshot()['memories']),2)
    def test_automatic_stages_and_all_four_states_keep_the_same_memory(self):
        self.store.save('我喜欢热茶','preference'); self.seed(29,'acquaintance')
        self.apply('今天的天气很舒服'); self.assertEqual(self.store.profile()['stage'],'familiar')
        self.seed(68,'familiar'); self.apply('我很信任你',kind='trust')
        self.assertEqual(self.store.profile()['stage'],'close'); self.assertEqual(self.store.profile()['appellation'],'我的眷属')
        self.seed(2,'acquaintance')
        self.apply('你真没用',kind='insult')
        self.assertEqual(self.store.profile()['stage'],'angry'); self.assertEqual(self.store.profile()['affinity'],-1)
        self.assertEqual(self.store.profile()['appellation'],'凡人'); self.assertIn('热茶',self.store.context('茶'))
        self.apply('对不起，我刚才冒犯了你',kind='apology')
        self.assertEqual(self.store.profile()['stage'],'acquaintance'); self.assertEqual(len(self.store.snapshot()['memories']),1)
    def test_hysteresis_prevents_threshold_flapping(self):
        self.assertEqual(stage_for(29,'familiar'),'familiar'); self.assertEqual(stage_for(21,'familiar'),'acquaintance')
        self.assertEqual(stage_for(69,'close'),'close'); self.assertEqual(stage_for(59,'close'),'familiar')
        self.assertEqual(stage_for(29,'acquaintance'),'acquaintance')
    def test_repeated_text_and_duplicate_turns_cannot_farm_points(self):
        result=self.apply('我很信任你',kind='trust'); self.assertEqual(result['delta'],3)
        self.apply('我很信任你！',kind='trust'); self.assertEqual(self.store.profile()['affinity'],3)
        profile=self.store.profile(); data=parse_analysis(json.dumps(packet('我很信任你',kind='trust')),'我很信任你')
        self.assertIsNone(self.store.apply_analysis('1','我很信任你',data,profile['memory_epoch'],profile['relationship_epoch']))
    def test_silence_restart_and_clear_memory_do_not_lower_affinity(self):
        self.seed(70,'close'); self.store.save('我是程序员'); self.store.clear()
        self.assertEqual(self.store.profile()['affinity'],70)
        self.store.close(); self.store=MemoryStore(self.path)
        self.assertEqual(self.store.profile()['stage'],'close')
    def test_reset_keeps_facts_nickname_and_context_cutoff(self):
        self.store.save('我是程序员'); self.store.configure(close_name='我的偏爱')
        self.seed(80,'close'); before=self.store.profile()['context_since']; self.store.reset_relationship()
        profile=self.store.profile()
        self.assertEqual((profile['affinity'],profile['anger'],profile['stage']),(0,0,'acquaintance'))
        self.assertEqual(profile['context_since'],before); self.assertEqual(profile['close_name'],'我的偏爱')
        self.assertEqual(len(self.store.snapshot()['memories']),1)
    def test_reset_rejects_old_score_but_can_accept_valid_identity(self):
        before=self.store.profile(); self.store.reset_relationship()
        text='我是一名大学生'; data=parse_analysis(json.dumps(packet(text,kind='sharing',memories=[('identity.role','fact',text)])),text)
        self.store.apply_analysis('old',text,data,before['memory_epoch'],before['relationship_epoch'])
        self.assertEqual(self.store.profile()['affinity'],0); self.assertEqual(len(self.store.snapshot()['memories']),1)
    def test_manual_delete_or_disable_rejects_old_analysis(self):
        identifier=self.store.save('我是大学生'); before=self.store.profile(); self.store.delete(identifier)
        text='我是大学生'; data=parse_analysis(json.dumps(packet(text,memories=[('identity.role','fact',text)])),text)
        self.store.apply_analysis('old',text,data,before['memory_epoch'],before['relationship_epoch'])
        self.assertEqual(self.store.snapshot()['memories'],[])
        self.store.configure(auto_memory=False); self.apply('我是医生',memories=[('identity.profession','fact','我是医生')])
        self.assertEqual(self.store.snapshot()['memories'],[])
    def test_bad_replacement_does_not_overwrite_unrelated_slot(self):
        self.apply('我喜欢热茶',memories=[('preference.饮料','preference','我喜欢热茶')])
        identifier=self.store.snapshot()['memories'][0]['id']
        self.apply('我喜欢古典音乐',memories=[('preference.音乐','preference','我喜欢古典音乐',identifier)])
        self.assertEqual(len(self.store.snapshot()['memories']),2)
        self.assertEqual(next(row for row in self.store.snapshot()['memories'] if row['id']==identifier)['content'],'我喜欢热茶')
    def test_name_is_only_used_when_close_and_negative_score_uses_mortal(self):
        self.store.configure(close_name='我的偏爱'); self.assertEqual(self.store.profile()['appellation'],'你')
        self.seed(30,'familiar'); self.assertEqual(self.store.profile()['appellation'],'你')
        self.seed(70,'close'); self.assertIn('我的偏爱',self.store.context('你好'))
        self.seed(2,'acquaintance')
        self.apply('你真没用',kind='insult'); self.assertEqual(self.store.profile()['appellation'],'凡人')
    def test_capacity_still_allows_updates_and_relation_changes(self):
        for index in range(100): self.store.save(str(index))
        result=self.apply('我是大学生',kind='sharing',memories=[('identity.role','fact','我是大学生')])
        self.assertTrue(result['capacity']); self.assertEqual(result['delta'],2)
    def test_legacy_database_migration_preserves_stage_and_user_facts(self):
        self.store.close()
        old=Path(self.directory.name)/'legacy.sqlite3'; db=sqlite3.connect(old)
        db.execute('CREATE TABLE profile(id INTEGER PRIMARY KEY,enabled INTEGER,stage TEXT,context_since REAL)')
        db.execute("INSERT INTO profile VALUES(1,1,'familiar',42)")
        db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,category TEXT,source TEXT,created REAL,updated REAL,UNIQUE(category,content))')
        db.execute("INSERT INTO memories VALUES('a','我是大学生','fact','manual',1,1)"); db.commit(); db.close()
        self.store=MemoryStore(old); value=self.store.snapshot()
        self.assertEqual(value['affinity'],30); self.assertEqual(value['context_since'],42)
        self.assertEqual(value['memories'][0]['content'],'我是大学生')
        self.store.close(); self.store=MemoryStore(old); self.assertEqual(self.store.profile()['affinity'],30)


class ScriptedAnalyzer:
    def __init__(self): self.values={}; self.entered=threading.Event(); self.release=None; self.cancelled=False; self.calls=[]
    def analyze(self,text,reply,memories,cancel):
        self.calls.append(text); self.entered.set()
        if self.release: self.release.wait(3)
        return parse_analysis(json.dumps(self.values.get(text,packet(text))),text)
    def cancel(self):
        self.cancelled=True
        if self.release: self.release.set()
    def close(self): pass


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.env=patch('client_models.environment',return_value='companion-fixture'); self.env.start()
        self.router,self.chat,self.voice=fake_router(); self.analyzer=ScriptedAnalyzer()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall,
                                        analysis_factory=lambda backend,config:self.analyzer)
    def tearDown(self): self.controller.close().result(5); self.env.stop(); self.directory.cleanup()
    def send(self,text,voice=False,wait=True):
        if voice: self.controller.pipeline.submit(text,input_metrics={'speech_end_to_submit_seconds':1.1})
        else: self.controller.submit(text).result(3)
        self.assertTrue(self.controller.pipeline.wait_finished(3)); self.controller.poll()
        if wait: self.assertTrue(self.controller.companion.wait_idle(3))
    def test_normal_text_and_voice_identity_are_saved_without_remember_command(self):
        for text,voice,key in [('我是一名大学生',False,'identity.role'),('我学计算机',True,'identity.major')]:
            self.analyzer.values[text]=packet(text,kind='sharing',memories=[(key,'fact',text)])
            self.send(text,voice)
        self.assertEqual(len(self.controller.memory.snapshot()['memories']),2)
    def test_failed_and_cancelled_turns_never_enqueue_analysis(self):
        self.chat.fail=True; self.send('失败的消息'); self.assertEqual(self.analyzer.calls,[])
        self.chat.fail=False; self.chat.release=threading.Event(); self.controller.submit('取消的消息').result(3)
        self.assertTrue(self.chat.entered.wait(1)); self.controller.cancel().result(3)
        self.assertTrue(self.controller.pipeline.wait_finished(3)); self.assertEqual(self.analyzer.calls,[])
    def test_slow_analysis_does_not_block_next_turn_or_device_call(self):
        self.analyzer.release=threading.Event(); self.send('我是大学生',wait=False)
        self.assertTrue(self.analyzer.entered.wait(1)); self.controller.start_call().result(3)
        self.send('我今天去散步了',wait=False); self.assertEqual(self.chat.calls,2)
        self.analyzer.release.set(); self.assertTrue(self.controller.companion.wait_idle(3))
        self.assertTrue(self.controller.call.active)
    def test_reset_during_analysis_keeps_call_history_and_ignores_old_score(self):
        text='我是一名大学生'; self.analyzer.values[text]=packet(text,kind='sharing',memories=[('identity.role','fact',text)])
        self.controller.start_call().result(3); self.analyzer.release=threading.Event(); self.send(text,wait=False)
        self.assertTrue(self.analyzer.entered.wait(1)); self.controller.reset_relationship().result(3)
        self.analyzer.release.set(); self.assertTrue(self.controller.companion.wait_idle(3))
        self.assertTrue(self.controller.call.active); self.assertEqual(self.controller.memory.profile()['affinity'],0)
        self.assertEqual(len(self.controller.records()),1); self.assertEqual(len(self.controller.memory.snapshot()['memories']),1)
    def test_delete_while_analysis_runs_cancels_pending_write(self):
        identifier=self.controller.save_memory('我是大学生').result(3)
        text='我是医生'; self.analyzer.values[text]=packet(text,memories=[('identity.profession','fact',text)])
        self.analyzer.release=threading.Event(); self.send(text,wait=False)
        self.assertTrue(self.analyzer.entered.wait(1)); self.controller.delete_memory(identifier).result(3)
        self.assertTrue(self.controller.companion.wait_idle(3)); self.assertEqual(self.controller.memory.snapshot()['memories'],[])
    def test_analysis_failure_keeps_chat_and_does_not_change_identity(self):
        self.analyzer.analyze=Mock(side_effect=ValueError('bad payload'))
        self.send('我是大学生')
        self.assertEqual(self.controller.records()[0]['state'],'completed')
        self.assertEqual(self.controller.companion.snapshot()['failed'],1)
        self.assertEqual(self.controller.memory.profile()['affinity'],0)
    def test_automatic_toggle_preserves_call_and_context_but_blocks_old_fact(self):
        text='我是大学生'; self.analyzer.values[text]=packet(text,kind='sharing',memories=[('identity.role','fact',text)])
        self.controller.start_call().result(3); self.analyzer.release=threading.Event(); self.send(text,wait=False)
        self.assertTrue(self.analyzer.entered.wait(1)); cutoff=self.controller.memory.profile()['context_since']
        self.controller.configure_memory(auto_memory=False).result(3)
        self.analyzer.release.set(); self.assertTrue(self.controller.companion.wait_idle(3))
        self.assertTrue(self.controller.call.active); self.assertEqual(self.controller.memory.profile()['context_since'],cutoff)
        self.assertEqual(self.controller.memory.snapshot()['memories'],[]); self.assertEqual(self.controller.memory.profile()['affinity'],2)
    def test_invalid_nickname_is_rejected_before_stopping_call(self):
        self.controller.start_call().result(3)
        for name in ('','a'*21,'名字\n'):
            with self.assertRaises(ValueError): self.controller.configure_memory(close_name=name)
        self.assertTrue(self.controller.call.active)
    def test_bounded_queue_does_not_accumulate_unbounded_work(self):
        self.analyzer.release=threading.Event(); self.send('第一句话',wait=False)
        self.assertTrue(self.analyzer.entered.wait(1))
        for index in range(20): self.send('新的消息'+str(index),wait=False)
        value=self.controller.companion.snapshot(); self.assertLessEqual(value['pending'],9); self.assertGreater(value['dropped'],0)
        self.analyzer.release.set(); self.assertTrue(self.controller.companion.wait_idle(3))
    def test_closing_cancels_analyzer_and_preserves_completed_chat(self):
        self.analyzer.release=threading.Event(); self.send('我是一名大学生',wait=False)
        self.assertTrue(self.analyzer.entered.wait(1)); self.controller.close().result(5)
        self.assertTrue(self.analyzer.cancelled); self.assertFalse(self.controller.companion.thread.is_alive())
    def test_switching_modes_shares_identity_and_relationship(self):
        text='我是程序员'; self.analyzer.values[text]=packet(text,kind='sharing',memories=[('identity.profession','fact',text)])
        self.send(text); self.controller.apply_preferences(Preferences(engine='gpt')).result(3)
        system=self.router.backends['gpt'][0]._messages('你好')[0][0]['content']
        self.assertIn('程序员',system); self.assertIn('好感度',system)


class AnalyzerProtocolTests(unittest.TestCase):
    def test_qwen_background_session_is_text_only_and_has_no_voice_delivery_prompt(self):
        text='我是一名大学生'; raw=json.dumps(packet(text,memories=[('identity.role','fact',text)]),ensure_ascii=False)
        socket=Socket(response(text=raw,audio=False))
        config=RealtimeConfig('wss://fixture.example/realtime',model=MODEL,api_key='fixture-key')
        chat=QwenRealtime(config=config,connector=lambda config:socket)
        analyzer=SemanticAnalyzer('qwen',config,client=chat)
        data=analyzer.analyze(text,'你说的我记下了。',[],threading.Event())
        self.assertEqual(data['memories'][0]['key'],'identity.role')
        self.assertEqual(socket.sent[0]['session']['modalities'],['text'])
        self.assertNotIn(config.voice_instructions,socket.sent[0]['session']['instructions'])
        self.assertFalse(any(event['type']=='input_audio_buffer.append' for event in socket.sent))
        self.assertNotIn('fixture-key',json.dumps(chat.metrics))
    def test_gpt_analysis_uses_current_customer_config_and_separate_system_prompt(self):
        text='我是程序员'; raw=json.dumps(packet(text,memories=[('identity.profession','fact',text)]),ensure_ascii=False)
        response=Mock(); response.status_code=200; response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        response.iter_lines.return_value=['data: '+json.dumps({'choices':[{'delta':{'content':raw}}]}),'data: [DONE]']
        post=Mock(return_value=response); config=ChatConfig('customer-model','https://fixture.example/v1','fixture-key')
        analyzer=SemanticAnalyzer('gpt',config,client=ChatClient(config=config,post=post))
        self.assertEqual(analyzer.analyze(text,'我记下了',[],threading.Event())['memories'][0]['key'],'identity.profession')
        call=post.call_args.kwargs; self.assertEqual(call['json']['model'],'customer-model')
        self.assertEqual(call['json']['max_tokens'],1800); self.assertIn('后台信息分析器',call['json']['messages'][0]['content'])
        self.assertFalse(call['allow_redirects'])


if __name__=='__main__': unittest.main()
