"""Verify single-click oath/replay with real local video and isolated data."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QWEN_REALTIME_URL','wss://fixture.example/realtime')
from PySide6.QtCore import QPoint,QRect,Qt,QEventLoop,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall,fake_router


def main():
    output=Path(sys.argv[1]); output.mkdir(parents=True,exist_ok=True)
    app=QApplication([]); app.setQuitOnLastWindowClosed(False)
    cases=[]; errors=[]
    def wait(predicate,seconds=5):
        deadline=time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline: QTest.qWait(10)
        assert predicate(),'oath UI timed out'
    with tempfile.TemporaryDirectory() as temporary, \
         patch('client_models.environment',return_value='oath-direct-play-fixture'), \
         patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')), \
         patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('microphone forbidden')), \
         patch('client_panels.input_devices',return_value=[]), \
         patch('client_panels.output_devices',return_value=[]):
        router,chat,voice=fake_router()
        controller=DesktopController(temporary,router=router,call_factory=FakeCall)
        controller.preferences=replace(controller.preferences,autoplay=False)
        with controller.memory.connection:
            controller.memory.connection.execute("UPDATE profile SET affinity=100,stage='close'")
        controller.memory.revision+=1
        window=ClientWindow(controller,ROOT); window.show(); dialog=None; date=None
        try:
            for index,(width,height) in enumerate(((1280,802),(960,602))):
                window.resize(width,height); QTest.qWait(250); window.refresh()
                assert window.oath_button.isEnabled()
                assert window.grab().save(str(output/f'entry-{width}.png'))
                with patch.object(controller,'begin_oath',wraps=controller.begin_oath) as begin:
                    QTest.mouseClick(window.oath_button,Qt.MouseButton.LeftButton)
                    dialog=window.oath_dialog
                    wait(lambda:dialog.started is not None)
                    assert begin.call_count==1
                    assert dialog.replay==bool(index)
                    assert dialog.caption.mode=='play' and not dialog.caption.visible_text
                    assert dialog.primary.text()=='跳过动画'
                    assert dialog.secondary.text()=='取消仪式'
                    assert dialog.grab().save(str(output/f'direct-playing-{width}.png'))
                    for button in (dialog.primary,dialog.secondary):
                        box=QRect(button.mapTo(dialog,QPoint(0,0)),button.size())
                        assert dialog.rect().contains(box)
                        assert (button.width(),button.height())==(96,36)
                    saved=set(); decoded=0; last_frame=0; deadline=time.monotonic()+24
                    points=(('invitation',3.5),('contact',8.6),('hold',14.5))
                    loop=QEventLoop(); timer=QTimer(); playback_errors=[]
                    def tick():
                        nonlocal decoded,last_frame
                        try:
                            assert time.monotonic()<deadline,'full playback timed out'
                            assert not dialog.scene.error,dialog.scene.error
                            elapsed=dialog.sound.position()
                            for name,at in points:
                                if elapsed>=at and name not in saved:
                                    assert dialog.grab().save(str(output/f'{name}-{width}.png'))
                                    saved.add(name)
                            if dialog.finished_ceremony and 'completed' not in saved:
                                decoded=dialog.scene.decoder.decoded; last_frame=dialog.scene.last_index
                                assert dialog.grab().save(str(output/f'completed-{width}.png'))
                                saved.add('completed')
                            if not dialog.isVisible(): loop.quit()
                        except Exception:
                            playback_errors.append(traceback.format_exc()); loop.quit()
                    timer.timeout.connect(tick); timer.start(30)
                    loop.exec(); timer.stop()
                    assert not playback_errors,playback_errors
                    assert not dialog.isVisible() and dialog.finished_ceremony
                    assert begin.call_count==1
                assert saved=={name for name,_ in points}|{'completed'}
                assert decoded==384 and last_frame==383
                profile=controller.memory.profile()
                assert profile['affinity']==100 and profile['stage']=='bond'
                assert not controller.snapshot()['oath_active']
                if date is None: date=profile['oath_completed_at']
                else: assert date==profile['oath_completed_at']
                window.refresh(); QTest.qWait(300)
                assert window.assets.stage=='bond'
                assert window.grab().save(str(output/f'returned-{width}.png'))
                cases.append({'window':[width,height],'dpr':window.devicePixelRatioF(),
                    'replay':bool(index),'home_clicks':1,'start_page':False,
                    'decoded_frames':decoded,'last_frame':last_frame,'natural_return':True,
                    'original_oath_date_preserved':bool(index),'buttons':[96,36]})
        except Exception as exc:
            errors.append(repr(exc))
            errors.append(traceback.format_exc())
        finally:
            if dialog: dialog.reject()
            controller.close().result(5)
            window.timer.stop(); window.motion_timer.stop(); window.close()
        report={'scale':os.environ.get('QT_SCALE_FACTOR','1'),'cases':cases,'errors':errors,
            'passed':not errors and len(cases)==2,'cloud_calls':chat.calls,
            'voice_service_calls':len(voice.jobs),'physical_audio':False,'microphone_calls':0,
            'database':'temporary fixtures only','final_bond_portrait_integrated':True}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False)); return 0 if report['passed'] else 1


if __name__=='__main__': sys.exit(main())
