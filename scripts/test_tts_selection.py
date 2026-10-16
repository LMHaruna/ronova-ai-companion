"""Offline engine selection, GPU service release, and model dropdown checks."""
from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import sys

from client_controller import DesktopController
from client_store import Preferences
from client_fixtures import FakeCall, FakeVoice, fake_router
from local_voice import LocalVoice, PYTHON

ROOT = Path(__file__).resolve().parents[1]

class SelectionTests(unittest.TestCase):
    def test_index_reference_emotion_cache_reuses_and_invalidates_features(self):
        source=str(ROOT/'runtime/indextts-2.5/source'); extras=str(ROOT/'runtime/indextts-2.5/extras')
        with patch.object(sys,'path',[*sys.path,source,extras]):
            import torch
            from indextts.gpt.model_v2 import UnifiedVoice
            model=UnifiedVoice.__new__(UnifiedVoice); torch.nn.Module.__init__(model); model.eval()
            model.get_emovec=Mock(side_effect=lambda features,lengths: features.sum())
            features=torch.ones(1,2,3); lengths=torch.tensor([3])
            self.assertEqual(model.merge_emovec(features,features,lengths,lengths).item(),6)
            self.assertEqual(model.merge_emovec(features,features,lengths,lengths,alpha=.5).item(),6)
            self.assertEqual(model.get_emovec.call_count,1)
            features.add_(1)
            self.assertEqual(model.merge_emovec(features,features,lengths,lengths).item(),12)
            self.assertEqual(model.get_emovec.call_count,2)
            model.merge_emovec(features,features,torch.tensor([2]),lengths)
            self.assertEqual(model.get_emovec.call_count,3)

    def test_profiles_persist_independently_and_old_preferences_keep_gpt(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'preferences.json'; path.write_text('{"engine":"gpt"}')
            prefs=Preferences.load(path)
            self.assertEqual(prefs.gpt_tts_engine,'gpt-sovitsv2proplus')
            replace(prefs,gpt_tts_engine='indextts-2.5').save(path)
            loaded=Preferences.load(path)
            self.assertEqual(loaded.gpt_tts_engine,'indextts-2.5')
            self.assertEqual(loaded.deepseek_tts_engine,'gpt-sovitsv2proplus')
            with self.assertRaises(ValueError): replace(loaded,gpt_tts_engine='invalid').validate()

    def test_both_workers_use_gpt_python_and_release_can_reopen(self):
        for engine in ('indextts-2.5','gpt-sovitsv2proplus'):
            voice=LocalVoice(tts_engine=engine,player=lambda *args:None)
            try:
                self.assertEqual(voice.python,PYTHON)
                voice.ready.set(); voice.prepare_metrics={'old':True}
                voice.release_worker()
                self.assertFalse(voice.ready.is_set()); self.assertFalse(voice._closed)
                self.assertEqual(voice.prepare_metrics,{})
                self.assertTrue(voice._play_thread.is_alive())
            finally: voice.close()

    def test_switch_releases_other_model_and_keeps_text_backend(self):
        with tempfile.TemporaryDirectory() as folder, patch('client_models.environment',return_value='fixture-key'):
            router,chat,voice=fake_router(); controller=DesktopController(folder,router=router,call_factory=FakeCall)
            old=FakeVoice(); old._process=object(); old.released=0
            def release(): old._process=None; old.released+=1
            old.release_worker=release
            new=FakeVoice(); new.engine='gpt'; new._process=None
            controller.local_voices={'gpt-sovitsv2proplus':old,'indextts-2.5':new}
            controller.owns_router=True; router.selected='gpt'
            text=router.backends['gpt'][0]; router.backends['gpt']=(text,old)
            try:
                controller.call.active=True; controller.call_requested=True
                self.assertTrue(controller.set_tts_engine('gpt','indextts-2.5').result(3))
                self.assertEqual(old.released,1); self.assertIs(router.backends['gpt'][0],text)
                self.assertIs(router.voice.snapshot(),new)
                self.assertFalse(controller.call.active); self.assertFalse(controller.call_requested)
                self.assertEqual(text.calls,0)
                self.assertEqual(controller.preferences.deepseek_tts_engine,'gpt-sovitsv2proplus')
            finally: controller.close().result(5)
            self.assertTrue(old.closed); self.assertTrue(new.closed)

class DropdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_dropdown_choices_links_and_switch_save_without_cloud(self):
        from client_app import ClientWindow
        from PySide6.QtTest import QTest
        with tempfile.TemporaryDirectory() as folder, patch('client_models.environment',return_value='fixture-key'), \
                patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')), \
                patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('microphone forbidden')):
            router,chat,voice=fake_router(); controller=DesktopController(folder,router=router,call_factory=FakeCall)
            window=ClientWindow(controller,ROOT); window.show()
            try:
                window.settings(); QTest.qWait(450)
                pane=window.settings_pane; pane.choose_models(); QTest.qWait(200)
                self.assertEqual(set(pane.model_tts_combos),{'gpt','deepseek'})
                for engine,combo in pane.model_tts_combos.items():
                    self.assertEqual([combo.itemData(i) for i in range(combo.count())],
                                     ['indextts-2.5','gpt-sovitsv2proplus'])
                    combo.setCurrentIndex(0); self.assertTrue(pane.pending.result(3)); pane.refresh()
                    self.assertEqual(getattr(controller.preferences,engine+'_tts_engine'),'indextts-2.5')
                self.assertIn('https://platform.deepseek.com/api_keys',pane.model_key_links['deepseek'].text())
                for engine in ('gpt','qwen'):
                    self.assertIn('https://platform.qianwenai.com/home/api-keys',pane.model_key_links[engine].text())
                directory=Path(os.environ.get('RONOVA_PREVIEW_DIR',ROOT/'runtime/verification-records/indextts-integration-20261014/ui'))
                directory.mkdir(parents=True,exist_ok=True)
                for engine in ('gpt','deepseek'):
                    pane.select_model(engine)
                    if pane.pending: pane.pending.result(3); pane.refresh()
                    QTest.qWait(400); self.app.processEvents()
                    self.assertTrue(window.grab().save(str(directory/(engine+'-tts.png'))))
                self.assertEqual(chat.calls,0)
            finally:
                controller.close().result(5); window.close(); self.app.processEvents()

if __name__=='__main__': unittest.main()
