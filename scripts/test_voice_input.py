"""Offline safety and lifecycle checks; no microphone or external chat calls."""
from pathlib import Path
import queue
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_input import InputConfig, TurnDetector, InputSession, LiveMicrophone, VoiceCall
from reply_pipeline import ReplyPipeline
from local_voice import SpeechJob
from chat_client import ChatCancelled

PCM = b'\x01\x00' * 320


class DetectorTests(unittest.TestCase):
    def push(self, detector, speech, count):
        events = []
        for index in range(count):
            events.extend(detector.push(PCM, speech, index * .02))
        return events

    def test_silence_and_short_noise_never_start(self):
        detector = TurnDetector()
        self.assertEqual(self.push(detector, False, 1000), [])
        self.assertEqual(self.push(detector, True, 5), [])
        self.assertEqual(self.push(detector, False, 100), [])

    def test_start_preserves_prefix_without_duplicate_current_frame(self):
        detector = TurnDetector()
        self.push(detector, False, 12)
        events = self.push(detector, True, 6)
        self.assertEqual([kind for kind, _ in events], ['start'])
        self.assertEqual(len(events[0][1]), 12)

    def test_short_pause_retains_one_utterance(self):
        detector = TurnDetector()
        self.push(detector, True, 20)
        self.assertNotIn('end', [kind for kind, _ in self.push(detector, False, 30)])
        self.push(detector, True, 20)
        events = self.push(detector, False, 45)
        self.assertEqual(sum(kind == 'end' for kind, _ in events), 1)
        self.assertEqual(self.push(detector, False, 100), [])

    def test_brief_cough_is_discarded(self):
        detector = TurnDetector()
        self.push(detector, True, 6)
        events = self.push(detector, False, 45)
        self.assertEqual(events[-1], ('discard', None))

    def test_long_speech_is_discarded_instead_of_chopped_and_sent(self):
        detector = TurnDetector(InputConfig(maximum_ms=500))
        events = self.push(detector, True, 25)
        self.assertTrue(any(kind == 'discard' for kind, _ in events))
        self.assertFalse(any(kind == 'end' for kind, _ in events))


class Recognizer:
    def __init__(self, final='今天很累'):
        self.final = final
        self.frames = []
    def create(self): return object()
    def feed(self, stream, pcm):
        self.frames.append(pcm)
        return '今天'
    def finish(self, stream): return self.final


class SessionTests(unittest.TestCase):
    def create(self, final='今天很累'):
        self.started, self.partial, self.finals, self.status = [], [], [], []
        self.recognizer = Recognizer(final)
        return InputSession(self.recognizer, lambda: self.started.append(True), self.partial.append,
                            lambda text, metrics: self.finals.append((text, metrics)), self.status.append)
    def feed(self, session, speech, count):
        for _ in range(count): session.process(PCM, speech, time.perf_counter())

    def test_partial_stays_local_and_only_complete_chinese_is_sent(self):
        session = self.create()
        self.feed(session, True, 20)
        self.assertEqual(self.partial, ['今天'])
        self.assertEqual(self.finals, [])
        self.feed(session, False, 45)
        self.feed(session, False, 100)
        self.assertEqual(len(self.finals), 1)
        self.assertEqual(self.finals[0][0], '今天很累')
        self.assertGreaterEqual(self.finals[0][1]['asr_decode_seconds'], 0)
        self.assertNotIn('vad_last_speech_at', self.finals[0][1])

    def test_non_chinese_or_empty_result_never_calls_chat(self):
        for final in ('', 'hello', '...'):
            session = self.create(final)
            self.feed(session, True, 20); self.feed(session, False, 45)
            self.assertEqual(self.finals, [])

    def test_two_turns_automatically_rearm_without_extra_controls(self):
        session = self.create()
        for _ in range(2):
            self.feed(session, True, 20); self.feed(session, False, 45)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(len(self.finals), 2)

    def test_overflow_or_manual_reset_discards_tail_until_full_silence(self):
        session = self.create()
        self.feed(session, True, 20)
        session.reset(wait_for_silence=True)
        self.feed(session, True, 100)
        self.feed(session, False, 30)
        self.feed(session, True, 20)
        self.assertEqual(self.finals, [])
        self.feed(session, False, 45)
        self.feed(session, True, 20); self.feed(session, False, 45)
        self.assertEqual(len(self.finals), 1)


