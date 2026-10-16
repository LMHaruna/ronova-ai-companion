"""Reusable streaming transport, bounded request history and content-free timings."""
import json
import os
import threading
import time
import requests
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from role_prompt import SYSTEM_PROMPT

BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
CHAT_COMPLETION_URL = f"{BASE_URL}/chat/completions"
MODEL = "qwen3.8-flash"


class ChatError(RuntimeError):
    pass


class ChatCancelled(ChatError):
    pass


@dataclass(frozen=True)
class ChatConfig:
    model: str = MODEL
    url: str = CHAT_COMPLETION_URL
    api_key: str | None = field(default=None, repr=False)
    provider: str = 'custom'

    def endpoint(self):
        if not isinstance(self.model,str) or not self.model or len(self.model)>200 or any(c.isspace() for c in self.model):
            raise ChatError('请填写有效的大语言模型名字')
        if not isinstance(self.url,str) or len(self.url)>2048 or any(c.isspace() for c in self.url):
            raise ChatError('请填写有效的大语言模型 URL 地址')
        parsed = urlsplit(self.url)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or
                (parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1')))):
            raise ChatError('大语言模型 URL 需使用 HTTPS 完整接口地址，本地接口可使用 HTTP；密钥请填写在密钥栏')
        path = parsed.path.rstrip('/')
        if path.endswith('/v1'): path += '/chat/completions'
        return urlunsplit((parsed.scheme,parsed.netloc,path,'',''))

    def key(self):
        variable = {'openai':'OPENAI_API_KEY','deepseek':'DEEPSEEK_API_KEY','custom':'ALI_API_Key'}.get(self.provider)
        if variable is None: raise ChatError('大语言模型提供商无效')
        return os.getenv(variable,'') if self.api_key is None else self.api_key

    def generation_options(self, max_tokens):
        if self.provider=='openai': return {'max_completion_tokens':max_tokens,'reasoning_effort':'none'}
        options = {'max_tokens':max_tokens}
        if self.provider=='deepseek': options['thinking'] = {'type':'disabled'}
        elif self.provider=='custom' and self.model=='qwen3.8-flash':
            options['enable_thinking'] = False
        return options

    def validate(self):
        self.endpoint()
        key = self.key()
        if not key or len(key)>4096 or any(c.isspace() for c in key):
            raise ChatError('请在对话模型设置中填写大语言模型 API Key')
        return self


class ChatClient:
    def __init__(self, post=None, history_char_budget=16000, history_turn_limit=20, config=None):
        self.config = config
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.context_provider = None
        self.session = requests.Session() if post is None else None
        self.post = post or self.session.post
        self.history_char_budget = history_char_budget
        self.history_turn_limit = history_turn_limit
        self.metrics = {}
        self._lock = threading.Lock()
        self._transport_lock = threading.Lock()
        self._response = None
        self._revision = 0

    def configured(self):
        return (self.config or ChatConfig()).validate()

    def clear(self):
        with self._lock:
            self.history = self.history[:1]
            self._revision += 1

    def cancel_transport(self):
        # Closing a socket may itself wait on a blocked read. Never do it on the UI thread.
        with self._lock:
            response = self._response
        if response is not None:
            def close():
                try:
                    response.close()
                except Exception:
                    pass
            threading.Thread(target=close, daemon=True).start()

    def close(self):
        self.cancel_transport()
        if self.session:
            self.session.close()

    def _messages(self, text):
        history = self.history
        system = dict(history[0])
        if self.context_provider: system['content'] += self.context_provider(text)
        remaining = self.history_char_budget - len(system["content"]) - len(text)
        # Incoming openings are assistant-only; never pair them with a later
        # user message or lose them because the history has an odd length.
        groups = []
        for message in history[1:]:
            if message['role']=='user' or not groups: groups.append([])
            groups[-1].append(message)
        pairs = []
        for pair in reversed(groups):
            size = sum(len(v["content"]) for v in pair)
            if len(pairs) >= self.history_turn_limit or size > remaining:
                break
            pairs.append(pair)
            remaining -= size
        recent = [message for pair in reversed(pairs) for message in pair]
        return [system, *recent, {"role": "user", "content": text}], len(groups) - len(pairs)

    def reply(self, text, on_token=None, max_tokens=512, cancel=None):
        cancel = cancel or threading.Event()
        started = time.perf_counter()
        metrics = {"model": self.config.model if self.config else MODEL, "content_events": 0, "reasoning_characters": 0}
        def check_cancel():
            if cancel.is_set():
                raise ChatCancelled("本轮已停止")
        with self._transport_lock:
            check_cancel()
            metrics['transport_wait_seconds'] = time.perf_counter() - started
            config = self.config
            if config: config.validate()
            key = config.key() if config else os.getenv("ALI_API_Key")
            if not key:
                raise ChatError("未设置环境变量 ALI_API_Key；设置后重新启动")
            with self._lock:
                messages, omitted = self._messages(text)
                revision = self._revision
            metrics.update(omitted_history_turns=omitted, request_characters=sum(len(m["content"]) for m in messages))
            payload = {"model": metrics['model'], "messages": messages, "stream": True,
                       **(config or ChatConfig()).generation_options(max_tokens)}
            metrics['thinking_disabled'] = (payload.get('enable_thinking') is False or
                                           payload.get('thinking') == {'type':'disabled'} or
                                           payload.get('reasoning_effort') == 'none')
            if not config or config.provider!='openai': payload['temperature'] = .7
            parts, complete = [], False
            try:
                with self.post(config.endpoint() if config else CHAT_COMPLETION_URL,
                               headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                               json=payload, stream=True, timeout=(10, 60), allow_redirects=False) as response:
                    with self._lock:
                        self._response = response
                    metrics["headers_seconds"] = time.perf_counter() - started
                    check_cancel()
                    if response.status_code != 200:
                        raise ChatError(f"聊天请求失败，HTTP {response.status_code}；可重试")
                    response.encoding = "utf-8"
                    # Small events must not wait for the default 512-byte read buffer.
                    for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                        check_cancel()
                        if isinstance(line, bytes):
                            line = line.decode("utf-8")
                        if not line or not line.startswith("data:"):
                            continue
                        metrics.setdefault("first_sse_seconds", time.perf_counter() - started)
                        data = line[5:].strip()
                        if data == "[DONE]":
                            complete = True
                            # Drain the finite response so the connection can re-enter the pool.
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            raise ChatError("聊天流数据损坏，文字保留；本轮未写入上下文") from None
                        if not isinstance(chunk, dict) or "error" in chunk:
                            raise ChatError("聊天流格式异常或返回错误；可重试")
                        choices = chunk.get("choices") or []
                        if not isinstance(choices, list) or (choices and not isinstance(choices[0], dict)):
                            raise ChatError("聊天流格式异常；可重试")
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if not isinstance(delta, dict):
                            raise ChatError("聊天流格式异常；可重试")
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            metrics.setdefault('first_reasoning_seconds', time.perf_counter() - started)
                            metrics['last_reasoning_seconds'] = time.perf_counter() - started
                            metrics["reasoning_characters"] += len(reasoning)
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            metrics.setdefault("first_content_seconds", time.perf_counter() - started)
                            metrics["last_content_seconds"] = time.perf_counter() - started
                            metrics["content_events"] += 1
                            parts.append(content)
                            if on_token:
                                on_token(content)
                        if choices[0].get("finish_reason") is not None:
                            complete = True
                check_cancel()
                reply = "".join(parts).strip()
                if not reply or not complete:
                    raise ChatError("聊天流未完整返回，文字保留；本轮未写入上下文")
                with self._lock:
                    check_cancel()
                    if revision != self._revision:
                        raise ChatCancelled("上下文已清除，本轮已停止")
                    self.history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
                return reply
            except requests.RequestException:
                check_cancel()
                raise ChatError("聊天连接中断或超时，文字保留；可重试") from None
            except (ValueError, AttributeError):
                check_cancel()
                raise ChatError("聊天流读取异常，文字保留；可重试") from None
            finally:
                metrics["total_seconds"] = time.perf_counter() - started
                metrics["cancelled"] = cancel.is_set()
                self.metrics = metrics
                with self._lock:
                    self._response = None
