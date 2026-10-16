"""Offline checks for immediate speech, reference reuse and drained PCM tails."""
from pathlib import Path
import ctypes
import io
import tempfile
import threading
import wave
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_voice import LocalVoice, SpeechJob, ROOT
from reply_pipeline import ReplyPipeline, SentenceBuffer
from pcm_player import PCMPlayer
from voice_features import ReferenceSpeakerCache


class ReferenceFeatureTests(unittest.TestCase):
    class Waveform:
        def __init__(self, voice):
            self.voice = voice
            self._version = 0

    def test_unchanged_reference_reuses_embedding_without_new_work(self):
        compute = Mock(side_effect=lambda wav: wav.voice)
        cache = ReferenceSpeakerCache(compute)
        waveform = self.Waveform('reference A')
        self.assertEqual(cache(waveform), 'reference A')
        self.assertEqual(cache(waveform), 'reference A')
        compute.assert_called_once_with(waveform)
        self.assertEqual((cache.hits, cache.misses), (1, 1))

    def test_replacement_or_in_place_edit_never_reuses_old_voice(self):
        cache = ReferenceSpeakerCache(lambda wav: wav.voice)
        waveform = self.Waveform('reference A')
        self.assertEqual(cache(waveform), 'reference A')
        waveform.voice = 'edited reference A'
        waveform._version += 1
        self.assertEqual(cache(waveform), 'edited reference A')
        self.assertEqual(cache(self.Waveform('reference B')), 'reference B')
        self.assertEqual((cache.hits, cache.misses), (0, 3))

    def test_unversioned_reference_is_recomputed_instead_of_staying_stale(self):
        compute = Mock(side_effect=lambda wav: wav.voice)
        cache = ReferenceSpeakerCache(compute)
        waveform = self.Waveform('reference A')
        del waveform._version
        self.assertEqual(cache(waveform), 'reference A')
        waveform.voice = 'edited reference A'
        self.assertEqual(cache(waveform), 'edited reference A')
        self.assertEqual(compute.call_count, 2)


