"""Offline regression checks for titled lore retrieval and both chat transports."""
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(os.environ.get('RONOVA_CODE_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from client_lore import LoreStore, build_index, split_body, STAGES, KINDS
from client_memory import MemoryStore
from client_controller import DesktopController
from client_companion import SemanticAnalyzer
from client_store import Preferences
from chat_client import ChatClient, ChatConfig
from qwen_realtime import QwenRealtime, RealtimeConfig, MODEL
from client_fixtures import FakeCall, FakeChat, FakeVoice, fake_router
from test_realtime_voice import Socket, response


def fixture(root, data=None, config=None, source=False):
    root=Path(root)
    (root/'knowledge').mkdir(exist_ok=True)
    (root/'config').mkdir(exist_ok=True)
    data=data if data is not None else json.loads((ROOT/'knowledge/ronova-lore.json').read_text(encoding='utf-8'))
    (root/'knowledge/ronova-lore.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    (root/'config/character-knowledge.json').write_text(json.dumps(config or {'enabled':True,'stage':STAGES[-1]}),encoding='utf-8')
    if source:
        (root/'docs').mkdir(exist_ok=True)
        (root/'docs/02-character-profile.md').write_bytes((ROOT/'docs/02-character-profile.md').read_bytes())
    return data


def lore_json(context):
    return json.loads(context[context.index('{"剧情阶段"'):])


def ids(rows):
    return [row.parent_id for row in rows]


def http_response(text='休息。'):
    result=Mock()
    result.status_code=200
    result.__enter__=Mock(return_value=result)
    result.__exit__=Mock(return_value=False)
    result.iter_lines.return_value=['data: '+json.dumps({'choices':[{'delta':{'content':text}}]}),'data: [DONE]']
    return result


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document=(ROOT/'docs/02-character-profile.md').read_bytes()
        cls.index=build_index(cls.document)

    def test_index_is_deterministic_and_matches_saved_build(self):
        self.assertEqual(self.index,build_index(self.document))
        self.assertEqual(self.index,json.loads((ROOT/'knowledge/ronova-lore.json').read_text(encoding='utf-8')))
        lf=self.document.replace(b'\r\n',b'\n').replace(b'\r',b'\n')
        self.assertEqual(self.index['source_sha256'],hashlib.sha256(lf).hexdigest())

    def test_newline_formats_preserve_the_entire_saved_index(self):
        lf=self.document.replace(b'\r\n',b'\n').replace(b'\r',b'\n')
        mixed=b''.join(line+(b'\r\n' if offset%2 else b'\n')
                       for offset,line in enumerate(lf.splitlines()))
        for document in (lf,lf.replace(b'\n',b'\r\n'),lf.replace(b'\n',b'\r'),mixed):
            with self.subTest(format=document[:80]):
                self.assertEqual(build_index(document),self.index)

    def test_every_chunk_has_unique_summary_title_and_source_fields(self):
        rows=self.index['chunks']
        self.assertEqual(len(rows),57)
        self.assertEqual(len({row['id'] for row in rows}),len(rows))
        for row in rows:
            with self.subTest(chunk=row['id']):
                self.assertGreater(len(row['title']),6)
                self.assertLess(len(row['title']),80)
                self.assertTrue(row['content'])
                self.assertLessEqual(len(row['content']),600)
                self.assertLessEqual(set(row['kinds']),KINDS)
                self.assertLessEqual(set(row['source_ids']),set(self.index['sources']))
                self.assertIn(row['time_scope'],STAGES)
                self.assertNotIn('｜',row['title'])

    def test_testimony_hypothesis_and_community_provenance_remain_distinct(self):
        lookup={row['parent_id']:row for row in self.index['chunks']}
        self.assertIn('IN_WORLD_ACCOUNT',lookup['KB-CANON-02']['kinds'])
        self.assertIn('INTERPRETATION',lookup['KB-CLUE-04']['kinds'])
        self.assertEqual(lookup['KB-MOE-02']['kinds'],['COMMUNITY_VIEW'])
        self.assertIn('胡桃',lookup['KB-CLUE-04']['content'])
        self.assertIn('不能',lookup['KB-CLUE-04']['boundary'])

    def test_table_rows_get_their_own_sources_and_evidence_layer(self):
        rows=[row for row in self.index['chunks'] if row['parent_id']=='KB-KEY-01']
        jokes=[row for row in rows if row['kinds']==['COMMUNITY_VIEW']]
        self.assertTrue(jokes)
        self.assertTrue(all(row['family']=='community' for row in jokes))
        self.assertTrue(all(len(row['source_ids'])<10 for row in rows))
        self.assertTrue(any('解读分歧' in row['title'] for row in rows))

    def test_new_or_missing_entry_is_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            build_index(self.document.replace(b'KB-CANON-11',b'KB-CANON-99'))
        with self.assertRaises(ValueError):
            build_index(self.document+b'\n#### KB-NEW-01\xef\xbd\x9cnew\n\nnew entry\n')

    def test_long_paragraphs_split_on_sentences_with_anatomy_of_text_preserved(self):
        body='这是第一条完整事件。'*40+'这是第二条完整事件。'*40
        pieces=split_body(body,'两件事件',limit=150)
        self.assertGreater(len(pieces),1)
        self.assertTrue(all(len(content)<=150 for _,content in pieces))
        self.assertEqual(''.join(content.replace('\n','') for _,content in pieces),body)
        self.assertTrue(all(content.endswith('。') for _,content in pieces))

    def test_oversized_sentence_is_bounded(self):
        body='甲'*1000+'。'
        pieces=split_body(body,'超长句子',limit=200)
        self.assertTrue(all(len(content)<=200 for _,content in pieces))
        self.assertEqual(''.join(content for _,content in pieces),body)


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lore=LoreStore(ROOT)
        if not cls.lore.enabled: raise AssertionError(cls.lore.warning)

    def test_plot_questions_return_expected_events(self):
        cases={
            '你和希巴拉克有什么交易':'KB-CANON-03',
            '卡皮塔诺最后怎么样了':'KB-CANON-06',
            'capitano的诅咒':'KB-CANON-05',
            '你和派蒙是什么关系':'KB-CANON-09',
            '阿斯莫代去了哪里':'KB-CANON-07',
            '天空岛收回了你的权能吗':'KB-CANON-11',
            '你是不是每天只能杀两个人':'KB-CANON-08',
            '你复活别人有代价吗':'KB-CANON-10',
            '自暴自弃':'KB-CANON-02',
        }
        for query,expected in cases.items():
            with self.subTest(query=query):
                rows=self.lore.retrieve(query)
                self.assertIn(expected,ids(rows)[:3])
                self.assertLessEqual(len(rows),5)
                self.assertTrue(all(row.family!='community' for row in rows))

    def test_jokes_aliases_traditional_spelling_and_english(self):
        cases={'为什么有人叫你瓦学妹':'KB-MOE-02','瓦學妹':'KB-MOE-02',
               '死之小馒头':'KB-MOE-07','你也是打工人吧':'KB-MOE-01',
               '你要找谁报警':'KB-MOE-03','RO TA TE':'KB-MOE-04',
               'roroSPEEN':'KB-MOE-04','HMPH':'KB-MOE-05',
               '大号派蒙':'KB-MOE-06','应急食品':'KB-MOE-06'}
        for query,expected in cases.items():
            with self.subTest(query=query):
                rows=self.lore.retrieve(query)
                self.assertEqual(rows[0].parent_id,expected)
                self.assertTrue(all(row.family=='community' and 'COMMUNITY_VIEW' in row.kinds for row in rows))

    def test_real_life_and_user_memory_questions_do_not_pull_lore(self):
        queries=['今天上班好累','我明天去面试','我今天吃了馒头','我今天报警了','我是打工人',
                 '你记得我的生日吗','我的生日你还记得吗','你对我有多了解','记住我是一名大学生',
                 '给我取一个昵称','明天我要参加工作会议','我制作了这个客户端']
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(self.lore.retrieve(query),[])
                self.assertEqual(self.lore.context(query),'')
                self.assertEqual(self.lore.joke_hint(query),[])

    def test_unknown_character_fields_are_not_fabricated(self):
        for query in ('你是谁','你的生日是什么时候','若娜瓦的生日','你喜欢吃什么'):
            with self.subTest(query=query):
                rows=self.lore.retrieve(query)
                self.assertEqual(ids(rows),['KB-ID-01'])
                self.assertIn('未在本次资料中确认',rows[0].content)

    def test_hypothesis_question_keeps_uncertainty_and_source(self):
        rows=self.lore.retrieve('胡桃的神之眼是你给的吗')
        self.assertEqual(rows[0].parent_id,'KB-CLUE-04')
        self.assertIn('INTERPRETATION',rows[0].kinds)
        self.assertIn('不能',rows[0].boundary)

    def test_latest_stage_keeps_loss_of_power_and_unknown_fate_in_context(self):
        payload=lore_json(self.lore.context('你和希巴拉克有什么交易'))
        self.assertEqual(payload['剧情阶段'],'snezhnaya_7_1')
        self.assertIn('权能被天空岛回收',payload['阶段状态']['内容'])
        self.assertIn('保持未知',payload['阶段状态']['使用边界'])

    def test_earlier_stage_excludes_future_events_and_jokes(self):
        earlier=LoreStore(ROOT,stage='natlan_5_1')
        for query in ('你和希巴拉克有什么交易','天空岛回收你的权能','剧院两人份','瓦学妹'):
            with self.subTest(query=query):
                rows=earlier.retrieve(query)
                self.assertTrue(all(row.time_scope=='natlan_5_1' for row in rows))
                self.assertNotIn('阶段状态',lore_json(earlier.context(query)))
        self.assertIn('KB-CANON-03',ids(earlier.retrieve('你和希巴拉克有什么交易')))

    def test_followup_uses_recent_user_topic_without_assistant_guess(self):
        history=[{'role':'user','content':'卡皮塔诺的不死诅咒'},
                 {'role':'assistant','content':'胡桃和影宝都认识你。'}]
        self.assertIn('KB-CANON-05',ids(self.lore.retrieve('他后来呢',history)))
        self.assertEqual(self.lore.retrieve('她后来呢',[history[-1]]),[])
        changed=[*history,{'role':'user','content':'今天上班好累'},{'role':'assistant','content':'休息。'}]
        self.assertEqual(self.lore.retrieve('为什么会这样',changed),[])

    def test_context_budget_keeps_complete_json_and_boundaries(self):
        for query in ('天理四影与权能回收','胡桃的神之眼','纳塔的死亡代价','瓦学妹'):
            with self.subTest(query=query):
                context=self.lore.context(query)
                self.assertLessEqual(len(context),4000)
                payload=lore_json(context)
                self.assertLessEqual(len(payload['知识片段']),5)
                lookup={row.id:row for row in self.lore.chunks}
                for row in payload['知识片段']:
                    self.assertEqual(row['使用边界'],lookup[row['id']].boundary)
                    self.assertEqual(row['内容'],lookup[row['id']].content)

    def test_queries_do_not_read_files_or_open_network_after_loading(self):
        with patch.object(Path,'read_text',side_effect=AssertionError('per-query disk read')), \
             patch.object(Path,'read_bytes',side_effect=AssertionError('per-query disk read')), \
             patch('socket.create_connection',side_effect=AssertionError('live socket')):
            self.assertTrue(self.lore.retrieve('天空岛回收权能'))
            self.assertTrue(self.lore.context('胡桃的神之眼'))
            self.assertTrue(self.lore.joke_hint('瓦学妹'))


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.data=fixture(self.root,source=True)

    def tearDown(self): self.directory.cleanup()

    def test_missing_index_or_disabled_setting_is_optional(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertFalse(LoreStore(empty).enabled)
            self.assertEqual(LoreStore(empty).warning,'')
        fixture(self.root,config={'enabled':False,'stage':STAGES[-1]})
        lore=LoreStore(self.root)
        self.assertFalse(lore.enabled)
        self.assertEqual(lore.context('瓦学妹'),'')

    def test_source_change_disables_stale_index(self):
        path=self.root/'docs/02-character-profile.md'
        path.write_bytes(path.read_bytes()+b'\n')
        lore=LoreStore(self.root)
        self.assertFalse(lore.enabled)
        self.assertIn('重新构建',lore.warning)
        self.assertEqual(lore.context('天空岛回收权能'),'')

    def test_line_ending_conversion_keeps_existing_lore_enabled(self):
        path=self.root/'docs/02-character-profile.md'
        lf=path.read_bytes().replace(b'\r\n',b'\n').replace(b'\r',b'\n')
        mixed=b''.join(line+(b'\r\n' if offset%2 else b'\n')
                       for offset,line in enumerate(lf.splitlines()))
        for document in (lf,lf.replace(b'\n',b'\r\n'),lf.replace(b'\n',b'\r'),mixed):
            with self.subTest(format=document[:80]):
                path.write_bytes(document)
                lore=LoreStore(self.root)
                self.assertTrue(lore.enabled); self.assertEqual(lore.warning,'')
                self.assertEqual(len(lore.chunks),57)
                self.assertIn('KB-CANON-11',lore.context('天空岛回收权能'))

    def test_legacy_raw_digest_is_still_accepted_for_unchanged_source(self):
        path=self.root/'docs/02-character-profile.md'
        data=copy.deepcopy(self.data)
        data['source_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        fixture(self.root,data)
        lore=LoreStore(self.root)
        self.assertTrue(lore.enabled)
        self.assertIn('KB-CANON-05',lore.context('卡皮塔诺的诅咒'))

    def test_actual_character_text_changes_still_disable_stale_lore(self):
        path=self.root/'docs/02-character-profile.md'
        original=path.read_bytes()
        changed=original.replace('死之执政'.encode(), '生之执政'.encode())
        self.assertNotEqual(original,changed)
        path.write_bytes(changed)
        lore=LoreStore(self.root)
        self.assertFalse(lore.enabled); self.assertEqual(lore.chunks,())
        self.assertIn('重新构建',lore.warning)
        self.assertEqual(lore.context('天空岛回收权能'),'')

    def test_corrupt_metadata_falls_back_without_breaking_client(self):
        mutations=[
            lambda d:d.update(version=True),
            lambda d:d.update(aliases={'队长':1}),
            lambda d:d['chunks'][0].update(kinds='OFFICIAL'),
            lambda d:d['chunks'][0].update(keywords=[None]),
            lambda d:d['chunks'][0].update(source_ids=['S999']),
            lambda d:d['chunks'].append(d['chunks'][0]),
            lambda d:d.update(source_document='../outside.md'),
            lambda d:d['chunks'][0].update(content='x'*4001),
        ]
        for index,mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                data=copy.deepcopy(self.data); mutate(data); fixture(self.root,data)
                lore=LoreStore(self.root)
                self.assertFalse(lore.enabled)
                self.assertTrue(lore.warning)
                self.assertEqual(lore.context('队长'), '')
                self.assertEqual(lore.joke_hint('瓦学妹'),[])

    def test_invalid_config_is_reported(self):
        for config in ({'stage':'unknown'},{'enabled':'true'},{'enabled':True,'stage':1}):
            with self.subTest(config=config):
                fixture(self.root,config=config)
                lore=LoreStore(self.root)
                self.assertFalse(lore.enabled); self.assertTrue(lore.warning)

    def test_title_alone_can_route_and_retrieve_a_new_topic(self):
        template=copy.deepcopy(self.data['chunks'][0])
        template.update(id='TEST-01',parent_id='TEST-01',title='织月灯塔的来源',
                        family='canon',keywords=[],source_ids=[],related_ids=[],
                        content='这是一个地点。',boundary='')
        other=dict(template,id='TEST-02',parent_id='TEST-02',title='另一个地方的来源')
        data=dict(self.data,chunks=[template,other],aliases={})
        fixture(self.root,data)
        lore=LoreStore(self.root)
        self.assertTrue(lore.enabled)
        self.assertEqual(lore.retrieve('织月灯塔')[0].id,'TEST-01')


class ControllerLoreTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        fixture(self.root)
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.root,router=self.router,call_factory=FakeCall)

    def tearDown(self):
        self.controller.close().result(5)
        self.directory.cleanup()

    def system(self,query):
        return self.chat._messages(query)[0][0]['content']

    def test_both_backend_contexts_include_shared_lore_and_user_memory(self):
        self.controller.save_memory('我是一名大学生','fact').result(3)
        for engine in ('qwen','gpt','qwen'):
            self.controller.apply_preferences(Preferences(engine=engine)).result(3)
            self.chat=self.router.backends[engine][0]
            system=self.system('卡皮塔诺的诅咒')
            self.assertIn('KB-CANON-05',system)
            self.assertIn('我是一名大学生',system)
            self.assertEqual(self.controller.memory.snapshot()['memories'][0]['content'],'我是一名大学生')

    def test_deepseek_context_uses_shared_lore_and_user_memory(self):
        chat=FakeChat(); chat.outputs_audio=False
        self.router.factories['deepseek']=lambda:(chat,FakeVoice())
        self.controller.save_memory('我是一名大学生','fact').result(3)
        self.controller.apply_preferences(Preferences(engine='deepseek')).result(3)
        system=chat._messages('天空岛收回了你的权能吗')[0][0]['content']
        self.assertIn('KB-CANON-11',system)
        self.assertIn('我是一名大学生',system)
        self.assertIn('阶段状态',system)

    def test_delete_and_edit_memory_refresh_both_contexts_without_modifying_lore(self):
        identifier=self.controller.save_memory('我是一名大学生','fact').result(3)
        old=[row.content for row in self.controller.lore.chunks]
        self.controller.save_memory('我是一名建筑师','fact',identifier).result(3)
        system=self.system('你和希巴拉克有什么交易')
        self.assertIn('我是一名建筑师',system); self.assertNotIn('我是一名大学生',system)
        self.controller.delete_memory(identifier).result(3)
        for engine in ('gpt','qwen'):
            self.controller.apply_preferences(Preferences(engine=engine)).result(3)
            self.chat=self.router.backends[engine][0]
            system=self.system('你和希巴拉克有什么交易')
            self.assertNotIn('我是一名建筑师',system)
            self.assertIn('KB-CANON-03',system)
        self.assertEqual(old,[row.content for row in self.controller.lore.chunks])

    def test_memory_off_still_allows_background_lore(self):
        self.controller.save_memory('我是一名大学生','fact').result(3)
        self.controller.configure_memory(enabled=False).result(3)
        system=self.system('天空岛收回了你的权能吗')
        self.assertNotIn('我是一名大学生',system)
        self.assertIn('KB-CANON-11',system)

    def test_clear_history_forgets_followup_topic_and_never_writes_lore_to_memory(self):
        self.chat.history.extend([{'role':'user','content':'卡皮塔诺的诅咒'},
                                  {'role':'assistant','content':'这段故事。'}])
        self.assertIn('KB-CANON-05',self.system('他后来呢'))
        self.controller.clear_history().result(3)
        self.assertNotIn('KB-CANON-05',self.system('他后来呢'))
        self.assertEqual(self.controller.memory.snapshot()['memories'],[])

    def test_default_factories_wire_lore_into_later_backend_and_analyzer(self):
        gpt=ChatClient(post=Mock(),config=ChatConfig('fixture','https://fixture.example/v1','fixture-key'))
        qwen=QwenRealtime(RealtimeConfig('wss://fixture.example/realtime',model=MODEL,api_key='fixture-key'),
                          connector=Mock())
        with tempfile.TemporaryDirectory() as directory:
            fixture(directory)
            with patch('client_controller.ChatClient',return_value=gpt), \
                 patch('client_controller.QwenRealtime',return_value=qwen), \
                 patch('client_controller.LocalVoice',return_value=FakeVoice()), \
                 patch('client_controller.RealtimeVoice',return_value=FakeVoice()):
                controller=DesktopController(directory,call_factory=FakeCall)
                try:
                    self.assertEqual(controller.router.selected,'qwen')
                    self.assertIn('KB-CANON-05',qwen._messages('卡皮塔诺的诅咒')[0][0]['content'])
                    with patch.object(controller.models,'config',return_value=gpt.config):
                        controller.router.select('gpt')
                    self.assertIn('KB-MOE-02',gpt._messages('瓦学妹')[0][0]['content'])
                    analyzer=controller.companion.factory('gpt',gpt.config)
                    try: self.assertIs(analyzer.lore,controller.lore)
                    finally: analyzer.close()
                    self.assertEqual(gpt.metrics,{})
                    self.assertEqual(qwen.metrics,{})
                finally: controller.close().result(5)


class TransportLoreTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        fixture(self.root)
        self.lore=LoreStore(self.root)
        self.memory=MemoryStore(self.root/'test-memory.sqlite3')
        self.identifier=self.memory.save('我是一名大学生','fact')

    def tearDown(self):
        self.memory.close(); self.directory.cleanup()

    def provider(self,chat):
        return lambda text:self.lore.context(text,chat.history[1:][-4:])+self.memory.context(text)

    def test_gpt_request_uses_titled_context_without_retaining_it_in_history(self):
        post=Mock(side_effect=lambda *a,**k:http_response())
        chat=ChatClient(post=post,config=ChatConfig('fixture','https://fixture.example/v1','fixture-key'))
        chat.context_provider=self.provider(chat)
        base=chat.history[0]['content']
        chat.reply('胡桃的神之眼是你给的吗')
        system=post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('胡桃神之眼故事中的无名神明身份假说',system)
        self.assertIn('INTERPRETATION',system)
        self.assertIn('我是一名大学生',system)
        self.assertIn(base,system); self.assertEqual(chat.history[0]['content'],base)
        self.memory.delete(self.identifier)
        chat.reply('今天上班好累')
        system=post.call_args.kwargs['json']['messages'][0]['content']
        self.assertNotIn('KB-CLUE-04',system)
        self.assertNotIn('我是一名大学生',system)

    def test_qwen_session_refreshes_lore_and_jokes_without_voice_on_analysis(self):
        sockets=[]
        def connector(config):
            ws=Socket(response(audio=False)); sockets.append(ws); return ws
        chat=QwenRealtime(RealtimeConfig('wss://fixture.example/realtime',model=MODEL,api_key='fixture-key'),connector=connector)
        chat.context_provider=self.provider(chat)
        base=chat.history[0]['content']
        chat.reply('天空岛收回了你的权能吗',audio_enabled=False)
        system=sockets[-1].sent[0]['session']['instructions']
        self.assertIn('KB-CANON-11',system); self.assertIn('我是一名大学生',system)
        chat.reply('瓦学妹',audio_enabled=False)
        system=sockets[-1].sent[0]['session']['instructions']
        self.assertIn('COMMUNITY_VIEW',system); self.assertIn('KB-MOE-02',system)
        self.assertNotIn('"id": "KB-CANON-11"',system)
        self.assertEqual(chat.history[0]['content'],base)

    def test_analyzer_receives_only_small_joke_hint_not_dialogue_rag(self):
        client=Mock()
        client.reply.return_value=json.dumps({'version':2,'memories':[],
            'interaction':{'kind':'neutral','evidence':'','confidence':1}})
        analyzer=SemanticAnalyzer('gpt',None,client=client,lore=self.lore)
        result=analyzer.analyze('瓦学妹','若娜瓦的背景介绍。',[],threading.Event())
        payload=json.loads(client.reply.call_args.args[0])
        self.assertLess(len(json.dumps(payload['玩家调侃背景'],ensure_ascii=False)),500)
        self.assertNotIn('知识片段',payload)
        self.assertIsNone(client.context_provider)
        self.assertEqual(result['memories'],[])
        analyzer.analyze('我是一名大学生','角色的世界观。',[],threading.Event())
        self.assertNotIn('玩家调侃背景',json.loads(client.reply.call_args.args[0]))

    def test_analyzer_qwen_hint_keeps_existing_text_only_protocol(self):
        client=Mock()
        client.reply.return_value=json.dumps({'version':2,'memories':[],
            'interaction':{'kind':'neutral','evidence':'','confidence':1}})
        analyzer=SemanticAnalyzer('qwen',None,client=client,lore=self.lore)
        analyzer.analyze('死之小馒头','回应。',[],threading.Event())
        self.assertIs(client.reply.call_args.kwargs['audio_enabled'],False)
        self.assertIn('玩家调侃背景',json.loads(client.reply.call_args.args[0]))


if __name__=='__main__': unittest.main()
