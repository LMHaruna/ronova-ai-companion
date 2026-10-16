"""Text completion, late PCM, and microphone events cannot truncate a typed reply."""
from pathlib import Path
import base64
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from client_controller import DesktopController
from local_voice import SpeechJob
from qwen_realtime import QwenRealtime, RealtimeConfig, MODEL
from realtime_voice import RealtimeVoice
from voice_input import VoiceCall
from voice_backends import BackendRouter
from client_fixtures import FakeCall
from test_realtime_voice import Socket, Player

CONFIG=RealtimeConfig('wss://offline.example.test/realtime?model='+MODEL)
TEXT='凡人，先休息。明天见。'
HEAD=b'\x10\x00'*2400
TAIL=b'\x25\x00'*1200


class HeldPlayer(Player):
    """Holds the device tail after all text and PCM have arrived."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.at_drain=threading.Event(); self.release=threading.Event(); self.stop_calls=0

    def finish(self):
        self.at_drain.set()
        while not self.release.wait(.005) and not self.cancel.is_set(): pass
        if not self.cancel.is_set(): super().finish()

    def stop(self): self.stop_calls+=1; self.release.set()


class Microphone:
    state='off'; error=None
    def __init__(self,**callbacks): self.callbacks=callbacks
    def start(self,device=None): self.state='listening'
    def stop(self): self.state='off'
    def reset(self): pass


class TextTailTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.key=patch('qwen_realtime.environment',return_value='offline-test-key'); self.key.start()
        self.players=[]
        def player(*args,**kwargs):
            result=HeldPlayer(*args,**kwargs); self.players.append(result); return result
        events=[{'type':'session.updated'}, {'type':'response.created','response':{'id':'r1'}},
                {'type':'response.audio.delta','delta':base64.b64encode(HEAD).decode()},
                {'type':'response.audio_transcript.delta','delta':TEXT},
                {'type':'response.audio_transcript.done','transcript':TEXT},
                {'type':'response.audio.delta','delta':base64.b64encode(TAIL).decode()},
                {'type':'response.audio.done'},
                {'type':'response.done','response':{'status':'completed'}}]
        self.socket=Socket(events)
        self.chat=QwenRealtime(CONFIG,connector=lambda config:self.socket,instructions='offline public fixture')
        self.voice=RealtimeVoice(config=CONFIG,player_factory=player)
        router=BackendRouter(qwen_factory=lambda:(self.chat,self.voice))
        self.controller=DesktopController(self.directory.name,router=router,call_factory=FakeCall)

    def tearDown(self):
        for player in self.players: player.release.set()
        self.controller.close().result(5); self.key.stop(); self.directory.cleanup()

    def submit_and_hold_tail(self):
        turn=self.controller.submit('离线测试，包含短句尾').result(2)
        self.assertTrue(turn.done.wait(2))
        self.wait_player()
        self.assertTrue(self.players[0].at_drain.wait(2))
        self.assertEqual(self.controller.records()[0]['state'],'completed')
        self.assertTrue(self.controller.snapshot()['playing'])
        self.assertTrue(self.controller.pipeline.responding)
        self.assertFalse(self.controller.pipeline.wait_finished(.02))
        self.assertFalse(turn.voice_job.finished.is_set())
        return turn

    def wait_player(self):
        deadline=time.monotonic()+2
        while not self.players and time.monotonic()<deadline: time.sleep(.005)
        self.assertTrue(self.players)

    def test_transcript_done_and_response_done_wait_for_late_audio_and_device_tail(self):
        turn=self.submit_and_hold_tail()
        self.assertEqual(b''.join(self.players[0].data),HEAD+TAIL)
        self.assertEqual(self.players[0].stop_calls,0)
        self.players[0].release.set()
        self.assertTrue(self.controller.pipeline.wait_finished(2))
        self.assertEqual(turn.voice_job.metrics['submitted_frames'],3600)
        self.assertEqual(turn.voice_job.metrics['completed_frames'],3600)
        self.assertTrue(turn.voice_job.metrics['playback_drained'])
        self.assertFalse(self.controller.pipeline.text_reply_active)

    def test_aec_and_headphone_callbacks_do_not_cancel_typed_reply_and_resume_after_tail(self):
        turn=self.submit_and_hold_tail()
        for mode in ('speakers-aec','headphones'):
            with self.subTest(mode=mode):
                call=VoiceCall(self.controller.pipeline,self.voice,Mock(),Mock(),microphone_factory=Microphone)
                call.start(mode,cancel_reply=False)
                try:
                    callbacks=call.microphone.callbacks
                    self.assertTrue(call._blocked())
                    callbacks['on_start'](); callbacks['on_confirmed'](); callbacks['on_final']('误识别的环境声音',{})
                    self.assertFalse(turn.cancel.is_set())
                    self.assertFalse(turn.voice_job.cancelled.is_set())
                    self.assertEqual(len(self.controller.records()),1)
                finally: call.stop(cancel_reply=False)
        call.start('speakers-aec',cancel_reply=False); self.assertTrue(call._blocked())
        self.players[0].release.set(); self.assertTrue(self.controller.pipeline.wait_finished(2))
        self.assertTrue(call._blocked())
        with patch('voice_input.time.perf_counter',return_value=time.perf_counter()+1):
            self.assertFalse(call._blocked())
        call.stop(cancel_reply=False)

    def test_explicit_stop_still_interrupts_typed_playback(self):
        turn=self.submit_and_hold_tail()
        self.controller.stop_playback().result(2)
        self.assertTrue(turn.voice_job.finished.wait(2))
        self.assertTrue(turn.voice_job.cancelled.is_set())
        self.assertEqual(self.players[0].stop_calls,1)

    def test_local_diagnostic_contains_frame_counts_without_chat_or_credentials(self):
        turn=self.submit_and_hold_tail()
        self.players[0].release.set(); self.assertTrue(self.controller.pipeline.wait_finished(2))
        turn.metrics['input']={'speech_end_to_submit_seconds':1.1,'audio_seconds':2.2,'transcript':'private-transcript-fixture'}
        turn.metrics['chat'].update(first_content_seconds=.2,reasoning_characters=0,thinking_disabled=True,
                                    reasoning_content='private-reasoning-fixture',api_key='private-key-fixture')
        turn.voice_job.created=turn.created+.5
        turn.voice_job.metrics['first_playback_seconds']=.25
        self.controller.poll()
        saved=Path(self.directory.name)/'runtime/last-client-playback.json'
        raw=saved.read_text(encoding='utf-8'); report=json.loads(raw)
        self.assertEqual(report['server']['audio_frames'],3600)
        self.assertEqual(report['server']['audio_done_events'],1)
        self.assertEqual(report['device']['source_submitted_frames'],3600)
        self.assertEqual(report['device']['source_completed_frames'],3600)
        self.assertTrue(report['device']['playback_drained'])
        self.assertEqual(report['source'],'text')
        self.assertEqual(report['version'],3)
        self.assertAlmostEqual(report['latency']['submit_to_first_playback_seconds'],.75)
        self.assertAlmostEqual(report['latency']['speech_end_to_first_playback_seconds'],1.85)
        self.assertEqual(report['chat']['first_content_seconds'],.2)
        self.assertTrue(report['chat']['thinking_disabled'])
        for private in ('private-transcript-fixture','private-reasoning-fixture','private-key-fixture'):
            self.assertNotIn(private,raw)
        self.assertNotIn(TEXT,raw); self.assertNotIn(turn.text,raw); self.assertNotIn('offline-test-key',raw)

    def test_previous_unprotected_path_reproduces_microphone_cancellation(self):
        turn=self.submit_and_hold_tail()
        self.controller.pipeline.protect_text_replies=False
        call=VoiceCall(self.controller.pipeline,self.voice,Mock(),Mock(),microphone_factory=Microphone)
        call.start('speakers-aec',cancel_reply=False)
        try:
            call.microphone.callbacks['on_start'](); call.microphone.callbacks['on_confirmed']()
            self.assertTrue(turn.cancel.is_set())
            self.assertTrue(turn.voice_job.cancelled.is_set())
        finally: call.stop(cancel_reply=False)

    def test_voice_originated_reply_retains_barge_in(self):
        turn=self.controller.pipeline.submit('自动语音测试',input_metrics={})
        self.assertTrue(turn.done.wait(2)); self.wait_player(); self.assertTrue(self.players[0].at_drain.wait(2))
        call=VoiceCall(self.controller.pipeline,self.voice,Mock(),Mock(),microphone_factory=Microphone)
        call.start('headphones',cancel_reply=False)
        try:
            self.assertFalse(call._blocked())
            call.microphone.callbacks['on_start'](); call.microphone.callbacks['on_confirmed']()
            self.assertTrue(turn.cancel.is_set()); self.assertTrue(turn.voice_job.cancelled.is_set())
        finally: call.stop(cancel_reply=False)


if __name__=='__main__': unittest.main()
