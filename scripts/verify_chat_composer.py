"""Exercise the embedded chat composer and scroll position with offline fixtures."""
from pathlib import Path
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(os.environ.get('RONOVA_ASSET_ROOT', Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(ROOT), str(ROOT/'scripts'), str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0, os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router


def main():
    directory = Path(os.environ.get('RONOVA_PREVIEW_DIR', ROOT/'runtime/verification-records/chat-composer-20261010'))
    directory.mkdir(parents=True, exist_ok=True)
    app = QApplication([]); app.setQuitOnLastWindowClosed(False)
    checks = []

    def wait(predicate):
        deadline = time.monotonic()+5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents(); QTest.qWait(10)
        assert predicate(), 'UI action timed out'
        app.processEvents(); QTest.qWait(60); app.processEvents()

    with tempfile.TemporaryDirectory(dir=directory) as temporary, \
            patch('requests.sessions.Session.request', side_effect=AssertionError('live network forbidden')), \
            patch('voice_input.LiveMicrophone.start', side_effect=AssertionError('live microphone forbidden')), \
            patch('client_models.environment', return_value='ui-fixture-key'):
        router, chat, voice = fake_router()
        controller = DesktopController(temporary, router=router, call_factory=FakeCall)
        window = ClientWindow(controller, ROOT)
        try:
            window.show(); app.processEvents()
            window.resize(960,602); app.processEvents()
            window.resize(1280,802); app.processEvents()
            window.text(); wait(lambda:window.overlay.phase == 'shown')
            assert window.overlay.viewport().width() == 328
            pane = window.text_pane

            def capture(name):
                assert window.grab().save(str(directory/(name+'.png')))

            assert pane.input.placeholderText() and not pane.send.isEnabled()
            capture('empty-input')
            QTest.keyClicks(pane.input, 'Hello')
            assert pane.input.toPlainText() == 'Hello' and not pane.input.placeholderText()
            assert pane.send.isEnabled()
            pane.input.selectAll(); QTest.keyClick(pane.input, Qt.Key.Key_Backspace)
            assert pane.input.placeholderText() and not pane.send.isEnabled()
            checks.append('typing hides the prompt and deleting all text restores it')

            calls = chat.calls
            app.sendEvent(pane.input, QInputMethodEvent('正在输入中文', []))
            assert pane.input.toPlainText() == '' and not pane.input.placeholderText()
            capture('chinese-composition')
            app.sendEvent(pane.input, QInputMethodEvent('', []))
            assert pane.input.placeholderText()
            app.sendEvent(pane.input, QInputMethodEvent('候选词', []))
            QTest.keyClick(pane.input, Qt.Key.Key_Return)
            assert chat.calls == calls
            app.sendEvent(pane.input, QInputMethodEvent('', []))
            pane.input.clear()
            assert pane.input.placeholderText()
            checks.append('Chinese IME preedit hides the prompt and candidate Enter does not send')

            app.sendEvent(pane.input, QInputMethodEvent('候选词', []))
            commit = QInputMethodEvent('', []); commit.setCommitString('你好，若娜瓦。')
            app.sendEvent(pane.input, commit)
            assert pane.input.toPlainText() == '你好，若娜瓦。' and not pane.input.placeholderText()
            capture('chinese-input-return-icon')
            checks.append('committed Chinese keeps the prompt hidden and enables the return icon')

            pane.input.clear()
            app.clipboard().setText('粘贴的中文消息\n第二行')
            QTest.keyClick(pane.input, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            assert pane.input.toPlainText() == '粘贴的中文消息\n第二行'
            assert not pane.input.placeholderText() and pane.send.isEnabled()
            pane.input.setPlainText('\n ')
            assert not pane.input.placeholderText() and not pane.send.isEnabled()
            checks.append('paste and multiline drafts hide the prompt; whitespace cannot send')

            pane.input.setPlainText('点击回车图标发送。')
            QTest.mouseClick(pane.send, Qt.MouseButton.LeftButton)
            wait(lambda:len(controller.records()) == 1 and controller.records()[-1]['state'] == 'completed')
            assert pane.input.toPlainText() == '' and pane.input.placeholderText()
            assert controller.records()[-1]['user'] == '点击回车图标发送。'
            checks.append('clicking the return icon sends once and clears the draft')
            pane.input.setPlainText('键盘发送。')
            QTest.keyClick(pane.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
            assert '\n' in pane.input.toPlainText() and len(controller.records()) == 1
            QTest.keyClick(pane.input, Qt.Key.Key_Return)
            wait(lambda:len(controller.records()) == 2 and controller.records()[-1]['state'] == 'completed')
            checks.append('Enter still sends and Shift+Enter still inserts a newline')

            # Keep the application timer from replacing the controlled long history.
            window.timer.stop(); window.motion_timer.stop()
            history = pane.history; bar = history.verticalScrollBar()
            records = [dict(id=i, user='较早的一条消息。'*6,
                            assistant='较早的一条回复。'*10, state='completed') for i in range(12)]
            history.refresh(records)
            wait(lambda:bar.maximum()>0 and bar.value()==bar.maximum())
            assert bar.maximum() > 0 and bar.value() == bar.maximum()
            checks.append('opening a long conversation settles at the latest message')

            bar.setValue(0); history.refresh(records); QTest.qWait(60)
            assert bar.value() == 0
            checks.append('unchanged refresh lets the user read earlier messages')
            records.append(dict(id=12, user='请继续。', assistant='', state='pending'))
            history.refresh(records); QTest.qWait(60)
            assert bar.value() == bar.maximum()
            checks.append('a new message brings scrolled history back to the latest turn')

            initial_maximum = bar.maximum()
            for size in (1, 8, 30, 60):
                bar.setValue(0)
                records[-1]['assistant'] = '若娜瓦正在逐段回复。'*size
                history.refresh(records); QTest.qWait(60)
                assert bar.value() == bar.maximum(), (size, bar.value(), bar.maximum())
            assert bar.maximum() > initial_maximum
            capture('streaming-latest-reply')
            checks.append('every streaming update follows the bottom after bubble height changes')

            records[-1]['assistant'] += '\n这是最新回复的结尾。'
            records[-1]['state'] = 'completed'
            history.refresh(records); QTest.qWait(60)
            assert bar.value() == bar.maximum()
            bubble = next(label for label in history.findChildren(QLabel)
                          if label.text() == records[-1]['assistant'])
            bottom = bubble.mapTo(history.viewport(), bubble.rect().bottomLeft())
            assert history.viewport().rect().contains(bottom)
            capture('completed-latest-reply')
            checks.append('completed reply keeps its final line in view')

            for width, height in ((960,602), (1440,900), (1280,802)):
                window.resize(width,height); QTest.qWait(60)
                assert bar.value() == bar.maximum()
            checks.append('reply remains at the bottom through the supported window sizes')

            bar.setValue(bar.maximum()//2); old_position = bar.value()
            history.refresh(records); QTest.qWait(60)
            assert bar.value() == old_position
            records[0]['assistant'] += '历史记录状态更新。'
            history.refresh(records); QTest.qWait(60)
            assert bar.value() == old_position
            checks.append('reading history stays stable when the latest reply is unchanged')

            report = dict(passed=len(checks), checks=checks, cloud_requests=0,
                          physical_microphone_started=False,
                          screenshots=[str(path) for path in directory.glob('*.png')])
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(dict(passed=len(checks),report=str(directory/'report.json')),ensure_ascii=False))
        finally:
            if not controller.closed: controller.close().result(5)
            window.timer.stop(); window.motion_timer.stop(); window.close(); app.processEvents()


if __name__ == '__main__': main()
