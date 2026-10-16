"""Desktop actions run outside the GUI thread; voice capture stays local."""
from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
import json
import math
import uuid
from client_oath import DURATION, validate_ceremony
from chat_client import ChatClient, ChatError
from client_store import HistoryStore, Preferences
from client_memory import MemoryStore, validate_memory, validate_close_name
from client_lore import LoreStore
from client_companion import CompanionWorker, SemanticAnalyzer
from client_calls import IncomingCalls
from client_ringtone import IncomingRingtone
from client_models import ClientModels, ModelProfile
from local_voice import LocalVoice, validate_resources as validate_local_voice
from pcm_player import PCMPlayer
from qwen_realtime import QwenRealtime
from realtime_voice import RealtimeVoice
from reply_pipeline import ReplyPipeline
from voice_backends import BackendRouter
from voice_input import VoiceCall

ROOT = Path(__file__).resolve().parent


class DesktopController:
    def __init__(self, root=ROOT, router=None, call_factory=VoiceCall, analysis_factory=None, call_planner_factory=None, call_clock=None, ringtone_factory=None):
        self.root = Path(root)
        self.preferences_path = self.root/'config/client-preferences.json'
        self.preferences = Preferences.load(self.preferences_path)
        # The GPT option is withdrawn from the desktop picker. Retain its
        # saved credentials/history, but restore a visible model on startup.
        if self.preferences.engine=='openai': self.preferences=replace(self.preferences,engine='qwen')
        self.models = ClientModels(self.root)
        self.store = HistoryStore(self.root/'data/chat-history.sqlite3')
        self.memory = MemoryStore(self.root/'data/companion-memory.sqlite3')
        self.lore = LoreStore(self.root)
        self.lock = threading.RLock()
        self.events = deque(maxlen=512)
        self.partial = ''
        self.message = '等待通话'
        self.call_requested = self.muted = self.closed = self.closing = False
        self.operation_busy = False
        self.pending_actions = 0
        self.oath_session = None
        self.current_event = None
        self.playback_record_id = None
        self.retry_target = None
        self.last_checkpoint = 0
        self.last_audio_diagnostic = None
        self.turn_tickets = {}
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='desktop-actions')
        def player(*args, **kwargs):
            return PCMPlayer(*args, **kwargs,volume=lambda:self.preferences.volume,
                             device=self.preferences.speaker,end_padding_seconds=.4,track_levels=True)
        self.local_voices = {}
        self.owns_router = router is None
        def create_voice(tts):
            voice = LocalVoice(status=self.status,tts_engine=tts)
            voice.pcm_factory = player
            return voice
        self.create_local_voice = create_voice
        def local(engine='gpt'):
            tts = getattr(self.preferences,engine+'_tts_engine','gpt-sovitsv2proplus')
            if tts not in self.local_voices:
                self.local_voices[tts] = create_voice(tts)
            chat = ChatClient(config=self.models.config(engine))
            chat.context_provider = self.context_provider(chat)
            return chat, self.local_voices[tts]
        def realtime():
            config = self.models.config('qwen')
            chat = QwenRealtime(config=config)
            chat.context_provider = self.context_provider(chat)
            return chat, RealtimeVoice(status=self.status,player_factory=player,config=config)
        self.router = router or BackendRouter(status=self.status,gpt_factory=local,
             qwen_factory=realtime,factories={name:lambda selected=name:local(selected) for name in ('openai','deepseek')})
        self.companion = CompanionWorker(self.memory,
            factory=analysis_factory or getattr(self.router,'analysis_factory',None) or
                (lambda backend,config:SemanticAnalyzer(backend,config,lore=self.lore)),
            changed=lambda result:self.emit({'kind':'memory_changed','saved':result['saved'],'relationship_changed':result['scored']}))
        try: self.router.select(self.preferences.engine)
        except ChatError as exc:
            self.router.selected = self.preferences.engine
            self.status(str(exc))
        self.restore_contexts()
        self.pipeline = ReplyPipeline(self.router.chat,self.router.voice,output=lambda text:None,
                                      status=self.status,voice_enabled=self.preferences.autoplay,
                                      on_event=self.turn_event,protect_text_replies=True)
        self.call = call_factory(self.pipeline,self.router.voice,output=lambda text:None,
                                 status=self.status,on_transcript=self.transcript)
        options = {'clock':call_clock} if call_clock else {}
        self.ringtone = (ringtone_factory or IncomingRingtone)(
            self.root/'design/client-audio/incoming-call.wav', volume=lambda:self.preferences.volume,
            device=lambda:self.preferences.speaker,
            failed=lambda identifier,message:self.emit({'kind':'incoming_ringtone_failed','id':identifier,'message':message}))
        self.incoming_calls = IncomingCalls(self,call_planner_factory,**options)
        if self.models.warning: self.status(self.models.warning)
        elif self.lore.warning: self.status(self.lore.warning)
        self.prepare_local_voice()

    def prepare_local_voice(self):
        """Schedule local-only warmup early; never wait for model readiness."""
        if self.closed or self.closing: return
        self.activate_local_voice()
        backend = self.router.backends.get(self.router.selected)
        if backend is None or getattr(backend[1], 'engine', None) != 'gpt': return
        try:
            backend[1].prepare()
        except Exception as exc:
            self.status('本地语音准备未完成（'+type(exc).__name__+'），文字聊天可继续')

    def activate_local_voice(self):
        if not self.owns_router: return
        engine = self.router.selected
        desired = getattr(self.preferences,engine+'_tts_engine','gpt-sovitsv2proplus') if engine!='qwen' else None
        for name, voice in self.local_voices.items():
            if name!=desired and getattr(voice,'_process',None) is not None:
                voice.release_worker()
        if desired is None or engine not in self.router.backends: return
        if desired not in self.local_voices:
            self.local_voices[desired] = self.create_local_voice(desired)
        chat, _ = self.router.backends[engine]
        self.router.backends[engine] = (chat,self.local_voices[desired])

    def set_tts_engine(self, engine, tts):
        if engine not in ('deepseek','gpt'): raise ValueError('该模型不使用本地语音')
        if tts not in ('gpt-sovitsv2proplus','indextts-2.5'): raise ValueError('语音合成引擎无效')
        def operation():
            validate_local_voice(tts)
            preferences = replace(self.preferences,**{engine+'_tts_engine':tts}).validate()
            if engine==self.router.selected:
                self.incoming_calls.invalidate()
                self.call.stop()
                self.pipeline.cancel(reason='voice_engine_changed')
                if not self.router.voice.wait_idle(3): raise RuntimeError('上一段声音还未停止，请稍后重试')
                self.call_requested = self.muted = False
                self.pipeline.voice_enabled = preferences.autoplay
            preferences.save(self.preferences_path)
            self.preferences = preferences
            if engine==self.router.selected: self.prepare_local_voice()
            self.status('语音引擎已保存，本地模型正在准备' if engine==self.router.selected else '语音引擎已保存')
            return True
        return self.action(operation)

    def context_provider(self, chat):
        def provide(text):
            if self.closed or self.closing: return ''
            calls=getattr(self,'incoming_calls',None)
            call_note=calls.context_note(self.memory.profile()['context_since']) if calls else ''
            return self.lore.context(text,chat.history[1:][-4:])+self.memory.context(text)+call_note
        return provide

    def emit(self, event):
        with self.lock: self.events.append(event)

    def status(self, message):
        with self.lock: self.message = message
        self.emit({'kind':'status','message':message})

    def transcript(self, kind, text):
        with self.lock: self.partial = text if kind == 'partial' else ''
        self.emit({'kind':'transcript','state':kind,'text':text})

    def restore_contexts(self):
        since = self.memory.snapshot()['context_since']
        for name,(chat,_) in self.router.backends.items():
            with chat._lock:
                chat.context_provider = self.context_provider(chat)
                chat.history = [chat.history[0],*self.store.context(name,since=since)]
                chat._revision += 1

    def turn_event(self, event):
        if event.get('source')=='incoming' and hasattr(self,'incoming_calls'):
            self.incoming_calls.opening_event(event)
        if event['kind']=='submitted' and event.get('source')!='incoming' and hasattr(self,'incoming_calls'):
            self.incoming_calls.activity()
            self.incoming_calls.dismiss('busy')
        with self.lock:
            if self.closed: return
            if event['kind'] == 'submitted':
                retry_id, self.retry_target = self.retry_target, None
                self.playback_record_id = self.store.begin(event['id'],event['backend'],event['text'],event['source'],retry_id)
                self.current_event = dict(event)
                profile=self.memory.profile()
                self.turn_tickets[event['id']]={'ticket':{key:profile[key] for key in ('memory_epoch','relationship_epoch')},
                                               'config':self.models.config(event['backend'])}
                self.partial = ''
            elif event['kind'] == 'delta':
                if self.current_event and self.current_event['id'] == event['id']:
                    self.current_event = dict(event)
            elif event['kind'] in ('completed','failed','cancelled'):
                self.store.update(event['id'],event['reply'],event['kind'],event.get('error'))
                if self.current_event and self.current_event['id'] == event['id']:
                    self.current_event = None
                if event['kind']=='completed' and event.get('source')!='incoming':
                    try:
                        if self.memory.capture(event['text']):
                            self.emit({'kind':'memory_changed'})
                            self.status('已保存这条记忆，可在设置中修改或删除')
                    except Exception:
                        self.status('记忆未保存，请在设置中检查或手动添加')
                pending=self.turn_tickets.pop(event['id'],None)
                if event['kind']=='completed' and pending and not self.closing and event.get('source')!='incoming':
                    self.companion.enqueue(event,**pending)
            self.emit(event)

    def poll(self):
        if not self.closed and not self.closing: self.incoming_calls.tick()
        if not self.closed and not self.closing and self.call_requested and not self.muted:
            microphone = self.call.timings()
            turn = self.pipeline.last_turn
            opening_failed = bool(self.incoming_calls.connected_id and turn and turn.source=='incoming' and turn.done.is_set() and turn.error)
            capture_failed = bool(microphone['error'] and microphone['state']=='off')
            if capture_failed or opening_failed:
                self.call_requested = self.muted = False
                def failed_capture():
                    self.call.stop()
                    self.incoming_calls.connection('failed')
                    self.pipeline.voice_enabled=self.preferences.autoplay
                    self.status('麦克风启动失败，通话已结束；请检查通话设备后重试' if capture_failed else
                                '来电开场播放失败，通话已结束；请检查声音和模型设置后重试')
                self.action(failed_capture)
        with self.lock:
            events = list(self.events); self.events.clear()
            if self.current_event and time.monotonic()-self.last_checkpoint > .5:
                self.store.update(self.current_event['id'],self.current_event['reply'])
                self.last_checkpoint = time.monotonic()
        self.save_audio_diagnostic()
        return events

    def save_audio_diagnostic(self):
        turn=self.pipeline.last_turn
        if not turn or not turn.done.is_set() or not turn.voice_job or not turn.voice_job.finished.is_set(): return
        if self.last_audio_diagnostic==turn.id: return
        job=turn.voice_job; voice=job.metrics; chat=turn.metrics.get('chat',{})
        fields=('submitted_frames','completed_frames','padding_frames','continuity_silence_frames','source_submitted_frames',
                'source_completed_frames','playback_drained','audio_seconds','first_playback_seconds',
                'playback_complete_seconds','estimated_underruns','estimated_longest_gap_seconds','continuity_error','cancelled')
        numeric = lambda source, keys: {key:source[key] for key in keys
                     if key in source and type(source[key]) in (int,float) and math.isfinite(source[key])}
        request_to_audio = None
        if type(voice.get('first_playback_seconds')) in (int,float):
            request_to_audio = max(0.0, job.created-turn.created+voice['first_playback_seconds'])
        latency = numeric(turn.metrics, ('chat_queue_seconds','first_text_seconds','first_sentence_seconds','text_total_seconds',
                                         'voice_prepare_requested_seconds'))
        if request_to_audio is not None:
            latency['submit_to_first_playback_seconds'] = request_to_audio
        input_metrics = turn.metrics.get('input', {})
        endpoint = input_metrics.get('speech_end_to_submit_seconds')
        if request_to_audio is not None and type(endpoint) in (int,float) and math.isfinite(endpoint):
            latency['speech_end_to_first_playback_seconds'] = endpoint+request_to_audio
        report={'version':3,'updated':time.time(),'backend':turn.backend,'source':turn.source,
                'turn_cancelled':turn.cancel.is_set(),'turn_failed':bool(turn.error),'playback_failed':bool(job.error),
                'stop_reason':turn.metrics.get('stop_reason',''),
                'server':{key:chat[key] for key in ('audio_frames','audio_events','audio_done_events',
                          'last_audio_seconds','transcript_done_seconds','response_done_seconds','response_status') if key in chat},
                'device':{key:voice[key] for key in fields if key in voice}}
        report['latency'] = latency
        report['input'] = numeric(input_metrics, ('audio_seconds','endpoint_silence_seconds','asr_decode_seconds',
                                                 'speech_end_to_submit_seconds'))
        report['chat'] = numeric(chat, ('transport_wait_seconds','headers_seconds','first_sse_seconds',
                                       'first_reasoning_seconds','last_reasoning_seconds','first_content_seconds',
                                       'last_content_seconds','total_seconds','reasoning_characters','content_events',
                                       'request_characters','omitted_history_turns'))
        if type(chat.get('thinking_disabled')) is bool:
            report['chat']['thinking_disabled'] = chat['thinking_disabled']
        report['synthesis'] = numeric(voice, ('prepare_wait_seconds','first_fragment_seconds','generation_seconds',
                                            'queue_wait_seconds','audio_seconds'))
        if voice.get('device') in ('cpu','cuda'): report['synthesis']['device'] = voice['device']
        if voice.get('tts_engine') in ('gpt-sovitsv2proplus','indextts-2.5'):
            report['synthesis']['tts_engine'] = voice['tts_engine']
        if type(voice.get('is_half')) is bool: report['synthesis']['is_half'] = voice['is_half']
        try:
            directory=self.root/'runtime'; directory.mkdir(exist_ok=True)
            target=directory/'last-client-playback.json'; temporary=target.with_suffix('.tmp')
            temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            temporary.replace(target); self.last_audio_diagnostic=turn.id
        except OSError: self.last_audio_diagnostic=turn.id

    def records(self):
        rows = self.store.recent()
        with self.lock:
            current = self.current_event
            if current:
                for row in rows:
                    if row['generation'] == current['id']: row['assistant'] = current['reply']
        return rows

    def action(self, operation, *, during_oath=False):
        with self.lock:
            if self.closed or self.closing: raise RuntimeError('客户端正在退出')
            if self.oath_session and not during_oath: raise RuntimeError('请先完成或取消誓约仪式')
            self.pending_actions+=1
        def run():
            self.operation_busy = True
            self.emit({'kind':'busy','value':True})
            try:
                with self.lock:
                    if self.oath_session and not during_oath: raise RuntimeError('请先完成或取消誓约仪式')
                return operation()
            except Exception as exc:
                self.retry_target = None
                message = str(exc) if isinstance(exc,(ChatError,ValueError,RuntimeError)) else '操作未完成（'+type(exc).__name__+'）'
                self.status(message)
                self.emit({'kind':'operation_failed','message':message})
            finally:
                with self.lock:
                    self.operation_busy = False
                    self.pending_actions-=1
                self.emit({'kind':'busy','value':False})
        return self.executor.submit(run)

    def submit(self, text, retry_id=None):
        text = text.strip()
        if not text or len(text)>4000: raise ValueError('请输入 1–4000 字的消息')
        def operation():
            self.router.validate(self.router.selected)
            if retry_id is not None:
                self.reply_record(retry_id, ('failed','cancelled','interrupted'), '该回复无法重试')
            self.call.reset_input()
            self.retry_target = retry_id
            return self.pipeline.submit(text)
        return self.action(operation)

    def start_call(self):
        incoming = self.incoming_calls.snapshot()
        if incoming: return self.answer_call(incoming['id'])
        def operation():
            self.incoming_calls.activity()
            self.router.validate(self.router.selected)
            self.pipeline.voice_enabled = True
            self.router.voice.prepare()
            self.call.start(self.preferences.call_mode,self.preferences.microphone)
            self.call_requested, self.muted = True, False
        return self.action(operation)

    def test_incoming_call(self):
        def operation():
            state = self.snapshot()
            # This operation is the only expected action in the queue.
            if self.pending_actions>1 or any(state[k] for k in ('call_requested','active','responding','playing','oath_active','incoming')):
                raise RuntimeError('请先结束当前通话、来电或回复')
            self.incoming_calls.activity()
            return self.incoming_calls.ring({'reason':'测试来电',
                'opening':''},test=True)
        return self.action(operation)

    def answer_call(self, identifier):
        self.ringtone.stop(identifier)
        def operation():
            row = self.incoming_calls.claim(identifier)
            try:
                if row['backend']!=self.router.selected: raise RuntimeError('模型已切换，请重新发起来电')
                self.router.validate(self.router.selected)
                self.pipeline.voice_enabled = True
                self.router.voice.prepare()
                # Capture starts only after explicit answer. Keep the opening
                # in the same pipeline so hang-up and barge-in cancel it too.
                self.call.start(self.preferences.call_mode,self.preferences.microphone)
                self.call_requested,self.muted = True,False
                self.incoming_calls.connection('connected')
                self.pipeline.submit_opening(row['opening'],call_id=identifier)
                self.emit({'kind':'incoming_answered','id':identifier})
                return True
            except Exception:
                self.call.stop()
                self.call_requested=self.muted=False
                self.pipeline.voice_enabled=self.preferences.autoplay
                self.incoming_calls.connection('failed')
                raise
        return self.action(operation)

    def decline_call(self, identifier, *, later=False):
        self.ringtone.stop(identifier)
        def operation():
            self.incoming_calls.activity()
            state='snoozed' if later else 'declined'
            if self.incoming_calls.dismiss(state,identifier):
                self.status('已设为稍后提醒' if later else '来电已拒接')
        return self.action(operation)

    def mute(self):
        def operation():
            if not self.call_requested: return
            if self.muted:
                self.call.start(self.preferences.call_mode,self.preferences.microphone,cancel_reply=False)
                self.muted = False
            else:
                self.call.stop(cancel_reply=False)
                self.muted = True
                self.status('麦克风已静音')
        return self.action(operation)

    def end_call(self):
        self.ringtone.stop()
        def operation():
            self.incoming_calls.dismiss('declined')
            self.incoming_calls.connection('ended')
            self.incoming_calls.activity()
            self.call.stop()
            self.call_requested = self.muted = False
            self.partial = ''
            self.status('通话已结束')
            self.pipeline.voice_enabled=self.preferences.autoplay
        return self.action(operation)

    def cancel(self):
        self.ringtone.stop()
        def operation():
            self.incoming_calls.dismiss('withdrawn')
            self.pipeline.cancel(reason='user_stop')
        return self.action(operation)

    def stop_playback(self, record_id=None):
        def stop():
            if record_id is not None:
                with self.lock:
                    if record_id != self.playback_record_id:
                        return
            turn=self.pipeline.last_turn
            if turn and turn.voice_job and not turn.voice_job.finished.is_set():
                turn.metrics['stop_reason']='user_stop_playback'
            self.router.voice.stop()
            with self.lock: self.playback_record_id = None
        return self.action(stop)

    def reply_record(self, record_id, states, error):
        row = next((row for row in reversed(self.records())
                    if (record_id is None or row['id']==record_id)
                    and row['backend']==self.router.selected and row['state'] in states),None)
        if row is None: raise ValueError(error)
        return row

    def retry_failed(self, record_id=None):
        row = self.reply_record(record_id, ('failed','cancelled','interrupted'), '没有需要重试的回复')
        if row['source']=='incoming': raise ValueError('来电开场已结束，请重新发起来电')
        return self.submit(row['user'], retry_id=row['id'])

    def replay(self, record_id=None):
        row = self.reply_record(record_id, ('completed',), '还没有可重播的回复')
        def operation():
            selected = self.reply_record(row['id'], ('completed',), '该回复已不可重播')
            self.pipeline.cancel()
            with self.lock: self.playback_record_id = selected['id']
            self.router.voice.speak(selected['assistant'])
        return self.action(operation)

    def apply_preferences(self, preferences):
        preferences = replace(preferences).validate()
        def operation():
            # Validate the new backend before changing a running call.
            changed = any(getattr(preferences,name) != getattr(self.preferences,name)
                          for name in ('engine','call_mode','microphone','speaker'))
            if changed: self.router.validate(preferences.engine)
            if changed or any(getattr(preferences,name)!=getattr(self.preferences,name)
                              for name in ('proactive_calls','call_start_hour','call_end_hour','call_daily_limit','call_cooldown_hours','call_idle_minutes')):
                self.incoming_calls.invalidate()
            self.incoming_calls.activity()
            if not changed:
                preferences.save(self.preferences_path)
                self.preferences = preferences
                self.pipeline.voice_enabled = preferences.autoplay or self.call_requested
                if not self.pipeline.voice_enabled: self.router.voice.stop()
                self.status('设置已保存')
                return
            resume = self.call_requested and not self.muted
            self.call.stop()
            if not self.router.voice.wait_idle(3): raise RuntimeError('上一段声音还未停止，请稍后重试')
            self.router.select(preferences.engine)
            self.restore_contexts()
            preferences.save(self.preferences_path)
            self.preferences = preferences
            self.pipeline.voice_enabled = preferences.autoplay or self.call_requested
            self.pipeline.last_reply = None
            self.prepare_local_voice()
            if resume:
                self.router.voice.prepare()
                self.call.start(preferences.call_mode,preferences.microphone)
            self.status('设置已保存')
        return self.action(operation)

    def configure_model(self, engine, model, url, api_key):
        profile = ModelProfile(model.strip(),url.strip(),api_key.strip()).validate()
        def operation():
            # Validate and encrypt before touching a running conversation.
            config = self.models.config(engine,profile)
            prepared = self.models.prepare(engine,profile)
            prior = self.models.snapshot()[engine]
            changed = prior!=profile or self.router.selected!=engine
            if changed:
                self.incoming_calls.invalidate()
                self.incoming_calls.connection('ended')
                self.companion.cancel_pending()
                self.call.stop()
                self.pipeline.cancel()
                if not self.router.voice.wait_idle(3): raise RuntimeError('上一段声音还未停止，请稍后重试')
            self.models.commit(prepared)
            if engine in self.router.backends:
                chat,voice = self.router.backends[engine]
                chat.config = config
                if engine=='qwen': voice.config = config
            self.router.select(engine)
            if changed: self.restore_contexts()
            preferences = replace(self.preferences,engine=engine)
            preferences.save(self.preferences_path)
            self.preferences = preferences
            if changed:
                self.call_requested = self.muted = False
                self.pipeline.last_reply = None
            self.pipeline.voice_enabled = preferences.autoplay or self.call_requested
            self.prepare_local_voice()
            self.status('模型配置已保存，后续对话使用此 API Key')
            return True
        return self.action(operation)

    def change_memory(self, operation):
        def change():
            self.incoming_calls.invalidate()
            self.incoming_calls.connection('ended')
            self.companion.cancel_pending()
            self.call.stop()
            self.pipeline.cancel(reason='memory_change')
            self.call_requested = self.muted = False
            self.partial = ''
            result = operation()
            # Old visible chat is retained but cannot resurrect an edited or
            # deleted fact, even after restart or a switch of backend.
            self.router.chat.clear()
            self.pipeline.last_reply = None
            self.emit({'kind':'memory_changed'})
            self.status('记忆设置已保存')
            return result if result is not None else True
        return self.action(change)

    def save_memory(self, content, category='fact', memory_id=None, *, expected_updated=None):
        content = validate_memory(content,category)
        return self.change_memory(lambda:self.memory.save(content,category,memory_id,expected_updated=expected_updated))

    def restore_memory(self, identifier, version_id, *, expected_updated=None):
        return self.change_memory(lambda:self.memory.restore_version(identifier,version_id,expected_updated))

    def delete_memory(self, identifier):
        return self.change_memory(lambda:self.memory.delete(identifier))

    def configure_memory(self, *, enabled=None, auto_memory=None, close_name=None):
        if enabled is not None and type(enabled) is not bool: raise ValueError('记忆开关无效')
        if auto_memory is not None and type(auto_memory) is not bool: raise ValueError('自动记忆开关无效')
        if close_name is not None: close_name=validate_close_name(close_name)
        if enabled is None and close_name is None and auto_memory is not None:
            def automatic():
                self.memory.configure(auto_memory=auto_memory)
                self.emit({'kind':'memory_changed'})
                return True
            return self.action(automatic)
        return self.change_memory(lambda:self.memory.configure(enabled=enabled,auto_memory=auto_memory,close_name=close_name))

    def oath_available(self):
        state=self.snapshot()
        return not any(state[key] for key in ('busy','call_requested','active','responding','playing','oath_active','incoming')) and not self.closing

    def begin_oath(self, *, replay=False, assets_ready=True):
        # The home oath/replay click authorizes playback; opening the dialog
        # rechecks readiness without requiring a second Start button.
        if not self.oath_available(): raise RuntimeError('请结束当前通话、回复、播放或保存操作后再开始')
        def operation():
            validate_ceremony()
            if assets_ready is not True: raise RuntimeError('仪式素材未就绪，请重试')
            state=self.snapshot()
            if any(state[key] for key in ('call_requested','active','responding','playing','oath_active','incoming')) or self.pending_actions>1:
                raise RuntimeError('请结束当前通话、回复、播放或保存操作后再开始')
            self.companion.cancel_pending()
            self.incoming_calls.invalidate()
            epoch=self.memory.prepare_oath(replay)
            token=uuid.uuid4().hex
            with self.lock:
                self.oath_session={'token':token,'epoch':epoch,'replay':replay,'started':time.monotonic(),'completed':False}
            self.emit({'kind':'oath_started','replay':replay})
            return token
        return self.action(operation)

    def finish_oath(self, token, *, skip=False):
        def operation():
            with self.lock:
                session=self.oath_session
                if not session or session['token']!=token: raise ValueError('这次仪式已经结束')
                if session['completed']: return True
                if not skip and time.monotonic()-session['started']<DURATION:
                    raise ValueError('仪式尚未到达完成点')
            if not session['replay']:
                self.memory.complete_oath(session['epoch'])
                self.restore_contexts()
                self.emit({'kind':'memory_changed'})
            with self.lock: session['completed']=True
            self.status('重温结束' if session['replay'] else '誓约已缔结')
            return True
        return self.action(operation,during_oath=True)

    def cancel_oath(self, token):
        def operation():
            with self.lock:
                if self.oath_session and self.oath_session['token']==token:
                    self.oath_session=None
            self.emit({'kind':'oath_closed'})
            return True
        return self.action(operation,during_oath=True)

    def reset_oath(self):
        def operation():
            self.incoming_calls.invalidate()
            self.incoming_calls.connection('ended')
            self.companion.cancel_pending()
            self.call.stop()
            self.pipeline.cancel(reason='oath_reset')
            with self.lock:
                self.oath_session=None
                self.call_requested=self.muted=False
                self.partial=''
            self.memory.reset_oath()
            self.restore_contexts()
            self.pipeline.last_reply=None
            self.emit({'kind':'memory_changed'})
            self.status('誓约已重置，好感度、记忆与聊天记录保留')
            return True
        return self.action(operation,during_oath=True)

    def reset_relationship(self):
        def reset():
            self.incoming_calls.invalidate()
            self.memory.reset_relationship()
            self.emit({'kind':'memory_changed'})
            self.status('好感度已重置，誓约、记忆和聊天记录保留')
            return True
        return self.action(reset)

    def clear_memories(self):
        return self.change_memory(self.memory.clear)

    def clear_history(self, include_memory=False):
        def operation():
            self.incoming_calls.invalidate()
            self.incoming_calls.connection('ended')
            self.incoming_calls.store.clear()
            self.companion.cancel_pending()
            self.call.stop()
            self.call_requested = self.muted = False
            self.call.reset_input()
            self.pipeline.clear()
            self.store.clear()
            if include_memory:
                self.memory.clear()
                self.emit({'kind':'memory_changed'})
            with self.lock: self.current_event = None
            self.status('聊天记录与长期记忆已清除，好感度与誓约保留' if include_memory else '本地聊天记录已清除')
            self.emit({'kind':'history_cleared'})
        return self.action(operation)

    def snapshot(self):
        # Never acquire pipeline/call locks while holding the observer lock.
        # The pipeline emits its events while holding its own lock.
        with self.lock:
            result = {'engine':self.router.selected,'call_requested':self.call_requested,'muted':self.muted,
                      'partial':self.partial,'message':self.message,'busy':self.pending_actions>0,'closed':self.closed,
                      'oath_active':self.oath_session is not None}
            playback_record_id = self.playback_record_id
        microphone = self.call.timings()
        result.update({
                    'active':bool(self.call.active),'microphone_state':microphone['state'],
                    'microphone_error':microphone['error'],'responding':self.pipeline.responding,
                    'voice_preparing':bool(getattr(self.router.voice,'preparing',False)),
                    'playing':self.router.voice.playback_active,
                    'playback_level':getattr(self.router.voice,'playback_level',0.0)})
        result['playback_record_id'] = playback_record_id if result['playing'] else None
        result['incoming'] = self.incoming_calls.snapshot() if hasattr(self,'incoming_calls') else None
        return result

    def close(self):
        if self.closing: return self.close_future
        self.closing = True
        self.ringtone.close()  # Do not wait behind queued model/actions to silence exit.
        with self.lock: self.oath_session=None
        def finish():
            try:
                voices = {id(v):v for v in [self.router.voice,*self.local_voices.values()]}
                for cleanup in (self.incoming_calls.close,self.call.stop,self.pipeline.close,self.companion.close,
                                *(voice.close for voice in voices.values())):
                    try: cleanup()
                    except Exception as exc: self.status('退出清理（'+type(exc).__name__+'）')
                with self.lock:
                    if self.current_event:
                        self.store.update(self.current_event['id'],self.current_event['reply'],'interrupted')
                        self.current_event = None
                    self.store.close()
                    self.memory.close()
            finally:
                self.closed = True
                self.emit({'kind':'closed'})
        self.close_future = self.executor.submit(finish)
        self.executor.shutdown(wait=False)
        return self.close_future
