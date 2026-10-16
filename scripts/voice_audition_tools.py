"""Reusable enrollment audition helpers; no comparison CLI or implicit requests."""
from pathlib import Path
import hashlib
import html
import json
import re
import sys
import time
import wave
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pcm_player import PCMPlayer
from qwen_realtime import QwenRealtime,RealtimeConfig
from realtime_voice import RealtimeVoice,READ_INSTRUCTIONS

TEXTS = (
    '你来了，凡人。今天发生了什么，说给我听听。说完以后，就早点休息吧。',
    '坏？凡人，你的判断太轻率。先坐下休息，等精神好些再继续。',
)

def wait_for(predicate, timeout=180):
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline: time.sleep(.02)
    if not predicate(): raise TimeoutError('本地检查超过等待上限')

def save_wav(path, pcm, rate):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate); wav.writeframes(pcm)

def silence_intervals(pcm, rate):
    values = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
    size = rate // 100
    rms = [float(np.sqrt(np.mean(values[index:index + size] ** 2)))
           for index in range(0, len(values), size)]
    intervals, start = [], None
    for index, quiet in enumerate([value < 180 for value in rms] + [False]):
        if quiet and start is None: start = index
        elif not quiet and start is not None:
            end = min(index * .01, len(values) / rate)
            if end - start * .01 >= .2:
                intervals.append({'start_seconds': start * .01, 'end_seconds': end,
                                  'duration_seconds': end - start * .01})
            start = None
    return intervals

def leading_silence(pcm, rate):
    values = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
    size = rate // 100
    for index in range(0, len(values), size):
        if np.sqrt(np.mean(values[index:index + size] ** 2)) >= 180:
            return index / rate
    return len(values) / rate

def artifacts(directory, backend, index, pcm, rate, player, base):
    stem = f'{backend}-{index + 1}'
    source = directory / (stem + '-source.wav')
    timeline = directory / (stem + '-playback.wav')
    save_wav(source, pcm, rate)
    events = list(player.write_events)
    pieces, cursor = [], 0
    for event in events:
        offset = event['offset_frames'] * 2
        pieces.append(pcm[cursor:offset])
        if event['gap_seconds'] > .025:
            pieces.append(b'\x00\x00' * round(event['gap_seconds'] * rate))
        end = offset + event['frames'] * 2
        pieces.append(pcm[offset:end]); cursor = end
    pieces.append(pcm[cursor:])
    save_wav(timeline, b''.join(pieces), rate)
    first = events[0]['submitted_at'] - base
    intervals = silence_intervals(pcm, rate)
    leading = leading_silence(pcm, rate)
    internal = [value for value in intervals
                if value['start_seconds'] > 0 and value['end_seconds'] < len(pcm)/rate/2 - .015]
    gaps = [{'at_source_seconds': event['offset_frames']/rate, 'seconds': event['gap_seconds']}
            for event in events if event['gap_seconds'] > .025]
    return {'source_wav': str(source), 'playback_wav': str(timeline),
            'audio_sha256': hashlib.sha256(pcm).hexdigest(), 'audio_seconds': len(pcm)/rate/2,
            'sample_rate': rate, 'request_to_first_device_write_seconds': first,
            'request_to_estimated_first_sound_seconds': first + leading,
            'software_gaps': gaps, 'longest_software_gap_seconds': max([v['seconds'] for v in gaps], default=0),
            'encoded_leading_silence_seconds': leading, 'encoded_internal_pauses': internal,
            'longest_encoded_internal_pause_seconds': max([v['duration_seconds'] for v in internal], default=0),
            'submitted_frames': player.submitted_frames, 'completed_frames': player.completed_frames,
            'playback_drained': not player.pending and player.completed_frames == player.submitted_frames,
            'write_events': [{**value, 'submitted_at': value['submitted_at'] - base} for value in events]}

def normalize(text): return re.sub(r'[^\w\u4e00-\u9fff]', '', text)