class ContinuityTests(unittest.TestCase):
    def test_local_ready_event_attributes_only_the_remaining_warmup_to_pending_jobs(self):
        voice=LocalVoice(player=lambda *args:None)
        process=Mock(stdout=io.StringIO('{"event":"ready","initialize_seconds":1}\n'))
        voice._process=process; voice._prepare_started=time.perf_counter()-1
        try:
            with patch.object(voice,'prepare'),patch.object(voice,'_send'):
                cold=voice.begin(); self.assertIsNone(cold.metrics['prepare_wait_seconds'])
                voice._read(process)
                self.assertGreaterEqual(cold.metrics['prepare_wait_seconds'],0)
                self.assertTrue(cold.finished.wait(2))
                warm=voice.begin(); self.assertEqual(warm.metrics['prepare_wait_seconds'],0)
                voice._queue.put({'event':'done','job':warm.id}); self.assertTrue(warm.finished.wait(2))
        finally: voice.close()

    def setUp(self):
        self.played = []
        self.voice = LocalVoice(player=lambda path, duration, cancel: self.played.append(path.name))
        self.job = SpeechJob()
        self.voice._jobs[self.job.id] = self.job
        self.directory = ROOT / 'runtime/chat-voice-tmp' / self.job.id
        self.directory.mkdir(parents=True)

    def tearDown(self):
        self.voice.close()

    def fragment(self, index):
        path = self.directory / f'{index:04d}.wav'
        path.write_bytes(b'local synthetic PCM stand-in')
        self.voice._queue.put({'event': 'fragment', 'job': self.job.id, 'index': index,
                               'path': str(path), 'audio_seconds': .1})

    def wait_for(self, predicate):
        deadline = time.perf_counter() + 1
        while not predicate() and time.perf_counter() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_fragment_plays_before_worker_done_and_before_tail_arrives(self):
        self.fragment(0)
        self.wait_for(lambda: len(self.played) == 1)
        self.assertFalse(self.job.finished.is_set())
        self.assertFalse(self.job.input_closed)
        self.fragment(1)
        self.voice._queue.put({'event': 'done', 'job': self.job.id})
        self.assertTrue(self.job.finished.wait(1))
        self.assertEqual(self.played, ['0000.wav', '0001.wav'])
        self.assertLess(self.job.metrics['first_playback_seconds'], self.job.metrics['synthesis_complete_seconds'])
        self.assertFalse(self.directory.exists())

    def test_stop_after_head_discards_late_unplayed_audio(self):
        self.fragment(0)
        self.wait_for(lambda: len(self.played) == 1)
        with patch.object(self.voice, '_send'):
            self.voice.stop()
        self.fragment(1)
        self.voice._queue.put({'event': 'done', 'job': self.job.id, 'cancelled': True})
        self.assertTrue(self.job.finished.wait(1))
        self.assertEqual(self.played, ['0000.wav'])
        self.assertFalse(self.directory.exists())

    def test_stop_during_playback_skips_remaining_sentences(self):
        def play(path, duration, cancel):
            self.played.append(path.name)
            self.job.cancelled.set()
        self.voice._player_override = play
        for index in range(3):
            self.fragment(index)
        self.voice._queue.put({'event': 'done', 'job': self.job.id})
        self.assertTrue(self.job.finished.wait(1))
        self.assertEqual(self.played, ['0000.wav'])
        self.assertFalse(self.directory.exists())

    def test_failed_tail_keeps_played_head_and_waits_for_ack(self):
        self.fragment(0)
        self.wait_for(lambda: len(self.played) == 1)
        with patch.object(self.voice, '_send'):
            self.voice._queue.put({'event': 'error', 'job': self.job.id, 'terminal': False, 'message': 'failed tail'})
            self.wait_for(lambda: self.job.cancelled.is_set())
            self.assertFalse(self.job.finished.is_set())
            self.assertTrue(self.directory.exists())
            self.voice._queue.put({'event': 'done', 'job': self.job.id})
            self.assertTrue(self.job.finished.wait(1))
        self.assertEqual(self.played, ['0000.wav'])
        self.assertFalse(self.directory.exists())

    def test_playback_failure_after_done_still_releases_job(self):
        self.voice._player_override = Mock(side_effect=OSError('device failed'))
        self.fragment(0)
        with patch.object(self.voice, '_send'):
            self.voice._queue.put({'event': 'done', 'job': self.job.id})
            self.assertTrue(self.job.finished.wait(1))
        self.assertIsNotNone(self.job.error)
        self.assertTrue(self.voice._play_thread.is_alive())
        self.assertFalse(self.directory.exists())

    def test_old_worker_eof_does_not_fail_new_job(self):
        old = Mock(stdout=io.StringIO(''))
        self.job.worker_process = object()
        self.voice._read(old)
        self.assertFalse(self.job.finished.is_set())
        self.assertIsNone(self.job.error)

    def test_weak_punctuation_emits_early_and_short_tail_flushes_once(self):
        sent = []
        buffer = SentenceBuffer(sent.append)
        buffer.feed('凡人，你的判断太轻率，明天见')
        self.assertEqual(sent, ['凡人，你的判断太轻率，'])
        buffer.finish()
        buffer.finish()
        self.assertEqual(sent, ['凡人，你的判断太轻率，', '明天见'])

    def test_removed_engine_commands_do_not_load_or_switch_any_model(self):
        import interface
        with patch.object(sys, 'argv', ['interface.py', '--no-voice', '--engine', 'gpt']), \
                patch('builtins.input', side_effect=['/voice index', '/voice gpt', '/voice engine', '/quit']), \
                patch('builtins.print') as output, \
                patch('interface.ChatClient'), patch('interface.LocalVoice') as factory:
            interface.main()
        messages = [str(call.args[0]) for call in output.call_args_list if call.args]
        self.assertEqual(sum('未知命令' in message for message in messages), 3)
        factory.return_value.prepare.assert_not_called()
        factory.return_value.switch_engine.assert_not_called()


