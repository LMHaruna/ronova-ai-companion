"""Profile six real Qt navigation paths using offline services and temp data."""
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QEventLoop,QTimer,Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall,fake_router
from client_panels import PhoneSurface,PageTransition


def summary(values):
    values=sorted(values)
    return {'count':len(values),'median_ms':round(statistics.median(values),3) if values else 0,
            'p95_ms':round(values[min(len(values)-1,int(len(values)*.95))],3) if values else 0,
            'max_ms':round(max(values),3) if values else 0}


def run():
    app=QApplication([]); app.setQuitOnLastWindowClosed(False)
    directory=Path(sys.argv[1]); directory.mkdir(parents=True,exist_ok=True)
    work={}; records=[]; checks=[]; observing=False
    def measure(cls):
        original=cls.paintEvent; key=cls.__name__+'.paintEvent'
        def painted(self,event):
            start=time.perf_counter(); result=original(self,event)
            if observing: work.setdefault(key,[]).append((time.perf_counter()-start)*1000)
            return result
        cls.paintEvent=painted
    for cls in (ClientWindow,PhoneSurface,PageTransition): measure(cls)
    def wait(predicate):
        deadline=time.perf_counter()+3
        while not predicate() and time.perf_counter()<deadline: QTest.qWait(5)
        assert predicate(),'transition did not finish'
    def pause(milliseconds):
        loop=QEventLoop(); QTimer.singleShot(milliseconds,loop.quit); loop.exec()
    with tempfile.TemporaryDirectory() as temporary, \
         patch('requests.sessions.Session.request',side_effect=AssertionError('live request forbidden')), \
         patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('physical mic forbidden')), \
         patch('client_models.environment',return_value='transition-fixture-key'), \
         patch('client_panels.input_devices',return_value=[]), \
         patch('client_panels.output_devices',return_value=[]):
        router,chat,voice=fake_router(); controller=DesktopController(temporary,router=router,call_factory=FakeCall)
        window=ClientWindow(controller,ROOT); window.show(); QTest.qWait(500)
        draft='界面切换期间保留的中文草稿'
        window.text_pane.input.setPlainText(draft)
        def settled(page):
            if page=='call': return window.overlay.phase=='hidden' and window.screen=='call'
            return (window.overlay.phase=='shown' and window.overlay.page==page and
                    window.overlay.surface.pages.transition.snapshot is None)
        def navigate(page):
            if page=='call': window.open_call()
            elif page=='text': window.text()
            else: window.settings()
        def prepare(source,target):
            if source=='settings':
                origin='call' if target=='call' else 'text'
                navigate(origin); wait(lambda:settled(origin))
                navigate('settings')
            else: navigate(source)
            wait(lambda:settled(source)); QTest.qWait(45)
        def sample(source,target,active=False):
            nonlocal observing
            prepare(source,target); work.clear(); gaps=[]; frames={}; values={}
            last=time.perf_counter(); frame_last={}
            def beat():
                nonlocal last
                now=time.perf_counter(); gaps.append((now-last)*1000); last=now
            def changed(value,channel):
                if type(value) not in (int,float): return
                now=time.perf_counter()
                if channel in frame_last: frames.setdefault(channel,[]).append((now-frame_last[channel])*1000)
                frame_last[channel]=now; values.setdefault(channel,[]).append(float(value))
            animations=(window.overlay.animation,window.overlay.surface.pages.transition.animation)
            callbacks=[(animation,lambda value,key=key:changed(value,key))
                       for animation,key in zip(animations,('phone','subpage'))]
            for animation,callback in callbacks: animation.valueChanged.connect(callback)
            heartbeat=QTimer(); heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
            heartbeat.timeout.connect(beat); heartbeat.start(8)
            observing=True; start=time.perf_counter()
            if source=='settings' and target!='settings': window.close_phone()
            else: navigate(target)
            dispatch=(time.perf_counter()-start)*1000
            wait(lambda:settled(target)); complete=(time.perf_counter()-start)*1000
            pause(70); observing=False; heartbeat.stop()
            for animation,callback in callbacks: animation.valueChanged.disconnect(callback)
            assert window.text_pane.input.toPlainText()==draft
            assert controller.call.active==active and controller.call_requested==active
            assert chat.calls==0
            records.append({'path':source+'→'+target,'size':[window.width(),window.height()],
                'active_call_and_playback':active,'dispatch_ms':round(dispatch,3),'completion_ms':round(complete,3),
                'heartbeat':summary(gaps),'animation_updates':{key:summary(items) for key,items in frames.items()},'animation_values':values,
                'paint':{key:summary(value) for key,value in work.items()},
                'draft_preserved':True,'call_preserved':True,'final_page_correct':True})
        try:
            paths=(('call','text'),('text','call'),('call','settings'),('settings','call'),('text','settings'),('settings','text'))
            for width,height in ((1280,802),(960,602),(1440,960)):
                window.resize(width,height); QTest.qWait(100)
                for source,target in paths: sample(source,target)
            checks.append('six directions complete at three sizes without losing the draft or starting capture')
            window.resize(1280,802); QTest.qWait(100)
            controller.start_call().result(3); voice.playback_active=True; voice.playback_level=.6; window.refresh()
            for source,target in paths: sample(source,target,active=True)
            checks.append('six directions preserve an active simulated call and playback')
            voice.playback_active=False; voice.playback_level=0; controller.end_call().result(3)
            work.clear(); observing=True
            for _ in range(4):
                for target in ('text','settings','call','settings','text'):
                    navigate(target); pause(25)
            wait(lambda:settled('text')); observing=False
            assert window.text_pane.input.toPlainText()==draft
            assert window.overlay.surface.pages.transition.snapshot is None
            assert window.overlay.effect.opacity()==1
            checks.append('twenty rapid reversals settle on the last target and release the old snapshots')
            subpages=[]
            navigate('settings'); wait(lambda:settled('settings'))
            settings=window.settings_pane; memory=settings.memory_page
            def visit(label,action,stack,target):
                action(); wait(lambda:stack.transition.snapshot is None)
                assert stack.currentWidget() is target
                assert window.text_pane.input.toPlainText()==draft and chat.calls==0
                subpages.append(label)
            for label,action,target in (
                ('models',settings.choose_models,settings.model_page),
                ('call-mode',lambda:settings.choose('call_mode','通话方式'),settings.choice_page),
                ('microphone',lambda:settings.choose('microphone','麦克风'),settings.choice_page),
                ('speaker',lambda:settings.choose('speaker','播放设备'),settings.choice_page),
                ('incoming-call',settings.open_call_settings,settings.call_page),
                ('clear-confirmation',settings.confirm_clear,settings.confirmation),
                ('memory',settings.open_memory,settings.memory_page)):
                visit(label,action,settings.stack,target)
                if label=='incoming-call':
                    visit('incoming-limit',lambda:settings.choose('call_daily_limit','每日最多来电'),settings.stack,settings.choice_page)
                    visit('incoming-back',lambda:settings.stack.setCurrentWidget(settings.call_page),settings.stack,settings.call_page)
                    visit('incoming-history',settings.open_call_history,settings.stack,settings.call_history_page)
                if label=='memory':
                    for name,action,page in (('memory-editor',lambda:memory.edit(None),memory.editor),
                        ('relationship',memory.choose_stage,memory.stages),
                        ('affinity-confirmation',memory.confirm_reset,memory.confirmation),
                        ('oath-confirmation',memory.confirm_oath_reset,memory.confirmation)):
                        visit(name,action,memory.stack,page)
                        visit(name+'-back',memory.show_home,memory.stack,memory.home)
                visit(label+'-back',lambda:settings.stack.setCurrentWidget(settings.root),settings.stack,settings.root)
            checks.append('settings, audio devices, incoming calls, memory and confirmation subpages return correctly')
            # Freeze exact animation times for visual review without perturbing
            # the performance measurements above with screenshot encoding.
            from PIL import Image,ImageDraw
            frames=[]
            for source,target in (('text','settings'),('settings','text'),('call','text'),('text','call')):
                prepare(source,target)
                if source=='settings': window.close_phone()
                else: navigate(target)
                animation=window.overlay.animation; animation.pause()
                row=[]
                for position in (0,70,140,210,280):
                    animation.setCurrentTime(position)
                    path=directory/f'{source}-{target}-{position:03}.png'
                    assert window.grab(window.overlay.navigation_bounds()).save(str(path))
                    row.append(str(path))
                animation.resume(); wait(lambda:settled(target)); frames.append(row)
            prepare('text','settings'); navigate('settings'); wait(lambda:settled('settings'))
            gif=[]; durations=[]
            for label,action in (('settings-models',settings.choose_models),
                                 ('models-settings',lambda:settings.stack.setCurrentWidget(settings.root))):
                action(); animation=settings.stack.transition.animation; animation.pause()
                row=[]
                for position in range(0,321,20):
                    animation.setCurrentTime(position)
                    capture=window.grab(window.overlay.navigation_bounds())
                    if position in (0,80,160,240,320):
                        path=directory/f'{label}-{position:03}.png'
                        assert capture.save(str(path)); row.append(str(path))
                    frame=capture.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
                    preview=Image.frombytes('RGBA',(frame.width(),frame.height()),bytes(frame.bits())).convert('RGB')
                    gif.append(preview.resize((328,660))); durations.append(500 if position==0 else 650 if position==320 else 20)
                animation.resume(); wait(lambda:settings.stack.transition.snapshot is None); frames.append(row)
            gif[0].save(directory/'settings-navigation.gif',save_all=True,append_images=gif[1:],duration=durations,loop=0)
            thumb=(240,484); sheet=Image.new('RGB',(5*thumb[0],len(frames)*(thumb[1]+24)), '#20262B')
            draw=ImageDraw.Draw(sheet)
            for y,row in enumerate(frames):
                for x,path in enumerate(row):
                    with Image.open(path) as frame: sheet.paste(frame.convert('RGB').resize(thumb),(x*thumb[0],y*(thumb[1]+24)+24))
                    draw.text((x*thumb[0]+5,y*(thumb[1]+24)+5),Path(path).stem,fill='white')
            sheet.save(directory/'transition-frames.jpg')
            for page in ('call','text','settings'):
                navigate(page); wait(lambda:settled(page)); QTest.qWait(80)
                assert window.grab().save(str(directory/(page+'.png')))
            report={'checks':checks,'passed':True,'records':records,'subpages':subpages,'dpr':window.devicePixelRatioF(),
                'measurement':'Qt offscreen event loop and paint timings; OS compositor/display FPS not measured',
                'cloud_requests':0,'physical_microphone_started':False,'physical_playback_started':False}
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'passed':True,'paths_checked':len(records),'dpr':window.devicePixelRatioF(),
                'dispatch_p95_ms':summary([r['dispatch_ms'] for r in records])['p95_ms'],
                'largest_heartbeat_ms':max(r['heartbeat']['max_ms'] for r in records),
                'largest_paint_ms':max(v['max_ms'] for r in records for v in r['paint'].values())},ensure_ascii=False))
        finally: controller.close().result(5); window.close()


if __name__=='__main__': run()
