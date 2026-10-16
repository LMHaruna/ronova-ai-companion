"""Video decoding, audio clock, cancellation and natural-completion gates."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from client_oath_audio import OathSound
from client_oath_video import OathVideoAssets,OathVideoScene,ASSET_ROOT
from client_oath_panel import OathDialog
from test_bond_oath import OathUiTests


class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def test_missing_modified_video_and_escaped_path_block_loading(self):
        source=json.loads((ASSET_ROOT/'manifest.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('missing.mp4','changed.mp4','../outside.mp4'):
                source['video']['file']=name
                (root/'manifest.json').write_text(json.dumps(source),encoding='utf-8')
                if name=='changed.mp4': (root/name).write_bytes(b'invalid-video')
                with self.assertRaisesRegex(RuntimeError,'素材未就绪'): OathVideoAssets(root)

    def test_real_decoder_consumes_all_frames_in_bounded_queue(self):
        scene=OathVideoScene(OathVideoAssets()); scene.begin()
        try:
            deadline=time.monotonic()+15
            while not scene.finished() and time.monotonic()<deadline:
                scene.advance(16); time.sleep(.005); self.app.processEvents()
                self.assertLessEqual(scene.decoder.frames.qsize(),12)
            self.assertTrue(scene.finished(),f'{scene.error}; decoded={scene.decoder.decoded}; index={scene.last_index}; done={scene.decoder.done.is_set()}; queue={scene.decoder.frames.qsize()}')
            self.assertEqual(scene.decoder.decoded,384)
            self.assertTrue(scene.ready)
        finally: scene.stop()

    def test_stop_releases_decoder_even_during_startup(self):
        scene=OathVideoScene(OathVideoAssets()); scene.begin(); scene.stop()
        self.assertTrue(scene.decoder.done.wait(3))
        self.assertFalse(scene.decoder.thread.is_alive())
        self.assertFalse(scene.finished())

    def test_one_mixed_track_uses_actual_device_clock_and_preferences(self):
        release=threading.Event(); entered=threading.Event(); settings={}
        class Player:
            def __init__(self,cancel,**kwargs):
                self.cancel=cancel; self.lock=threading.RLock(); self.rate=24000
                self.handle=True; self.frames=0; settings.update(kwargs)
            def _device_position(self): return self.frames
            def write(self,path): settings['file']=Path(path).name; entered.set(); return True
            def finish(self):
                while not release.is_set() and not self.cancel.is_set(): self.cancel.wait(.005)
            def stop(self): pass
            def close(self): self.handle=False
        assets=OathVideoAssets()
        sound=OathSound((assets.audio,),(0,),enabled=True,volume=.37,device=3,
                        player_factory=Player,duration=16,end_padding_seconds=0)
        sound.start(); self.assertTrue(entered.wait(1))
        try:
            with sound.lock: sound.player.frames=24000*7
            self.assertEqual(sound.position(),7)
            self.assertEqual(settings['file'],'narration.wav')
            self.assertEqual(settings['volume'],.37); self.assertEqual(settings['device'],3)
            self.assertEqual(settings['end_padding_seconds'],0)
            self.assertFalse(sound.finished())
            release.set(); self.assertTrue(sound.done.wait(1)); self.assertEqual(sound.position(),16)
        finally: sound.stop(); release.set()

    def test_muted_clock_runs_without_creating_player(self):
        factory=unittest.mock.Mock(side_effect=AssertionError('device forbidden'))
        sound=OathSound(('mixed.wav',),(0,),enabled=False,volume=1,device=None,
                        player_factory=factory,duration=16)
        sound.start(); sound.started-=3
        self.assertGreaterEqual(sound.position(),3); self.assertTrue(sound.finished()); factory.assert_not_called()

    def test_audio_failure_uses_wall_clock_and_subtitle_fallback(self):
        def fail(*args,**kwargs): raise OSError('fixture device failure')
        sound=OathSound(('mixed.wav',),(0,),enabled=True,volume=1,device=None,
                        player_factory=fail,duration=16)
        sound.start(); self.assertTrue(sound.done.wait(1)); sound.started-=3
        self.assertGreaterEqual(sound.position(),3); self.assertIn('字幕继续',sound.error)


class VideoUiTests(OathUiTests):
    def test_unshown_dialog_does_not_spawn_video_decoder(self):
        self.dialog=OathDialog(self.window)
        self.assertIsNone(self.dialog.scene.decoder); self.assertIsNone(self.dialog.sound)
        self.assertFalse(self.controller.memory.profile()['oath_completed'])

    def test_natural_save_waits_for_video_eof_even_after_audio(self):
        dialog=self.start_ui(); dialog.started=time.monotonic()-17
        dialog.sound.started=dialog.started; self.controller.oath_session['started']=dialog.started
        with patch.object(dialog.sound,'position',return_value=16), \
             patch.object(dialog.scene,'finished',return_value=False):
            dialog.refresh(); self.assertFalse(self.controller.memory.profile()['oath_completed'])

    def test_video_failure_does_not_save_and_explicit_skip_can_complete(self):
        dialog=self.start_ui(); dialog.scene.error='动画暂无法播放'
        with patch.object(dialog.scene,'advance'):
            dialog.refresh(); self.assertFalse(self.controller.memory.profile()['oath_completed'])
            self.assertIn('无法播放',dialog.status.text())
        dialog.primary.click(); self.wait(lambda:dialog.finished_ceremony)
        self.assertTrue(self.controller.memory.profile()['oath_completed'])

    def test_cancel_stops_video_process_and_keeps_oath_uncompleted(self):
        dialog=self.start_ui(); decoder=dialog.scene.decoder
        dialog.reject(); self.wait(lambda:not dialog.isVisible())
        self.assertTrue(decoder.done.wait(3)); self.assertFalse(decoder.thread.is_alive())
        self.assertFalse(self.controller.memory.profile()['oath_completed'])


def load_tests(loader,tests,pattern):
    suite=loader.loadTestsFromTestCase(VideoTests)
    for name in VideoUiTests.__dict__:
        if name.startswith('test_'): suite.addTest(VideoUiTests(name))
    return suite
