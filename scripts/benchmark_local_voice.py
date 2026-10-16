"""Measure local synthesis with fixed test text; no cloud, microphone or playback."""
import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEXT = '凡人，先把眼前的事做好。其他的，稍后再说。'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path)
    parser.add_argument('--engine', choices=('gpt-sovitsv2proplus','indextts-2.5'),default='gpt-sovitsv2proplus')
    parser.add_argument('--audio-output',type=Path)
    parser.add_argument('--sentence-stream',action='store_true',help='Use the desktop sentence buffer and append queue.')
    parser.add_argument('--check-cancel',action='store_true')
    parser.add_argument('--text',default=TEXT,help='Explicit local benchmark text; no cloud request.')
    parser.add_argument('--device', choices=('auto', 'cpu', 'cuda'), default='auto')
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.text.strip(): parser.error('text must not be empty')
    if not 1 <= args.runs <= 5:
        parser.error('runs must be between 1 and 5')
    python = args.python or ROOT / 'runtime/voice-venv/Scripts/python.exe'
    env = dict(os.environ, PYTHONUTF8='1', PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
               RONOVA_VOICE_DEVICE=args.device, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    for name in list(env):
        if 'api_key' in name.casefold() or 'api_token' in name.casefold():
            env.pop(name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    events = queue.Queue()
    jobs = []
    with args.output.with_suffix('.stderr.log').open('w', encoding='utf-8') as log:
        worker='index_voice_worker.py' if args.engine=='indextts-2.5' else 'voice_worker.py'
        process = subprocess.Popen([str(python), '-s', '-u', str(ROOT / worker)], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def reader():
            for line in process.stdout:
                try: events.put(json.loads(line))
                except ValueError: pass
            events.put({'event': 'process_exit'})
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        def send(action, **values):
            process.stdin.write(json.dumps(dict(action=action, **values), ensure_ascii=False)+'\n')
            process.stdin.flush()
        def receive(timeout):
            event = events.get(timeout=timeout)
            if event['event'] in ('boot_error', 'error', 'process_exit'):
                raise RuntimeError(str(event))
            return event
        try:
            started = time.perf_counter()
            send('prepare')
            while True:
                ready = receive(300)
                if ready['event'] == 'ready': break
                if ready['event'] == 'status': print(ready['message'], flush=True)
            prepared = time.perf_counter()-started
            if args.device == 'cuda' and ready['device'] != 'cuda':
                raise RuntimeError('CUDA benchmark requested but voice fell back to CPU')
            print(json.dumps({'ready_seconds': prepared, 'device': ready['device'],
                              'is_half': ready['is_half'], 'gpu': ready['gpu_name']}), flush=True)
            runs = []
            for index in range(args.runs):
                job = uuid.uuid4().hex
                jobs.append(job)
                start = time.perf_counter()
                send('begin', job=job)
                if args.sentence_stream:
                    from reply_pipeline import SentenceBuffer
                    clauses=[]; buffer=SentenceBuffer(clauses.append)
                    buffer.feed(args.text); buffer.finish()
                else: clauses=[args.text]
                for clause in clauses: send('append', job=job, text=clause)
                send('finish', job=job)
                audio_seconds = 0
                pcm = []; audio_params = None
                first = None
                fragments=[]; playhead=None; gaps=[]
                while True:
                    event = receive(180)
                    if event.get('job') != job: continue
                    if event['event'] == 'fragment':
                        if first is None: first = time.perf_counter()-start
                        with wave.open(event['path'], 'rb') as audio:
                            duration=audio.getnframes()/audio.getframerate()
                            audio_seconds += duration
                            arrival=time.perf_counter()-start
                            fragments.append({'arrival_seconds':arrival,'audio_seconds':duration})
                            if playhead is not None: gaps.append(max(0.0,arrival-playhead))
                            playhead=max(playhead or 0,arrival)+duration
                            if index==0 and args.audio_output:
                                audio_params=audio.getparams(); pcm.append(audio.readframes(audio.getnframes()))
                    if event['event'] == 'done': break
                result = dict(event, run=index+1, wall_seconds=time.perf_counter()-start,
                              first_audio_seconds=first, audio_seconds=audio_seconds,
                              fragment_arrivals=fragments,estimated_inter_segment_wait_seconds=gaps,
                              realtime_factor=event['generation_seconds']/audio_seconds)
                runs.append(result)
                if pcm:
                    args.audio_output.parent.mkdir(parents=True,exist_ok=True)
                    with wave.open(str(args.audio_output),'wb') as audio:
                        audio.setparams(audio_params); audio.writeframes(b''.join(pcm))
                print(json.dumps(result), flush=True)
            cancel_result=None
            if args.check_cancel:
                job=uuid.uuid4().hex; jobs.append(job)
                send('begin',job=job); send('append',job=job,text=TEXT*4)
                time.sleep(.3)
                started_cancel=time.perf_counter(); send('cancel',job=job); send('finish',job=job)
                while True:
                    event=receive(15)
                    if event.get('job')==job and event['event']=='done': break
                cancel_result={'seconds':time.perf_counter()-started_cancel,'cancelled':event['cancelled']}
                if not event['cancelled']: raise RuntimeError('Cancelled job was not acknowledged')
            report = {'passed': True, 'python': str(python), 'tts_engine':args.engine,
                      'cancellation':cancel_result,
                      'sentence_stream':args.sentence_stream,'requested_device': args.device,
                      'prepare': ready, 'ready_seconds': prepared, 'test_text': args.text, 'runs': runs,
                      'cloud_requests': 0, 'physical_playback': False, 'microphone_started': False}
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        finally:
            if process.poll() is None:
                try: send('close'); process.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired): process.kill(); process.wait(timeout=10)
            thread.join(timeout=2)
            for stream in (process.stdin,process.stdout):
                try: stream.close()
                except OSError: pass
            for job in jobs:
                path = ROOT / 'runtime/chat-voice-tmp' / job
                if path.exists():
                    if (path.resolve() != path.absolute() or path.is_symlink()
                            or path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        raise RuntimeError('Unexpected benchmark audio path')
                    shutil.rmtree(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
