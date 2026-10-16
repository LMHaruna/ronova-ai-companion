"""Offline reference, echo admission, resource and speaker-mode regressions."""
import ctypes
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from echo_control import PlaybackReference, SpeexAEC, SpeakerInput
from pcm_player import PCMPlayer, Position
from voice_input import VoiceCall, InputConfig, InputSession, LiveMicrophone
from chat_client import ChatClient
from reply_pipeline import ReplyPipeline
from local_voice import SpeechJob

ZERO=bytes(640)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.ref=PlaybackReference(seconds=1)
        self.ref.begin(16000)
        self.ref.append(np.full(1600,1234,dtype='<i2').tobytes(),0)

    def test_queued_future_audio_is_not_audible(self):
        self.ref.observe(0,1.)
        self.ref.observe(0,1.1)
        data,valid,_=self.ref.read(1.08)
        self.assertTrue(valid); self.assertEqual(data,ZERO)

    def test_playhead_maps_pcm_and_stationary_gap_to_silence(self):
        self.ref.observe(0,1.)
        self.ref.observe(1600,1.1)
        data,valid,_=self.ref.read(1.08)
        self.assertTrue(valid)
        self.assertTrue((np.frombuffer(data,'<i2')==1234).all())
        self.ref.observe(1600,1.2)
        data,valid,_=self.ref.read(1.18)
        self.assertTrue(valid); self.assertEqual(data,ZERO)

    def test_cancelled_reference_keeps_recent_pcm_but_has_no_future_audio(self):
        self.ref.observe(0,1.)
        self.ref.observe(800,1.05,ended=True)
        past,valid,_=self.ref.read(1.04)
        self.assertTrue(valid); self.assertNotEqual(past,ZERO)
        future,valid,_=self.ref.read(1.1)
        self.assertTrue(valid); self.assertEqual(future,ZERO)

    def test_memory_is_bounded_and_expired_pcm_fails_closed(self):
        self.ref.append(np.full(32000,5678,dtype='<i2').tobytes(),1600)
        self.assertEqual(self.ref.stored_frames,16000)
        self.ref.observe(0,1.)
        self.ref.observe(1600,1.1)
        _,valid,_=self.ref.read(1.08)
        self.assertFalse(valid)

    def test_new_playback_rejects_previous_audio_and_clock_fault(self):
        self.ref.observe(0,1.)
        self.ref.observe(1600,1.1)
        generation=self.ref.generation
        self.ref.begin(32000)
        self.assertEqual(self.ref.generation,generation+1)
        self.assertEqual(self.ref.stored_frames,0)
        self.assertFalse(self.ref.read(1.08)[1])
        self.ref.unavailable('driver failed')
        self.assertFalse(self.ref.read(1.08)[1])

    def test_backwards_device_position_is_rejected(self):
        self.ref.observe(100,1.)
        with self.assertRaises(ValueError): self.ref.observe(50,1.1)

    def test_quantized_running_clock_does_not_invent_a_silent_gap(self):
        self.ref.observe(100,1.,running=True)
        self.ref.observe(100,1.02,running=True)
        self.ref.observe(740,1.04,running=True)
        data,valid,_=self.ref.read(1.03)
        self.assertTrue(valid); self.assertNotEqual(data,ZERO)


class AdmissionTests(unittest.TestCase):
    def make(self,playing=True,valid=True,factory=None):
        self.playing=playing
        self.status=Mock()
        self.ref=Mock(return_value=(ZERO,valid,1))
        self.aec=Mock()
        self.aec.process.side_effect=lambda pcm,ref:pcm
        return SpeakerInput(self.ref,lambda:self.playing,self.status,factory or (lambda:self.aec))

    def test_missing_library_falls_back_to_guard_and_allows_later_speech(self):
        p=self.make(factory=Mock(side_effect=OSError('missing DLL')))
        self.assertEqual(p.state,'fallback')
        with patch('echo_control.time.perf_counter',return_value=1):
            self.assertFalse(p.process(ZERO,1)[1])
        self.playing=False
        with patch('echo_control.time.perf_counter',return_value=1.4):
            self.assertTrue(p.process(ZERO,1.4)[1])
        self.status.assert_called_once()

    def test_bad_reference_never_admits_playback_audio(self):
        p=self.make(valid=False)
        self.assertFalse(p.process(ZERO,1)[1])
        self.aec.process.assert_not_called()

    def test_processor_failure_releases_native_state_and_guards_playback(self):
        p=self.make()
        self.aec.process.side_effect=RuntimeError('failed')
        self.assertFalse(p.process(ZERO,1)[1])
        self.assertEqual(p.state,'fallback')
        self.aec.close.assert_called_once()
        p.close(); self.aec.close.assert_called_once()

    def test_microphone_only_speech_is_preserved(self):
        p=self.make(playing=False)
        voice=np.full(320,3000,dtype='<i2').tobytes()
        self.assertEqual(p.process(voice,1),(voice,True))

    def test_suppressed_echo_is_silence_and_active_user_turn_can_finish(self):
        p=self.make()
        p._generation=1; p._warmup=0
        self.aec.process.return_value=ZERO
        self.aec.process.side_effect=None
        frame=np.full(320,3000,dtype='<i2').tobytes()
        cleaned,admitted=p.process(frame,1)
        self.assertTrue(admitted); self.assertEqual(cleaned,ZERO)
        recognizer=Mock()
        recognizer.feed.return_value='中文'; recognizer.finish.return_value='中文'
        final=Mock()
        session=InputSession(recognizer,Mock(),Mock(),final,Mock())
        for _ in range(20): session.process(frame,True,1)
        for _ in range(45): session.process(cleaned,False,2)
        final.assert_called_once()

    def test_delayed_echo_is_rejected_even_before_filter_converges(self):
        p=self.make()
        p._generation=1; p._warmup=0
        rng=np.random.default_rng(42)
        played=rng.integers(-4000,4000,size=12*320,dtype=np.int16)
        recorded=np.concatenate((np.zeros(640,dtype=np.int16),played[:-640]))
        results=[]
        for index in range(0,len(played),320):
            self.ref.return_value=(played[index:index+320].tobytes(),True,1)
            results.append(p.process(recorded[index:index+320].tobytes(),index/16000)[0])
        self.assertTrue(all(item==ZERO for item in results[5:]))
        self.assertGreater(p.echo_dominant_frames,0)

    def test_reset_rewarms_and_close_is_idempotent(self):
        p=self.make()
        p._warmup=0; p.reset()
        self.assertEqual(p._warmup,25)
        p.close(); p.close()
        self.aec.close.assert_called_once()
        self.assertEqual(p.state,'off')


