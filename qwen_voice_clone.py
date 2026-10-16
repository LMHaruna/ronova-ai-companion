"""One-time Qwen-Audio enrollment using account-scoped temporary storage.

Protocol references:
https://platform.qianwenai.com/docs/developer-guides/speech/voice-cloning
https://platform.qianwenai.com/docs/api-reference/more/upload-file-get-temporary-url
No upload, enrollment or network request occurs when this module is imported.
"""
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import math
import re
import wave

import requests

TARGET_MODEL = 'qwen-audio-3.1-realtime-plus'
API_BASE = 'https://maas.qianwenaiapi.com/api/v1'
ENROLLMENT_MODEL = 'voice-enrollment'


class EnrollmentError(RuntimeError):
    pass


def inspect_audio(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size >= 10 * 1024 * 1024:
        raise EnrollmentError('参考 WAV 必须存在且小于 10MB')
    try:
        with wave.open(str(path), 'rb') as wav:
            rate, width, channels, frames = (wav.getframerate(), wav.getsampwidth(),
                                           wav.getnchannels(), wav.getnframes())
            if wav.getcomptype() != 'NONE' or width != 2 or channels != 1 or rate < 24000:
                raise EnrollmentError('复刻样本须为单声道、16bit PCM、至少 24kHz WAV')
            seconds = frames / rate
            if not 10 <= seconds <= 20:
                raise EnrollmentError('本项目复刻样本须为 10–20 秒')
    except (wave.Error, EOFError):
        raise EnrollmentError('参考 WAV 无法读取') from None
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'seconds': seconds,
            'sample_rate': rate, 'channels': channels, 'sample_width': width,
            'bytes': path.stat().st_size}


