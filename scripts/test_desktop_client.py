"""Local persistence and desktop lifecycle regression tests; no API or mic."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from client_controller import DesktopController
from client_store import HistoryStore, Preferences
from pcm_player import PCMPlayer, scale_pcm
from client_fixtures import FakeCall, fake_router


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.path=Path(self.directory.name)/'history.sqlite3'
        self.store=HistoryStore(self.path)
    def tearDown(self): self.store.close(); self.directory.cleanup()
    def test_restart_restores_only_complete_pairs_and_separate_backends(self):
        self.store.begin('one','qwen','今天累了'); self.store.update('one','休息吧','completed')
        self.store.begin('two','gpt','晚安'); self.store.update('two','晚安','completed')
        self.store.begin('three','qwen','失败'); self.store.update('three','部分','failed')
        self.store.begin('four','qwen','未结束'); self.store.update('four','部分')
        self.store.close(); self.store=HistoryStore(self.path)
        self.assertEqual(self.store.recent()[-1]['state'],'interrupted')
        self.assertEqual([m['content'] for m in self.store.context('qwen')],['今天累了','休息吧'])
        self.assertEqual([m['content'] for m in self.store.context('gpt')],['晚安','晚安'])
    def test_retry_reuses_record_and_rejects_late_old_generation(self):
        self.store.begin('one','qwen','你好'); self.store.update('one','部分','failed')
        self.store.begin('two','qwen','你好',retry_id='one')
        self.store.update('one','旧消息','completed'); self.store.update('two','你好','completed')
        rows=self.store.recent(); self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['assistant'],'你好'); self.assertEqual(rows[0]['attempts'],2)

    def test_same_timestamp_preserves_insertion_order_and_latest_limit(self):
        with patch('client_store.time.time',return_value=1000):
            for generation in ('third','first','second','fourth'):
                self.store.begin(generation,'qwen',generation)
                self.store.update(generation,generation+' reply','completed')
        self.assertEqual([row['id'] for row in self.store.recent()],['third','first','second','fourth'])
        self.assertEqual([row['id'] for row in self.store.recent(2)],['second','fourth'])
        self.assertEqual([m['content'] for m in self.store.context('qwen',2)],
                         ['second','second reply','fourth','fourth reply'])
    def test_clear_rejects_late_completion_and_removes_context(self):
        self.store.begin('one','qwen','你好'); self.store.clear(); self.store.update('one','旧消息','completed')
        self.assertEqual(self.store.recent(),[]); self.assertEqual(self.store.context('qwen'),[])
    def test_context_and_visible_history_are_bounded(self):
        for index in range(30):
            self.store.begin(str(index),'qwen',str(index)); self.store.update(str(index),'回复','completed')
        self.assertEqual(len(self.store.context('qwen')),40)
        self.assertEqual(len(self.store.recent(10)),10)


class PreferenceTests(unittest.TestCase):
    def test_retired_desktop_speaker_mode_migrates_and_keeps_headphones(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text(json.dumps({'call_mode':'speakers','volume':.5}),encoding='utf-8')
            restored=Preferences.load(path)
            self.assertEqual(restored.call_mode,'speakers-aec')
            self.assertEqual(restored.volume,.5)
            self.assertEqual(Preferences().call_mode,'speakers-aec')
            self.assertEqual(Preferences(call_mode='headphones').validate().call_mode,'headphones')

    def test_preferences_roundtrip_excludes_unknown_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text(json.dumps({'engine':'qwen','volume':.5,'DASHSCOPE_API_KEY':'test-secret'}),encoding='utf-8')
            Preferences.load(path).save(path)
            self.assertNotIn('test-secret',path.read_text())
            self.assertEqual(Preferences.load(path).volume,.5)
    def test_invalid_preferences_fail_without_saving(self):
        for preferences in (Preferences(engine='unknown'),Preferences(volume=float('nan')),
            Preferences(volume=-1),Preferences(microphone=True),Preferences(autoplay='false')):
            with self.assertRaises(ValueError): preferences.validate()
    def test_volume_scaling_keeps_duration_and_exact_unity_pcm(self):
        import struct
        pcm=struct.pack('<hhhh',32767,-32768,100,-100)
        self.assertIs(scale_pcm(pcm,1),pcm)
        self.assertEqual(struct.unpack('<hhhh',scale_pcm(pcm,.5)),(16384,-16384,50,-50))
        self.assertEqual(scale_pcm(pcm,0),b'\x00'*len(pcm))

    def test_playback_and_echo_reference_use_same_scaled_pcm_and_device(self):
        import ctypes
        import struct
        api,reference=Mock(),Mock()
        received=[]
        api.waveOutOpen.side_effect=lambda handle,device,*rest:setattr(handle._obj,'value',1) or 0
        api.waveOutPrepareHeader.return_value=0
        api.waveOutGetPosition.side_effect=lambda handle,position,size:setattr(position._obj.data,'value',0) or 0
        def written(handle,header,size):
            received.append(ctypes.string_at(header._obj.data,header._obj.length))
            header._obj.flags|=1
            return 0
        api.waveOutWrite.side_effect=written
        api.waveOutUnprepareHeader.return_value=api.waveOutClose.return_value=0
        pcm=struct.pack('<hh',1000,-1000)
        with patch('pcm_player.C.WinDLL',return_value=api):
            player=PCMPlayer(threading.Event(),reference=reference,volume=.5,device=2)
        player.write_pcm(pcm,24000); player.finish()
        self.assertEqual(received,[struct.pack('<hh',500,-500)])
        reference.append.assert_called_once_with(received[0],0)
        self.assertEqual(api.waveOutOpen.call_args.args[1],2)
        self.assertEqual(player.submitted_frames,player.completed_frames)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
    def tearDown(self):
        self.controller.close().result(5); self.directory.cleanup()
    def wait(self):
        self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.controller.poll()
    def test_opening_client_does_not_start_capture_or_request(self):
        self.assertEqual(self.router.selected,'qwen'); self.assertFalse(self.controller.call.active)
        self.assertEqual(self.chat.calls,0)
    def test_complete_text_turn_is_persisted_and_restored_after_restart(self):
        self.controller.submit('今天累了').result(2); self.wait()
        row=self.controller.records()[0]; self.assertEqual(row['state'],'completed')
        self.controller.close().result(5)
        router,chat,voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=router,call_factory=FakeCall)
        self.assertEqual(chat.history[-2]['content'],'今天累了')
        self.assertEqual(chat.history[-1]['content'],'凡人，先坐下休息。')
        self.assertEqual(chat.calls,0)
    def test_failed_retry_does_not_duplicate_user_record(self):
        self.chat.fail=True; self.controller.submit('今天累了').result(2); self.wait()
        self.assertEqual(self.controller.records()[0]['state'],'failed')
        self.chat.fail=False; self.controller.retry_failed().result(2); self.wait()
        self.assertEqual(len(self.controller.records()),1)
        self.assertEqual(self.controller.records()[0]['state'],'completed')
    def test_cancelled_partial_is_visible_but_excluded_from_context(self):
        self.chat.release=threading.Event()
        self.controller.submit('今天累了').result(2); self.assertTrue(self.chat.entered.wait(1))
        self.controller.cancel().result(2); self.wait()
        row=self.controller.records()[0]
        self.assertEqual(row['state'],'cancelled'); self.assertTrue(row['assistant'])
        self.assertEqual(self.controller.store.context('qwen'),[])
    def test_mute_and_resume_keep_response_and_stop_capture(self):
        self.controller.start_call().result(2)
        previous=self.voice.stopped
        self.controller.mute().result(2)
        self.assertFalse(self.controller.call.active); self.assertTrue(self.controller.muted)
        self.assertEqual(self.voice.stopped,previous)
        self.controller.mute().result(2)
        self.assertTrue(self.controller.call.active); self.assertIs(self.controller.call.starts[-1],False)
    def test_voice_final_automatically_submits_without_manual_send(self):
        from voice_input import VoiceCall
        class Microphone:
            state='off'; error=None
            def __init__(self,**callbacks): self.callbacks=callbacks
            def start(self,device=None): self.state='listening'
            def stop(self): self.state='off'
            def reset(self): pass
        self.controller.call=VoiceCall(self.controller.pipeline,self.controller.router.voice,
            output=lambda text:None,status=self.controller.status,microphone_factory=Microphone,
            on_transcript=self.controller.transcript)
        self.controller.start_call().result(2)
        callbacks=self.controller.call.microphone.callbacks
        callbacks['on_start'](); callbacks['on_partial']('今天累了')
        self.assertEqual(self.controller.partial,'今天累了')
        callbacks['on_final']('今天累了',{'speech_end_to_submit_seconds':1.1})
        self.wait(); self.assertEqual(self.controller.records()[0]['source'],'voice')
        self.controller.end_call().result(2)
        callbacks['on_start'](); callbacks['on_final']('过期声音',{})
        self.assertEqual(self.chat.calls,1)
    def test_volume_only_change_preserves_running_call(self):
        self.controller.start_call().result(2); count=len(self.controller.call.stops)
        preferences=Preferences(volume=.4)
        self.controller.apply_preferences(preferences).result(2)
        self.assertEqual(len(self.controller.call.stops),count)
        self.assertTrue(self.controller.call.active)
    def test_ui_snapshot_never_deadlocks_stream_observer(self):
        self.chat.release=threading.Event(); self.controller.submit('你好').result(2)
        self.assertTrue(self.chat.entered.wait(1))
        snapshots=[]
        thread=threading.Thread(target=lambda:[snapshots.append(self.controller.snapshot()) for _ in range(50)])
        thread.start(); self.chat.release.set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.wait()
        self.assertEqual(len(snapshots),50)
    def test_clear_removes_database_and_all_backend_contexts(self):
        self.controller.submit('你好').result(2); self.wait()
        self.controller.clear_history().result(2)
        self.assertEqual(self.controller.records(),[]); self.assertEqual(len(self.chat.history),1)

    def test_backend_switch_restores_each_completed_context_separately(self):
        self.controller.submit('千问第一轮').result(2); self.wait()
        self.controller.apply_preferences(Preferences(engine='gpt')).result(2)
        self.controller.submit('GPT 第一轮').result(2); self.wait()
        self.controller.apply_preferences(Preferences(engine='qwen')).result(2)
        self.assertEqual([row['backend'] for row in self.controller.records()],['qwen','gpt'])
        self.assertEqual(self.chat.history[-2]['content'],'千问第一轮')
        self.assertEqual(self.router.backends['gpt'][0].history[-2]['content'],'GPT 第一轮')

    def test_invalid_new_backend_keeps_running_call(self):
        from chat_client import ChatError
        self.controller.start_call().result(2); count=len(self.controller.call.stops)
        with patch.object(self.chat,'configured',side_effect=ChatError('缺少配置')):
            self.controller.apply_preferences(Preferences(call_mode='headphones')).result(2)
        self.assertTrue(self.controller.call.active)
        self.assertEqual(len(self.controller.call.stops),count)

    def test_default_qwen_does_not_construct_gpt_worker(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('client_controller.LocalVoice',side_effect=AssertionError('GPT must stay lazy')), \
                patch('client_controller.QwenRealtime.configured'):
            other=DesktopController(directory,call_factory=FakeCall)
            try:
                self.assertEqual(other.router.selected,'qwen')
                self.assertNotIn('gpt',other.router.backends)
            finally: other.close().result(5)


class VoiceStartupTests(unittest.TestCase):
    def make_controller(self, engine='deepseek', fail=False, missing_key=False):
        from client_fixtures import FakeChat, FakeVoice
        from voice_backends import BackendRouter
        class PreparingVoice(FakeVoice):
            engine='gpt'
            def __init__(self):
                super().__init__(); self.ready=threading.Event(); self.prepare_calls=0
            @property
            def preparing(self): return self.prepare_calls>0 and not self.ready.is_set() and not fail
            def prepare(self):
                self.prepare_calls+=1
                if fail: raise RuntimeError('offline preparation failure')
                return self.ready
        self.directory=tempfile.TemporaryDirectory()
        self.local_chat=FakeChat(); self.local_chat.outputs_audio=False
        self.local_voice=PreparingVoice()
        if missing_key:
            from chat_client import ChatError
            self.local_chat.config=object()
            self.local_chat.configured=lambda: (_ for _ in ()).throw(ChatError('缺少配置'))
        self.cloud_chat,self.cloud_voice=FakeChat(),FakeVoice()
        self.cloud_voice.prepare=Mock(side_effect=AssertionError('cloud voice must remain lazy'))
        self.router=BackendRouter(qwen_factory=lambda:(self.cloud_chat,self.cloud_voice),
            factories={'deepseek':lambda:(self.local_chat,self.local_voice)})
        Preferences(engine=engine).save(Path(self.directory.name)/'config/client-preferences.json')
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(lambda:self.controller.close().result(5))

    def test_saved_local_model_prepares_without_waiting_requesting_or_capturing(self):
        self.make_controller()
        self.assertEqual(self.local_voice.prepare_calls,1)
        self.assertFalse(self.local_voice.ready.is_set())
        self.assertEqual(self.local_chat.calls,0)
        self.assertFalse(self.controller.call.active)
        self.assertTrue(self.controller.snapshot()['voice_preparing'])
        self.local_voice.ready.set()
        self.assertFalse(self.controller.snapshot()['voice_preparing'])

    def test_switch_to_local_model_prepares_before_first_reply(self):
        self.make_controller('qwen')
        self.cloud_voice.prepare.assert_not_called()
        self.assertNotIn('deepseek',self.router.backends)
        self.controller.apply_preferences(Preferences(engine='deepseek')).result(2)
        self.assertEqual(self.local_voice.prepare_calls,1)
        self.assertEqual(self.local_chat.calls,0)
        self.assertFalse(self.controller.call.active)

    def test_missing_cloud_key_does_not_defer_local_warmup(self):
        self.make_controller(missing_key=True)
        self.assertEqual(self.router.selected,'deepseek')
        self.assertEqual(self.local_voice.prepare_calls,1)
        self.assertEqual(self.local_chat.calls,0)

    def test_failed_startup_preparation_preserves_text_and_exit(self):
        self.make_controller(fail=True)
        self.assertIn('文字聊天可继续',self.controller.message)
        turn=self.controller.submit('离线启动失败测试').result(2)
        self.assertTrue(turn.done.wait(2))
        self.assertIsNone(turn.error)
        self.assertEqual(turn.reply,'凡人，先坐下休息。')
        self.assertIsNone(turn.voice_job)
        self.controller.close().result(5)
        self.assertTrue(self.local_voice.closed)


if __name__=='__main__': unittest.main()
