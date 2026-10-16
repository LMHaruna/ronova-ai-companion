"""Offline checks of startup routing, speech style and provider acknowledgement."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chat_client import ChatError
import interface
from qwen_realtime import MODEL, QwenRealtime, RealtimeConfig, VOICE_INSTRUCTIONS
from voice_backends import BackendRouter, default_backend

URL = 'wss://maas.qianwenaiapi.com/api-ws/v1/realtime?model=' + MODEL


class Socket:
    def __init__(self, emotion=False):
        self.sent = []
        self.events = iter([
            {'type': 'session.updated', 'session': {'enable_speech_emotion': emotion}},
            {'type': 'response.text.delta', 'delta': '休息。'},
            {'type': 'response.done', 'response': {'status': 'completed'}},
        ])
    def send(self, raw): self.sent.append(json.loads(raw))
    def recv(self): return json.dumps(next(self.events))
    def close(self, **kwargs): pass


class CalmTests(unittest.TestCase):
    def client(self, config=None, emotion=False):
        ws = Socket(emotion)
        client = QwenRealtime(config or RealtimeConfig(URL), connector=lambda cfg: ws,
                              instructions='逐字朗读正文。')
        return client, ws

    @patch('qwen_realtime.environment', return_value='offline-test')
    def test_audio_session_retains_role_and_adds_calm_delivery(self, env):
        client, ws = self.client()
        # Supply a small valid PCM chunk so the normal audio completion check runs.
        import base64
        ws.events = iter([{'type': 'session.updated', 'session': {'enable_speech_emotion': False}},
                          {'type': 'response.audio.delta', 'delta': base64.b64encode(b'\x01\x00'*240).decode()},
                          {'type': 'response.audio_transcript.done', 'transcript': '休息。'},
                          {'type': 'response.done', 'response': {'status': 'completed'}}])
        self.assertEqual(client.reply('休息。'), '休息。')
        session = ws.sent[0]['session']
        self.assertIs(session['enable_speech_emotion'], False)
        self.assertEqual(session['instructions'], '逐字朗读正文。\n\n' + VOICE_INSTRUCTIONS)
        self.assertEqual(client.history[0]['content'], '逐字朗读正文。')
        self.assertIs(client.metrics['server_enable_speech_emotion'], False)
        self.assertFalse(any(event['type'].startswith('input_audio_buffer') for event in ws.sent))

    @patch('qwen_realtime.environment', return_value='offline-test')
    def test_text_only_uses_original_instructions(self, env):
        client, ws = self.client()
        client.reply('休息。', audio_enabled=False)
        self.assertEqual(ws.sent[0]['session']['instructions'], '逐字朗读正文。')
        self.assertFalse(client.metrics['voice_instructions_applied'])
        self.assertEqual(ws.sent[0]['session']['voice'],client.config.voice)

    @patch('qwen_realtime.environment', return_value='offline-test')
    def test_missing_voice_error_is_actionable_without_leaking_provider_content(self, env):
        client, ws = self.client()
        ws.events = iter([{'type':'error','error':{'code':'InvalidVoice',
                          'message':'voice does not exist; secret-provider-detail'}}])
        with self.assertRaisesRegex(ChatError,'当前 API Key 无法使用配置的音色') as failure:
            client.reply('休息。')
        self.assertNotIn('secret-provider-detail',str(failure.exception))
        self.assertEqual(len(client.history),1)

    @patch('qwen_realtime.environment', return_value='offline-test')
    def test_server_rejecting_calm_setting_stops_before_request(self, env):
        client, ws = self.client(emotion=True)
        with self.assertRaises(ChatError): client.reply('休息。', audio_enabled=False)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(len(client.history), 1)

    def test_malformed_style_settings_fail_before_connect(self):
        for config in (RealtimeConfig(URL, enable_speech_emotion='false'),
                       RealtimeConfig(URL, enable_speech_emotion=0),
                       RealtimeConfig(URL, voice_instructions=None),
                       RealtimeConfig(URL, voice_instructions='x'*1025)):
            with self.subTest(config=config):
                connector = Mock()
                with patch('qwen_realtime.environment', return_value='offline-test'):
                    with self.assertRaises(ChatError):
                        QwenRealtime(config, connector=connector).reply('休息。')
                connector.assert_not_called()

    @patch('qwen_realtime.environment', return_value='')
    def test_saved_settings_are_used(self, env):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'config').mkdir()
            (root/'config/realtime-voice.json').write_text(json.dumps({
                'url': URL, 'enable_speech_emotion': False, 'voice_instructions': '沉稳。'}), encoding='utf-8')
            with patch('qwen_realtime.ROOT', root): config = RealtimeConfig.load()
            self.assertIs(config.enable_speech_emotion, False)
            self.assertEqual(config.voice_instructions, '沉稳。')


class StartupTests(unittest.TestCase):
    def test_all_three_entry_modes_select_qwen_without_local_prewarm(self):
        for args in ([], ['--call', 'speakers'], ['--call', 'speakers-aec']):
            with self.subTest(args=args): self.start(args, 'qwen')

    def test_explicit_gpt_still_works(self): self.start(['--engine', 'gpt'], 'gpt')

    def start(self, args, selected):
        local_chat, local_voice, remote_chat, remote_voice = Mock(), Mock(), Mock(), Mock()
        router = BackendRouter(local_chat, local_voice, qwen_factory=lambda: (remote_chat, remote_voice))
        with patch.object(sys, 'argv', ['interface.py'] + args), \
                patch('interface.ChatClient', return_value=local_chat), \
                patch('interface.LocalVoice', return_value=local_voice), \
                patch('interface.BackendRouter', return_value=router), \
                patch('interface.ReplyPipeline'), patch('interface.VoiceCall'), \
                patch('builtins.input', return_value='/quit'), patch('builtins.print'):
            interface.main()
        self.assertEqual(router.selected, selected)
        if selected == 'qwen':
            remote_chat.configured.assert_called_once()
            remote_voice.prepare.assert_called_once()
            local_voice.prepare.assert_not_called()
        else:
            local_voice.prepare.assert_called_once()
            remote_chat.configured.assert_not_called()
        local_voice.close.assert_called_once()

    def test_default_and_cli_override_agree(self):
        self.assertEqual(default_backend(), 'qwen')
        self.assertEqual(interface.argument_parser().parse_args([]).engine, 'qwen')
        self.assertEqual(interface.argument_parser().parse_args(['--engine', 'gpt']).engine, 'gpt')


if __name__ == '__main__': unittest.main()
