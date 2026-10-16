"""Editable desktop service profiles, separate from speaker synthesis settings."""
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
import threading
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from chat_client import ChatConfig, CHAT_COMPLETION_URL, MODEL as CHAT_MODEL
from client_credentials import crypt, user_credentials_path, write_encrypted
from qwen_realtime import RealtimeConfig, MODEL as QWEN_MODEL, environment

ROOT = Path(__file__).resolve().parent
ENGINES = ('qwen', 'openai', 'deepseek', 'gpt')
ENGINE_LABELS = {'qwen':'千问 3.1', 'openai':'ChatGPT', 'deepseek':'DeepSeek', 'gpt':'自定义模型'}


@dataclass(frozen=True)
class ModelProfile:
    model: str
    url: str
    api_key: str = field(default='', repr=False)

    def validate(self):
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model)>200 or any(c.isspace() for c in self.model):
            raise ValueError('请填写有效的模型名字')
        if not isinstance(self.url, str) or not self.url.strip() or len(self.url)>2048 or any(c.isspace() for c in self.url):
            raise ValueError('请填写有效的 URL 地址')
        if not isinstance(self.api_key, str) or len(self.api_key)>4096 or any(c.isspace() for c in self.api_key):
            raise ValueError('API Key 格式无效，请检查空格或换行')
        return self


class ClientModels:
    def __init__(self, root=ROOT, credentials_path=None):
        root = Path(root)
        # Tests and isolated previews use their own temporary credentials file.
        self.path = Path(credentials_path) if credentials_path else (
            user_credentials_path() if root.resolve()==ROOT.resolve() else root/'config/client-models.bin')
        self.lock = threading.RLock()
        self.warning = ''
        self.realtime = RealtimeConfig.load()
        parsed = urlsplit(self.realtime.url)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
        self.profiles = {
            'qwen': ModelProfile(QWEN_MODEL, url, environment('DASHSCOPE_API_KEY')),
            'gpt': ModelProfile(CHAT_MODEL, CHAT_COMPLETION_URL, environment('ALI_API_Key')),
            'openai': ModelProfile('gpt-6-sol', 'https://api.openai.com/v1/chat/completions', environment('OPENAI_API_KEY')),
            'deepseek': ModelProfile('deepseek-flash', 'https://api.deepseek.com/chat/completions', environment('DEEPSEEK_API_KEY'))}
        defaults = root/'config/client-models.json'
        if defaults.exists():
            saved = json.loads(defaults.read_text(encoding='utf-8-sig'))
            for name in ('qwen', 'gpt'):
                value = saved.get(name, {})
                self.profiles[name] = replace(self.profiles[name],model=value.get('model',self.profiles[name].model),
                                               url=value.get('url',self.profiles[name].url)).validate()
        self.presets = {name:replace(profile,api_key='') for name,profile in self.profiles.items() if name!='gpt'}
        if self.path.exists():
            # An unreadable local vault must never silently reuse an owner's env key.
            self.profiles = {name:replace(profile,api_key='') for name,profile in self.profiles.items()}
            try:
                value = json.loads(crypt(self.path.read_bytes(), decrypt=True).decode('utf-8'))
                if not isinstance(value,dict) or value.get('version') not in (1,2): raise ValueError()
                saved_profiles = value['profiles']
                if not isinstance(saved_profiles,dict) or not {'qwen','gpt'} <= saved_profiles.keys(): raise ValueError()
                if value['version']==2 and not set(ENGINES) <= saved_profiles.keys(): raise ValueError()
                # V1 vaults contain two engines. Keep their customer credentials;
                # new providers start empty, never borrowing a legacy key.
                loaded = {name:(ModelProfile(**saved_profiles[name]).validate() if name in saved_profiles else profile)
                          for name,profile in self.profiles.items()}
                for name, profile in loaded.items(): self.config(name,profile).endpoint()
                self.profiles = loaded
            except (OSError,ValueError,RuntimeError,TypeError,KeyError):
                self.warning = '本机模型配置无法读取，请在设置中重新填写 API Key。'

    def snapshot(self):
        with self.lock: return dict(self.profiles)

    def config(self, engine, profile=None):
        if engine not in ENGINES: raise ValueError('对话模型选项无效')
        profile = profile or self.snapshot()[engine]
        profile.validate()
        if engine=='gpt': return ChatConfig(profile.model,profile.url,profile.api_key)
        if engine in ('openai','deepseek'):
            preset = self.presets[engine]
            if (profile.model,profile.url)!=(preset.model,preset.url):
                raise ValueError('此模型使用内置接口，请重新保存 API Key')
            return ChatConfig(profile.model,profile.url,profile.api_key,provider=engine)
        if engine!='qwen': raise ValueError('对话模型选项无效')
        parsed = urlsplit(profile.url.strip())
        query = parse_qsl(parsed.query,keep_blank_values=True)
        if parsed.fragment or any(name!='model' for name,_ in query) or len(query)>1:
            raise ValueError('千问 URL 仅允许 model 查询参数，API Key 请填写在密钥栏')
        # The dedicated model field takes precedence over an existing URL model.
        url = urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))
        return replace(self.realtime,url=url,model=profile.model,api_key=profile.api_key)

    def prepare(self, engine, profile):
        config = self.config(engine,profile)
        config.validate()
        profiles = self.snapshot(); profiles[engine] = profile
        payload = json.dumps({'version':2,'profiles':{name:asdict(value) for name,value in profiles.items()}},
                             ensure_ascii=False).encode('utf-8')
        return profiles, crypt(payload)

    def commit(self, prepared):
        profiles, encrypted = prepared
        with self.lock:
            write_encrypted(self.path,encrypted)
            self.profiles = profiles
            self.warning = ''
