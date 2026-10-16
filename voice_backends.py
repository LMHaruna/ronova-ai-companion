"""Session-local backend switch with a configurable startup selection."""
import json
from qwen_realtime import QwenRealtime, ROOT
from realtime_voice import RealtimeVoice


def default_backend():
    path = ROOT / 'config/realtime-voice.json'
    saved = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    name = saved.get('default_backend', 'qwen')
    if name not in ('gpt', 'qwen'):
        raise ValueError('默认语音引擎须为 gpt 或 qwen')
    return name


class BackendRouter:
    def __init__(self, chat=None, voice=None, status=None, qwen_factory=None, gpt_factory=None, factories=None):
        self.selected = 'gpt'
        self.backends = {'gpt': (chat, voice)} if chat is not None and voice is not None else {}
        self.status = status
        self.qwen_factory = qwen_factory or (lambda: (QwenRealtime(), RealtimeVoice(status=status)))
        self.gpt_factory = gpt_factory
        self.factories = dict(factories or {})
        for name,(backend,_) in self.backends.items(): backend.backend = name
        self.chat = ChatRoute(self)
        self.voice = VoiceRoute(self)

    def validate(self, name):
        if name not in ('gpt', 'qwen', 'openai', 'deepseek'): raise ValueError('对话模型选项无效')
        if name not in self.backends:
            if name in self.factories: self.backends[name] = self.factories[name]()
            elif name == 'qwen': self.backends[name] = self.qwen_factory()
            elif name=='gpt' and self.gpt_factory: self.backends[name] = self.gpt_factory()
            else: raise ValueError('模型后端未配置')
        self.backends[name][0].backend = name
        if name == 'qwen' or getattr(self.backends[name][0],'config',None) is not None:
            # A missing key/URL leaves the current backend and call untouched.
            self.backends[name][0].configured()

    def select(self, name):
        self.validate(name)
        self.selected = name


class ChatRoute:
    def __init__(self, router): self.router = router
    def snapshot(self): return self.router.backends[self.router.selected][0]
    @property
    def metrics(self): return self.snapshot().metrics
    def cancel_transport(self):
        for chat, _ in self.router.backends.values(): chat.cancel_transport()
    def clear(self):
        for chat, _ in self.router.backends.values(): chat.clear()
    def close(self):
        for chat, _ in self.router.backends.values(): chat.close()


class VoiceRoute:
    def __init__(self, router): self.router = router
    def snapshot(self): return self.router.backends[self.router.selected][1]
    def __getattr__(self, name): return getattr(self.snapshot(), name)
    def voices(self):
        return {id(voice):voice for _,voice in self.router.backends.values()}.values()
    def stop(self):
        for voice in self.voices(): voice.stop()
    def wait_idle(self, timeout=None):
        return all(voice.wait_idle(timeout) for voice in self.voices())
    def close(self):
        for voice in self.voices(): voice.close()
