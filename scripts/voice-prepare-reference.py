"""Copy an explicitly selected, automatically transcribed interval into a reference WAV."""
from pathlib import Path
import argparse
import hashlib
import json
import math
import wave
import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True)
parser.add_argument("--start", type=float, required=True)
parser.add_argument("--end", type=float, required=True)
parser.add_argument("--text", required=True)
parser.add_argument("--id", required=True)
args = parser.parse_args()
source = ROOT / "voice-reference" / args.source
if source.resolve().parent != (ROOT / "voice-reference").resolve():
    raise ValueError("Source must be an original reference WAV")
with wave.open(str(source), "rb") as wav:
    sr, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
    if width != 2:
        raise ValueError("Expected 16-bit PCM source")
    original = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).reshape(-1, channels).astype(np.float64) / 32768
if not (0 <= args.start < args.end <= len(original) / sr):
    raise ValueError("Invalid reference interval")
clip = original[round(args.start * sr):round(args.end * sr)]
mono = clip.mean(axis=1)
if np.sqrt(np.mean(mono ** 2)) < np.max(np.sqrt(np.mean(clip ** 2, axis=0))) * 0.65:
    mono = clip[:, np.argmax(np.mean(clip ** 2, axis=0))]
    channel_method = "louder channel (phase cancellation guard)"
else:
    channel_method = "mean of source channels"
mono = resample_poly(mono, 32000 // math.gcd(sr, 32000), sr // math.gcd(sr, 32000))
peak = float(np.max(np.abs(mono)))
if peak < 1e-6:
    raise ValueError("Silent source")
gain = 10 ** (-3 / 20) / peak
mono *= gain
fade = min(round(0.005 * 32000), len(mono) // 2)
mono[:fade] *= np.linspace(0, 1, fade)
mono[-fade:] *= np.linspace(1, 0, fade)
target = ROOT / "voice-reference/prepared" / f"{args.id}.wav"
target.parent.mkdir(parents=True, exist_ok=True)
with wave.open(str(target), "wb") as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(32000)
    wav.writeframes(np.clip(np.round(mono * 32768), -32768, 32767).astype(np.int16).tobytes())
manifest = target.parent / "references.json"
data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"references": []}
data["references"] = [item for item in data["references"] if item["id"] != args.id]
data["references"].append({"id": args.id, "path": str(target.relative_to(ROOT)), "source": str(source.relative_to(ROOT)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "start": args.start, "end": args.end, "seconds": round(len(mono) / 32000, 3), "text": args.text, "transcript_status": "automatic ASR; not human verified", "channel_method": channel_method, "gain_db": round(20 * math.log10(gain), 3), "peak_target_dbfs": -3, "edge_fade_ms": 5, "source_unmodified": True})
manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(data["references"][-1], ensure_ascii=False, indent=2))