class FakeMicrophone:
    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.state, self.error = 'off', None
        self.reset_count = 0
    def start(self, device): self.state = 'listening'
    def stop(self): self.state = 'off'
    def reset(self): self.reset_count += 1


class CallTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = Mock(responding=False)
        self.voice = Mock(playback_active=False)
        self.call = VoiceCall(self.pipeline, self.voice, Mock(), Mock(), FakeMicrophone)
        self.call.start('headphones')
        self.pipeline.reset_mock()

    def test_candidate_onset_does_not_cancel_pending_response(self):
        self.pipeline.responding = True
        self.call.microphone.callbacks['on_start']()
        self.pipeline.cancel.assert_not_called()

    def test_confirmed_speech_cancels_pending_generation_and_playback(self):
        self.pipeline.responding = True
        self.call.microphone.callbacks['on_start']()
        self.call.microphone.callbacks['on_confirmed']()
        self.pipeline.cancel.assert_called_once()

    def test_final_submits_once_with_input_metrics(self):
        callbacks = self.call.microphone.callbacks
        callbacks['on_start']()
        callbacks['on_final']('中文', {'audio_seconds': 1})
        self.pipeline.submit.assert_called_once_with('中文', input_metrics={'audio_seconds': 1})

    def test_closed_or_replaced_call_rejects_late_callbacks(self):
        old = self.call.microphone.callbacks
        self.call.stop()
        old['on_start'](); old['on_confirmed'](); old['on_final']('旧识别', {})
        self.pipeline.submit.assert_not_called()
        self.call.start('headphones')
        old['on_start'](); old['on_confirmed'](); old['on_final']('旧识别', {})
        self.pipeline.submit.assert_not_called()

    def test_manual_stop_or_typed_message_rejects_inflight_final(self):
        callbacks = self.call.microphone.callbacks
        callbacks['on_start']()
        self.call.reset_input()
        self.pipeline.responding = True
        callbacks['on_confirmed']()
        callbacks['on_final']('应丢弃', {})
        self.pipeline.submit.assert_not_called()
        self.pipeline.cancel.assert_not_called()
        self.assertEqual(self.call.microphone.reset_count, 1)

    def test_headphones_allow_input_during_playback(self):
        self.voice.playback_active = True
        self.assertFalse(self.call._blocked())

    def test_speaker_guard_covers_playback_and_acoustic_tail(self):
        self.call.mode = 'speakers'
        with patch('voice_input.time.perf_counter', return_value=1):
            self.voice.playback_active = True
            self.assertTrue(self.call._blocked())
        self.voice.playback_active = False
        with patch('voice_input.time.perf_counter', return_value=1.2):
            self.assertTrue(self.call._blocked())
        with patch('voice_input.time.perf_counter', return_value=1.4):
            self.assertFalse(self.call._blocked())

    def test_natural_eof_closes_microphone_without_cancelling_tail(self):
        self.call.stop(cancel_reply=False)
        self.assertEqual(self.call.microphone.state, 'off')
        self.pipeline.cancel.assert_not_called()


