"""Replay a supplied local WAV through the desktop text/audio path.

Use --play-existing-audio --audio PATH for local playback without network or
microphone access. --live-fixed-turn explicitly requests one cloud turn instead.
"""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import sys
import tempfile
import wave
import datetime
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client_controller import DesktopController
from client_store import Preferences
from client_fixtures import FakeCall
from qwen_realtime import QwenRealtime, RealtimeConfig, MODEL

TEXT='你来了，凡人。今天发生了什么，说给我听听。说完以后，就早点休息吧。'


class RecordedSocket:
    def __init__(self,pcm):
        self.events=iter(self.stream(pcm)); self.sent=[]

    def stream(self,pcm):
        yield {'type':'session.updated'}
        yield {'type':'response.created','response':{'id':'local-recording'}}
        yield {'type':'response.audio_transcript.delta','delta':TEXT}
        yield {'type':'response.audio_transcript.done','transcript':TEXT}
        for start in range(0,len(pcm),4800):
            yield {'type':'response.audio.delta','delta':base64.b64encode(pcm[start:start+4800]).decode()}
        yield {'type':'response.audio.done'}
        yield {'type':'response.done','response':{'status':'completed'}}

    def send(self,value): self.sent.append(json.loads(value))
    def recv(self): return json.dumps(next(self.events))
    def close(self,**kwargs): pass
    def shutdown(self): pass


def read_recording(source):
    """Validate the Qwen PCM format before opening a controller or audio device."""
    with wave.open(str(source),'rb') as wav:
        if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getcomptype())!=(1,2,24000,'NONE'):
            raise ValueError('录音须为单声道、16bit PCM、24000 Hz WAV')
        frames=wav.getnframes()
        if not 0<frames<=24000*120:
            raise ValueError('录音时长须大于 0 且不超过 120 秒')
        pcm=wav.readframes(frames)
        if len(pcm)!=frames*2:
            raise ValueError('WAV 音频数据不完整')
        return pcm


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--play-existing-audio',action='store_true')
    mode.add_argument('--live-fixed-turn',action='store_true',help='显式授权后，单次真实千问文字回复；无重试、无历史或麦克风上传')
    parser.add_argument('--audio',type=Path,help='本地 24kHz 单声道 16bit WAV；相对路径以项目目录为准')
    args=parser.parse_args(argv)
    if args.live_fixed_turn:
        if args.audio is not None: parser.error('--audio 仅用于 --play-existing-audio')
        return live_fixed_turn()
    if args.audio is None: parser.error('本地回放需要 --audio 指定现有录音')
    source=(ROOT/args.audio).resolve()
    try:
        pcm=read_recording(source)
    except (OSError,wave.Error,EOFError,ValueError) as error:
        parser.error(str(error))
    socket=RecordedSocket(pcm)
    client=QwenRealtime(RealtimeConfig('wss://offline.example.test/realtime?model='+MODEL),
                        connector=lambda config:socket,instructions='Local playback fixture')
    controller=None
    with tempfile.TemporaryDirectory() as directory, \
            patch('client_controller.QwenRealtime',return_value=client), \
            patch('qwen_realtime.environment',side_effect=lambda name:'local-fixture-key' if name=='DASHSCOPE_API_KEY' else ''), \
            patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')), \
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('microphone forbidden')):
        prefs=Preferences.load(ROOT/'config/client-preferences.json'); prefs.engine='qwen'
        prefs.autoplay=True; prefs.save(Path(directory)/'config/client-preferences.json')
        try:
            controller=DesktopController(directory,call_factory=FakeCall)
            turn=controller.submit('本地回放：检查多句回复和句尾').result(5)
            assert turn is not None,'text submission failed'
            assert turn.done.wait(30)
            assert not turn.error and turn.voice_job is not None,'audio job failed to start'
            snapshot=controller.snapshot()
            protected_after_text_done=controller.pipeline.text_reply_active
            assert controller.pipeline.wait_finished(30)
            metrics=turn.voice_job.metrics
            assert metrics['source_submitted_frames']==metrics['source_completed_frames']==len(pcm)//2
            assert metrics['submitted_frames']==metrics['completed_frames']==len(pcm)//2+9600
            assert metrics['padding_frames']==9600
            assert metrics['playback_drained'] and not turn.voice_job.cancelled.is_set()
            assert not turn.error and not turn.voice_job.error
            assert metrics['playback_complete_seconds']>turn.metrics['text_total_seconds']
            assert protected_after_text_done and snapshot['playing']
            now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            report={'date':now.date().isoformat(),'source':str(source),
                    'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                    'scope':'existing accepted recording via Qwen protocol, desktop pipeline, real Windows waveOut',
                    'cloud_requests':0,'physical_microphone_started':False,'voice_reenrolled':False,
                    'actual_windows_output_used':True,'output_device':prefs.speaker,'volume':prefs.volume,
                    'source_frames':len(pcm)//2,'source_audio_seconds':len(pcm)/48000,
                    'transcript_done_before_audio':True,'text_done_before_playback_done':True,
                    'text_reply_protected_during_device_tail':protected_after_text_done,
                    'submitted_frames':metrics['submitted_frames'],'completed_frames':metrics['completed_frames'],
                    'source_submitted_frames':metrics['source_submitted_frames'],
                    'source_completed_frames':metrics['source_completed_frames'],
                    'padding_frames':metrics['padding_frames'],'end_padding_seconds':.4,
                    'playback_drained':metrics['playback_drained'],'cancelled':False,
                    'estimated_underruns':metrics['estimated_underruns'],
                    'estimated_longest_gap_seconds':metrics['estimated_longest_gap_seconds'],
                    'first_playback_seconds':metrics['first_playback_seconds'],
                    'text_total_seconds':turn.metrics['text_total_seconds'],
                    'playback_complete_seconds':metrics['playback_complete_seconds'],
                    'human_tail_listening_accepted':False,'physical_aec_verified':False}
            path=ROOT/'runtime/verification-records'/f'text-playback-{now:%Y%m%d-%H%M%S}.json'
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))
        finally:
            if controller: controller.close().result(5)


