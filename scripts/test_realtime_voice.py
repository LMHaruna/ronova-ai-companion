"""Offline protocol, native playback and switching checks; no live API or mic."""
from pathlib import Path
import base64
import json
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_client import ChatError, ChatCancelled
from local_voice import LocalVoice, SpeechJob
from qwen_realtime import MODEL, QwenRealtime, RealtimeConfig
from realtime_voice import RealtimeVoice
from reply_pipeline import ReplyPipeline
from voice_backends import BackendRouter

CONFIG = RealtimeConfig('wss://maas.qianwenaiapi.com/api-ws/v1/realtime?model=' + MODEL)
PCM = b'\x01\x00' * 2400


class Socket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []
        self.closed = False
    def send(self, value): self.sent.append(json.loads(value))
    def recv(self):
        event = next(self.events, '')
        if callable(event): event = event()
        return event if isinstance(event, str) else json.dumps(event)
    def close(self, **kwargs): self.closed = True
    def shutdown(self): self.closed = True


def response(text='凡人，休息吧。', audio=True):
    events = [{'type': 'session.created'}, {'type': 'session.updated'},
              {'type': 'response.created', 'response': {'id': 'r1'}}]
    if audio:
        events.append({'type': 'response.audio.delta', 'response_id': 'r1',
                       'delta': base64.b64encode(PCM).decode()})
    events += [{'type': 'response.audio_transcript.delta' if audio else 'response.text.delta', 'delta': text},
               {'type': 'response.done', 'response': {'status': 'completed'}}]
    return events


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.env = patch('qwen_realtime.environment', return_value='test-key-only')
        self.env.start()
    def tearDown(self): self.env.stop()
    def client(self, events):
        self.socket = Socket(events)
        return QwenRealtime(CONFIG, connector=lambda config: self.socket, instructions='public test instructions')
    def test_text_input_native_audio_and_complete_history(self):
        client = self.client(response())
        sounds, words = [], []
        self.assertEqual(client.reply('休息', words.append, on_audio=lambda p, r: sounds.append((p, r))), '凡人，休息吧。')
        self.assertEqual(sounds, [(PCM, 24000)])
        self.assertEqual(words, ['凡人，休息吧。'])
        self.assertEqual(len(client.history), 3)
        self.assertTrue(self.socket.closed)
        self.assertEqual(self.socket.sent[0]['session']['turn_detection'], None)
        self.assertEqual(self.socket.sent[-1]['type'], 'response.create')
        self.assertFalse(any(e['type'] == 'input_audio_buffer.append' for e in self.socket.sent))
        self.assertNotIn('test-key-only', json.dumps(client.metrics))
    def test_done_output_can_supply_missing_transcript(self):
        events = response()[:4] + [{'type': 'response.done', 'response': {'status': 'completed',
            'output': [{'content': [{'type': 'audio', 'transcript': '休息。'}]}]}}]
        client = self.client(events)
        words = []
        self.assertEqual(client.reply('休息', words.append), '休息。')
        self.assertEqual(words, ['休息。'])
    def test_text_only_does_not_request_or_play_audio(self):
        client = self.client(response(audio=False)); audio = Mock()
        client.reply('休息', on_audio=audio, audio_enabled=False)
        audio.assert_not_called()
        self.assertEqual(self.socket.sent[0]['session']['modalities'], ['text'])
    def test_malformed_events_never_commit_history_or_show_secret(self):
        broken = ['broken', '[]', {'type': 'error', 'error': {'message': 'test-key-only'}},
                  {'type': 'response.audio.delta', 'delta': '*invalid*'},
                  {'type': 'response.audio.delta', 'delta': base64.b64encode(b'x').decode()},
                  {'type': 'response.audio_transcript.done', 'transcript': []},
                  {'type': 'response.done', 'response': {'status': 'failed'}}]
        for event in broken:
            with self.subTest(event=event):
                client = self.client([{'type': 'session.updated'}, event])
                with self.assertRaises(ChatError) as error: client.reply('休息')
                self.assertEqual(len(client.history), 1)
                self.assertTrue(self.socket.closed)
                self.assertNotIn('test-key-only', str(error.exception))
    def test_truncated_audio_and_missing_audio_fail(self):
        for events in (response()[:-1], response(audio=False)):
            client = self.client(events)
            with self.assertRaises(ChatError): client.reply('休息')
            self.assertEqual(len(client.history), 1)
    def test_cancel_during_stream_rejects_later_audio_and_history(self):
        cancel = threading.Event(); events = response()
        def cancelled_event():
            cancel.set()
            return events[3]
        client = self.client(events[:3] + [cancelled_event] + events[4:]); audio = Mock()
        with self.assertRaises(ChatCancelled): client.reply('休息', cancel=cancel, on_audio=audio)
        audio.assert_not_called()
        self.assertEqual(len(client.history), 1)
        self.assertTrue(self.socket.closed)
        client.connector = lambda config: Socket(response())
        self.assertEqual(client.reply('重试'), '凡人，休息吧。')
    def test_clear_during_reply_cannot_restore_old_history(self):
        client = self.client(response())
        with self.assertRaises(ChatCancelled): client.reply('休息', on_token=lambda text: client.clear())
        self.assertEqual(len(client.history), 1)
    def test_foreign_response_audio_is_ignored(self):
        events = response()
        events.insert(3, {'type': 'response.audio.delta', 'response_id': 'old', 'delta': '*invalid*'})
        client = self.client(events); audio = Mock()
        client.reply('休息', on_audio=audio)
        audio.assert_called_once_with(PCM, 24000)
    def test_ram_and_transmitted_history_are_bounded(self):
        client = QwenRealtime(CONFIG, connector=lambda config: Socket(response()), instructions='public')
        client.history_turn_limit = 2
        for index in range(5): client.reply(str(index))
        self.assertEqual(len(client.history), 5)
        self.assertEqual([m['content'] for m in client.history if m['role'] == 'user'], ['3', '4'])
    def test_invalid_endpoint_and_key_source_fail_before_network(self):
        for url in ('http://example.test', 'wss://user:password@example.test',
                    'wss://example.test?key=secret', 'wss://example.test?model=qwen3-tts',
                    'wss://example.test?model=' + MODEL + '&model=' + MODEL,
                    'wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime'):
            with self.assertRaises(ChatError): RealtimeConfig(url).validate()
        with self.assertRaises(ChatError): RealtimeConfig(CONFIG.url, key_environment='ALI_API_Key').validate()
        with patch('qwen_realtime.environment', return_value=''):
            with self.assertRaises(ChatError): CONFIG.validate()
    def test_excessive_transcript_fails_without_history_commit(self):
        client = self.client([{'type':'session.updated'}, {'type':'response.text.delta','delta':'x'*16001}])
        with self.assertRaises(ChatError): client.reply('public',audio_enabled=False)
        self.assertEqual(len(client.history),1)


