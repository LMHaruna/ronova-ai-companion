"""Download the official inference weight and verify its published SHA256."""
from pathlib import Path
import hashlib
import json
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "v2Pro/s2Gv2ProPlus.pth"
TARGET = ROOT / "runtime/gpt-sovits/GPT_SoVITS/pretrained_models" / NAME
EXPECTED_SHA256 = "d42a22bbbf65fb2bbdd45ad6a66841156977db45c7aabe0a6992ff378d9c7d3b"
HF_URL = "https://huggingface.co/lj1995/GPT-SoVITS/resolve/336b2ec/" + NAME
SOURCE_PAGE = "https://huggingface.co/lj1995/GPT-SoVITS/blob/main/" + NAME

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    source_url = HF_URL
    started = time.perf_counter()
    if TARGET.exists():
        if sha256(TARGET) != EXPECTED_SHA256:
            raise RuntimeError("Existing weight does not match the official hash; preserved for inspection")
    else:
        partial = TARGET.with_name(TARGET.name + ".download-part")
        if "--modelscope" in sys.argv:
            print("Downloading from official ModelScope archive", flush=True)
            sys.path.insert(0, str(ROOT / "scripts"))
            from voice_inspect_modelscope import RemoteZip, URL
            source_url = URL
            with zipfile.ZipFile(RemoteZip(URL)) as archive:
                entry = archive.getinfo("pretrained_models/" + NAME)
                with archive.open(entry) as source, partial.open("wb") as output:
                    total = 0
                    while chunk := source.read(8 * 1024 * 1024):
                        output.write(chunk)
                        total += len(chunk)
                        print(f"Downloaded {total // (1024 * 1024)} MiB", flush=True)
        else:
            download_hf_or_modelscope(partial)
            source_url = SOURCE_USED[0]
        if sha256(partial) != EXPECTED_SHA256:
            raise RuntimeError("Downloaded weight failed the official SHA256 check; not installed")
        partial.replace(TARGET)
    report = {"source_page": SOURCE_PAGE, "source_url": source_url,
              "file": str(TARGET.relative_to(ROOT)), "bytes": TARGET.stat().st_size,
              "sha256": sha256(TARGET), "expected_sha256": EXPECTED_SHA256,
              "download_seconds": time.perf_counter() - started}
    (ROOT / "runtime/v2proplus-download-20261003.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)

SOURCE_USED = [HF_URL]

def download_hf_or_modelscope(partial):
        try:
            print("Downloading from official Hugging Face repository", flush=True)
            with urllib.request.urlopen(HF_URL, timeout=30) as response, partial.open("wb") as output:
                total = 0
                next_update = 16 * 1024 * 1024
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    total += len(chunk)
                    if total >= next_update:
                        print(f"Downloaded {total // (1024 * 1024)} MiB", flush=True)
                        next_update += 16 * 1024 * 1024
        except Exception as exc:
            print(f"Hugging Face unavailable ({type(exc).__name__}); using official ModelScope archive", flush=True)
            sys.path.insert(0, str(ROOT / "scripts"))
            from voice_inspect_modelscope import RemoteZip, URL
            SOURCE_USED[0] = URL
            with zipfile.ZipFile(RemoteZip(URL)) as archive:
                entry = archive.getinfo("pretrained_models/" + NAME)
                with archive.open(entry) as source, partial.open("wb") as output:
                    total = 0
                    while chunk := source.read(8 * 1024 * 1024):
                        output.write(chunk)
                        total += len(chunk)
                        print(f"Downloaded {total // (1024 * 1024)} MiB", flush=True)

if __name__ == "__main__":
    main()
