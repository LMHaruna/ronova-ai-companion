"""Offline interaction and visual checks for smooth phone motion."""
from pathlib import Path
from concurrent.futures import Future
import json
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall,fake_router


def main():
    directory=Path(sys.argv[1]); directory.mkdir(parents=True,exist_ok=True)
    app=QApplication([]); app.setQuitOnLastWindowClosed(False)
    checks=[]
    def wait(predicate):
        deadline=time.monotonic()+5
        while not predicate() and time.monotonic()<deadline: QTest.qWait(10)
        assert predicate(),'interaction timed out'
    with tempfile.TemporaryDirectory() as temporary, \
            patch('requests.sessions.Session.request',side_effect=AssertionError('live network forbidden')), \
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('live microphone forbidden')), \
            patch('client_models.environment',return_value='motion-fixture-key'), \
            patch('client_panels.input_devices',return_value=[{'id':7,'name':'测试麦克风'}]), \
            patch('client_panels.output_devices',return_value=[{'id':1,'name':'测试扬声器'}]):
        router,chat,voice=fake_router()
        controller=DesktopController(temporary,router=router,call_factory=FakeCall)
        window=ClientWindow(controller,ROOT); window.show(); QTest.qWait(300)
        release=threading.Event()
        try:
            window.settings(); wait(lambda:window.overlay.phase=='shown')
            settings=window.settings_pane
            wait(lambda:settings.device_future is None)
            # A blocked driver must not delay opening or navigating the page.
            entered=threading.Event()
            def slow_devices():
                entered.set(); assert release.wait(5)
                return [(None,'系统默认'),(8,'慢速设备')],[(None,'系统默认')]
            with patch.object(settings,'scan_devices',side_effect=slow_devices):
                window.close_phone(); wait(lambda:window.overlay.phase=='shown' and window.overlay.page=='text')
                window.settings(); wait(entered.is_set)
                wait(lambda:window.overlay.phase=='shown')
                assert not release.is_set() and settings.isEnabled()
                settings.apply_change('volume',.37)
                wait(lambda:settings.pending is None)
                assert controller.preferences.volume==.37
                settings.choose('engine','对话模型'); QTest.qWait(220)
                assert settings.stack.currentWidget() is settings.model_page
                release.set(); wait(lambda:settings.device_future is None)
            checks.append('blocked audio-device discovery does not block page animation or model navigation')

            row=window.voice_button
            row.press_feedback(); row.release_feedback()
            samples=[]
            for _ in range(35): QTest.qWait(8); samples.append(row.feedback)
            assert max(samples)>.85 and samples[-1]==0
            assert max(abs(b-a) for a,b in zip(samples,samples[1:]))<.8, samples
            row.press_feedback(); QTest.qWait(90); row.release_feedback(); QTest.qWait(40)
            prior=row.feedback; row.press_feedback(); assert row.feedback==prior
            row.release_feedback(); QTest.qWait(280)
            checks.append('short clicks complete the press and release; rapid re-presses keep their current position')

            settings.select_model('qwen'); QTest.qWait(50)
            detail=settings.model_details['qwen']; progress=detail.get_reveal()
            assert 0<progress<1
            settings.select_model('qwen'); assert detail.get_reveal()==progress
            QTest.qWait(35); progress=detail.get_reveal()
            settings.select_model('qwen'); assert detail.get_reveal()==progress
            wait(lambda:detail.get_reveal()==1)
            checks.append('foldout changes direction immediately without snapping to an endpoint')

            settings.select_model('qwen'); wait(lambda:detail.get_reveal()==0)
            settings.select_model('qwen'); heights=[]
            for _ in range(45): QTest.qWait(8); heights.append(detail.height())
            full_height=detail.height()
            overshoot=max(heights)-full_height
            # The key-only form is shorter than the former URL/model form.
            # Its spring excursion scales with the form, rather than a fixed
            # pixel range from that older layout.
            expected_overshoot=round(full_height*.08)
            assert detail.get_reveal()==1 and max(2,expected_overshoot-3)<=overshoot<=expected_overshoot+3, overshoot
            assert heights[-1]==full_height and max(heights)>heights[-1]
            checks.append('expanded panel has a visible rebound and settles at its exact final height')

            # A slow save must not disable all controls or freeze the reveal.
            pending=Future(); settings.pending=pending
            settings.select_model('gpt'); QTest.qWait(40)
            settings.select_model('qwen'); QTest.qWait(280)
            assert settings.expanded_model=='qwen' and detail.get_reveal()==1
            assert settings.selector.isEnabled() and settings.root.isEnabled()
            pending.set_result(None); wait(lambda:settings.pending is None)
            assert controller.preferences.engine=='qwen'
            checks.append('rapid model choices remain animated during a pending save and apply the last choice')

            window.text(); wait(lambda:window.overlay.phase=='shown')
            window.text_pane.input.setPlainText('保留草稿')
            for _ in range(4):
                window.settings(); assert window.overlay.effect.opacity()==1
                QTest.qWait(35)
                window.text(); assert window.overlay.effect.opacity()==1
                QTest.qWait(35)
            QTest.qWait(340)
            assert window.text_pane.input.toPlainText()=='保留草稿'
            assert window.overlay.surface.pages.transition.snapshot is None
            checks.append('rapid chat/settings transitions keep full screen opacity, preserve drafts and release snapshots')

            settings.stack.setCurrentWidget(settings.root)
            window.settings(); wait(lambda:window.overlay.phase=='shown')
            settings.choose('engine','对话模型'); QTest.qWait(220)
            layouts=[]
            for width,height in ((1280,802),(960,602)):
                window.resize(width,height); QTest.qWait(100)
                for mode in ('collapsed','qwen','gpt'):
                    if mode=='collapsed': settings.choose_models()
                    else: settings.select_model(mode)
                    QTest.qWait(430)
                    for engine,form in settings.model_details.items():
                        if engine==mode:
                            assert form.isVisible() and form.get_reveal()==1
                            for edit in settings.model_fields[engine].values():
                                assert edit.isVisible() and form.form.rect().contains(edit.geometry())
                            assert form.form.rect().contains(settings.model_save_buttons[engine].geometry())
                        else: assert not form.isVisible() and form.height()==0
                    mask=window.overlay.corner_mask
                    assert mask.pixelColor(0,0).alpha()==0
                    assert mask.pixelColor(mask.width()//2,mask.height()//2).alpha()==255
                    assert window.grab().save(str(directory/f'models-{mode}-{width}.png'))
                    layouts.append({'window':[width,height],'mode':mode,'dpr':window.devicePixelRatioF()})
            checks.append('both window sizes preserve complete model forms, mutually exclusive reveals and rounded clipping')
            (directory/'layout.json').write_text(json.dumps(layouts,indent=2),encoding='utf-8')
            report={'checks':checks,'passed':len(checks),'layout_checks':len(layouts),
                    'dpr':window.devicePixelRatioF(),'foldout_overshoot_px':overshoot,
                    'cloud_requests':0,'physical_microphone_started':False}
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))
        finally:
            release.set(); controller.close().result(5); window.close()


if __name__=='__main__': main()