class IsolationAndMeasurementTests(unittest.TestCase):
    def test_windows_case_variants_of_cloud_keys_are_not_in_worker_environment(self):
        voice = LocalVoice(player=Mock())
        try:
            with patch('local_voice.subprocess.Popen',side_effect=OSError('offline launch blocked')) as launch, \
                    patch('local_voice.os.environ', {'ALI_API_KEY':'test', 'dashscope_api_key':'test',
                          'QWEN_REALTIME_API_KEY':'test','PATH':'keep'}):
                with self.assertRaises(OSError): voice.prepare()
                child = launch.call_args.kwargs['env']
                self.assertEqual(child['PATH'],'keep')
                self.assertFalse(any('api_key' in name.casefold() for name in child))
        finally: voice.close()
    def test_short_leading_silence_is_counted_in_first_sound(self):
        from voice_audition_tools import leading_silence
        pcm = b'\x00\x00'*1200 + b'\xff\x7f'*2400
        self.assertAlmostEqual(leading_silence(pcm,24000),.05)


class Player:
    def __init__(self, cancel, reference=None):
        self.cancel = cancel
        self.pending = []
        self.underruns = 0; self.longest_gap_seconds = 0
        self.submitted_frames = 0; self.completed_frames = 0
        self.write_events = []; self.data = []; self.drained = False
    def write_pcm(self, pcm, rate):
        if self.cancel.is_set(): return False
        self.data.append(pcm); self.submitted_frames += len(pcm)//2
        return True
    def finish(self): self.completed_frames = self.submitted_frames; self.drained = True
    def stop(self): pass
    def close(self): pass


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.env = patch('qwen_realtime.environment', return_value='test-key-only'); self.env.start()
        self.players = []
        def factory(*args, **kwargs):
            player = Player(*args, **kwargs); self.players.append(player); return player
        self.voice = RealtimeVoice(config=CONFIG, player_factory=factory)
    def tearDown(self): self.voice.close(); self.env.stop()
    def test_pcm_order_complete_tail_and_no_temporary_wav(self):
        job = self.voice.begin()
        pcm = PCM * 3 + b'\x7f\x00' * 200
        self.voice.append_pcm(job, pcm); self.voice.end(job)
        self.assertTrue(job.finished.wait(1))
        self.assertEqual(b''.join(self.players[0].data), pcm)
        self.assertTrue(self.players[0].drained)
        self.assertEqual(job.metrics['submitted_frames'], job.metrics['completed_frames'])
        self.assertTrue(job.metrics['playback_drained'])
        self.assertTrue(job.audio_queue.empty())
        self.assertEqual(job.audio_queue.maxsize, 32)
    def test_cancelled_job_discards_pcm_and_next_job_recovers(self):
        job = self.voice.begin(); self.voice.stop()
        self.voice.append_pcm(job, PCM); self.voice.end(job)
        self.assertTrue(job.finished.wait(1)); self.assertFalse(self.players)
        job2 = self.voice.begin(); self.voice.append_pcm(job2, PCM); self.voice.end(job2)
        self.assertTrue(job2.finished.wait(1)); self.assertEqual(b''.join(self.players[0].data), PCM)
    def test_invalid_pcm_is_rejected_before_playback(self):
        job = self.voice.begin()
        for pcm, rate in ((b'x',24000),(b'',24000),(PCM,16000),('data',24000)):
            with self.assertRaises(ValueError): self.voice.append_pcm(job, pcm, rate)
        self.voice.end(job); self.assertTrue(job.finished.wait(1))


