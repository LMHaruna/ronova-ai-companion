"""Oath migration, cancellation, atomic save, stale work and real offline Qt UI."""
import json
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from client_memory import MemoryStore
from client_relationship import stage_for
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router
from client_app import ClientWindow
from client_memory_panel import MemoryPane
from client_oath_panel import OathDialog
from client_oath import DURATION, LINES
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import QPoint, Qt


def seed(store, score, prior='close'):
    with store.lock, store.connection:
        store.connection.execute('UPDATE profile SET affinity=?,stage=?',(score,prior))
    store.revision+=1


def complete(store):
    seed(store,100)
    return store.complete_oath(store.prepare_oath())


class OathStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'memory.sqlite3'
        self.store=MemoryStore(self.path)
    def tearDown(self):
        self.store.close(); self.directory.cleanup()

    def test_old_score_one_hundred_does_not_complete_oath(self):
        self.store.close(); self.path.unlink()
        old=sqlite3.connect(self.path)
        with old:
            old.execute('CREATE TABLE profile(id INTEGER PRIMARY KEY, enabled INTEGER, stage TEXT, context_since REAL, affinity INTEGER)')
            old.execute("INSERT INTO profile VALUES(1,1,'close',0,100)")
            old.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,category TEXT,source TEXT,created REAL,updated REAL)')
            old.execute("INSERT INTO memories VALUES('old','我喜欢清茶','preference','manual',1,2)")
        old.close()
        self.store=MemoryStore(self.path)
        profile=self.store.snapshot()
        self.assertEqual(profile['affinity'],100); self.assertEqual(profile['stage'],'close')
        self.assertFalse(profile['oath_completed']); self.assertEqual(profile['oath_completed_at'],0)
        self.assertEqual(profile['memories'][0]['id'],'old')
        self.assertEqual(profile['memories'][0]['content'],'我喜欢清茶')

    def test_completion_is_independent_of_memory_capacity(self):
        for index in range(100): self.store.save('记忆'+str(index))
        complete(self.store)
        self.assertEqual(len(self.store.snapshot()['memories']),100)
        self.assertEqual(self.store.profile()['stage'],'bond')

    def test_completion_survives_restart_without_score_or_date_changes(self):
        completed_at=complete(self.store)
        self.store.close(); self.store=MemoryStore(self.path)
        self.assertEqual(self.store.profile()['oath_completed_at'],completed_at)
        self.assertEqual(self.store.profile()['affinity'],100)
        self.assertEqual(self.store.profile()['stage'],'bond')

    def test_bond_drop_restore_and_negative_override(self):
        complete(self.store)
        for score,expected in ((90,'bond'),(89,'close'),(60,'close'),(59,'familiar'),(21,'acquaintance'),(-1,'angry'),(90,'bond')):
            seed(self.store,score,'bond')
            self.assertEqual(self.store.profile()['stage'],expected)
            self.assertTrue(self.store.profile()['oath_completed'])
        self.assertEqual(stage_for(100,'bond',False),'close')

    def test_score_reset_preserves_oath_name_and_memories(self):
        date=complete(self.store); self.store.save('我喜欢茶')
        self.store.configure(close_name='我的偏爱'); self.store.reset_relationship()
        profile=self.store.snapshot()
        self.assertEqual(profile['stage'],'acquaintance'); self.assertEqual(profile['affinity'],0)
        self.assertEqual(profile['oath_completed_at'],date); self.assertEqual(profile['close_name'],'我的偏爱')
        self.assertEqual(len(profile['memories']),1)
        seed(self.store,90); self.assertEqual(self.store.profile()['stage'],'bond')

    def test_memory_clear_and_disable_preserve_oath(self):
        date=complete(self.store); self.store.save('我喜欢茶')
        self.store.configure(enabled=False); self.store.clear()
        self.assertEqual(self.store.profile()['oath_completed_at'],date)
        context=self.store.context('你好')
        self.assertIn('羁绊',context); self.assertIn('已缔结',context)
        self.assertEqual(self.store.snapshot()['memories'],[])

    def test_oath_reset_preserves_other_data_and_can_recomplete(self):
        complete(self.store); self.store.save('我喜欢茶'); before=self.store.snapshot()
        self.store.reset_oath(); after=self.store.snapshot()
        self.assertFalse(after['oath_completed']); self.assertEqual(after['oath_completed_at'],0)
        self.assertEqual(after['affinity'],100); self.assertEqual(after['stage'],'close')
        self.assertEqual(after['memories'],before['memories'])
        self.assertGreater(after['context_since'],before['context_since'])
        self.assertTrue(self.store.complete_oath(self.store.prepare_oath()))

    def test_below_one_hundred_and_uncompleted_replay_are_rejected(self):
        for score in (0,89,98,99):
            seed(self.store,score)
            with self.assertRaises(ValueError): self.store.prepare_oath()
        with self.assertRaises(ValueError): self.store.prepare_oath(replay=True)

    def test_stale_analysis_cannot_change_score_or_memories_after_start(self):
        seed(self.store,100); profile=self.store.profile(); self.store.prepare_oath()
        text='我喜欢清茶'
        data={'memories':[{'key':'preference.tea','category':'preference','content':text,'evidence':text,
            'confidence':1,'operation':'add','replace_id':'','previous_content':'','previous_updated':0}],
            'interaction':{'kind':'distance','evidence':text,'confidence':1}}
        result=self.store.apply_analysis('old',text,data,profile['memory_epoch'],profile['relationship_epoch'])
        self.assertEqual(result['saved'],0); self.assertFalse(result['scored'])
        self.assertEqual(self.store.profile()['affinity'],100)

    def test_reset_invalidates_old_completion_token(self):
        seed(self.store,100); epoch=self.store.prepare_oath(); self.store.reset_oath()
        with self.assertRaises(ValueError): self.store.complete_oath(epoch)
        self.assertFalse(self.store.profile()['oath_completed'])

    def test_sqlite_failure_rolls_back_flag_date_stage_and_context(self):
        seed(self.store,100); epoch=self.store.prepare_oath(); before=self.store.profile()
        self.store.connection.execute("CREATE TRIGGER fail BEFORE UPDATE ON profile WHEN NEW.stage='bond' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.complete_oath(epoch)
        self.assertEqual(self.store.profile(),before)


class OathControllerFixture:
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.patches=[patch('client_models.environment',return_value='oath-offline-fixture'),
            patch.dict(os.environ,{'QWEN_REALTIME_URL':'wss://fixture.example/realtime'}),
            patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')),
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('microphone forbidden'))]
        for item in self.patches: item.start()
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        seed(self.controller.memory,100)
    def tearDown(self):
        self.controller.close().result(5)
        for item in reversed(self.patches): item.stop()
        self.directory.cleanup()
    def start(self,replay=False): return self.controller.begin_oath(replay=replay).result(3)
    def finish(self,token): return self.controller.finish_oath(token,skip=True).result(3)


