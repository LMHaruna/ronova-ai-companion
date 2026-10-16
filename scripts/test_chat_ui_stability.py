"""Transient chat scrolling, inline voice choices and external links, all offline."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QLabel
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall,fake_router
from client_store import Preferences


class ChatUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
    def setUp(self):
        self.guards=[patch('client_models.environment',return_value='offline-fixture'),
                     patch('requests.sessions.Session.request',side_effect=AssertionError('No network')),
                     patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('No microphone'))]
        for guard in self.guards:guard.start()
        self.directory=tempfile.TemporaryDirectory()
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        self.window=ClientWindow(self.controller,ROOT);self.window.show();self.app.processEvents();QTest.qWait(220)
        self.window.timer.stop();self.window.motion_timer.stop();self.window.grab()
    def tearDown(self):
        self.controller.close().result(5);self.window.close();self.app.processEvents()
        self.directory.cleanup()
        for guard in reversed(self.guards):guard.stop()
    def settle(self,delay=40):
        self.app.processEvents();self.window.grab();QTest.qWait(delay);self.app.processEvents()
    def until(self,predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:self.settle(15)
        self.assertTrue(predicate())
    def history(self):
        history=self.window.text_pane.history
        rows=[dict(id=i,user='较早的一条消息。'*6,assistant='较早的一条回复。'*10,state='completed') for i in range(10)]
        history.refresh(rows);self.settle(100)
        self.until(lambda:history.verticalScrollBar().maximum()>0 and history.verticalScrollBar().value()==history.verticalScrollBar().maximum())
        return history,rows
    def pending(self,pane):
        if pane.pending:pane.pending.result(3)
        pane.refresh();self.settle()
    def models(self):
        self.window.settings();self.settle(230)
        pane=self.window.settings_pane;pane.choose_models();self.settle(200)
        pane.select_model('gpt');self.pending(pane)
        self.until(lambda:pane.model_details['gpt'].get_reveal()==1)
        return pane
    def capture(self,name):
        directory=Path(os.environ.get('RONOVA_PREVIEW_DIR',ROOT/'runtime/verification-records/chat-ui-20261015/previews'))
        directory.mkdir(parents=True,exist_ok=True)
        self.assertTrue(self.window.grab().save(str(directory/name)))

    def test_streamed_reply_reuses_bubbles_and_never_collapses_the_scroll_range(self):
        history,rows=self.history();bar=history.verticalScrollBar()
        old=history.action_buttons[0]['replay']
        rows.append(dict(id=10,user='继续。',assistant='新的回复。',state='pending'))
        history.refresh(rows);self.settle()
        bubble=next(label for label in history.findChildren(QLabel)
                    if label.objectName()=='assistantBubble' and label.property('recordId')==10)
        ranges=[];positions=[]
        bar.rangeChanged.connect(lambda low,high:ranges.append(high));bar.valueChanged.connect(positions.append)
        for size in (3,8,18,36):
            rows[-1]['assistant']='最新回复正在连续生成。'*size
            history.refresh(rows);self.settle()
            self.assertIs(history.action_buttons[0]['replay'],old)
            self.assertEqual(bubble.text(),rows[-1]['assistant'])
            self.assertEqual(bar.value(),bar.maximum())
        self.assertTrue(ranges);self.assertNotIn(0,ranges)
        rows[-1]['state']='completed';history.refresh(rows);self.settle(100)
        self.assertTrue(all(after>=before for before,after in zip(positions,positions[1:])))
        events=[];bar.valueChanged.connect(events.append)
        for _ in range(15):history.refresh(rows);self.settle(5)
        self.assertEqual(events,[]);self.assertEqual(bar.value(),bar.maximum())
        bottom=bubble.mapTo(history.viewport(),bubble.rect().bottomLeft())
        self.assertTrue(history.viewport().rect().contains(bottom))
        self.capture('chat-latest-stable.png')

    def test_reading_older_messages_preserves_position_and_clearing_removes_actions(self):
        history,rows=self.history();bar=history.verticalScrollBar()
        bar.setValue(bar.maximum()//2);position=bar.value()
        for _ in range(5):history.refresh(rows);self.settle(8)
        self.assertEqual(bar.value(),position)
        rows[0]['assistant']+='历史回复的补充说明。';history.refresh(rows);self.settle()
        self.assertEqual(bar.value(),position)
        history.refresh([]);self.settle()
        self.assertEqual(history.toPlainText(),'');self.assertEqual(history.action_buttons,{})
        self.assertEqual(history.reply_avatars,{})

    def test_voice_picker_expands_inline_rebounds_and_fits_the_model_form(self):
        pane=self.models();picker=pane.model_tts_combos['gpt'];outer=pane.model_details['gpt']
        self.assertEqual(picker.foldout.height(),0);initial=outer.height()
        reveals=[];picker.foldout.animation.valueChanged.connect(lambda value:reveals.append(float(value)))
        QTest.mouseClick(picker.header,Qt.MouseButton.LeftButton)
        self.settle(80)
        self.assertGreater(picker.foldout.get_reveal(),0)
        self.until(lambda:picker.foldout.get_reveal()==1 and picker.foldout.animation.state().name=='Stopped')
        self.assertGreater(max(reveals),1)
        self.assertGreater(outer.height(),initial)
        self.assertLessEqual(picker.header.title.fontMetrics().horizontalAdvance(picker.currentText()),picker.header.title.width())
        for option in picker.options:
            self.assertTrue(option.isVisible())
            self.assertLessEqual(option.mapTo(outer,option.rect().bottomLeft()).y(),outer.height())
        self.capture('tts-inline-open.png')
        draft=pane.model_fields['gpt']['api_key'];draft.setText('unchanged-offline-draft')
        QTest.mouseClick(picker.header,Qt.MouseButton.LeftButton);self.settle(50)
        QTest.mouseClick(picker.header,Qt.MouseButton.LeftButton)
        self.until(lambda:picker.foldout.get_reveal()==1)
        self.assertEqual(draft.text(),'unchanged-offline-draft')

    def test_click_voice_option_saves_once_closes_and_preserves_other_provider(self):
        pane=self.models();picker=pane.model_tts_combos['gpt']
        other=self.controller.preferences.deepseek_tts_engine
        with patch.object(self.controller,'set_tts_engine',wraps=self.controller.set_tts_engine) as save:
            QTest.mouseClick(picker.header,Qt.MouseButton.LeftButton)
            self.until(lambda:picker.foldout.get_reveal()==1)
            QTest.mouseClick(picker.options[0],Qt.MouseButton.LeftButton)
            self.pending(pane)
            self.until(lambda:not picker.foldout.isVisible())
            save.assert_called_once_with('gpt','indextts-2.5')
        self.assertEqual(picker.currentData(),'indextts-2.5')
        self.assertEqual(Preferences.load(self.controller.preferences_path).gpt_tts_engine,'indextts-2.5')
        self.assertEqual(self.controller.preferences.deepseek_tts_engine,other)
        self.assertEqual(self.chat.calls,0)
        self.capture('tts-selected-gray-link.png')

    def test_gray_api_links_open_their_exact_webpages_on_a_click(self):
        pane=self.models()
        expected={'qwen':'https://platform.qianwenai.com/home/api-keys',
                  'gpt':'https://platform.qianwenai.com/home/api-keys',
                  'deepseek':'https://platform.deepseek.com/api_keys'}
        for engine,url in expected.items():
            for name,detail in pane.model_details.items():detail.animate(name==engine)
            self.settle(420)
            link=pane.model_key_links[engine]
            self.assertIn('color:#89919C',link.text())
            doc=QTextDocument();doc.setDocumentMargin(0);doc.setDefaultFont(link.font())
            doc.setHtml(link.text());doc.setTextWidth(link.contentsRect().width())
            point=None
            for y in range(2,int(doc.size().height()),2):
                for x in range(4,link.width()-4,4):
                    if doc.documentLayout().anchorAt(QPointF(x,y))==url:
                        point=QPoint(x,y+round((link.height()-doc.size().height())/2));break
                if point is not None:break
            self.assertIsNotNone(point)
            with patch('client_panels.QDesktopServices.openUrl',return_value=True) as opened:
                QTest.mouseClick(link,Qt.MouseButton.LeftButton,pos=point);self.app.processEvents()
                opened.assert_called_once()
                self.assertEqual(opened.call_args.args[0].toString(),url)
        self.assertEqual(self.chat.calls,0)


if __name__=='__main__':unittest.main()
