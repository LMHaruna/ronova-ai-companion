"""Text turns to native Qwen realtime audio; no microphone/reference upload."""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import base64
import json
import os
import socket
import sys
import threading
import time
from chat_client import ChatClient, ChatError, ChatCancelled
from role_prompt import SYSTEM_PROMPT

MODEL = "qwen-audio-3.1-realtime-plus"
ROOT = Path(__file__).resolve().parent
VOICE_INSTRUCTIONS = ('语音表达保持从容、冷静、克制，带一点高傲和距离感。'
                      '语速平稳，音高变化适度，句尾自然收稳；关心时温和但笃定，'
                      '不使用兴奋、欢快、撒娇或夸张笑意的语气。'
                      '按正文自然说话，不朗读这些发声要求。')


def environment(name):
    value = os.getenv(name)
    if value or sys.platform != 'win32':
        return value or ''
    # Settings added in Windows after this app started need no app restart.
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else ''
    except OSError:
        return ''


@dataclass(frozen=True)
class RealtimeConfig:
    url: str
    voice: str = 'longanqian'
    key_environment: str = 'DASHSCOPE_API_KEY'
    voice_target_model: str = ''
    enable_speech_emotion: bool = False
    voice_instructions: str = VOICE_INSTRUCTIONS
    model: str = MODEL
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls):
        config = ROOT / 'config/realtime-voice.json'
        saved = json.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
        return cls(environment('QWEN_REALTIME_URL') or saved.get('url', ''),
                   environment('QWEN_REALTIME_VOICE') or saved.get('voice', 'longanqian'),
                   saved.get('key_environment', 'DASHSCOPE_API_KEY'),
                   saved.get('voice_target_model', ''),
                   saved.get('enable_speech_emotion', False),
                   saved.get('voice_instructions', VOICE_INSTRUCTIONS),
                   saved.get('model', MODEL))

    def endpoint(self):
        if not isinstance(self.model,str) or not self.model or len(self.model)>200 or any(c.isspace() for c in self.model):
            raise ChatError('请填写有效的千问实时模型名字')
        if not isinstance(self.enable_speech_emotion, bool):
            raise ChatError('情绪增强配置须为 true 或 false')
        if not isinstance(self.voice_instructions, str) or len(self.voice_instructions) > 1024:
            raise ChatError('发声要求须为不超过 1024 字的文字')
        parsed = urlsplit(self.url.strip())
        if (parsed.scheme != 'wss' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or '{' in parsed.netloc or '}' in parsed.netloc):
            raise ChatError('请配置 QWEN_REALTIME_URL：业务空间对应的 wss:// 实时接口地址')
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(name != 'model' for name, _ in query) or len(query) > 1:
            raise ChatError('实时接口 URL 仅允许 model 查询参数，密钥须放环境变量')
        if query and query[0][1] != self.model:
            raise ChatError('实时接口 URL 中的 model 与模型名字不一致')
        if not self.voice or any(char in self.voice for char in '\r\n'):
            raise ChatError('实时音色配置无效')
        if self.voice_target_model and self.voice_target_model != self.model:
            raise ChatError('若娜瓦复刻音色绑定 ' + self.voice_target_model + '，请使用兼容的模型名字')
        if self.voice.startswith(('qwen-audio-', 'qwen3-tts-', 'cosyvoice-')) and not self.voice.startswith(self.model + '-'):
            raise ChatError('该复刻音色与所选实时模型不兼容，请核对模型名字')
        if self.key_environment != 'DASHSCOPE_API_KEY':
            raise ChatError('实时接口只读取独立的 DASHSCOPE_API_KEY')
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({'model': self.model}), ''))

    def key(self):
        return environment(self.key_environment) if self.api_key is None else self.api_key

    def validate(self):
        self.endpoint()
        key = self.key()
        if not key or len(key)>4096 or any(c.isspace() for c in key):
            raise ChatError('请在对话模型设置中填写千问 API Key，或配置 DASHSCOPE_API_KEY')


def connect(config):
    dependency = ROOT / 'runtime/realtime-tools'
    if str(dependency) not in sys.path:
        sys.path.insert(0, str(dependency))
    try:
        import websocket
    except ImportError:
        raise ChatError('缺少实时连接依赖，请运行 scripts/setup_realtime_voice.py') from None
    websocket.enableTrace(False)
    # No redirects: credentials cannot be forwarded to another host.
    ws = websocket.create_connection(config.endpoint(),
        header={'Authorization': 'Bearer ' + config.key()},
        timeout=10, redirect_limit=0, enable_multithread=True)
    ws.settimeout(.25)
    return ws


