"""Layer readiness, visual sequencing and cached audio cancellation/drain."""
from dataclasses import replace
import json
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_CODE_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPainter,QImage,QColor
from client_oath_audio import OathSound
from client_oath_motion import OathAssets, OathScene, motion_at, REQUIRED
from client_oath import LINES
from test_bond_oath import OathUiTests


class MotionAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        # Old production layers were removed after switching to video. Exercise
        # legacy loading/draw order with isolated assets, never restore old art.
        self.temporary=tempfile.TemporaryDirectory(); self.root=Path(self.temporary.name)
        image=QImage(4,4,QImage.Format.Format_ARGB32); image.fill(QColor(255,255,255,128))
        image.save(str(self.root/'layer.png'))
        image_hash=hashlib.sha256((self.root/'layer.png').read_bytes()).hexdigest()
        images={name:{'file':'layer.png','sha256':image_hash,
                     'target':[0,0,1122,1402],'pivot':[0,0]} for name in REQUIRED}
        audio=[]
        for index in range(3):
            path=self.root/f'{index}.wav'
            with wave.open(str(path),'wb') as sound:
                sound.setnchannels(1); sound.setsampwidth(2); sound.setframerate(8000)
                sound.writeframes(b'\0\0'*8000)
            audio.append({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.saved={'canvas':[1122,1402],'lines':list(LINES),'images':images,'audio':audio}
        self.write_manifest()
    def tearDown(self): self.temporary.cleanup()
    def write_manifest(self):
        (self.root/'manifest.json').write_text(json.dumps(self.saved),encoding='utf-8')
    def test_voice_duration_never_overlaps_lines_or_cuts_last_line(self):
        assets=OathAssets(self.root)
        self.assertEqual(len(assets.clips),3)
        self.assertGreaterEqual(assets.duration,16)
        self.assertGreaterEqual(assets.line_times[1],5.2)
        self.assertGreaterEqual(assets.line_times[2],11)
    def test_missing_and_modified_layer_fail_before_start(self):
        self.saved['images']['waiting']['file']='missing.png'; self.write_manifest()
        with self.assertRaisesRegex(RuntimeError,'素材未就绪'): OathAssets(self.root)
        self.saved['images']['waiting']['file']='waiting.png'
        (self.root/'waiting.png').write_bytes(b'changed'); self.write_manifest()
        with self.assertRaisesRegex(RuntimeError,'素材未就绪'): OathAssets(self.root)
    def test_path_escape_is_rejected(self):
        assets=OathAssets(self.root)
        with self.assertRaises(ValueError): assets.confined('../outside.png')
    def test_open_palm_precedes_reach_and_contact(self):
        early=motion_at(1.9); reaching=motion_at(4); contact=motion_at(6.5)
        self.assertGreater(early.invitation,.9); self.assertEqual(early.reach,0)
        self.assertGreater(reaching.reach,0); self.assertEqual(reaching.contact,0)
        self.assertEqual(contact.contact,1)
    def test_foreground_support_is_drawn_after_glove(self):
        scene=OathScene(OathAssets(self.root)); scene.resize(500,600); scene.advance(6.1)
        with patch.object(scene,'draw') as draw: scene.grab()
        names=[call.args[1] for call in draw.call_args_list]
        self.assertLess(names.index('viewer-hand'),names.index('her-arm'))
        self.assertLess(names.index('her-arm'),names.index('contact-cover'))


class CachedAudioTests(unittest.TestCase):
    def player(self,cancel,**settings):
        outer=self
        class Player:
            def write(self,path): outer.written.append(Path(path).name); return True
            def finish(self):
                outer.entered.set()
                while not outer.release.is_set() and not cancel.is_set(): cancel.wait(.005)
            def stop(self): outer.stops+=1
            def close(self): pass
        return Player()
    def setUp(self):
        self.written=[]; self.stops=0; self.entered=threading.Event(); self.release=threading.Event()
    def sound(self,**changes):
        args=dict(enabled=True,volume=.7,device=None,player_factory=self.player); args.update(changes)
        return OathSound(('1.wav','2.wav','3.wav'),(0,.01,.02),**args)
    def test_disabled_and_zero_volume_never_open_device(self):
        for args in ({'enabled':False},{'volume':0}):
            sound=self.sound(**args); sound.start(); self.assertTrue(sound.finished())
        self.assertEqual(self.written,[])
    def test_completion_waits_for_device_drain_and_start_is_idempotent(self):
        sound=self.sound(); sound.start(); sound.start()
        self.assertTrue(self.entered.wait(1)); self.assertFalse(sound.finished())
        self.release.set(); self.assertTrue(sound.done.wait(1))
        self.assertEqual(self.written,['1.wav','2.wav','3.wav'])
    def test_cancel_stops_active_device_and_prevents_later_clips(self):
        sound=self.sound(); sound.start(); self.assertTrue(self.entered.wait(1))
        sound.stop(); self.assertTrue(sound.done.wait(1))
        self.assertEqual(self.written,['1.wav']); self.assertEqual(self.stops,1)
    def test_device_failure_finishes_with_subtitle_fallback(self):
        def failing(*args,**kwargs): raise OSError('fixture device unavailable')
        sound=self.sound(player_factory=failing); sound.start(); self.assertTrue(sound.done.wait(1))
        self.assertIn('字幕继续',sound.error)


class OathAnimationUiTests(OathUiTests):
    # Keep only new scenarios; inherited lifecycle cases are run once by their own suite.
    def test_animation_missing_assets_disables_start_without_record(self):
        with patch('client_oath_panel.OathAssets',side_effect=RuntimeError('仪式素材未就绪')):
            dialog=self.preview()
        self.assertFalse(dialog.primary.isEnabled()); self.assertIn('未就绪',dialog.status.text())
        self.assertFalse(self.controller.memory.profile()['oath_completed'])
    def test_ceremony_does_not_finish_before_last_audio_drains(self):
        dialog=self.start_ui()
        dialog.started=time.monotonic()-dialog.assets.duration-1
        self.controller.oath_session['started']=dialog.started
        dialog.sound.started=dialog.started
        dialog.sound.done.set()
        with patch.object(dialog.sound,'position',return_value=dialog.assets.duration), \
             patch.object(dialog.scene,'finished',return_value=True):
            with patch.object(dialog.sound,'finished',return_value=False):
                dialog.refresh(); self.assertFalse(self.controller.memory.profile()['oath_completed'])
            dialog.refresh(); self.wait(lambda:self.controller.memory.profile()['oath_completed'])
    def test_direct_play_creates_sound_and_cancel_stops_it(self):
        dialog=self.start_ui(); self.assertIsNotNone(dialog.sound)
        with patch.object(dialog.sound,'stop') as stop:
            dialog.reject(); self.wait(lambda:not dialog.isVisible())
        self.assertGreaterEqual(stop.call_count,1)


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for cls in (MotionAssetsTests,CachedAudioTests): suite.addTests(loader.loadTestsFromTestCase(cls))
    for name in OathAnimationUiTests.__dict__:
        if name.startswith('test_'): suite.addTest(OathAnimationUiTests(name))
    return suite


if __name__=='__main__': unittest.main()