def prepare_reference(source, destination, start=.66, end=15.96):
    """Extend the accepted A source interval; write a separate mono sample."""
    import numpy as np
    from scipy.signal import resample_poly
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise EnrollmentError('样本目标须为新文件，不能覆盖原声或现有参考')
    with wave.open(str(source), 'rb') as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
        if wav.getsampwidth() != 2 or wav.getcomptype() != 'NONE':
            raise EnrollmentError('原声须为 16bit PCM WAV')
        if not 0 <= start < end <= wav.getnframes() / rate or not 10 <= end-start <= 20:
            raise EnrollmentError('参考截取范围无效')
        wav.setpos(round(start * rate))
        clip = np.frombuffer(wav.readframes(round((end-start) * rate)), dtype='<i2')
        clip = clip.reshape(-1, channels).astype(np.float64) / 32768
    mono = clip.mean(axis=1)
    powers = np.mean(clip ** 2, axis=0)
    method = 'mean of source channels'
    if np.sqrt(np.mean(mono ** 2)) < np.sqrt(powers.max()) * .65:
        mono = clip[:, powers.argmax()]
        method = 'louder channel (phase cancellation guard)'
    divisor = math.gcd(rate, 32000)
    mono = resample_poly(mono, 32000//divisor, rate//divisor)
    peak = float(np.max(np.abs(mono)))
    if peak < 1e-6:
        raise EnrollmentError('样本无有效声音')
    gain = 10 ** (-3/20) / peak
    mono *= gain
    fade = min(160, len(mono)//2)
    mono[:fade] *= np.linspace(0, 1, fade)
    mono[-fade:] *= np.linspace(1, 0, fade)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(32000)
        wav.writeframes(np.clip(np.round(mono*32768), -32768, 32767).astype('<i2').tobytes())
    return {**inspect_audio(destination), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'start': start, 'end': end, 'channel_method': method,
            'gain_db': 20*math.log10(gain), 'peak_dbfs': -3, 'edge_fade_ms': 5,
            'source_unmodified': True, 'background_separation': False}


def valid_voice_id(value):
    if not isinstance(value, str) or not re.fullmatch(re.escape(TARGET_MODEL) + r'-[A-Za-z0-9_-]{1,160}', value):
        raise EnrollmentError('返回的音色 ID 未绑定到 Qwen-Audio 3.1，保留当前音色')
    return value


class VoiceEnrollment:
    def __init__(self, api_key, session=None):
        if not api_key or any(c in api_key for c in '\r\n'):
            raise EnrollmentError('缺少独立的 DASHSCOPE_API_KEY')
        self.api_key = api_key
        self.session = session or requests.Session()
        # requests has no status/connection retries unless an adapter is added.

    def close(self):
        self.session.close()

    def _request(self, method, url, stage, auth=True, **kwargs):
        headers = kwargs.pop('headers', {})
        if auth:
            headers = {**headers, 'Authorization': 'Bearer ' + self.api_key,
                       'Content-Type': 'application/json'}
        try:
            response = self.session.request(method, url, headers=headers,
                    timeout=(10, 60), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise EnrollmentError(stage + '网络失败；未自动重试') from None
        if response.status_code != 200:
            # The message/body may echo URLs, policies or keys; show only a safe code.
            code = ''
            try:
                value = response.json().get('code', '')
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,79}', value):
                    code = ' / ' + value
            except (ValueError, AttributeError):
                pass
            raise EnrollmentError(f'{stage}失败（HTTP {response.status_code}{code}）；未自动重试')
        return response

    @staticmethod
    def _json(response, field, stage):
        try:
            value = response.json()[field]
        except (ValueError, KeyError, TypeError):
            raise EnrollmentError(stage + '返回格式无效') from None
        if not isinstance(value, dict):
            raise EnrollmentError(stage + '返回格式无效')
        return value

    def upload(self, reference):
        info = inspect_audio(reference)
        response = self._request('GET', API_BASE + '/uploads', '获取临时上传凭证',
                params={'action': 'getPolicy', 'model': ENROLLMENT_MODEL})
        policy = self._json(response, 'data', '上传凭证')
        host = policy.get('upload_host', '')
        parsed = urlsplit(host)
        if (parsed.scheme != 'https' or not parsed.hostname
                or not parsed.hostname.endswith('.oss-cn-beijing.aliyuncs.com')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port not in (None, 443) or parsed.path not in ('', '/')):
            raise EnrollmentError('平台返回了非北京 OSS HTTPS 上传地址，停止上传')
        if policy.get('x_oss_object_acl') != 'private':
            raise EnrollmentError('临时上传凭证未限定私有访问，停止上传')
        directory = policy.get('upload_dir', '')
        if not isinstance(directory, str) or not directory.startswith('dashscope-instant/') or '..' in directory:
            raise EnrollmentError('临时存储目录无效')
        fields = {'OSSAccessKeyId': 'oss_access_key_id', 'Signature': 'signature',
                  'policy': 'policy', 'x-oss-object-acl': 'x_oss_object_acl',
                  'x-oss-forbid-overwrite': 'x_oss_forbid_overwrite'}
        if any(not isinstance(policy.get(name), str) or not policy[name] for name in fields.values()):
            raise EnrollmentError('临时上传凭证字段不完整')
        # ASCII content name; neither path nor original recording name is uploaded.
        name = 'ronova-' + info['sha256'][:16] + '.wav'
        key = directory.rstrip('/') + '/' + name
        files = {field: (None, policy[name]) for field, name in fields.items()}
        files.update(key=(None, key), success_action_status=(None, '200'))
        with Path(reference).open('rb') as audio:
            files['file'] = (name, audio, 'audio/wav')
            self._request('POST', host, '上传复刻样本', auth=False, files=files)
        # Never log or persist credentials, signatures or the private temporary URL.
        return 'oss://' + key

    def create(self, reference_url, prefix='ronova'):
        if not re.fullmatch(r'[A-Za-z0-9]{1,10}', prefix):
            raise EnrollmentError('音色前缀须为 1–10 位英文字母或数字')
        if not reference_url.startswith('oss://dashscope-instant/'):
            raise EnrollmentError('请使用平台私有临时音频 URL')
        response = self._request('POST', API_BASE + '/services/audio/tts/customization',
            '创建音色', headers={'X-DashScope-OssResourceResolve': 'enable'},
            json={'model': ENROLLMENT_MODEL, 'input': {'action': 'create_voice',
                  'target_model': TARGET_MODEL, 'prefix': prefix, 'url': reference_url}})
        output = self._json(response, 'output', '音色创建')
        return valid_voice_id(output.get('voice_id'))

    def query(self, voice):
        valid_voice_id(voice)
        response = self._request('POST', API_BASE + '/services/audio/tts/customization',
            '查询音色', json={'model': ENROLLMENT_MODEL,
            'input': {'action': 'query_voice', 'voice_id': voice}})
        output = self._json(response, 'output', '音色查询')
        if output.get('target_model') != TARGET_MODEL:
            raise EnrollmentError('服务端音色绑定模型不匹配，保留当前音色')
        status = output.get('status')
        if status not in ('OK', 'DEPLOYING', 'UNDEPLOYED'):
            raise EnrollmentError('服务端音色状态无效')
        return status


def activate(config_path, voice, status):
    """Apply an enrolled and auditioned voice; preserve all other settings."""
    import json
    valid_voice_id(voice)
    if status != 'OK':
        raise EnrollmentError('音色尚未通过审核，保留当前音色')
    config_path = Path(config_path)
    saved = json.loads(config_path.read_text(encoding='utf-8-sig'))
    if saved.get('model') != TARGET_MODEL:
        raise EnrollmentError('当前实时模型与复刻音色不匹配')
    if saved.get('voice') != voice:
        saved['previous_voice'] = saved.get('voice', 'longanqian')
    saved.update(voice=voice, voice_target_model=TARGET_MODEL, voice_label='若娜瓦',
                 voice_type='cloned', voice_enrollment_manifest='config/qwen-voice-enrollment.json')
    temporary = config_path.with_name(config_path.name + '.tmp')
    temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(config_path)