class QwenRealtime(ChatClient):
    outputs_audio = True

    def __init__(self, config=None, connector=None, instructions=SYSTEM_PROMPT):
        super().__init__(post=lambda *a, **k: None)
        self.history[0]['content'] = instructions
        self.config = config
        self.connector = connector or connect
        self._socket = None
        self._closed = False

    def configured(self):
        config = self.config or RealtimeConfig.load()
        config.validate()
        return config

    def cancel_transport(self):
        with self._lock:
            ws = self._socket
        if ws:
            # Shutdown wakes recv without waiting on the UI thread.
            def stop():
                try:
                    ws.shutdown()
                except Exception:
                    pass
            threading.Thread(target=stop, daemon=True).start()

    def close(self):
        with self._lock:
            self._closed = True
        self.cancel_transport()

    def reply(self, text, on_token=None, cancel=None, on_audio=None, audio_enabled=True):
        cancel = cancel or threading.Event()
        started = time.perf_counter()
        metrics = {'model': MODEL, 'audio_events': 0, 'content_events': 0,
                   'audio_done_events':0,'microphone_audio_sent': False, 'reference_audio_sent': False}
        frames=0
        ws = None
        def check():
            if cancel.is_set() or self._closed:
                raise ChatCancelled('本轮已停止')
        def send(event):
            check()
            ws.send(json.dumps(event, ensure_ascii=False))
        def receive(deadline):
            while True:
                check()
                if time.perf_counter() > deadline:
                    raise ChatError('实时语音接口超时，可重试')
                try:
                    raw = ws.recv()
                except Exception as exc:
                    if isinstance(exc, (socket.timeout, TimeoutError)) or type(exc).__name__ == 'WebSocketTimeoutException':
                        continue
                    raise
                check()
                if not raw or len(raw) > 1024 * 1024:
                    raise ChatError('实时语音连接中断或事件过大')
                event = json.loads(raw)
                if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                    raise ChatError('实时语音事件格式错误')
                if event['type'] == 'error':
                    # Never print provider error messages, headers, bodies or keys.
                    details = event.get('error', {})
                    if isinstance(details, dict):
                        description = ' '.join(str(details.get(key, '')) for key in ('code', 'message')).lower()
                        if ('voice' in description or '音色' in description) and any(
                                term in description for term in ('not found', 'not exist', 'does not exist', 'invalid', '不存在', '无效')):
                            raise ChatError('当前 API Key 无法使用配置的音色；请在 config/realtime-voice.json 中填写该账号可用的音色编号，或使用内置音色。')
                    raise ChatError('实时语音服务返回错误，请核对模型权限、音色和配置')
                return event
        with self._transport_lock:
            try:
                check()
                config = self.configured()
                metrics['model'] = config.model
                with self._lock:
                    messages, omitted = self._messages(text)
                    revision = self._revision
                ws = self.connector(config)
                with self._lock:
                    self._socket = ws
                check()
                metrics['connect_seconds'] = time.perf_counter() - started
                instructions = messages[0]['content']
                if audio_enabled and config.voice_instructions:
                    instructions += '\n\n' + config.voice_instructions
                metrics['enable_speech_emotion'] = config.enable_speech_emotion
                metrics['voice_instructions_applied'] = bool(audio_enabled and config.voice_instructions)
                session = {
                    'modalities': ['text', 'audio'] if audio_enabled else ['text'],
                    'voice': config.voice, 'instructions': instructions,
                    'enable_speech_emotion': config.enable_speech_emotion,
                    'input_audio_format': 'pcm', 'output_audio_format': 'pcm',
                    'output_audio': {'language': 'zh'}, 'turn_detection': None,
                    'max_history_turns': 20, 'enable_search': False}
                send({'type': 'session.update', 'session': session})
                deadline = time.perf_counter() + 10
                while True:
                    event = receive(deadline)
                    if event['type'] == 'session.updated':
                        applied = event.get('session', {})
                        if isinstance(applied, dict) and 'enable_speech_emotion' in applied:
                            emotion = applied['enable_speech_emotion']
                            metrics['server_enable_speech_emotion'] = emotion if isinstance(emotion, bool) else None
                            if emotion is not config.enable_speech_emotion:
                                raise ChatError('服务端未应用情绪增强设置，本轮已停止')
                        break
                metrics['session_ready_seconds'] = time.perf_counter() - started
                metrics['voice'] = config.voice
                metrics['omitted_history_turns'] = omitted
                for message in messages[1:]:
                    send({'type': 'conversation.item.create', 'item': {'type': 'message',
                        'role': message['role'], 'content': [{'type': 'output_text' if message['role'] == 'assistant' else 'input_text',
                            'text': message['content']}]}})
                send({'type': 'response.create'})
                metrics['response_requested_seconds'] = time.perf_counter() - started
                parts, final_text, frames, characters = [], '', 0, 0
                response_id = None
                deadline = time.perf_counter() + 90
                while True:
                    event = receive(deadline)
                    kind = event['type']
                    if kind == 'response.created':
                        response_id = event.get('response', {}).get('id')
                    elif event.get('response_id') and response_id and event['response_id'] != response_id:
                        continue
                    if kind in ('response.audio_transcript.delta', 'response.text.delta'):
                        delta = event.get('delta')
                        if not isinstance(delta, str):
                            raise ChatError('实时语音字幕格式错误')
                        if delta:
                            characters += len(delta)
                            if characters > 16000:
                                raise ChatError('本轮实时字幕超过16000字符上限')
                            metrics.setdefault('first_content_seconds', time.perf_counter() - started)
                            metrics['content_events'] += 1
                            parts.append(delta)
                            if on_token: on_token(delta)
                    elif kind in ('response.audio_transcript.done', 'response.text.done'):
                        metrics['transcript_done_seconds']=time.perf_counter()-started
                        final_text = event.get('transcript', event.get('text', ''))
                        if not isinstance(final_text, str):
                            raise ChatError('实时语音字幕格式错误')
                    elif kind == 'response.audio.delta':
                        if not audio_enabled:
                            raise ChatError('纯文字会话收到未请求的音频')
                        delta = event.get('delta')
                        if not isinstance(delta, str):
                            raise ChatError('实时语音音频格式错误')
                        pcm = base64.b64decode(delta, validate=True)
                        if not pcm or len(pcm) % 2 or len(pcm) > 24000 * 2 * 5:
                            raise ChatError('实时语音 PCM 数据无效或过大')
                        metrics.setdefault('first_audio_seconds', time.perf_counter() - started)
                        metrics['last_audio_seconds']=time.perf_counter()-started
                        metrics['audio_events'] += 1
                        frames += len(pcm) // 2
                        if frames > 24000 * 120:
                            raise ChatError('本轮语音超过120秒上限')
                        if on_audio: on_audio(pcm, 24000)
                    elif kind == 'response.audio.done':
                        metrics['audio_done_events']+=1
                    elif kind == 'response.done':
                        response = event.get('response', {})
                        metrics['response_status']=response.get('status')
                        metrics['response_done_seconds']=time.perf_counter()-started
                        if response.get('status') != 'completed':
                            raise ChatError('实时语音回应未完整结束，本轮未提交上下文')
                        if not final_text and not parts:
                            final_text = ''.join(content.get('transcript', content.get('text', ''))
                                for item in response.get('output', [])
                                for content in item.get('content', [])
                                if content.get('type') in ('audio', 'text'))
                        break
                reply = final_text or ''.join(parts)
                if not reply or len(reply) > 16000 or audio_enabled and not frames:
                    raise ChatError('实时语音没有完整文字或声音，本轮未提交上下文')
                if not parts and on_token: on_token(reply)
                with self._lock:
                    check()
                    if revision != self._revision:
                        raise ChatCancelled('上下文已清除，本轮已停止')
                    # Bound retained RAM as well as each transmitted context.
                    # Dynamic memory belongs to this request, never to retained
                    # history; a later edit/delete must not leave a stale copy.
                    self.history = [self.history[0], *messages[1:-1], {'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}]
                    while len(self.history) > 2 * self.history_turn_limit + 1 or sum(len(m['content']) for m in self.history) > self.history_char_budget:
                        if len(self.history) <= 3: break
                        del self.history[1:3]
                metrics['audio_frames'] = frames
                return reply
            except (ChatError, ChatCancelled):
                raise
            except Exception:
                check()
                raise ChatError('实时语音连接或数据读取失败；可重试，详情未输出') from None
            finally:
                if ws:
                    try: ws.close(timeout=.1)
                    except Exception: pass
                with self._lock:
                    self._socket = None
                metrics.update(total_seconds=time.perf_counter() - started, cancelled=cancel.is_set(),audio_frames=frames)
                self.metrics = metrics
