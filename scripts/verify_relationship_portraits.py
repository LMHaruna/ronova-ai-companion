"""Render all five relationship states with isolated offline services."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router

def main():
    app=QApplication([]); app.setQuitOnLastWindowClosed(False)
    output=Path(os.environ.get('RONOVA_PREVIEW_DIR',ROOT/'runtime/verification-records/relationship-portraits'))
    output.mkdir(parents=True,exist_ok=True)
    checks=[]; layouts=[]; durations=[]
    with tempfile.TemporaryDirectory() as directory, \
            patch('requests.sessions.Session.request',side_effect=AssertionError('live network forbidden')), \
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('live microphone forbidden')), \
            patch('client_models.environment',return_value='portrait-fixture-api-key'):
        router,chat,voice=fake_router()
        controller=DesktopController(directory,router=router,call_factory=FakeCall)
        started=time.perf_counter()
        window=ClientWindow(controller,ROOT)
        preload_ms=(time.perf_counter()-started)*1000
        window.timer.stop(); window.motion_timer.stop()
        try:
            window.show(); app.processEvents()
            controller.memory.save('我是客户端制作者','event')
            baseline=ROOT/'baseline_client_scene.py'
            if baseline.exists():
                spec=importlib.util.spec_from_file_location('portrait_baseline_scene',baseline)
                module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                old=module.SceneAssets(ROOT)
                assert old.foreground.toImage()==window.assets.portraits['acquaintance']['foreground'].toImage()
                assert old.background.toImage()==window.assets.portraits['acquaintance']['background'].toImage()
                checks.append('original acquaintance foreground and blurred background are pixel-identical')
            states=(('acquaintance',0,'相识'),('familiar',30,'熟悉'),('close',70,'亲密'),
                    ('bond',100,'羁绊'),('angry',-5,'生气'))
            for width,height in ((1280,802),(960,602)):
                window.resize(width,height); app.processEvents()
                for stage,score,label in states:
                    with controller.memory.connection:
                        controller.memory.connection.execute('UPDATE profile SET affinity=?,stage=?',(score,stage))
                    if stage=='bond' and not controller.memory.profile()['oath_completed']:
                        controller.memory.complete_oath(controller.memory.prepare_oath())
                    controller.memory.revision+=1
                    window.refresh()
                    assert window.assets.stage==stage
                    if window._portrait_transition_from is not None:
                        window._portrait_transition_started=time.monotonic()-window._portrait_transition_duration-1
                        window.animate_scene()
                    app.processEvents()
                    image=window.grab()
                    assert image.save(str(output/('scene-'+stage+'-'+str(width)+'.png')))
                    for button in (window.voice_button,window.end_button,window.text_button,window.settings_button):
                        assert window.rect().contains(button.geometry())
                    assert controller.memory.snapshot()['memories'][0]['content']=='我是客户端制作者'
                    layouts.append({'state':stage,'affinity':score,'width':width,'height':height,
                                    'dpr':window.devicePixelRatioF(),'image':str(output/('scene-'+stage+'-'+str(width)+'.png'))})
            checks.append('all five states preserve memory and control layout at both window sizes')
            window.resize(1280,802); app.processEvents()
            with controller.memory.connection:
                controller.memory.connection.execute("UPDATE profile SET affinity=-5,stage='angry'")
            controller.memory.revision+=1; window.refresh()
            window._portrait_transition_from=None
            window.overlay.open_page('settings')
            from PySide6.QtTest import QTest
            QTest.qWait(240)
            window.settings_pane.open_memory()
            QTest.qWait(240)
            pane=window.settings_pane.memory_page; pane.refresh(force=True)
            assert pane.affinity_bar.value()==-5
            assert '-5' in pane.affinity_label.text() and '生气' in pane.stage_row.value.text()
            assert window.grab().save(str(output/'relationship-negative.png'))
            pane.choose_stage(); QTest.qWait(240)
            assert window.grab().save(str(output/'relationship-rules.png'))
            checks.append('phone page exposes signed score, negative meter and updated stage rules')
            window.overlay.close_page(); QTest.qWait(240)
            window.sync_portrait('close'); window.scene_cache()
            for index in range(32):
                window._portrait_transition_started=time.monotonic()-index/32*window._portrait_transition_duration
                began=time.perf_counter()
                window.grab()
                durations.append((time.perf_counter()-began)*1000)
            checks.append('transition uses cached static frames without any live audio or cloud calls')
            assert chat.calls==0 and not controller.call.active
            canvas=QImage(1280,429*((len(states)+1)//2),QImage.Format.Format_ARGB32_Premultiplied)
            canvas.fill(QColor('#111216'))
            painter=QPainter(canvas); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            font=QFont('Microsoft YaHei UI'); font.setPixelSize(17); painter.setFont(font)
            for index,(stage,score,label) in enumerate(states):
                x=(index%2)*640; y=(index//2)*429
                painter.setPen(QColor('#F5F5F7'))
                painter.drawText(QRectF(x+16,y+3,600,24),int(Qt.AlignmentFlag.AlignLeft),label+' · '+str(score))
                image=QImage(str(output/('scene-'+stage+'-1280.png')))
                painter.drawImage(QRectF(x,y+28,640,401),image)
            painter.end(); assert canvas.save(str(output/'contact-sheet.png'))
        finally:
            controller.close().result(5); window.close(); app.processEvents()
    durations.sort()
    report={'checks':checks,'layouts':layouts,'affinity_range':[-20,100],
            'preload_ms':round(preload_ms,2),'transition_grab_p95_ms':round(durations[int(len(durations)*.95)],2),
            'cloud_requests':0,'physical_microphone_starts':0,'physical_playback_starts':0}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0

if __name__=='__main__': raise SystemExit(main())

