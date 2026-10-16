"""Download only selected entries from the official ModelScope deployment ZIP."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import hashlib
import json
import zipfile
from voice_inspect_modelscope import RemoteZip, URL

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "runtime/gpt-sovits"
NAMES = [
    "chinese-hubert-base/config.json", "chinese-hubert-base/preprocessor_config.json", "chinese-hubert-base/pytorch_model.bin",
    "chinese-roberta-wwm-ext-large/config.json", "chinese-roberta-wwm-ext-large/tokenizer.json", "chinese-roberta-wwm-ext-large/pytorch_model.bin",
    "s1v3.ckpt", "v2Pro/s2Gv2ProPlus.pth", "sv/pretrained_eres2netv2w24s4ep4.ckpt",
]


def download(name):
    target = PROJECT / "GPT_SoVITS/pretrained_models" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with zipfile.ZipFile(RemoteZip(URL)) as archive:
        entry = archive.getinfo("pretrained_models/" + name)
        if not target.exists() or target.stat().st_size != entry.file_size:
            partial = target.with_name(target.name + ".ms-part")
            with archive.open(entry) as source, partial.open("wb") as output:
                while chunk := source.read(8 * 1024 * 1024):
                    output.write(chunk)
            partial.replace(target)
        with target.open("rb") as asset:
            for chunk in iter(lambda: asset.read(1024 * 1024), b""):
                digest.update(chunk)
    print("Ready:", name, flush=True)
    return {"path": str(target.relative_to(ROOT)), "bytes": target.stat().st_size, "sha256": digest.hexdigest(), "zip_crc32": entry.CRC}


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = [future.result() for future in as_completed([pool.submit(download, name) for name in NAMES])]
    (ROOT / "runtime/modelscope-download-manifest.json").write_text(json.dumps({"official_source": URL, "files": results}, indent=2), encoding="utf-8")