class MicrophoneTests(unittest.TestCase):
    def create(self, **kwargs):
        return LiveMicrophone(Mock(), Mock(), Mock(), Mock(), **kwargs)

    def test_callback_is_bounded_and_reports_overflow_without_blocking(self):
        mic = self.create(config=InputConfig(queue_frames=2))
        timing = Mock(currentTime=1, inputBufferAdcTime=1)
        for _ in range(10): mic._callback(PCM, 320, timing, False)
        self.assertEqual(mic._queue.qsize(), 2)
        self.assertTrue(mic._overrun.is_set())
        mic.stop(); mic._callback(PCM, 320, timing, False)
        self.assertTrue(mic._queue.empty())
        mic.on_final.assert_not_called()

    def test_stop_during_model_load_never_opens_microphone(self):
        entered, release = threading.Event(), threading.Event()
        def factory():
            entered.set(); release.wait(2); return Recognizer()
        stream_factory = Mock()
        mic = self.create(recognizer_factory=factory, stream_factory=stream_factory)
        mic.start()
        self.assertTrue(entered.wait(1))
        stopper = threading.Thread(target=mic.stop); stopper.start()
        self.assertTrue(mic._stop.wait(1))
        release.set(); stopper.join(2)
        self.assertEqual(mic.state, 'off')
        stream_factory.assert_not_called()

    def test_permission_failure_closes_state_and_allows_retry(self):
        stream_factory = Mock(side_effect=PermissionError('permission denied'))
        mic = self.create(recognizer_factory=Recognizer, stream_factory=stream_factory)
        with patch('sounddevice.check_input_settings'):
            for _ in range(2):
                mic.start(); mic._thread.join(2)
                self.assertEqual(mic.state, 'off')
                self.assertIn('PermissionError', mic.error)
        self.assertEqual(stream_factory.call_count, 2)
        mic.on_final.assert_not_called()

    def test_stop_aborts_and_closes_live_device_and_clears_audio(self):
        stream = Mock(active=True)
        mic = self.create(recognizer_factory=Recognizer, stream_factory=Mock(return_value=stream))
        with patch('sounddevice.check_input_settings'):
            mic.start()
            deadline = time.perf_counter() + 2
            while mic.state == 'preparing' and time.perf_counter() < deadline: time.sleep(.005)
            self.assertEqual(mic.state, 'listening')
            mic.stop()
        self.assertEqual(mic.state, 'off')
        stream.abort.assert_called(); stream.close.assert_called_once()
        self.assertTrue(mic._queue.empty())
        mic.on_final.assert_not_called()


class PipelineMetricsTests(unittest.TestCase):
    def test_new_voice_turn_interrupts_old_chat_and_ignores_late_tokens(self):
        entered, release = threading.Event(), threading.Event()
        text_output = []
        class Voice:
            prepare_metrics = {}
            playback_active = False
            def stop(self): pass
            def begin(self): return SpeechJob()
            def append(self, job, text): pass
            def end(self, job): job.input_closed = True; job.finished.set()
        class Chat:
            metrics = {}
            def reply(self, text, on_token, cancel):
                if text == '旧问题':
                    entered.set(); release.wait(2)
                    on_token('旧回复应被丢弃。')
                    if cancel.is_set(): raise ChatCancelled('cancelled')
                on_token('新回复已接续。'); return '新回复已接续。'
            def cancel_transport(self):
                if entered.is_set(): release.set()
            def close(self): pass
        voice = Voice()
        pipeline = ReplyPipeline(Chat(), voice, text_output.append, Mock())
        call = VoiceCall(pipeline, voice, Mock(), Mock(), FakeMicrophone)
        try:
            call.start('headphones')
            old = pipeline.submit('旧问题')
            self.assertTrue(entered.wait(1))
            call.microphone.callbacks['on_start']()
            self.assertFalse(old.cancel.is_set())
            call.microphone.callbacks['on_confirmed']()
            self.assertTrue(old.cancel.is_set())
            call.microphone.callbacks['on_final']('新问题', {'speech_end_to_submit_seconds': .9})
            self.assertTrue(pipeline.wait_finished(2))
            self.assertNotIn('旧回复应被丢弃。', ''.join(text_output))
            self.assertEqual(pipeline.last_reply, '新回复已接续。')
        finally:
            release.set(); call.stop(); pipeline.close()

    def test_responding_includes_audio_drain_and_combines_input_delay(self):
        job = SpeechJob()
        class Voice:
            prepare_metrics = {}
            def stop(self): pass
            def begin(self): return job
            def append(self, job, text): job.metrics['first_playback_seconds'] = .5
            def end(self, job): job.input_closed = True
        class Chat:
            metrics = {}
            def reply(self, text, on_token, cancel):
                on_token('去休息一会儿。'); return '去休息一会儿。'
            def cancel_transport(self): pass
            def close(self): pass
        pipeline = ReplyPipeline(Chat(), Voice(), Mock(), Mock())
        try:
            turn = pipeline.submit('本地测试', {'speech_end_to_submit_seconds': .9})
            self.assertTrue(turn.done.wait(1))
            self.assertTrue(pipeline.responding)
            result = pipeline.timings()
            self.assertAlmostEqual(result['vad_speech_end_to_first_playback_seconds'],
                                   .9 + result['request_to_first_playback_seconds'])
            job.finished.set()
            self.assertFalse(pipeline.responding)
            pipeline.cancel()
            self.assertFalse(turn.cancel.is_set())
        finally:
            pipeline.close()


if __name__ == '__main__':
    unittest.main()
