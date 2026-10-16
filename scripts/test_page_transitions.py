"""Navigation continuity and real frame endpoints, without live services."""
import unittest
import os
from pathlib import Path
import time
from unittest.mock import patch

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

import test_model_providers as fixtures
from client_panels import AnimatedStack, PageTransition


def settle(animation):
    deadline=time.monotonic()+2
    while animation.snapshot is not None and time.monotonic()<deadline: QTest.qWait(10)
    assert animation.snapshot is None and animation.incoming is None


class TransitionFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_complete_frames_blend_without_revealing_the_underlying_page(self):
        host=QWidget(); host.resize(80,100); host.setStyleSheet('background:blue'); host.show()
        transition=PageTransition(host)
        old=QPixmap(80,100); old.fill(QColor('red'))
        new=QPixmap(80,100); new.fill(QColor('green'))
        transition.start(old,new); transition.animation.pause()
        try:
            for progress,expected in ((0,QColor('red')),(1,QColor('green'))):
                transition.set_progress(progress)
                self.assertEqual(host.grab().toImage().pixelColor(40,50),expected)
            transition.set_progress(.5)
            middle=host.grab().toImage().pixelColor(40,50)
            self.assertEqual(middle.blue(),0)
            self.assertLessEqual(abs(middle.red()-128),1)
            self.assertLessEqual(abs(middle.green()-64),1)
            self.assertTrue(transition.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        finally: transition.cancel(); host.close()

    def test_index_navigation_reverses_and_resize_releases_both_frames(self):
        stack=AnimatedStack(); stack.resize(180,240)
        pages=[QWidget() for _ in range(3)]
        for page,color in zip(pages,('red','green','blue')):
            page.setStyleSheet('background:'+color); stack.addWidget(page)
        stack.show(); self.app.processEvents()
        try:
            stack.setCurrentIndex(1); self.assertEqual(stack.transition.direction,1)
            QTest.qWait(90); visible=stack.grab().toImage()
            stack.setCurrentIndex(0)
            self.assertEqual(stack.transition.direction,-1)
            self.assertEqual(stack.transition.snapshot.toImage(),visible)
            stack.resize(220,300); self.app.processEvents()
            self.assertIsNone(stack.transition.snapshot); self.assertIsNone(stack.transition.incoming)
            self.assertIs(stack.currentWidget(),pages[0])
            stack.setCurrentIndex(2); settle(stack.transition)
        finally: stack.close()

    def test_transparent_subpage_has_an_opaque_backing_and_no_old_controls_at_the_end(self):
        host=QWidget(); host.resize(200,240); host.show()
        transition=PageTransition(host)
        old=QPixmap(200,240); old.fill(QColor('red'))
        new=QPixmap(200,240); new.fill(Qt.GlobalColor.transparent)
        painter=QPainter(new); painter.fillRect(20,20,30,30,QColor('green')); painter.end()
        try:
            for direction in (1,-1):
                transition.start(old,new,duration=320,direction=direction); transition.animation.pause()
                def sample(x,y):
                    frame=host.grab().toImage(); scale=frame.devicePixelRatio()
                    return frame.pixelColor(round(x*scale),round(y*scale))
                transition.set_progress(.5)
                covered=150 if direction>0 else 50
                visible=50 if direction>0 else 150
                self.assertEqual(sample(covered,180),QColor('#F5F6F8'))
                self.assertEqual(sample(visible,180),QColor('red'))
                transition.set_progress(1)
                self.assertEqual(sample(100,180),QColor('#F5F6F8'))
                self.assertEqual(sample(35,35),QColor('green'))
                transition.cancel()
        finally: transition.cancel(); host.close()


class PhoneNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ProviderUI.setUpClass.__func__(cls)

    setUp=fixtures.ProviderUI.setUp
    tearDown=fixtures.ProviderUI.tearDown

    def test_settings_chat_and_call_reversals_start_from_the_visible_frame(self):
        window=self.window; overlay=window.overlay
        window.text_pane.input.setPlainText('切换时保留的草稿')
        with patch('client_panels.input_devices',return_value=[]),patch('client_panels.output_devices',return_value=[]):
            for navigate in (window.settings,window.text,window.open_call,window.settings,window.open_call,window.text):
                before=window.grab(overlay.navigation_bounds()).toImage()
                navigate()
                self.assertEqual(overlay.transition.snapshot.toImage(),before)
                self.assertEqual(window.grab(overlay.navigation_bounds()).toImage(),before)
                QTest.qWait(65)
        settle(overlay.transition)
        self.assertEqual((overlay.page,overlay.phase,overlay.effect.opacity()),('text','shown',1))
        self.assertEqual(window.text_pane.input.toPlainText(),'切换时保留的草稿')
        self.assertFalse(self.controller.call.active); self.assertEqual(self.chat.calls,0)

    def test_leaving_a_settings_subpage_captures_it_before_resetting_settings(self):
        window=self.window; overlay=window.overlay; pane=window.settings_pane
        window.settings(); settle(overlay.transition)
        pane.open_call_settings(); settle(pane.stack.transition)
        before=window.grab(overlay.navigation_bounds()).toImage()
        window.close_phone()
        self.assertEqual(overlay.transition.snapshot.toImage(),before)
        QTest.qWait(80)
        before=window.grab(overlay.navigation_bounds()).toImage()
        window.settings()
        self.assertEqual(overlay.transition.snapshot.toImage(),before)
        self.assertIs(pane.stack.currentWidget(),pane.root)
        self.assertIsNone(pane.stack.transition.snapshot)
        settle(overlay.transition)

    def test_resizing_during_each_direction_finishes_on_the_target_page(self):
        window=self.window; overlay=window.overlay
        for navigate,target in ((window.settings,'settings'),(window.text,'text'),(window.open_call,None)):
            navigate(); QTest.qWait(65)
            size=(960,602) if window.width()==1280 else (1280,802)
            window.resize(*size); self.app.processEvents()
            self.assertIsNone(overlay.transition.snapshot); self.assertIsNone(overlay.transition.incoming)
            self.assertEqual(overlay.page,target)
            self.assertEqual(overlay.phase,'hidden' if target is None else 'shown')
        self.assertFalse(self.controller.call.active)

    def test_reopening_settings_while_returning_to_call_updates_its_return_destination(self):
        window=self.window; overlay=window.overlay
        window.settings(); settle(overlay.transition)
        window.open_call(); QTest.qWait(65)
        window.settings()
        self.assertEqual(window.settings_origin,'call')
        settle(overlay.transition)
        window.close_phone(); settle(overlay.transition)
        self.assertEqual((window.screen,overlay.phase),('call','hidden'))

    def test_transition_does_not_block_a_return_click_and_a_notification_can_reopen_chat(self):
        window=self.window; overlay=window.overlay
        window.settings(); QTest.qWait(65)
        back=next(button for button in window.settings_pane.root.findChildren(QPushButton)
                  if button.text()=='‹ 返回')
        point=back.mapTo(overlay.surface,back.rect().center())
        QTest.mouseClick(overlay.viewport(),Qt.MouseButton.LeftButton,pos=overlay.mapFromScene(QPointF(point)))
        settle(overlay.transition)
        self.assertEqual(overlay.page,'text')
        window.open_call(); QTest.qWait(65)
        overlay.notify('转场期间的提示')
        settle(overlay.transition)
        self.assertEqual(overlay.page,'text'); self.assertTrue(overlay.surface.toast.isVisible())
        self.assertEqual(overlay.surface.toast.text(),'转场期间的提示')

    def test_settings_return_to_call_and_navigation_preserves_a_running_call(self):
        window=self.window; overlay=window.overlay
        self.controller.start_call().result(3)
        self.voice.playback_active=True; self.voice.playback_level=.6; window.refresh()
        window.open_call(); settle(overlay.transition)
        window.settings(); settle(overlay.transition)
        window.close_phone(); settle(overlay.transition)
        self.assertEqual(window.screen,'call'); self.assertEqual(overlay.phase,'hidden')
        window.text(); settle(overlay.transition)
        self.assertTrue(self.controller.call.active); self.assertTrue(self.voice.playback_active)
        self.assertEqual(self.chat.calls,0)

    def test_each_settings_subpage_is_complete_before_the_transition_layer_is_hidden(self):
        window=self.window; overlay=window.overlay; pane=window.settings_pane
        with patch('client_panels.input_devices',return_value=[]),patch('client_panels.output_devices',return_value=[]):
            window.settings(); settle(overlay.transition)
        window.timer.stop()
        for action in (pane.choose_models,lambda:pane.choose('call_mode','通话方式'),
                       lambda:pane.choose('microphone','麦克风'),lambda:pane.choose('speaker','播放设备'),
                       pane.open_call_settings,pane.open_memory,pane.confirm_clear):
            for navigate in (action,lambda:pane.stack.setCurrentWidget(pane.root)):
                navigate(); transition=pane.stack.transition
                self.assertIsNotNone(transition.snapshot)
                transition.animation.pause(); transition.set_progress(1)
                during=window.grab(overlay.navigation_bounds()).toImage()
                incoming=transition.incoming.toImage()
                transition.cancel()
                self.app.processEvents()
                after=window.grab(overlay.navigation_bounds()).toImage()
                import numpy as np
                def pixels(frame):
                    image=frame.convertToFormat(frame.Format.Format_RGBA8888)
                    return np.frombuffer(image.bits(),dtype=np.uint8).reshape(image.height(),image.width(),4).astype(int)
                # Alpha-composited glyph edges can round by 1-2 levels. Larger
                # differences expose stale controls, wrong layouts or a jump.
                largest=int(np.abs(pixels(during)-pixels(after)).max())
                if largest>2:
                    directory=Path(os.environ['RONOVA_PREVIEW_DIR']); directory.mkdir(parents=True,exist_ok=True)
                    during.save(str(directory/'subpage-during.png')); after.save(str(directory/'subpage-after.png'))
                    incoming.save(str(directory/'subpage-incoming.png'))
                self.assertLessEqual(largest,2)
        self.assertEqual(self.chat.calls,0); self.assertFalse(self.controller.call.active)

    def test_nested_navigation_can_interrupt_its_parent_and_release_layers_on_resize_or_exit(self):
        window=self.window; overlay=window.overlay; pane=window.settings_pane; memory=pane.memory_page
        window.settings(); settle(overlay.transition)
        pane.open_memory(); QTest.qWait(80)
        before=window.grab(overlay.navigation_bounds()).toImage()
        memory.edit(None)
        self.assertIsNone(pane.stack.transition.snapshot)
        self.assertEqual(memory.stack.transition.snapshot.toImage(),before)
        self.assertEqual(window.grab(overlay.navigation_bounds()).toImage(),before)
        window.resize(960,602); self.app.processEvents()
        self.assertIsNone(memory.stack.transition.snapshot)
        self.assertIs(memory.stack.currentWidget(),memory.editor)
        memory.show_home(); QTest.qWait(80); window.open_call()
        self.assertIsNone(memory.stack.transition.snapshot)
        settle(overlay.transition)
        self.assertEqual(overlay.phase,'hidden'); self.assertEqual(window.screen,'call')

    def test_rebuilt_memory_and_history_cards_hide_before_the_next_frame_is_captured(self):
        pane=self.window.settings_pane; memory=pane.memory_page; store=self.controller.memory
        self.window.settings(); settle(self.window.overlay.transition)
        pane.open_memory(); settle(pane.stack.transition)
        old=memory.list_layout.itemAt(0).widget()
        store.save('离线界面检查记录')
        memory.refresh(force=True)
        self.assertTrue(old.isHidden())
        record=store.snapshot()['memories'][0]
        memory.edit(record); settle(memory.stack.transition)
        memory.show_versions(); settle(memory.stack.transition)
        old=memory.history_layout.itemAt(0).widget()
        memory.show_versions()
        self.assertTrue(old.isHidden())


if __name__=='__main__': unittest.main()
