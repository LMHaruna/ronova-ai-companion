"""Enrollment safety, model binding and configuration tests; fully offline."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
import wave
from unittest.mock import Mock, patch
import requests

project = Path(__file__).resolve().parent
if not (project/'qwen_voice_clone.py').is_file(): project = project.parent
sys.path.insert(0, str(project))
from qwen_voice_clone import (API_BASE, TARGET_MODEL, EnrollmentError, VoiceEnrollment,
                              activate, inspect_audio, prepare_reference, valid_voice_id)
from qwen_realtime import RealtimeConfig, QwenRealtime
from test_realtime_voice import Socket, response

VOICE = TARGET_MODEL + '-ronova-offlinetest'


def reply(value, status=200):
    response = Mock(status_code=status)
    response.json.return_value = value
    return response


def wav(path, channels=1, rate=32000, seconds=12, silent=False):
    with wave.open(str(path),'wb') as handle:
        handle.setnchannels(channels); handle.setsampwidth(2); handle.setframerate(rate)
        handle.writeframes((b'\x00\x00' if silent else b'\xd0\x07')*int(rate*seconds)*channels)


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.audio = self.root/'reference.wav'; wav(self.audio)
        self.session = Mock()
        self.client = VoiceEnrollment('offline-test-key',self.session)
    def tearDown(self): self.directory.cleanup()
    def policy(self, **changes):
        data = {'upload_host':'https://dashscope-file-test.oss-cn-beijing.aliyuncs.com',
                'upload_dir':'dashscope-instant/account/date/id', 'x_oss_object_acl':'private',
                'x_oss_forbid_overwrite':'true', 'oss_access_key_id':'temporary-id',
                'signature':'private-signature', 'policy':'private-policy'}
        data.update(changes); return reply({'data':data})
    def test_private_upload_is_model_scoped_and_does_not_forward_bearer(self):
        self.session.request.side_effect = [self.policy(),reply({})]
        url = self.client.upload(self.audio)
        policy, upload = self.session.request.call_args_list
        self.assertEqual(policy.args,('GET', API_BASE+'/uploads'))
        self.assertEqual(policy.kwargs['params']['model'],'voice-enrollment')
        self.assertEqual(upload.kwargs['headers'],{})
        self.assertFalse(upload.kwargs['allow_redirects'])
        self.assertEqual(upload.kwargs['files']['x-oss-object-acl'],(None,'private'))
        self.assertTrue(url.startswith('oss://dashscope-instant/'))
        self.assertNotIn('reference.wav',url)
    def test_public_policy_rejected_before_audio_upload(self):
        self.session.request.return_value = self.policy(x_oss_object_acl='public-read')
        with self.assertRaises(EnrollmentError): self.client.upload(self.audio)
        self.session.request.assert_called_once()
    def test_unknown_or_insecure_upload_host_rejected(self):
        for host in ('https://example.test','http://a.oss-cn-beijing.aliyuncs.com',
                     'https://a.oss-cn-beijing.aliyuncs.com.example.test',
                     'https://a.oss-cn-beijing.aliyuncs.com?secret=1'):
            self.session.request.reset_mock()
            self.session.request.return_value = self.policy(upload_host=host)
            with self.assertRaises(EnrollmentError): self.client.upload(self.audio)
            self.session.request.assert_called_once()
    def test_create_uses_realtime_enrollment_protocol_and_model(self):
        self.session.request.return_value = reply({'output':{'voice_id':VOICE}})
        self.assertEqual(self.client.create('oss://dashscope-instant/account/sample.wav'),VOICE)
        call = self.session.request.call_args
        self.assertEqual(call.kwargs['json'],{'model':'voice-enrollment', 'input':{
            'action':'create_voice','target_model':TARGET_MODEL,'prefix':'ronova',
            'url':'oss://dashscope-instant/account/sample.wav'}})
        self.assertEqual(call.kwargs['headers']['X-DashScope-OssResourceResolve'],'enable')
        self.assertFalse(call.kwargs['allow_redirects'])
    def test_wrong_voice_response_cannot_be_activated(self):
        self.session.request.return_value = reply({'output':{'voice_id':'qwen3-tts-vc-ronova-id'}})
        with self.assertRaises(EnrollmentError): self.client.create('oss://dashscope-instant/a.wav')
    def test_timeout_is_not_retried_and_never_echoes_credentials(self):
        self.session.request.side_effect = requests.Timeout('offline-test-key private-policy')
        with self.assertRaises(EnrollmentError) as caught:
            self.client.create('oss://dashscope-instant/a.wav')
        self.session.request.assert_called_once()
        self.assertNotIn('offline-test-key',str(caught.exception))
    def test_error_body_is_not_echoed_or_retried(self):
        self.session.request.return_value = reply({'message':'offline-test-key', 'code':'InvalidParameter'},400)
        with self.assertRaises(EnrollmentError) as caught: self.client.query(VOICE)
        self.assertIn('InvalidParameter',str(caught.exception))
        self.assertNotIn('offline-test-key',str(caught.exception))
        self.session.request.assert_called_once()
    def test_query_checks_model_binding_and_status(self):
        for data in ({'target_model':'qwen3-tts','status':'OK'},
                     {'target_model':TARGET_MODEL,'status':'unknown'}):
            self.session.request.return_value = reply({'output':data})
            with self.assertRaises(EnrollmentError): self.client.query(VOICE)
        self.session.request.return_value = reply({'output':{'target_model':TARGET_MODEL,'status':'OK','resource_link':'private'}})
        self.assertEqual(self.client.query(VOICE),'OK')
    def test_reference_constraints_rejected_without_network(self):
        for channels,rate,seconds in ((2,32000,12),(1,16000,12),(1,32000,5)):
            wav(self.audio,channels,rate,seconds)
            with self.assertRaises(EnrollmentError): self.client.upload(self.audio)
        self.session.request.assert_not_called()
    def test_source_stays_unchanged_and_prepared_audio_meets_constraints(self):
        source=self.root/'source.wav'; destination=self.root/'prepared.wav'
        wav(source,2,44100,18)
        checksum=hashlib.sha256(source.read_bytes()).hexdigest()
        data=prepare_reference(source,destination)
        self.assertAlmostEqual(data['seconds'],15.3)
        self.assertEqual(data['channels'],1)
        self.assertEqual(data['sample_rate'],32000)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),checksum)
        with self.assertRaises(EnrollmentError): prepare_reference(source,destination)
    def test_activate_preserves_url_and_default_and_refuses_pending_voice(self):
        config=self.root/'config.json'
        initial={'model':TARGET_MODEL,'voice':'longanqian','url':'wss://example.test','default_backend':'gpt','reference_upload_enabled':False}
        config.write_text(json.dumps(initial),encoding='utf-8')
        for status in ('DEPLOYING','UNDEPLOYED'):
            with self.assertRaises(EnrollmentError): activate(config,VOICE,status)
            self.assertEqual(json.loads(config.read_text()),initial)
        activate(config,VOICE,'OK')
        saved=json.loads(config.read_text(encoding='utf-8'))
        self.assertEqual(saved['voice'],VOICE); self.assertEqual(saved['voice_target_model'],TARGET_MODEL)
        self.assertEqual(saved['default_backend'],'gpt'); self.assertEqual(saved['url'],initial['url'])
        self.assertEqual(saved['previous_voice'],'longanqian')
    def test_realtime_rejects_wrong_bound_model_before_connect(self):
        with patch('qwen_realtime.environment',return_value='offline-test-key'):
            for voice,model in ((VOICE,'qwen3-tts'),('qwen3-tts-vc-ronova',''),('qwen-audio-3.0-realtime-plus-ronova','')):
                with self.assertRaises(RuntimeError): RealtimeConfig('wss://example.test',voice,voice_target_model=model).validate()
    def test_realtime_first_session_uses_enrolled_voice_and_sends_no_reference(self):
        config=RealtimeConfig('wss://example.test',VOICE,voice_target_model=TARGET_MODEL)
        socket=Socket(response())
        client=QwenRealtime(config,connector=lambda config:socket,instructions='public')
        with patch('qwen_realtime.environment',return_value='offline-test-key'):
            client.reply('public')
        self.assertEqual(socket.sent[0]['session']['voice'],VOICE)
        self.assertFalse(any(e['type']=='input_audio_buffer.append' for e in socket.sent))
        self.assertFalse(client.metrics['reference_audio_sent'])


if __name__=='__main__': unittest.main()
