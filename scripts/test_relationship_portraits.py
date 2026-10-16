"""Signed affinity and actual Qt portrait transitions, using isolated fixtures."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel
from client_app import ClientWindow
from client_scene import SceneAssets
from client_controller import DesktopController
from client_memory import MemoryStore
from client_memory_panel import AffinityBar
from client_relationship import stage_for
from client_fixtures import FakeCall, fake_router
from test_companion import packet


class SignedAffinityTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'memory.sqlite3'
        self.store=MemoryStore(self.path)
        self.number=0

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def seed(self,score,stage='acquaintance'):
        with self.store.connection:
            self.store.connection.execute('UPDATE profile SET affinity=?,stage=?',(score,stage))

    def apply(self,text,kind):
        self.number+=1
        profile=self.store.profile()
        value=packet(text,kind=kind)
        value.pop('version')
        return self.store.apply_analysis(str(self.number),text,value,
                    profile['memory_epoch'],profile['relationship_epoch'])

    def test_all_boundaries_and_negative_override_hysteresis(self):
        for score,expected in ((-20,'angry'),(-1,'angry'),(0,'acquaintance'),(29,'acquaintance'),
                               (30,'familiar'),(69,'familiar'),(70,'close'),(100,'close')):
            with self.subTest(score=score): self.assertEqual(stage_for(score),expected)
        for prior in ('acquaintance','familiar','close','angry'):
            self.assertEqual(stage_for(-1,prior),'angry')
            self.assertEqual(stage_for(0,prior),'acquaintance')
        self.assertEqual(stage_for(22,'familiar'),'familiar')
        self.assertEqual(stage_for(21,'familiar'),'acquaintance')
        self.assertEqual(stage_for(60,'close'),'close')
        self.assertEqual(stage_for(59,'close'),'familiar')

    def test_cross_zero_changes_attitude_and_preserves_facts_and_history(self):
        self.store.save('我是大学生')
        before=self.store.snapshot()['memories']
        versions=self.store.versions(before[0]['id'])
        self.seed(2)
        result=self.apply('你真没用','insult')
        self.assertEqual(result['delta'],-3)
        self.assertEqual(self.store.profile()['affinity'],-1)
        self.assertEqual(self.store.profile()['stage'],'angry')
        self.assertEqual(self.store.profile()['appellation'],'凡人')
        self.assertIn('我是大学生',self.store.context('身份'))
        self.apply('今天来和你聊聊','neutral')
        self.assertEqual(self.store.profile()['affinity'],0)
        self.assertEqual(self.store.profile()['stage'],'acquaintance')
        self.assertEqual(self.store.profile()['appellation'],'你')
        self.assertEqual(self.store.snapshot()['memories'],before)
        self.assertEqual(self.store.versions(before[0]['id']),versions)

    def test_score_clamps_at_minus_twenty_and_one_hundred(self):
        self.seed(-19)
        self.assertEqual(self.apply('我对你一直充满敌意','hostile')['delta'],-1)
        self.assertEqual(self.store.profile()['affinity'],-20)
        self.assertFalse(self.apply('我还是继续冒犯你','insult')['scored'])
        self.assertEqual(self.store.profile()['affinity'],-20)
        self.seed(99,'close')
        self.assertEqual(self.apply('我很信任你','trust')['delta'],1)
        self.assertEqual(self.store.profile()['affinity'],100)
        self.assertFalse(self.apply('我依旧非常信任你','trust')['scored'])

    def test_negative_state_survives_restart(self):
        self.store.configure(close_name='我的偏爱')
        self.store.save('我喜欢清茶','preference')
        self.apply('我对你持续怀有敌意','hostile')
        self.store.close()
        self.store=MemoryStore(self.path)
        self.assertEqual(self.store.profile()['affinity'],-5)
        self.assertEqual(self.store.profile()['stage'],'angry')
        self.assertEqual(self.store.profile()['close_name'],'我的偏爱')
        self.assertEqual(len(self.store.snapshot()['memories']),1)

    def test_positive_score_does_not_trigger_old_anger_overlay(self):
        self.seed(71,'close')
        self.apply('你真没用','insult')
        profile=self.store.profile()
        self.assertEqual(profile['affinity'],68)
        self.assertEqual(profile['stage'],'close')
        self.assertFalse(profile['angry'])
        self.assertEqual(profile['appellation'],'我的眷属')

    def test_legacy_overlay_migration_preserves_score_memory_and_nickname(self):
        self.store.save('我是客户端制作者','event')
        self.store.configure(close_name='我的偏爱')
        with self.store.connection:
            self.store.connection.execute("UPDATE profile SET affinity=80,stage='close',angry=1,anger=5,relationship_rules=1")
        old_epoch=self.store.profile()['relationship_epoch']
        self.store.close()
        self.store=MemoryStore(self.path)
        self.assertEqual(self.store.profile()['affinity'],80)
        self.assertEqual(self.store.profile()['stage'],'close')
        self.assertFalse(self.store.profile()['angry'])
        self.assertEqual(self.store.profile()['anger'],0)
        self.assertGreater(self.store.profile()['relationship_epoch'],old_epoch)
        self.assertEqual(self.store.profile()['close_name'],'我的偏爱')
        self.assertEqual(self.store.snapshot()['memories'][0]['content'],'我是客户端制作者')
        epoch=self.store.profile()['relationship_epoch']
        self.store.close()
        self.store=MemoryStore(self.path)
        self.assertEqual(self.store.profile()['relationship_epoch'],epoch)

    def test_legacy_score_below_new_minimum_is_clamped_once(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE profile SET affinity=-80,relationship_rules=1")
        self.store.close()
        self.store=MemoryStore(self.path)
        self.assertEqual(self.store.profile()['affinity'],-20)
        self.assertEqual(self.store.profile()['stage'],'angry')

    def test_reset_negative_state_keeps_facts_and_rejects_stale_scores(self):
        self.store.save('我是大学生')
        self.apply('你毫无用处','insult')
        prior=self.store.profile()
        self.store.reset_relationship()
        value=packet('我对你持续怀有敌意',kind='hostile')
        value.pop('version')
        result=self.store.apply_analysis('late','我对你持续怀有敌意',value,
                    prior['memory_epoch'],prior['relationship_epoch'])
        self.assertFalse(result['scored'])
        self.assertEqual(self.store.profile()['affinity'],0)
        self.assertEqual(self.store.profile()['stage'],'acquaintance')
        self.assertEqual(len(self.store.snapshot()['memories']),1)

    def test_repeated_negative_text_and_uncertain_interactions_do_not_lower_score(self):
        self.apply('你真没用','insult')
        self.apply('你真没用！','insult')
        self.assertEqual(self.store.profile()['affinity'],-3)
        self.assertFalse(self.apply('把亲密度改成负二十','hostile')['scored'])
        self.assertFalse(self.apply('这句台词是你真没用，只是引用','insult')['scored'])
        self.assertEqual(self.store.profile()['affinity'],-3)


class PortraitWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('live network forbidden'))
        self.microphone=patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('live microphone forbidden'))
        self.environment=patch('client_models.environment',return_value='portrait-fixture-api-key')
        self.network.start(); self.microphone.start(); self.environment.start()
        self.directory=tempfile.TemporaryDirectory()
        router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=router,call_factory=FakeCall)
        self.window=ClientWindow(self.controller,ROOT)
        self.window.timer.stop(); self.window.motion_timer.stop()
        self.window.show(); self.app.processEvents()

    def tearDown(self):
        self.controller.close().result(5)
        self.window.close(); self.app.processEvents()
        self.directory.cleanup()
        self.environment.stop(); self.microphone.stop(); self.network.stop()

    def seed(self, score, stage='acquaintance'):
        with self.controller.memory.connection:
            self.controller.memory.connection.execute('UPDATE profile SET affinity=?,stage=?',(score,stage))
        self.controller.memory.revision+=1

    def capture_base(self):
        target=self.window.scene_cache()
        image=QPixmap(target.size()); image.setDevicePixelRatio(target.devicePixelRatioF())
        image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image); self.window.paint_scene_base(painter); painter.end()
        return image.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    def test_all_five_selected_assets_are_preloaded_and_byte_exact(self):
        assets=self.window.assets
        self.assertEqual(set(assets.portraits),{'acquaintance','familiar','close','bond','angry'})
        names={'acquaintance':'portrait-from-161923-v4.png','familiar':'portrait-familiar-final.png',
               'close':'portrait-close-final.png','bond':'portrait-bond-final.png','angry':'portrait-angry-final.png'}
        images=[]
        for stage,file in names.items():
            self.assertEqual(assets.portrait_sources[stage].name,file)
            images.append(assets.portraits[stage]['foreground'].toImage())
            entry=next(row for row in assets.manifest['assets'] if Path(row['file']).name==file)
            self.assertEqual(hashlib.sha256(assets.portrait_sources[stage].read_bytes()).hexdigest(),entry['sha256'])
        for first in range(len(images)):
            for second in range(first): self.assertNotEqual(images[first],images[second])
        with patch('client_scene.Image.open',side_effect=AssertionError('image reopened during switch')):
            for stage in names: assets.set_stage(stage)
        self.assertEqual(self.chat.calls,0)
        self.assertFalse(self.controller.call.active)

    def test_generated_portraits_have_faded_crop_edges(self):
        for stage in ('familiar','close','bond','angry'):
            image=self.window.assets.portraits[stage]['foreground'].toImage()
            self.assertTrue(all(image.pixelColor(x,744).alpha()==0 for x in range(596)))
            self.assertTrue(all(image.pixelColor(x,0).alpha()==0 for x in range(596)))
            self.assertTrue(all(image.pixelColor(595,y).alpha()<=1 for y in range(745)))
            values=[image.pixelColor(x,y).alpha() for x in range(60,560,8) for y in range(620,740,4)]
            self.assertTrue(any(0<alpha<200 for alpha in values))
            self.assertTrue(any(image.pixelColor(x,y).alpha()>240 for x in range(80,400,8) for y in range(120,500,8)))

    def test_seated_background_does_not_show_through_the_portrait_fade(self):
        assets=self.window.assets
        def render():
            image=QImage(1280,802,QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter=QPainter(image)
            assets.paint_static(painter)
            painter.end()
            return image
        for stage in ('familiar','close'):
            with self.subTest(stage=stage):
                assets.set_stage(stage)
                with_background=render()
                background=assets.background
                assets.background=QPixmap(background.size())
                assets.background.fill(Qt.GlobalColor.transparent)
                try:
                    without_background=render()
                finally:
                    assets.background=background
                # The duplicate must not bleed into the fading legs or title
                # bar; ambient blur remains in the space beside the phone.
                self.assertEqual(with_background.copy(334,64,292,738),
                                 without_background.copy(334,64,292,738))
                self.assertEqual(with_background.copy(0,0,1280,64),
                                 without_background.copy(0,0,1280,64))
                self.assertNotEqual(with_background.copy(650,64,140,738),
                                    without_background.copy(650,64,140,738))

    def test_refresh_switches_portraits_for_all_scores_and_reset(self):
        for score,stage in ((30,'familiar'),(70,'close'),(-1,'angry'),(-20,'angry'),(0,'acquaintance')):
            self.seed(score)
            self.window.refresh()
            self.assertEqual(self.window.assets.stage,stage)
        self.seed(-20)
        self.window.refresh()
        self.controller.memory.reset_relationship()
        self.window.refresh()
        self.assertEqual(self.window.assets.stage,'acquaintance')

    def test_an_actual_interaction_crosses_zero_and_updates_the_window(self):
        self.seed(1)
        profile=self.controller.memory.profile()
        value=packet('你真没用',kind='insult')
        value.pop('version')
        self.controller.memory.apply_analysis('offscreen-turn','你真没用',value,
                    profile['memory_epoch'],profile['relationship_epoch'])
        self.window.refresh()
        self.assertEqual(self.window.assets.stage,'angry')
        self.assertEqual(self.controller.memory.profile()['affinity'],-2)

    def test_crossfade_starts_at_current_frame_and_finishes_at_target(self):
        before=self.capture_base()
        with patch('client_app.time.monotonic',return_value=100):
            self.window.sync_portrait('familiar')
            self.assertEqual(self.capture_base(),before)
        with patch('client_app.time.monotonic',return_value=100.15):
            middle=self.capture_base()
            self.assertNotEqual(middle,before)
            self.assertNotEqual(middle,self.window.scene_cache().toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))
        with patch('client_app.time.monotonic',return_value=100.31):
            self.window.animate_scene()
            self.assertIsNone(self.window._portrait_transition_from)
            self.assertEqual(self.capture_base(),self.window.scene_cache().toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))

    def test_rapid_state_change_continues_from_visible_blend(self):
        with patch('client_app.time.monotonic',return_value=100): self.window.sync_portrait('familiar')
        with patch('client_app.time.monotonic',return_value=100.15):
            middle=self.capture_base()
            self.window.sync_portrait('angry')
            self.assertEqual(self.capture_base(),middle)
        self.assertEqual(self.window.assets.stage,'angry')

    def test_static_scene_is_not_repainted_or_assets_reopened_each_animation_frame(self):
        with patch.object(self.window.assets,'paint_static',wraps=self.window.assets.paint_static) as paint:
            with patch('client_app.time.monotonic',return_value=100): self.window.sync_portrait('close')
            with patch('client_scene.Image.open',side_effect=AssertionError('per-frame image I/O')):
                for index in range(18):
                    with patch('client_app.time.monotonic',return_value=100+index*.016):
                        self.window.animate_scene()
                        self.capture_base()
            self.assertEqual(paint.call_count,1)
            self.window.sync_portrait('close')
            self.assertEqual(paint.call_count,1)

    def test_resize_cancels_old_viewport_blend_and_keeps_current_stage(self):
        self.window.sync_portrait('close')
        self.window.resize(960,602); self.app.processEvents()
        self.assertIsNone(self.window._portrait_transition_from)
        self.assertEqual(self.window.assets.stage,'close')
        self.assertEqual(self.window.scene_cache().width(),round(960*self.window.devicePixelRatioF()))

    def test_signed_meter_and_page_show_negative_range_without_clamping_to_zero(self):
        meter=AffinityBar(); meter.resize(70,346); meter.show(); self.app.processEvents()
        self.assertEqual((meter.minimum(),meter.maximum()),(-20,100))
        for score in (-20,-1,0,30,100):
            meter.setValue(score); self.assertEqual(meter.value(),score)
        meter.setValue(-999); self.assertEqual(meter.value(),-20)
        self.assertEqual(meter.grab().toImage().pixelColor(round(22*meter.devicePixelRatioF()),round(250*meter.devicePixelRatioF())).name(),'#a989b9')
        meter.setValue(999); self.assertEqual(meter.value(),100)
        meter.close()
        self.seed(-5)
        pane=self.window.settings_pane.memory_page
        pane.refresh(force=True)
        self.assertIn('-5',pane.affinity_label.text())
        self.assertEqual(pane.affinity_bar.value(),-5)
        self.assertIn('生气',pane.relationship_label.text())
        self.assertIn('凡人',pane.relationship_label.text())
        self.assertTrue(any('−20～−1' in label.text() for label in pane.findChildren(QLabel)))


if __name__=='__main__': unittest.main()