def realtime_runs(directory, config=None):
    report = {'backend':'qwen','runs':[], 'live_requests':0}
    try:
        config = config or RealtimeConfig.load(); config.validate()
        report['model'] = 'qwen-audio-3.1-realtime-plus'; report['voice'] = config.voice
        report['url'] = config.endpoint()
    except Exception as exc:
        report['blocked'] = str(exc); return report
    for index,text in enumerate(TEXTS):
        captures, players = [], []
        def factory(*args,**kwargs):
            player = PCMPlayer(*args,**kwargs); players.append(player); return player
        voice = RealtimeVoice(config=config,player_factory=factory,
                              on_pcm=lambda job,pcm,rate: captures.append(pcm))
        client = QwenRealtime(config=config,instructions=READ_INSTRUCTIONS)
        record = {'text':text}; job = None
        try:
            voice.prepare()
            base = time.perf_counter(); job = voice.begin()
            report['live_requests'] += 1
            transcript = client.reply(text,cancel=job.cancelled,
                    on_audio=lambda pcm,rate: voice.append_pcm(job,pcm,rate))
            voice.end(job); wait_for(job.finished.is_set)
            if job.error: raise RuntimeError('Qwen 播放失败')
            record.update(transcript=transcript, same_words=normalize(transcript)==normalize(text),
                          metrics=dict(job.metrics), client_metrics=dict(client.metrics),
                          **artifacts(directory,'qwen',index,b''.join(captures),24000,players[-1],base))
            assert record['playback_drained']
            print('QWEN_RUN '+json.dumps({k:record[k] for k in
                  ('same_words','request_to_first_device_write_seconds','longest_software_gap_seconds','audio_seconds')},ensure_ascii=False),flush=True)
        except Exception as exc:
            voice.stop(); record.update(error_type=type(exc).__name__,
                    error=str(exc) if isinstance(exc,RuntimeError) else '请求或播放检查失败',
                    client_metrics=dict(client.metrics))
            if captures:
                path = directory / f'qwen-{index + 1}-partial.wav'
                save_wav(path,b''.join(captures),24000); record['partial_wav'] = str(path)
            print('QWEN_FAILED '+record['error_type'],flush=True)
        finally:
            client.close(); voice.close()
        report['runs'].append(record)
    return report

def make_review(directory, report):
    cards = []
    for backend in report['backends']:
        for index,run in enumerate(backend['runs']):
            if 'source_wav' not in run:
                cards.append('<section><h2>Qwen 请求未完成</h2><p>'+html.escape(run.get('error',''))+'</p></section>'); continue
            name = 'GPT-SoVITS V2ProPlus / A' if backend['backend']=='gpt' else 'Qwen 3.1 / '+backend['voice']
            first = run['request_to_first_device_write_seconds']; gap = run['longest_software_gap_seconds']
            sound = run['request_to_estimated_first_sound_seconds']
            cards.append(f'<section><h2>{html.escape(name)} · 样本 {index+1}</h2><p>{html.escape(run["text"])}</p>'
                         f'<p>首音提交 {first:.3f} 秒 · 估计首有声 {sound:.3f} 秒 · 最长播放空档 {gap:.3f} 秒</p>'
                         '<p>实际播放时间线（插入测得的缓冲空档）</p>'
                         f'<audio controls src="{Path(run["playback_wav"]).name}"></audio>'
                         '<p>原始合成音频（保留模型停顿）</p>'
                         f'<audio controls src="{Path(run["source_wav"]).name}"></audio></section>')
    page = '<!doctype html><meta charset="utf-8"><title>语音对照试听</title><style>body{max-width:900px;margin:36px auto;padding:0 20px;font:16px/1.7 system-ui;background:#15131a;color:#eee}section{padding:20px;margin:20px 0;background:#24212c;border-radius:12px}audio{width:100%}h2{font-size:20px}</style>'
    page += '<h1>语音对照试听</h1><p>相同固定文字；GPT 已预热，Qwen 包含本次连接。首音提交为播放器写入估计，未经声卡回录。试听文件从首音频块开始，不包含请求等待。自然停顿与数据未及时到达造成的空档分别统计；音色请以试听判断。</p>' + ''.join(cards)
    (directory/'review.html').write_text(page,encoding='utf-8')