class SwitchTests(unittest.TestCase):
    def test_failed_configuration_leaves_gpt_selected(self):
        qwen = Mock(); qwen.configured.side_effect = ChatError('missing config')
        gpt, voice = Mock(), Mock()
        router = BackendRouter(gpt, voice, qwen_factory=lambda: (qwen, Mock()))
        with self.assertRaises(ChatError): router.select('qwen')
        self.assertEqual(router.selected, 'gpt'); self.assertIs(router.chat.snapshot(), gpt)
    def test_contexts_remain_separate_and_clear_clears_both(self):
        gpt, qwen, local, remote = Mock(), Mock(), Mock(), Mock()
        router = BackendRouter(gpt, local, qwen_factory=lambda: (qwen, remote))
        router.select('qwen'); self.assertIs(router.voice.snapshot(), remote)
        router.select('gpt'); self.assertIs(router.voice.snapshot(), local)
        router.chat.clear(); gpt.clear.assert_called_once(); qwen.clear.assert_called_once()
    def test_direct_audio_pipeline_does_not_run_sentence_tts(self):
        job = SpeechJob(); job.finished.set()
        voice = Mock(); voice.begin.return_value = job
        class Chat:
            outputs_audio = True
            metrics = {}
            def reply(self, text, on_token, cancel, on_audio, audio_enabled):
                on_audio(PCM,24000); on_token('凡人，休息吧。'); return '凡人，休息吧。'
            def cancel_transport(self): pass
            def close(self): pass
        pipeline = ReplyPipeline(Chat(), voice, Mock(), Mock())
        try:
            turn = pipeline.submit('休息'); self.assertTrue(turn.done.wait(1))
            voice.append.assert_not_called(); voice.append_pcm.assert_called_once_with(job,PCM,24000)
            voice.end.assert_called_once_with(job)
        finally: pipeline.close()
    def test_inflight_turn_ends_on_captured_voice_after_router_switch(self):
        started, release = threading.Event(), threading.Event()
        job = SpeechJob(); local, remote = Mock(), Mock(); local.begin.return_value = job
        class Chat:
            metrics = {}
            def reply(self, text, on_token, cancel):
                on_token('凡人，休息吧。'); started.set(); release.wait(1); return '凡人，休息吧。'
            def cancel_transport(self): pass
            def close(self): pass
        router = BackendRouter(Chat(), local, qwen_factory=lambda: (Mock(), remote))
        pipeline = ReplyPipeline(router.chat, router.voice, Mock(), Mock())
        try:
            turn = pipeline.submit('休息'); self.assertTrue(started.wait(1))
            router.select('qwen'); release.set(); self.assertTrue(turn.done.wait(1))
            local.end.assert_called_once_with(job); remote.end.assert_not_called()
        finally: release.set(); pipeline.close()


if __name__ == '__main__': unittest.main()
