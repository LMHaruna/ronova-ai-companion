"""Append/correct distinctions, stale guards, recoverable history and privacy."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
if os.environ.get('RONOVA_CODE_ROOT'):sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
from client_memory import MemoryStore
from client_relationship import parse_analysis
from client_controller import DesktopController
from client_fixtures import FakeCall,fake_router


def data(text,key='identity.role',category='fact',prior=None,content=None,confidence=.99):
    value={'version':2,'memories':[{'key':key,'category':category,'content':content or text,
        'evidence':text,'confidence':confidence,'operation':'update' if prior else 'add',
        'replace_id':prior['id'] if prior else '', 'previous_content':prior['content'] if prior else '',
        'previous_updated':prior['updated'] if prior else 0}],
        'interaction':{'kind':'sharing','evidence':text,'confidence':.99}}
    return parse_analysis(json.dumps(value,ensure_ascii=False),text)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.path=Path(self.directory.name)/'mem.sqlite3'
        self.store=MemoryStore(self.path);self.number=0
    def tearDown(self):self.store.close();self.directory.cleanup()
    def add(self,text,**kwargs):
        self.number+=1;profile=self.store.profile()
        self.store.apply_analysis(str(self.number),text,data(text,**kwargs),profile['memory_epoch'],profile['relationship_epoch'])
        return self.store.snapshot()['memories'][0]
    def contents(self):return {row['content'] for row in self.store.snapshot()['memories']}

    def test_architect_student_and_software_creation_coexist(self):
        self.add('我是一名建筑师',key='identity.profession')
        self.add('我是一名研究生')
        self.add('顺便加一句，是我制作的这个客户端',content='我制作的这个客户端')
        self.assertEqual(len(self.contents()),3)
        newest=self.store.snapshot()['memories'][0]
        self.assertEqual((newest['category'],newest['slot']),('event','event.software_creation'))
    def test_same_role_key_does_not_mean_exclusive_roles(self):
        self.add('我是一名建筑师');self.add('我也是一名研究生')
        self.assertEqual(self.contents(),{'我是一名建筑师','我也是一名研究生'})
    def test_wrong_software_update_is_demoted_to_append(self):
        prior=self.add('我是一名研究生')
        self.add('更正，我制作了客户端',prior=prior,content='我制作了客户端')
        self.assertIn(prior['content'],self.contents());self.assertEqual(len(self.contents()),2)
    def test_role_update_without_explicit_correction_appends(self):
        prior=self.add('我是一名研究生');self.add('我是一名建筑师',prior=prior)
        self.assertEqual(len(self.contents()),2)
    def test_explicit_role_correction_updates_only_target_and_archives(self):
        student=self.add('我是一名大学生');architect=self.add('我是一名建筑师')
        self.add('我不再是大学生，我已经毕业开始工作了',prior=student)
        rows=self.store.snapshot()['memories'];self.assertEqual(len(rows),2)
        self.assertIn(architect['content'],self.contents());self.assertNotIn(student['content'],self.contents())
        self.assertEqual(len(self.store.versions(student['id'])),2)
        self.assertEqual(self.store.versions(student['id'])[1]['content'],student['content'])
    def test_explicit_name_correction_updates_same_record(self):
        prior=self.add('我的名字叫小林',key='identity.name')
        self.add('更正我的名字，我叫小李',key='identity.name',prior=prior,content='我叫小李')
        self.assertEqual(self.contents(),{'我叫小李'})
        self.assertEqual(self.store.snapshot()['memories'][0]['id'],prior['id'])
    def test_name_field_cannot_be_used_to_replace_profession(self):
        prior=self.add('我是一名建筑师',key='identity.profession')
        self.add('更正我的名字，我叫小李',key='identity.name',prior=prior,content='我叫小李')
        self.assertEqual(len(self.contents()),2);self.assertIn(prior['content'],self.contents())
    def test_legacy_unkeyed_identity_needs_supported_specific_correction(self):
        identifier=self.store.save('我的身份是一个建筑师')
        prior=self.store.snapshot()['memories'][0]
        self.add('更正我的职业，我现在是一名程序员',key='identity.profession',prior=prior,content='我现在是一名程序员')
        self.assertEqual(len(self.contents()),1)
        self.assertEqual(self.store.snapshot()['memories'][0]['id'],identifier)
    def test_legacy_replace_id_is_never_authority_to_overwrite(self):
        identifier=self.store.save('我是一名建筑师');profile=self.store.profile();text='我制作了客户端'
        value={'version':1,'memories':[{'key':'identity.role','category':'fact','content':text,
            'evidence':text,'confidence':.99,'replace_id':identifier}],
            'interaction':{'kind':'sharing','evidence':text,'confidence':.99}}
        self.store.apply_analysis('legacy',text,parse_analysis(json.dumps(value),text),profile['memory_epoch'],profile['relationship_epoch'])
        self.assertEqual(len(self.contents()),2);self.assertIn('我是一名建筑师',self.contents())
    def test_partial_year_correction_preserves_school_and_major(self):
        prior=self.add('我现在正在南京大学读建筑学的研一研究生',key='identity.education')
        self.add('更正，我现在是研二',key='identity.education',prior=prior,content='我现在是研二')
        self.assertIn(prior['content'],self.contents());self.assertEqual(len(self.contents()),2)
    def test_full_year_correction_preserves_components_in_updated_record(self):
        prior=self.add('我正在南京大学读建筑学的研一研究生',key='identity.education')
        text='更正，我正在南京大学读建筑学的研二研究生'
        self.add(text,key='identity.education',prior=prior,content=text[3:])
        self.assertEqual(len(self.contents()),1);self.assertIn(text[3:],self.contents())
    def test_addition_even_with_correction_word_does_not_replace_role(self):
        prior=self.add('我是一名研究生')
        self.add('顺便更正我的身份，我也是建筑师',prior=prior,content='我也是建筑师')
        self.assertEqual(len(self.contents()),2)
    def test_same_event_topic_appends_distinct_events(self):
        first=self.add('我制作了客户端')
        self.add('我开发了一个网站')
        self.assertEqual(len(self.contents()),2);self.assertIn(first['content'],self.contents())
    def test_old_snapshot_update_cannot_overwrite_newer_value(self):
        prior=self.add('我的名字叫小林',key='identity.name')
        self.store.save('我的名字叫小陈','fact',prior['id'])
        self.add('更正我的名字，我叫小李',key='identity.name',prior=prior,content='我叫小李')
        self.assertEqual(self.contents(),{'我的名字叫小陈'})
    def test_duplicate_append_does_not_create_history_noise(self):
        prior=self.add('我是一名建筑师');self.add('我是一名建筑师')
        self.assertEqual(len(self.contents()),1);self.assertEqual(len(self.store.versions(prior['id'])),1)
    def test_low_confidence_update_is_saved_separately(self):
        prior=self.add('我的名字叫小林',key='identity.name')
        self.add('更正我的名字，我叫小李',key='identity.name',prior=prior,content='我叫小李',confidence=.91)
        self.assertEqual(len(self.contents()),2)
    def test_same_preference_key_cannot_replace_an_unrelated_fact(self):
        prior=self.add('我喜欢热茶',key='preference.饮料',category='preference')
        self.add('更正，我是一名研究生',key='preference.饮料',category='preference',prior=prior,content='我是一名研究生')
        self.assertEqual(len(self.contents()),2);self.assertIn(prior['content'],self.contents())
    def test_preference_correction_explicitly_mentions_old_preference(self):
        prior=self.add('我喜欢热茶',key='preference.饮料',category='preference')
        self.add('更正，我不喜欢热茶，我喜欢咖啡',key='preference.饮料',category='preference',prior=prior,content='我喜欢咖啡')
        self.assertEqual(self.contents(),{'我喜欢咖啡'})
    def test_protocol_update_requires_precise_old_version(self):
        prior=self.add('我的名字叫小林',key='identity.name');text='更正我的名字，我叫小李'
        value=data(text,key='identity.name',prior=prior,content='我叫小李')
        for field,value_bad in (('previous_content',''),('previous_updated',0),('replace_id',''),('operation','delete')):
            broken=json.loads(json.dumps({'version':2,**value}));broken['memories'][0][field]=value_bad
            with self.subTest(field=field),self.assertRaises(ValueError):parse_analysis(json.dumps(broken),text)


class HistoryTests(unittest.TestCase):
    setUp=SafetyTests.setUp;tearDown=SafetyTests.tearDown
    def test_manual_edit_restore_and_undo_restore_preserve_all_versions(self):
        identifier=self.store.save('我喜欢热茶','preference');original=self.store.versions(identifier)[0]
        self.store.save('我喜欢咖啡','preference',identifier)
        coffee=self.store.versions(identifier)[0];self.store.restore_version(identifier,original['id'])
        self.assertEqual(self.store.snapshot()['memories'][0]['content'],'我喜欢热茶')
        self.assertEqual(len(self.store.versions(identifier)),3)
        self.store.restore_version(identifier,coffee['id'])
        self.assertEqual(self.store.snapshot()['memories'][0]['content'],'我喜欢咖啡')
        self.assertEqual(len(self.store.versions(identifier)),4)
    def test_automatic_evidence_and_source_survive_restore(self):
        profile=self.store.profile();text='我的名字叫小林'
        self.store.apply_analysis('original-voice',text,data(text,key='identity.name'),profile['memory_epoch'],profile['relationship_epoch'])
        row=self.store.snapshot()['memories'][0];original=self.store.versions(row['id'])[0]
        self.store.save('我的名字叫小李','fact',row['id']);self.store.restore_version(row['id'],original['id'])
        restored=self.store.snapshot()['memories'][0]
        self.assertEqual(restored['source'],'restored');self.assertEqual(restored['evidence'],text)
        self.assertEqual(restored['source_turn'],'original-voice')
        self.assertEqual(self.store.versions(row['id'])[2]['source'],'automatic')
    def test_stale_manual_editor_rejects_overwrite(self):
        identifier=self.store.save('我喜欢热茶');row=self.store.snapshot()['memories'][0]
        self.store.save('我喜欢咖啡','fact',identifier)
        with self.assertRaises(ValueError):self.store.save('我喜欢牛奶','fact',identifier,expected_updated=row['updated'])
        self.assertEqual(self.store.snapshot()['memories'][0]['content'],'我喜欢咖啡')
    def test_frozen_clock_cannot_reuse_a_version_marker(self):
        with patch('client_memory.time.time',return_value=123.0):
            identifier=self.store.save('我喜欢茶');row=self.store.snapshot()['memories'][0]
            version=self.store.versions(identifier)[0]
            self.store.save('我喜欢咖啡','fact',identifier)
            newer=self.store.snapshot()['memories'][0]
            self.assertGreater(newer['updated'],row['updated'])
            with self.assertRaises(ValueError):self.store.save('我喜欢牛奶','fact',identifier,expected_updated=row['updated'])
            self.store.restore_version(identifier,version['id'],newer['updated'])
            self.assertGreater(self.store.snapshot()['memories'][0]['updated'],newer['updated'])
    def test_restore_checks_current_version_and_target_ownership(self):
        first=self.store.save('我喜欢茶');version=self.store.versions(first)[0]
        row=self.store.snapshot()['memories'][0];second=self.store.save('我喜欢音乐')
        self.store.save('我喜欢咖啡','fact',first)
        with self.assertRaises(ValueError):self.store.restore_version(first,version['id'],row['updated'])
        with self.assertRaises(ValueError):self.store.restore_version(second,version['id'])
    def test_restore_duplicate_does_not_delete_another_memory(self):
        first=self.store.save('我喜欢茶');version=self.store.versions(first)[0]
        self.store.save('我喜欢咖啡','fact',first);self.store.save('我喜欢茶')
        with self.assertRaises(ValueError):self.store.restore_version(first,version['id'])
        self.assertEqual(len(self.store.snapshot()['memories']),2)
    def test_history_does_not_enter_model_context(self):
        first=self.store.save('我喜欢unique-old-secret');self.store.save('我喜欢unique-new-fact','fact',first)
        self.assertNotIn('unique-old-secret',self.store.context('你好'))
        self.assertIn('unique-old-secret',self.store.versions(first)[1]['content'])
    def test_delete_removes_history_and_old_bytes(self):
        first=self.store.save('我喜欢history-private-marker');version=self.store.versions(first)[0]
        self.store.save('我喜欢新事物','fact',first);self.store.delete(first)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM memory_versions').fetchone()[0],0)
        with self.assertRaises(ValueError):self.store.restore_version(first,version['id'])
        self.assertNotIn(b'history-private-marker',self.path.read_bytes())
    def test_clear_removes_all_versions_and_preserves_affinity(self):
        first=self.store.save('我喜欢茶');self.store.save('我喜欢咖啡','fact',first)
        with self.store.connection:self.store.connection.execute('UPDATE profile SET affinity=35')
        self.store.clear()
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM memory_versions').fetchone()[0],0)
        self.assertEqual(self.store.profile()['affinity'],35)
    def test_history_pagination_has_one_current_version(self):
        first=self.store.save('0')
        for index in range(1,57):self.store.save(str(index),'fact',first)
        newest=self.store.versions(first,limit=20);older=self.store.versions(first,limit=20,offset=20)
        self.assertEqual(len(newest),20);self.assertEqual(len(older),20)
        self.assertEqual(sum(row['is_current'] for row in newest+older),1)
        self.assertEqual(newest[0]['content'],'56')
        self.assertEqual(self.store.versions(first,offset=56)[0]['content'],'0')
    def test_history_write_failure_rolls_back_memory_edit(self):
        first=self.store.save('我喜欢茶')
        with patch.object(self.store,'_record_version',side_effect=sqlite3.OperationalError('fixture failure')):
            with self.assertRaises(sqlite3.OperationalError):self.store.save('我喜欢咖啡','fact',first)
        self.assertEqual(self.store.snapshot()['memories'][0]['content'],'我喜欢茶')
        self.assertEqual(len(self.store.versions(first)),1)
    def test_old_database_migrates_current_value_once_without_rewriting(self):
        first=self.store.save('我是一名建筑师');row=dict(self.store.snapshot()['memories'][0])
        self.store.close();db=sqlite3.connect(self.path)
        db.execute('DROP TABLE memory_versions');db.commit();db.close()
        self.store=MemoryStore(self.path)
        self.assertEqual(self.store.snapshot()['memories'][0],row)
        self.assertEqual(self.store.versions(first)[0]['reason'],'baseline')
        self.store.close();self.store=MemoryStore(self.path)
        self.assertEqual(len(self.store.versions(first)),1)
    def test_changing_category_drops_old_automatic_topic(self):
        profile=self.store.profile();text='我是一名建筑师'
        self.store.apply_analysis('voice',text,data(text),profile['memory_epoch'],profile['relationship_epoch'])
        first=self.store.snapshot()['memories'][0]['id'];self.store.save('我喜欢建筑','preference',first)
        self.assertEqual(self.store.snapshot()['memories'][0]['slot'],'')


class RestoreControllerTests(unittest.TestCase):
    def test_restore_clears_stale_context_and_preserves_chat_affinity(self):
        with tempfile.TemporaryDirectory() as directory,patch('client_models.environment',return_value='history-fixture'):
            router,chat,voice=fake_router();controller=DesktopController(directory,router=router,call_factory=FakeCall)
            try:
                first=controller.save_memory('我喜欢茶').result(3);version=controller.memory.versions(first)[0]
                controller.save_memory('我喜欢咖啡','fact',first).result(3)
                controller.submit('随便聊聊').result(3);self.assertTrue(controller.pipeline.wait_finished(3))
                controller.poll();self.assertTrue(controller.companion.wait_idle(3))
                score=controller.memory.profile()['affinity'];count=len(controller.records())
                controller.start_call().result(3)
                self.assertTrue(controller.restore_memory(first,version['id']).result(3))
                self.assertFalse(controller.call.active);self.assertEqual(len(controller.records()),count)
                self.assertEqual(controller.memory.profile()['affinity'],score)
                self.assertNotIn('咖啡',chat._messages('你好')[0][0]['content'])
                self.assertGreater(controller.memory.profile()['memory_epoch'],0)
            finally:controller.close().result(5)


if __name__=='__main__':unittest.main()
