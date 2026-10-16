"""Paced local prerecorded audio -> streaming ASR -> mock chat -> optional real TTS."""
from pathlib import Path
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import soundfile as sf
import webrtcvad
from scipy.signal import resample_poly
from voice_input import ChineseRecognizer, InputSession, InputConfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tts', action='store_true', help='Mock chat, real local GPT synthesis and waveOut playback')
    parser.add_argument('--model', choices=('small', 'standard'), default='standard')
    args = parser.parse_args()
    os.environ.pop('ALI_API_Key', None)
    models = {'small': 'sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01',
              'standard': 'sherpa-onnx-streaming-zipformer-ctc-zh-int8-2025-06-30'}
    r = ChineseRecognizer(models[args.model])
    voice = pipeline = None
    records, finals, starts, partials, turns = [], [], [], [], []
    def status(message): print('[local]', message, flush=True)
    if args.tts:
        from local_voice import LocalVoice
        from reply_pipeline import ReplyPipeline
        class LocalChat:
            metrics = {'source': 'offline_mock', 'network_requests': 0}
            def reply(self, text, on_token, cancel):
                result = '凡人，你的判断太轻率。'
                on_token(result)
                return result
            def cancel_transport(self): pass
            def close(self): pass
        voice = LocalVoice(status=status)
        voice.prepare()
        if not voice.ready.wait(60):
            voice.close()
            raise RuntimeError(voice.prepare_error or 'TTS preparation timed out')
        pipeline = ReplyPipeline(LocalChat(), voice, lambda text: None, status)
    def final(text, metrics):
        finals.append({'text': text, **metrics})
        if pipeline: turns.append(pipeline.submit(text, input_metrics=metrics))
    def speech_start():
        starts.append(True)
        if pipeline and pipeline.responding:
            pipeline.cancel()
            status('Prerecorded new speech cancelled the previous local reply')
    try:
        for name in ('A', 'B'):
            path = ROOT / f'voice-reference/prepared/{name}.wav'
            original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            pcm, rate = sf.read(path, dtype='float32', always_2d=True)
            mono = pcm.mean(axis=1)
            divisor = math.gcd(rate, 16000)
            audio = resample_poly(mono, 16000 // divisor, rate // divisor)
            audio = np.concatenate((np.zeros(4800), audio, np.zeros(25600)))
            samples = (np.clip(audio, -1, 1) * 32767).astype('<i2')
            session = InputSession(r, speech_start, partials.append, final, status)
            vad = webrtcvad.Vad(2)
            beginning = time.perf_counter()
            decode_times = []
            before = len(finals)
            for offset in range(0, len(samples), 320):
                frame = samples[offset:offset + 320]
                if len(frame) < 320: frame = np.pad(frame, (0, 320 - len(frame)))
                due = beginning + offset / 16000
                delay = due - time.perf_counter()
                if delay > 0: time.sleep(delay)
                started = time.perf_counter()
                data = frame.tobytes()
                session.process(data, vad.is_speech(data, 16000), due + .02)
                decode_times.append(time.perf_counter() - started)
            utterances = finals[before:]
            if not utterances: raise RuntimeError(f'{name}: no Chinese turn recognized')
            record = {'reference': name, 'reference_unchanged': hashlib.sha256(path.read_bytes()).hexdigest() == original_hash,
                      'audio_seconds_with_padding': len(samples) / 16000,
                      'elapsed_seconds': time.perf_counter() - beginning,
                      'max_frame_process_seconds': max(decode_times),
                      'processing_seconds': sum(decode_times), 'utterances': utterances}
            if pipeline:
                if not pipeline.wait_finished(60) or not voice.wait_idle(10):
                    raise RuntimeError('Local voice loop failed to drain')
                if pipeline.last_turn.error or pipeline.last_turn.voice_job.error:
                    raise RuntimeError('Local voice loop reported an error')
                record['local_loop'] = pipeline.timings()
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
        now = datetime.now(timezone(timedelta(hours=8)))
        result = {'date': now.date().isoformat(), 'asr_model': models[args.model],
                  'mode': 'paced prerecorded Chinese; no live microphone',
                  'cloud': 'offline mock only; zero network requests', 'real_device_playback': args.tts,
                  'speech_starts': len(starts), 'partial_updates': len(partials), 'records': records,
                  'loop_turns': [{'cancel_signal_set': turn.cancel.is_set(),
                                 'cancelled_during_voice': turn.voice_job.metrics.get('cancelled') if turn.voice_job else None,
                                 'error': turn.error,
                                 'text_finished': turn.done.is_set(),
                                 'voice_finished': turn.voice_job.finished.is_set() if turn.voice_job else None,
                                 'voice_error': turn.voice_job.error if turn.voice_job else None,
                                 'voice': dict(turn.voice_job.metrics) if turn.voice_job else None}
                                for turn in turns],
                  'live_conversation_verified': False, 'speaker_aec_verified': False}
        if any(turn.error or turn.voice_job and turn.voice_job.error for turn in turns):
            raise RuntimeError('A previous local turn failed')
        suffix = 'tts' if args.tts else 'asr'
        (ROOT / f'runtime/continuous-voice-{args.model}-{suffix}-{now:%Y%m%d}.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        if pipeline: pipeline.close()
        if voice: voice.close()


if __name__ == '__main__': main()
