"""Download public Chinese streaming ASR assets, never microphone data or keys."""
from pathlib import Path
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import tarfile
import time
import requests

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"small": "sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01",
          "standard": "sherpa-onnx-streaming-zipformer-ctc-zh-int8-2025-06-30"}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', choices=MODELS, default='standard')
    args = parser.parse_args()
    name = MODELS[args.model]
    url = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{name}.tar.bz2"
    os.environ.pop("ALI_API_Key", None)
    base = ROOT / "runtime/models"
    base.mkdir(parents=True, exist_ok=True)
    archive = base / (name + ".tar.bz2")
    digest = hashlib.sha256()
    started = time.perf_counter()
    size = 0
    with requests.get(url, stream=True, timeout=(15, 60)) as response:
        response.raise_for_status()
        with archive.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    with tarfile.open(archive, "r:bz2") as source:
        for member in source.getmembers():
            target = (base / member.name).resolve()
            if not target.is_relative_to(base.resolve()) or member.issym() or member.islnk():
                raise RuntimeError("Unsafe model archive member")
        source.extractall(base, filter="data")
    now = datetime.now(timezone(timedelta(hours=8)))
    manifest = {"date": now.date().isoformat(), "model": name, "source": url, "archive_bytes": size,
                "archive_sha256": digest.hexdigest(), "seconds": time.perf_counter() - started,
                "files": {str(p.relative_to(base / name)): {"bytes": p.stat().st_size,
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in (base / name).rglob("*") if p.is_file()}}
    (ROOT / f"runtime/asr-model-{args.model}-{now:%Y%m%d}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "files"}, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
