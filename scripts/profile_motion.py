"""Measure the actual Qt event loop with isolated offline services."""
from pathlib import Path
import json
import os
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(ROOT), str(ROOT/'scripts'), str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'):
    sys.path.insert(0, os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router
from client_panels import PhoneSurface, ModelFoldout


def summarize(values):
    values = sorted(values)
    return {'count': len(values), 'median_ms': round(statistics.median(values), 3) if values else 0,
            'p95_ms': round(values[min(len(values)-1, int(len(values)*.95))], 3) if values else 0,
            'max_ms': round(max(values), 3) if values else 0}


def main():
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    timings = {}
    for cls, method in ((ClientWindow, 'paintEvent'), (PhoneSurface, 'paintEvent'),
                        (ModelFoldout, 'place_form')):
        original = getattr(cls, method)
        key = cls.__name__+'.'+method
        def measured(self, *args, _original=original, _key=key):
            begin = time.perf_counter()
            result = _original(self, *args)
            timings.setdefault(_key, []).append((time.perf_counter()-begin)*1000)
            return result
        setattr(cls, method, measured)
    report = {}
    with tempfile.TemporaryDirectory() as temporary, \
            patch('requests.sessions.Session.request', side_effect=AssertionError('live network forbidden')), \
            patch('voice_input.LiveMicrophone.start', side_effect=AssertionError('live microphone forbidden')), \
            patch('client_models.environment', return_value='motion-fixture-key'), \
            patch('client_panels.input_devices', return_value=[{'id':7,'name':'测试麦克风'}]), \
            patch('client_panels.output_devices', return_value=[{'id':1,'name':'测试扬声器'}]):
        router, chat, voice = fake_router()
        controller = DesktopController(temporary, router=router, call_factory=FakeCall)
        window = ClientWindow(controller, ROOT)
        window.show()
        QTest.qWait(300)
        def sample(name, action, milliseconds=500):
            timings.clear()
            intervals = []
            last = time.perf_counter()
            heartbeat = QTimer()
            heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
            def beat():
                nonlocal last
                now = time.perf_counter()
                intervals.append((now-last)*1000)
                last = now
            heartbeat.timeout.connect(beat)
            heartbeat.start(8)
            begin = time.perf_counter()
            action()
            action_ms = (time.perf_counter()-begin)*1000
            loop = QEventLoop()
            QTimer.singleShot(milliseconds, loop.quit)
            loop.exec()
            heartbeat.stop()
            report[name] = {'action_ms':round(action_ms,3), 'heartbeat':summarize(intervals),
                            'work':{key:summarize(value) for key,value in timings.items()}}
        try:
            sample('idle', lambda:None)
            sample('call_button', lambda:QTest.mouseClick(window.voice_button,Qt.MouseButton.LeftButton))
            controller.end_call().result(5)
            sample('open_settings', window.settings)
            settings = window.settings_pane
            sample('model_page', lambda:settings.choose('engine','对话模型'))
            sample('expand', lambda:settings.select_model('qwen'))
            sample('switch', lambda:settings.select_model('gpt'))
            sample('collapse', lambda:settings.select_model('gpt'))
            sample('close_settings', window.close_phone)
            sample('open_chat', window.text)
            sample('chat_to_settings', window.settings)
            sample('autoplay', settings.autoplay.click)
            report['cloud_requests'] = 0
            report['physical_microphone_started'] = False
        finally:
            controller.close().result(5)
            window.close()
    target = Path(sys.argv[1])
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