def live_fixed_turn():
    from realtime_voice import RealtimeVoice
    directory=ROOT/'runtime'/('text-tail-live-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True,exist_ok=False)
    parts=[]
    def capture(job,pcm,rate): parts.append(pcm)
    client=QwenRealtime(); controller=None
    with tempfile.TemporaryDirectory() as temporary, \
            patch('client_controller.QwenRealtime',return_value=client), \
            patch('client_controller.RealtimeVoice',side_effect=lambda **kwargs:RealtimeVoice(on_pcm=capture,**kwargs)), \
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('microphone forbidden')):
        prefs=Preferences.load(ROOT/'config/client-preferences.json'); prefs.engine='qwen'
        prefs.autoplay=True; prefs.save(Path(temporary)/'config/client-preferences.json')
        try:
            controller=DesktopController(temporary,call_factory=FakeCall)
            turn=controller.submit('只用一句话回答：我准备休息十分钟。').result(5)
            assert turn and turn.done.wait(100),'response did not finish'
            assert controller.pipeline.wait_finished(60),'device did not drain'
            controller.poll()
            pcm=b''.join(parts)
            if pcm:
                with wave.open(str(directory/'source.wav'),'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(pcm)
            (directory/'reply.txt').write_text(turn.reply,encoding='utf-8')
            report=json.loads((Path(temporary)/'runtime/last-client-playback.json').read_text(encoding='utf-8'))
            report.update(cloud_requests=1,maximum_requests=1,physical_microphone_started=False,
                          existing_history_sent=False,recorded_source_frames=len(pcm)//2,
                          request_text='只用一句话回答：我准备休息十分钟。',
                          human_tail_listening_accepted=False)
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            assert not turn.error and not turn.voice_job.error and not turn.cancel.is_set()
            assert report['server']['audio_frames']==report['device']['source_submitted_frames']==report['device']['source_completed_frames']==len(pcm)//2
            assert report['device']['playback_drained'] and report['device']['padding_frames']==9600
            print(json.dumps({'report':str(directory/'report.json'),'source':str(directory/'source.wav'),
                              'cloud_requests':1,'all_source_frames_played':True,'padding_seconds':.4},ensure_ascii=False))
        finally:
            if controller: controller.close().result(5)


if __name__=='__main__': main()