class OathControllerTests(OathControllerFixture,unittest.TestCase):
    def test_skip_completes_once_and_does_not_score_or_call_services(self):
        token=self.start(); self.assertTrue(self.finish(token))
        date=self.controller.memory.profile()['oath_completed_at']
        self.assertTrue(self.finish(token))
        self.assertEqual(self.controller.memory.profile()['oath_completed_at'],date)
        self.assertEqual(self.controller.memory.profile()['affinity'],100)
        self.assertEqual(self.chat.calls,0); self.assertEqual(len(self.voice.jobs),0)

    def test_cancel_then_old_callback_and_fresh_restart(self):
        token=self.start(); self.controller.cancel_oath(token).result(3)
        self.assertFalse(self.finish(token)); self.assertFalse(self.controller.memory.profile()['oath_completed'])
        second=self.start(); self.assertNotEqual(token,second); self.assertTrue(self.finish(second))

    def test_close_unsaved_then_restart_does_not_autoplay_or_complete(self):
        self.start(); self.controller.close().result(5)
        router,_,_=fake_router()
        self.controller=DesktopController(self.directory.name,router=router,call_factory=FakeCall)
        self.assertFalse(self.controller.snapshot()['oath_active'])
        self.assertFalse(self.controller.memory.profile()['oath_completed'])
        self.assertEqual(self.controller.memory.profile()['affinity'],100)

    def test_restart_completed_keeps_date_without_ceremony(self):
        self.finish(self.start()); date=self.controller.memory.profile()['oath_completed_at']
        self.controller.close().result(5); router,_,_=fake_router()
        self.controller=DesktopController(self.directory.name,router=router,call_factory=FakeCall)
        self.assertFalse(self.controller.snapshot()['oath_active'])
        self.assertEqual(self.controller.memory.profile()['oath_completed_at'],date)

    def test_replay_below_bond_threshold_keeps_score_stage_and_date(self):
        self.finish(self.start()); self.controller.cancel_oath(self.controller.oath_session['token']).result(3)
        date=self.controller.memory.profile()['oath_completed_at']; seed(self.controller.memory,89)
        token=self.start(replay=True); self.assertTrue(self.finish(token))
        profile=self.controller.memory.profile()
        self.assertEqual(profile['affinity'],89); self.assertEqual(profile['stage'],'close')
        self.assertEqual(profile['oath_completed_at'],date)

    def test_oath_blocks_dialogue_and_mutations_until_closed(self):
        token=self.start()
        for operation in (lambda:self.controller.submit('你好'),self.controller.start_call,
                          self.controller.reset_relationship,self.controller.clear_memories,
                          lambda:self.controller.configure_memory(auto_memory=False)):
            with self.assertRaises(RuntimeError): operation()
        self.controller.cancel_oath(token).result(3)
        self.assertTrue(self.controller.oath_available())

    def test_call_reply_and_playback_prevent_start(self):
        self.controller.call.active=True
        with self.assertRaises(RuntimeError): self.start()
        self.controller.call.active=False; self.voice.playback_active=True
        with self.assertRaises(RuntimeError): self.start()
        self.voice.playback_active=False
        self.chat.release=threading.Event(); self.controller.submit('你好').result(3)
        self.assertTrue(self.chat.entered.wait(2))
        with self.assertRaises(RuntimeError): self.start()
        self.controller.cancel().result(3); self.assertTrue(self.controller.pipeline.wait_finished(3))

    def test_pending_save_and_duplicate_start_are_blocked(self):
        entered=threading.Event(); release=threading.Event()
        future=self.controller.action(lambda:(entered.set(),release.wait(2)))
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(RuntimeError): self.start()
        finally: release.set(); future.result(3)
        self.start()
        with self.assertRaises(RuntimeError): self.start()

    def test_start_rechecks_score_and_asset_readiness(self):
        seed(self.controller.memory,99)
        self.assertFalse(self.controller.begin_oath().result(3))
        seed(self.controller.memory,100)
        self.assertFalse(self.controller.begin_oath(assets_ready=False).result(3))
        self.assertFalse(self.controller.snapshot()['oath_active'])

    def test_natural_completion_requires_end_point(self):
        token=self.start(); self.assertFalse(self.controller.finish_oath(token).result(3))
        started=self.controller.oath_session['started']
        with patch('client_controller.time.monotonic',return_value=started+DURATION+.1):
            self.assertTrue(self.controller.finish_oath(token).result(3))

    def test_save_failure_can_retry_without_false_success(self):
        token=self.start()
        with patch.object(self.controller.memory,'complete_oath',side_effect=sqlite3.OperationalError('fixture')):
            self.assertFalse(self.finish(token))
        self.assertFalse(self.controller.memory.profile()['oath_completed'])
        self.assertFalse(self.controller.oath_session['completed'])
        self.assertTrue(self.finish(token))

    def test_completion_and_reset_refresh_both_model_histories(self):
        for name,(chat,_) in self.router.backends.items():
            self.controller.store.begin(name,name,'过去的誓约')
            self.controller.store.update(name,'此前已缔结','completed')
        self.controller.restore_contexts()
        self.assertTrue(any(len(chat.history)>1 for chat,_ in self.router.backends.values()))
        token=self.start(); self.finish(token); self.controller.cancel_oath(token).result(3)
        for chat,_ in self.router.backends.values(): self.assertEqual(len(chat.history),1)
        self.controller.reset_oath().result(3)
        for chat,_ in self.router.backends.values():
            self.assertEqual(len(chat.history),1)
            payload=json.loads(self.controller.memory.context('你好').splitlines()[-1])
            self.assertFalse(payload['誓约状态']['已缔结'])
        self.assertEqual(len(self.controller.records()),2)

    def test_reset_oath_cancels_unfinished_session_and_old_callback(self):
        token=self.start(); self.assertTrue(self.controller.reset_oath().result(3))
        self.assertFalse(self.finish(token)); self.assertFalse(self.controller.snapshot()['oath_active'])
        self.assertFalse(self.controller.memory.profile()['oath_completed'])

    def test_completed_oath_context_reaches_both_backends_with_memory_off(self):
        self.finish(self.start()); token=self.controller.oath_session['token']
        self.controller.cancel_oath(token).result(3)
        self.controller.configure_memory(enabled=False).result(3)
        for chat,_ in self.router.backends.values():
            messages=chat._messages('你好')[0]
            payload=json.loads(messages[0]['content'].splitlines()[-1])
            self.assertTrue(payload['誓约状态']['已缔结']); self.assertEqual(payload['当前关系'],'羁绊')

    def test_actual_background_worker_is_cancelled_on_begin(self):
        from test_companion import ScriptedAnalyzer, packet
        analyzer=ScriptedAnalyzer(); analyzer.release=threading.Event()
        text='我希望保持距离'; analyzer.values[text]=packet(text,kind='distance')
        self.controller.companion.factory=lambda backend,config:analyzer
        self.controller.submit(text).result(3)
        self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.assertTrue(analyzer.entered.wait(2))
        token=self.start(); self.assertTrue(analyzer.cancelled)
        self.assertTrue(self.controller.companion.wait_idle(3))
        self.assertEqual(self.controller.memory.profile()['affinity'],100)
        self.assertTrue(self.finish(token))


