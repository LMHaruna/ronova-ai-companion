"""Voice pause, gradual reveal, layout and ceremony data preservation."""
from pathlib import Path
import hashlib,json,tempfile,unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRectF
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtTest import QTest
from client_oath import LINES
from client_oath_caption import DialogueTimeline
from client_oath_video import OathVideoAssets,ASSET_ROOT
from test_bond_oath import OathUiTests

class CaptionTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.assets=OathVideoAssets()
    def test_no_text_before_voice_onset_then_characters_follow_measured_audio(self):
        timeline=DialogueTimeline(self.assets.captions)
        self.assertEqual(timeline.sample(2.39).text,"")
        self.assertEqual(timeline.sample(2.7).text,"我代")
        self.assertEqual(timeline.sample(3.4).text,"我代表的可是死")
        self.assertEqual(timeline.sample(4.1).text,LINES[0])
    def test_hesitation_keeps_prefix_still_then_reveals_next_word(self):
        timeline=DialogueTimeline(self.assets.captions)
        for at in (6.95,7.2,7.8):
            self.assertEqual(timeline.sample(at).text,"可这一次……")
        self.assertEqual(timeline.sample(8.0).text,"可这一次……我")
        self.assertEqual(timeline.sample(8.0),timeline.sample(8.0))
        self.assertNotIn(LINES[0],timeline.sample(8.0).text)
    def test_previous_line_fades_before_next_speech_and_last_line_is_complete(self):
        timeline=DialogueTimeline(self.assets.captions)
        self.assertEqual(timeline.sample(11.19).opacity,0)
        self.assertEqual(timeline.sample(11.3).text,"")
        self.assertEqual(timeline.sample(12.9).text,LINES[-1])
    def test_reversed_or_corrupted_character_timing_refuses_start(self):
        saved=json.loads((ASSET_ROOT/"manifest.json").read_text(encoding="utf-8"))
        timing=json.loads((ASSET_ROOT/saved["captions"]["file"]).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            import shutil
            root=Path(directory)
            for name in ("video","audio","poster","ending"):
                shutil.copyfile(ASSET_ROOT/saved[name]["file"],root/saved[name]["file"])
            timing["lines"][0]["characters"][0]["at"]=5.0
            file=root/"dialogue-timing.json";file.write_text(json.dumps(timing),encoding="utf-8")
            saved["captions"]["sha256"]=hashlib.sha256(file.read_bytes()).hexdigest()
            (root/"manifest.json").write_text(json.dumps(saved),encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError,"未就绪"):OathVideoAssets(root)

class CaptionUiTests(OathUiTests):
    def test_video_covers_window_and_compact_buttons_remain_in_lower_right(self):
        dialog=self.preview()
        for width,height in ((960,602),(1280,802),(1440,900)):
            dialog.resize(width,height);QTest.qWait(40)
            self.assertEqual(dialog.scene.size(),dialog.size())
            self.assertIsNone(dialog.findChild(QWidget,"oathDetails"))
            self.assertEqual(dialog.primary.width(),96)
            self.assertEqual(dialog.secondary.width(),96)
            self.assertLessEqual(width-dialog.primary.geometry().right(),27)
            self.assertLessEqual(height-dialog.primary.geometry().bottom(),27)
            self.assertGreater(dialog.secondary.x(),width*.70)
    def test_caption_stays_beside_art_and_all_lines_fit_above_buttons(self):
        dialog=self.preview()
        for width,height in ((900,560),(960,602),(1280,802),(1440,900)):
            dialog.resize(width,height);QTest.qWait(40)
            area=dialog.caption.text_area();picture=dialog.scene.picture_rect()
            self.assertGreater(area.left(),picture.right())
            self.assertLessEqual(area.right(),width-20)
            shift=0
            metrics=QFontMetricsF(dialog.caption.font())
            for line in dialog.assets.captions:
                placements=dialog.caption.placements(line["text"])
                for char,(x,y) in zip(line["text"],placements):
                    self.assertGreaterEqual(x,area.left())
                    self.assertLessEqual(x+metrics.horizontalAdvance(char),width-20)
                    self.assertLess(y+shift+metrics.descent(),dialog.primary.y()-10)
                shift+=placements[-1][1]-placements[0][1]+metrics.height()*2.25
    def test_dialog_caption_follows_audio_clock_and_skip_keeps_all_text(self):
        dialog=self.start_ui()
        with patch.object(dialog.sound,"position",return_value=7.3):
            dialog.started=1;dialog.sound.started=1;dialog.refresh()
            self.assertEqual(dialog.caption.visible_text,"可这一次……")
        dialog.primary.click();self.wait(lambda:dialog.finished_ceremony)
        self.assertEqual(dialog.caption.visible_text,"\n".join(LINES))
        self.assertTrue(dialog.caption.isVisible())
    def test_direct_play_has_no_early_dialogue_and_cancel_does_not_write_oath(self):
        dialog=self.start_ui()
        self.assertEqual(dialog.caption.visible_text,"")
        self.assertEqual(dialog.caption.mode,"play")
        self.assertIsNotNone(dialog.sound);dialog.secondary.click()
        self.wait(lambda:not dialog.isVisible())
        self.assertFalse(self.controller.memory.profile()["oath_completed"])

def load_tests(loader,tests,pattern):
    suite=loader.loadTestsFromTestCase(CaptionTimingTests)
    for name in CaptionUiTests.__dict__:
        if name.startswith("test_"):suite.addTest(CaptionUiTests(name))
    return suite

