"""Local ASR only; keep raw reference WAVs unchanged."""
from pathlib import Path
import json
import time
import math
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from faster_whisper import WhisperModel
from opencc import OpenCC

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "voice-reference/prepared"
OUTPUT.mkdir(parents=True, exist_ok=True)
model = WhisperModel(str(ROOT / "runtime/models/whisper-small"), device="cpu", compute_type="int8", cpu_threads=6, num_workers=1, local_files_only=True)
convert = OpenCC("t2s")
records = []
for source in sorted((ROOT / "voice-reference").glob("录音*.wav")):
    started = time.perf_counter()
    # Decode PCM directly: newer PyAV removed metadata_errors used by this Whisper release.
    pcm, rate = sf.read(source, dtype="float32", always_2d=True)
    mono = pcm.mean(axis=1)
    if np.sqrt(np.mean(mono ** 2)) < np.max(np.sqrt(np.mean(pcm ** 2, axis=0))) * 0.65:
        mono = pcm[:, np.argmax(np.mean(pcm ** 2, axis=0))]
    divisor = math.gcd(rate, 16000)
    audio = resample_poly(mono, 16000 // divisor, rate // divisor).astype(np.float32)
    segments, info = model.transcribe(audio, language="zh", beam_size=5, vad_filter=True, condition_on_previous_text=False, word_timestamps=True)
    items = []
    for segment in segments:
        item = {"start": round(segment.start, 3), "end": round(segment.end, 3), "text": convert.convert(segment.text.strip()), "avg_logprob": round(segment.avg_logprob, 4), "no_speech_prob": round(segment.no_speech_prob, 4), "words": [{"start": word.start, "end": word.end, "text": convert.convert(word.word), "probability": word.probability} for word in (segment.words or [])]}
        items.append(item)
        print(source.name, item["start"], item["end"], item["text"], flush=True)
    records.append({"source": str(source.relative_to(ROOT)), "duration": info.duration, "asr_seconds": round(time.perf_counter() - started, 3), "segments": items})
    (OUTPUT / "transcripts.json").write_text(json.dumps({"method": "faster-whisper-small, CPU int8, beam5, Chinese; automatic transcript, not human verified", "recordings": records}, ensure_ascii=False, indent=2), encoding="utf-8")
print("Transcription finished.", flush=True)