class TailCompletionTests(unittest.TestCase):
    def test_short_final_text_reaches_tts_before_finish_and_waits_for_audio(self):
        commands = []
        job = SpeechJob()
        class Voice:
            prepare_metrics = {}
            def stop(self): pass
            def begin(self): commands.append('begin'); return job
            def append(self, target, text): commands.append(('append', text))
            def end(self, target): commands.append('finish'); target.input_closed = True
        class Chat:
            metrics = {}
            def reply(self, text, on_token, cancel):
                on_token('坏？凡人，你的判断太轻率。'); on_token('明天见')
                return '坏？凡人，你的判断太轻率。明天见'
            def cancel_transport(self): pass
            def close(self): pass
        pipeline = ReplyPipeline(Chat(), Voice(), lambda text: None, lambda text: None)
        try:
            turn = pipeline.submit('local only')
            self.assertTrue(turn.done.wait(1))
            self.assertEqual(commands, ['begin', ('append', '坏？凡人，你的判断太轻率。'), ('append', '明天见'), 'finish'])
            self.assertFalse(pipeline.wait_finished(.03))
            job.finished.set()
            self.assertTrue(pipeline.wait_finished(.03))
        finally: pipeline.close()

    def test_natural_input_eof_waits_without_cancelling_playback(self):
        import interface
        pending, release = threading.Event(), threading.Event()
        voice, pipeline = Mock(), Mock()
        pipeline.wait_finished.side_effect = lambda: (pending.set(), release.wait(2))
        errors = []
        def run():
            try: interface.main()
            except Exception as exc: errors.append(exc)
        with patch.object(sys, 'argv', ['interface.py', '--no-voice', '--engine', 'gpt']), \
                patch('builtins.input', side_effect=EOFError), patch('builtins.print'), \
                patch('interface.ChatClient'), patch('interface.LocalVoice', return_value=voice), \
                patch('interface.ReplyPipeline', return_value=pipeline):
            thread = threading.Thread(target=run); thread.start()
            try:
                self.assertTrue(pending.wait(1))
                self.assertTrue(thread.is_alive())
                voice.close.assert_not_called()
                pipeline.cancel.assert_not_called()
            finally: release.set(); thread.join(2)
        self.assertFalse(errors)
        voice.wait_idle.assert_called_once()
        voice.close.assert_called_once()


class DeviceTailTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.api = Mock()
        self.headers, self.received = [], []
        def opened(handle, *args): handle._obj.value = 1; return 0
        def written(handle, header, size):
            self.headers.append(header._obj)
            self.received.append(ctypes.string_at(header._obj.data, header._obj.length))
            return 0
        self.api.waveOutOpen.side_effect = opened
        self.api.waveOutWrite.side_effect = written
        for name in ('waveOutPrepareHeader', 'waveOutUnprepareHeader', 'waveOutClose'):
            getattr(self.api, name).return_value = 0
        def reset(handle):
            for header in self.headers: header.flags |= 1
            return 0
        self.api.waveOutReset.side_effect = reset
        with patch('pcm_player.C.WinDLL', return_value=self.api):
            self.player = PCMPlayer(self.cancel)
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'tail.wav'
        self.pcm = b'\x01\x00'*3200 + b'\x7f\x00'*400
        with wave.open(str(self.path), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(32000); wav.writeframes(self.pcm)

    def tearDown(self):
        self.cancel.set(); self.player.close(); self.directory.cleanup()

    def test_normal_finish_waits_for_last_header_without_reset(self):
        self.assertTrue(self.player.write(self.path))
        self.assertEqual(self.received, [self.pcm])
        finished = threading.Event()
        thread = threading.Thread(target=lambda: (self.player.finish(), finished.set()))
        thread.start()
        try:
            self.assertFalse(finished.wait(.03))
            self.api.waveOutClose.assert_not_called()
            self.api.waveOutReset.assert_not_called()
            self.headers[-1].flags |= 1
            self.assertTrue(finished.wait(1))
            self.assertEqual(self.player.submitted_frames, 3600)
            self.assertEqual(self.player.completed_frames, 3600)
            self.api.waveOutReset.assert_not_called()
            self.api.waveOutClose.assert_called_once()
        finally: self.cancel.set(); thread.join(1)

    def test_explicit_cancel_still_resets_outstanding_audio(self):
        self.player.write(self.path); self.cancel.set()
        self.player.stop(); self.player.finish()
        self.api.waveOutReset.assert_called_once()
        self.assertEqual(self.player.completed_frames, 0)


if __name__ == '__main__':
    unittest.main()
