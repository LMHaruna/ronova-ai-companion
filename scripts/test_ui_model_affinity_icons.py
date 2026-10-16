"""Paint boundaries, score position and withdrawn-model startup, offline."""
from dataclasses import replace
import math
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router
from client_panels import ReplyActionButton
from test_model_providers import ProviderUI


class UiUpdateTests(ProviderUI):
    def test_call_settings_keep_two_modes_and_save_interruptible_speakers(self):
        self.window.settings(); QTest.qWait(350)
        pane=self.window.settings_pane
        pane.choose('call_mode','通话方式'); self.app.processEvents()
        self.assertEqual([(row.property('choice'),row.title.text()) for row in pane.selection_buttons],
                         [('speakers-aec','扬声器'),('headphones','耳机')])
        for mode,label in (('headphones','耳机'),('speakers-aec','扬声器')):
            row=next(row for row in pane.selection_buttons if row.property('choice')==mode)
            QTest.mouseClick(row,Qt.MouseButton.LeftButton)
            if pane.pending: pane.pending.result(3)
            QTest.qWait(150); self.app.processEvents()
            self.assertEqual(self.controller.preferences.call_mode,mode)
            self.assertEqual(pane.mode.value.text(),label)
            from client_store import Preferences
            self.assertEqual(Preferences.load(self.controller.preferences_path).call_mode,mode)
        directory=Path(os.environ['RONOVA_PREVIEW_DIR']); directory.mkdir(parents=True,exist_ok=True)
        self.assertTrue(self.window.grab().save(str(directory/'call-modes-two.png')))
        pane.choose_models(); QTest.qWait(150)
        for row in pane.model_rows:
            if row.property('choice')=='qwen': self.assertNotIn('独立显卡',row.subtitle.text())
            else: self.assertIn('需要 NVIDIA 独立显卡',row.subtitle.text())
        pane.select_model('gpt')
        if pane.pending: pane.pending.result(3)
        QTest.qWait(450); self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(directory/'local-voice-gpu-required.png')))

    def test_score_pixels_follow_fill_endpoint_on_the_right(self):
        centers=[]
        for score in (0,30,100,70,10,0,-5,-20):
            self.seed(score); meter=self.window.affinity_bar
            capture=meter.grab().toImage(); dpr=meter.devicePixelRatioF()
            rail,label,fill=meter.layout_geometry()
            self.assertGreater(label.left(),rail.right())
            self.assertGreaterEqual(label.top(),0); self.assertLessEqual(label.bottom(),meter.height())
            color='#a989b9' if score<0 else '#d93652' if score else '#d8d1d4'
            points=[(x,y) for x in range(math.ceil(label.left()*dpr),capture.width())
                    for y in range(capture.height()) if capture.pixelColor(x,y).name()==color]
            self.assertTrue(points,f'number is missing or clipped at {score}')
            center=(min(y for x,y in points)+max(y for x,y in points))/2
            self.assertLess(abs(center/dpr-(rail.bottom()-fill)),6)
            centers.append(center)
        self.assertGreater(centers[0],centers[1]); self.assertGreater(centers[1],centers[2])
        self.assertLess(centers[2],centers[3]); self.assertLess(centers[3],centers[4]); self.assertLess(centers[4],centers[5])

    def test_all_reply_icon_strokes_have_space_inside_the_button(self):
        directory=Path(os.environ['RONOVA_PREVIEW_DIR']); directory.mkdir(parents=True,exist_ok=True)
        for action in ('retry','replay','stop'):
            for enabled in (False,True):
                button=ReplyActionButton(action,'fixture',lambda:None)
                button.setEnabled(enabled); button.show(); self.app.processEvents()
                try:
                    capture=button.grab().toImage(); dpr=button.devicePixelRatioF()
                    color='#687487' if enabled else '#bbc2cc'
                    points=[(x,y) for x in range(capture.width()) for y in range(capture.height())
                            if capture.pixelColor(x,y).name()==color]
                    self.assertTrue(points)
                    margin=round(2*dpr)
                    self.assertGreaterEqual(min(x for x,y in points),margin)
                    self.assertGreaterEqual(min(y for x,y in points),margin)
                    self.assertLessEqual(max(x for x,y in points),capture.width()-1-margin)
                    self.assertLessEqual(max(y for x,y in points),capture.height()-1-margin)
                    self.assertTrue(button.grab().save(str(directory/f'icon-{action}-{enabled}.png')))
                finally: button.close()

    def test_saved_gpt_selection_falls_back_without_erasing_credentials(self):
        profile=replace(self.controller.models.snapshot()['openai'],api_key='retained-fixture-key')
        self.controller.models.commit(self.controller.models.prepare('openai',profile))
        preferences=replace(self.controller.preferences,engine='openai'); preferences.save(self.controller.preferences_path)
        self.controller.close().result(5)
        router,chat,voice=fake_router()
        restored=DesktopController(self.directory.name,router=router,call_factory=FakeCall)
        try:
            self.assertEqual(restored.preferences.engine,'qwen'); self.assertEqual(router.selected,'qwen')
            self.assertEqual(restored.models.snapshot()['openai'],profile)
            self.assertFalse(restored.call.active); self.assertEqual(chat.calls,0)
        finally: restored.close().result(5)

    def test_render_chat_controls_and_three_score_positions(self):
        directory=Path(os.environ['RONOVA_PREVIEW_DIR']); directory.mkdir(parents=True,exist_ok=True)
        self.controller.store.begin('ui-reply','qwen','今天遇到一点麻烦。')
        self.controller.store.update('ui-reply','遇到什么麻烦了？说来听听。','completed')
        self.window.refresh(); QTest.qWait(450)
        for score in (0,30,100):
            self.seed(score); QTest.qWait(400)
            self.assertTrue(self.window.grab().save(str(directory/f'chat-affinity-{score}.png')))
        self.window.settings(); QTest.qWait(450); self.window.settings_pane.choose_models(); QTest.qWait(450)
        self.assertTrue(self.window.grab().save(str(directory/'models-three.png')))


if __name__=='__main__': unittest.main()