class NativeTests(unittest.TestCase):
    def test_native_library_checks_frame_lengths_and_releases_state(self):
        aec=SpeexAEC()
        try:
            self.assertEqual(len(aec.process(ZERO,ZERO)),640)
            with self.assertRaises(ValueError): aec.process(b'x',ZERO)
        finally: aec.close(); aec.close()
        self.assertIsNone(aec.echo); self.assertIsNone(aec.preprocess)


class PlaybackPositionTests(unittest.TestCase):
    def test_position_supports_samples_bytes_ms_and_does_not_reset_on_reference_fault(self):
        api=Mock(); ref=Mock()
        with patch('pcm_player.C.WinDLL',return_value=api): player=PCMPlayer(threading.Event(),reference=ref)
        player.handle.value=1; player.rate=32000; player.submitted_frames=32000
        for kind,value,frames in ((2,320,320),(4,640,320),(1,10,320)):
            def get(handle,position,size):
                position._obj.kind=kind; position._obj.data.value=value; return 0
            api.waveOutGetPosition.side_effect=get
            player.poll_reference()
            self.assertEqual(ref.observe.call_args.args[0],frames)
        api.waveOutGetPosition.side_effect=None
        api.waveOutGetPosition.return_value=5
        player.poll_reference()
        ref.unavailable.assert_called_once(); api.waveOutReset.assert_not_called()


class CallModeTests(unittest.TestCase):
    def test_received_audio_cannot_have_a_future_capture_timestamp(self):
        mic=LiveMicrophone(Mock(),Mock(),Mock(),Mock())
        with patch('voice_input.time.perf_counter',return_value=10):
            mic._callback(ZERO,320,Mock(currentTime=1,inputBufferAdcTime=1),False)
            self.assertEqual(mic._queue.get_nowait()[1],10)
            mic._callback(ZERO,320,Mock(currentTime=1,inputBufferAdcTime=.93),False)
            self.assertAlmostEqual(mic._queue.get_nowait()[1],9.95)

    def test_aec_mode_is_explicit_and_uses_longer_onset_without_playback_blocking(self):
        mic=Mock(state='listening',error=None)
        factory=Mock(return_value=mic)
        voice=Mock(playback_active=True)
        call=VoiceCall(Mock(responding=False),voice,Mock(),Mock(),factory)
        call.start('speakers-aec')
        try:
            self.assertFalse(call._blocked())
            self.assertEqual(factory.call_args.kwargs['config'].onset_ms,240)
            self.assertIn('processor_factory',factory.call_args.kwargs)
        finally: call.stop()


class SustainedSessionTests(unittest.TestCase):
    def test_two_hundred_turns_with_network_failures_recover_and_bound_request_history(self):
        sizes=[]; counter=0
        class Response:
            def __init__(self,code): self.status_code=code
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def close(self): pass
            def iter_lines(self,**options):
                yield 'data: '+json.dumps({'choices':[{'delta':{'content':'这是一段测试回复。'},'finish_reason':None}]})
                yield 'data: [DONE]'
        def post(url,**options):
            nonlocal counter
            counter+=1
            sizes.append(len(options['json']['messages']))
            return Response(503 if counter%17==0 else 200)
        class Voice:
            prepare_metrics={}
            def begin(self): return SpeechJob()
            def append(self,job,text): pass
            def end(self,job): job.input_closed=True; job.finished.set()
            def stop(self): pass
        chat=ChatClient(post=post)
        pipeline=ReplyPipeline(chat,Voice(),Mock(),Mock())
        failures=0
        try:
            with patch.dict(os.environ,{'ALI_API_Key':'offline-test-only'}):
                for index in range(200):
                    turn=pipeline.submit('本地测试')
                    self.assertTrue(pipeline.wait_finished(1))
                    failures+=int(bool(turn.error))
                    self.assertFalse(pipeline.responding)
            self.assertEqual(failures,11)
            self.assertEqual((len(chat.history)-1)//2,189)
            self.assertLessEqual(max(sizes),42)
            self.assertFalse(pipeline._queue.qsize())
            chat.clear(); self.assertEqual(len(chat.history),1)
        finally: pipeline.close()
        self.assertFalse(pipeline._thread.is_alive())


if __name__=='__main__': unittest.main()
