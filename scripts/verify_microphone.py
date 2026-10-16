"""Brief native microphone open/close checks; no transcripts, files or network audio."""
from pathlib import Path
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_input import LiveMicrophone


class NoTranscription:
    def create(self): return None
    def feed(self, stream, pcm): return ''
    def finish(self, stream): return ''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', type=int)
    args = parser.parse_args()
    os.environ.pop('ALI_API_Key', None)
    checks = []
    for _ in range(2):
        count = [0]
        mic = LiveMicrophone(lambda: None, lambda text: None, lambda text, metrics: None,
                             lambda text: None, recognizer_factory=NoTranscription)
        callback = mic._callback
        def observed(*args):
            count[0] += 1
            callback(*args)
        mic._callback = observed
        try:
            mic.start(args.device)
            deadline = time.perf_counter() + 5
            while mic.state == 'preparing' and time.perf_counter() < deadline:
                time.sleep(.01)
            if mic.state != 'listening':
                raise RuntimeError(mic.error or 'Microphone startup timed out')
            time.sleep(.4)
        finally:
            beginning = time.perf_counter()
            mic.stop()
        check = {'callbacks': count[0], 'stop_seconds': time.perf_counter() - beginning,
                 'state': mic.state, 'thread_stopped': not mic._thread.is_alive(),
                 'stream_released': mic._stream is None, 'ram_queue_empty': mic._queue.empty(),
                 'error': mic.error}
        checks.append(check)
        if (count[0] == 0 or mic.error or mic.state != 'off' or not check['thread_stopped']
                or not check['stream_released'] or not check['ram_queue_empty']):
            raise RuntimeError(json.dumps(check))
    now = datetime.now(timezone(timedelta(hours=8)))
    result = {'date': now.date().isoformat(), 'device': args.device, 'capture_seconds_per_cycle': .4,
              'raw_audio_saved': False, 'transcription_enabled': False, 'cloud_requests': 0,
              'checks': checks}
    (ROOT / f'runtime/microphone-lifecycle-{now:%Y%m%d}.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