class OathUiTests(OathControllerFixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
    def setUp(self):
        super().setUp()
        self.controller.preferences=replace(self.controller.preferences,autoplay=False)
        self.device_patches=[patch('client_panels.input_devices',return_value=[]),patch('client_panels.output_devices',return_value=[])]
        for item in self.device_patches: item.start()
        self.window=ClientWindow(self.controller,ROOT); self.window.show()
        self.pane=self.window.settings_pane.memory_page
        self.dialog=None
    def tearDown(self):
        if self.dialog:
            self.dialog.reject()
            self.wait(lambda:not self.dialog.isVisible())
        self.window.timer.stop(); self.window.motion_timer.stop()
        super().tearDown(); self.window.close()
        for item in reversed(self.device_patches): item.stop()
    def wait(self,predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline: QTest.qWait(10)
        self.assertTrue(predicate())
    def preview(self):
        self.dialog=OathDialog(self.window); self.dialog.open(); QTest.qWait(60)
        return self.dialog
    def start_ui(self):
        dialog=self.preview()
        self.wait(lambda:dialog.token is not None)
        return dialog

    def test_constructing_unshown_dialog_has_no_side_effect(self):
        before=self.controller.memory.profile(); self.dialog=OathDialog(self.window)
        QTest.qWait(60)
        self.assertEqual(self.controller.memory.profile(),before)
        self.assertIsNone(self.dialog.token); self.assertIsNone(self.dialog.pending)
        self.assertEqual(self.chat.calls,0)

    def test_ui_preview_does_not_stop_active_call(self):
        self.controller.call.active=True; stops=len(self.controller.call.stops)
        dialog=self.preview()
        self.assertFalse(dialog.primary.isEnabled()); dialog.reject()
        self.assertTrue(self.controller.call.active); self.assertEqual(len(self.controller.call.stops),stops)

    def test_ui_cancel_and_window_close_do_not_complete(self):
        dialog=self.start_ui(); dialog.close()
        self.wait(lambda:not dialog.isVisible())
        self.assertFalse(self.controller.memory.profile()['oath_completed'])
        self.assertFalse(self.controller.snapshot()['oath_active'])

    def test_ui_skip_keeps_all_subtitles_readable_until_return(self):
        dialog=self.start_ui(); dialog.primary.click()
        self.wait(lambda:dialog.finished_ceremony)
        self.assertTrue(dialog.isVisible()); self.assertEqual(dialog.primary.text(),'返回日常')
        self.assertEqual(dialog.caption.visible_text,'\n'.join(LINES))
        self.assertEqual(dialog.caption.mode,'all')
        self.assertTrue(dialog.caption.isVisible())
        dialog.primary.click(); self.wait(lambda:not dialog.isVisible())
        self.window.refresh(); self.assertEqual(self.window.assets.stage,'bond')
        self.assertEqual(self.controller.memory.profile()['stage'],'bond')

    def test_ui_failed_save_shows_retry_and_preserves_cancel(self):
        dialog=self.start_ui()
        with patch.object(self.controller.memory,'complete_oath',side_effect=sqlite3.OperationalError('fixture')):
            dialog.primary.click(); self.wait(lambda:dialog.primary.text()=='重试保存')
        self.assertFalse(self.controller.memory.profile()['oath_completed'])
        self.assertTrue(dialog.secondary.isEnabled()); dialog.primary.click()
        self.wait(lambda:dialog.finished_ceremony)

    def test_ui_entry_updates_for_score_busy_replay_and_date(self):
        self.window.refresh(); self.assertTrue(self.window.oath_button.isEnabled())
        self.assertTrue(self.window.oath_button.isVisible())
        seed(self.controller.memory,99); self.pane.refresh(force=True)
        self.window.refresh()
        self.assertFalse(self.window.oath_button.isEnabled()); self.assertIn('达到100',self.window.oath_button.toolTip())
        self.assertTrue(self.window.oath_button.isHidden())
        complete(self.controller.memory); seed(self.controller.memory,89); self.pane.refresh(force=True)
        self.window.refresh()
        self.assertEqual(self.window.oath_button.text(),'重温誓约'); self.assertIn('缔结于',self.window.oath_button.toolTip())
        self.assertTrue(self.window.oath_button.isHidden()); self.assertFalse(self.window.oath_button.isEnabled())
        self.assertFalse(self.pane.oath_reset.isHidden())
        seed(self.controller.memory,100); self.window.refresh()
        self.assertTrue(self.window.oath_button.isVisible()); self.assertTrue(self.window.oath_button.isEnabled())
        self.voice.playback_active=True; self.pane.refresh(force=True)
        self.window.refresh(); self.assertFalse(self.window.oath_button.isEnabled())

    def test_ui_home_entry_starts_directly_and_settings_has_no_duplicate(self):
        self.assertFalse(hasattr(self.pane,'oath_button'))
        QTest.mouseClick(self.window.oath_button,Qt.MouseButton.LeftButton)
        self.dialog=self.window.oath_dialog; self.wait(lambda:self.dialog.token is not None)
        self.assertTrue(self.controller.snapshot()['oath_active'])
        self.assertEqual(self.dialog.caption.mode,'play')
        self.assertEqual(self.dialog.primary.text(),'跳过动画')
        self.assertEqual(self.controller.memory.profile()['affinity'],100)
        self.window.open_oath(); self.assertIs(self.dialog,self.window.oath_dialog)
        self.dialog.secondary.click(); self.wait(lambda:not self.dialog.isVisible())

    def test_ui_home_entry_stays_visible_beside_phone_pages_and_scales(self):
        for width,height in ((1280,802),(960,602),(1440,900)):
            self.window.text(); QTest.qWait(220)
            self.window.resize(width,height); QTest.qWait(40)
            entry=self.window.oath_button.geometry()
            self.assertTrue(self.window.rect().contains(entry))
            self.assertLess(entry.right(),self.window.offset_x+800*self.window.scale)
            self.assertGreater(entry.left(),self.window.offset_x+626*self.window.scale)
            self.assertGreater(entry.top(),self.window.offset_y+640*self.window.scale)
            self.window.open_call(); QTest.qWait(220)
            self.assertEqual(self.window.oath_button.geometry(),entry)
            self.window.settings(); QTest.qWait(220)
            self.assertEqual(self.window.oath_button.geometry(),entry)
            self.assertTrue(self.window.oath_button.isVisible())
            self.assertFalse(self.window.oath_button.geometry().intersects(self.window.overlay.geometry()))
        complete(self.controller.memory); self.window.refresh()
        self.assertEqual(self.window.oath_button.text(),'重温誓约')
        self.window.text(); QTest.qWait(220)
        self.assertEqual(self.window.oath_button.geometry(),entry)
        self.window.open_call(); QTest.qWait(220)
        self.assertEqual(self.window.oath_button.geometry(),entry)
        self.window.settings(); QTest.qWait(220)
        self.assertEqual(self.window.oath_button.geometry(),entry)

    def test_ui_unlock_hint_points_to_home_without_opening_phone_page(self):
        seed(self.controller.memory,99); self.window.refresh()
        self.window._oath_unlock_notified=False
        seed(self.controller.memory,100)
        with patch.object(self.window,'isActiveWindow',return_value=True),patch('client_app.QToolTip.showText') as show:
            self.window.refresh(); self.window.refresh()
        self.assertEqual(show.call_count,1)
        self.assertIn('手机左侧的爱心',show.call_args.args[1])
        self.assertEqual(self.window.overlay.page,'text')
        self.assertEqual(self.window.screen,'home')

    def test_ui_home_entry_disables_during_active_ceremony(self):
        QTest.mouseClick(self.window.oath_button,Qt.MouseButton.LeftButton)
        self.dialog=self.window.oath_dialog
        self.wait(lambda:self.dialog.token is not None)
        self.window.refresh(); self.assertFalse(self.window.oath_button.isEnabled())
        self.dialog.secondary.click(); self.wait(lambda:not self.dialog.isVisible())
        self.window.refresh(); self.assertTrue(self.window.oath_button.isEnabled())

    def test_ui_replay_home_entry_autostarts_and_preserves_date(self):
        complete(self.controller.memory)
        date=self.controller.memory.profile()['oath_completed_at']
        self.window.refresh()
        with patch.object(self.controller,'begin_oath',wraps=self.controller.begin_oath) as begin:
            QTest.mouseClick(self.window.oath_button,Qt.MouseButton.LeftButton)
            self.dialog=self.window.oath_dialog
            self.wait(lambda:self.dialog.token is not None)
            self.assertEqual(begin.call_count,1)
            self.assertTrue(begin.call_args.kwargs['replay'])
        self.assertEqual(self.dialog.caption.mode,'play')
        self.assertEqual(self.dialog.primary.text(),'跳过动画')
        self.dialog.secondary.click(); self.wait(lambda:not self.dialog.isVisible())
        self.assertEqual(self.controller.memory.profile()['oath_completed_at'],date)

    def test_ui_showing_same_dialog_again_does_not_restart_session(self):
        with patch.object(self.controller,'begin_oath',wraps=self.controller.begin_oath) as begin:
            dialog=self.start_ui(); token=dialog.token
            dialog.hide(); dialog.show(); QTest.qWait(50)
            self.assertEqual(begin.call_count,1)
            self.assertEqual(dialog.token,token)

    def test_ui_close_before_scheduled_start_does_not_create_session(self):
        before=self.controller.memory.profile()
        with patch.object(self.controller,'begin_oath',wraps=self.controller.begin_oath) as begin:
            self.dialog=OathDialog(self.window); self.dialog.open(); self.dialog.reject()
            QTest.qWait(60)
            self.assertEqual(begin.call_count,0)
        self.assertEqual(self.controller.memory.profile(),before)
        self.assertIsNone(self.dialog.sound)
        self.assertFalse(self.controller.snapshot()['oath_active'])

    def test_ui_cancel_during_pending_start_never_starts_video_or_sound(self):
        from concurrent.futures import Future
        pending=Future()
        with patch.object(self.controller,'begin_oath',return_value=pending):
            self.dialog=OathDialog(self.window)
            with patch.object(self.dialog.scene,'begin',wraps=self.dialog.scene.begin) as decode:
                self.dialog.open(); QTest.qWait(40)
                self.assertIs(self.dialog.pending,pending)
                self.assertTrue(self.dialog.secondary.isEnabled())
                self.dialog.secondary.click(); self.assertTrue(self.dialog.close_requested)
                pending.set_result('pending-start-fixture')
                self.wait(lambda:not self.dialog.isVisible())
                self.assertEqual(decode.call_count,0)
        self.assertIsNone(self.dialog.sound)
        self.assertFalse(self.controller.memory.profile()['oath_completed'])

    def test_ui_failed_autostart_can_retry_without_a_start_page(self):
        from concurrent.futures import Future
        failed=Future(); failed.set_result(None)
        with patch.object(self.controller,'begin_oath',return_value=failed):
            dialog=self.preview()
            self.wait(lambda:dialog.primary.text()=='重试播放')
        self.assertEqual(dialog.primary.text(),'重试播放')
        self.assertEqual(dialog.caption.mode,'play')
        dialog.primary.click(); self.wait(lambda:dialog.token is not None)
        self.assertEqual(dialog.primary.text(),'跳过动画')

    def test_ui_heart_corners_do_not_open_the_ceremony(self):
        button=self.window.oath_button
        self.assertFalse(button.hitButton(QPoint(1,1)))
        self.assertTrue(button.hitButton(button.rect().center()))
        QTest.mouseClick(button,Qt.MouseButton.LeftButton,pos=QPoint(1,1))
        self.assertIsNone(self.window.oath_dialog)
        QTest.mouseClick(button,Qt.MouseButton.LeftButton)
        self.dialog=self.window.oath_dialog
        self.wait(lambda:self.dialog.isVisible())

    def test_ui_heart_hides_below_one_hundred_even_after_oath(self):
        for completed in (False,True):
            if completed: complete(self.controller.memory)
            for score in (-20,0,30,70,99,100,99):
                seed(self.controller.memory,score); self.window.refresh()
                self.assertEqual(self.window.oath_button.isVisible(),score==100)
                self.assertEqual(self.window.oath_button.isEnabled(),score==100)

    def test_ui_reset_confirmation_is_distinct_from_score_reset(self):
        complete(self.controller.memory); self.pane.confirm_oath_reset()
        self.assertIn('清除缔结状态',self.pane.delete_detail.text())
        self.pane.delete_confirmed(); self.wait(lambda:self.pane.pending is None)
        self.assertFalse(self.controller.memory.profile()['oath_completed'])
        self.assertEqual(self.controller.memory.profile()['affinity'],100)

    def test_ui_natural_end_returns_to_daily_view(self):
        dialog=self.start_ui()
        dialog.started=time.monotonic()-dialog.assets.duration-1
        self.controller.oath_session['started']=dialog.started
        dialog.sound.started=dialog.started
        dialog.sound.done.set()
        with patch.object(dialog.sound,'position',return_value=dialog.assets.duration), \
             patch.object(dialog.scene,'finished',return_value=True):
            dialog.refresh(); self.wait(lambda:not dialog.isVisible())
        self.assertTrue(self.controller.memory.profile()['oath_completed'])
        self.assertFalse(self.controller.snapshot()['oath_active'])

    def test_ui_missing_script_prevents_start_and_save(self):
        with patch('client_oath_panel.validate_ceremony',side_effect=RuntimeError('仪式字幕未就绪')):
            dialog=self.preview()
        self.assertFalse(dialog.primary.isEnabled())
        self.assertFalse(self.controller.snapshot()['oath_active'])
        self.assertFalse(self.controller.memory.profile()['oath_completed'])


if __name__=='__main__': unittest.main()
