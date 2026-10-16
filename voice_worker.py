"""Persistent local voice worker with automatic CUDA selection and CPU fallback."""
from pathlib import Path
import gc
import json
import os
import queue
import re
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / "runtime/gpt-sovits"
TRANSPORT = sys.stdout
sys.stdout = sys.stderr
LOCK = threading.Lock()
THREADS_CONFIGURED = False
SYNTHESIZE_ON_MAIN_THREAD = False


def emit(event, job=None, **values):
    with LOCK:
        TRANSPORT.write(json.dumps({"event": event, "job": job, **values}, ensure_ascii=False) + "\n")
        TRANSPORT.flush()


def initialize(force_cpu=False):
    global THREADS_CONFIGURED
    started = time.perf_counter()
    selection = json.loads((ROOT / "config/voice-selection.json").read_text(encoding="utf-8"))
    version = selection["version"]
    if version != "v2ProPlus":
        raise ValueError("Unsupported local voice model selection")
    weights = "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"
    if not (PROJECT / weights).is_file():
        raise FileNotFoundError(f"Voice weight missing: {weights}")
    cache = ROOT / "runtime/chat-voice-cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "OMP_NUM_THREADS": "6",
                       "language": "zh_CN", "version": version, "MPLCONFIGDIR": str(ROOT / "runtime/matplotlib-cache"),
                       "NLTK_DATA": str(ROOT / "runtime/gpt-nltk-data"),
                       "TEMP": str(cache), "TMP": str(cache), "TOKENIZERS_PARALLELISM": "false",
                       "HF_HOME": str(cache / "huggingface"),
                       "TORCH_HOME": str(cache / "torch"),
                       "TORCH_EXTENSIONS_DIR": str(cache / "torch-extensions"),
                       "TORCHINDUCTOR_CACHE_DIR": str(cache / "torchinductor"),
                       "NUMBA_CACHE_DIR": str(cache / "numba")})
    os.environ["PATH"] = str(PROJECT) + os.pathsep + os.environ["PATH"]
    os.chdir(PROJECT)
    sys.path[:0] = [str(PROJECT), str(PROJECT / "GPT_SoVITS")]
    emit("status", message="正在准备本地语音运行库")
    import torch
    if not THREADS_CONFIGURED:
        torch.set_num_threads(6)
        torch.set_num_interop_threads(2)
        THREADS_CONFIGURED = True
    import jieba
    import jieba.posseg
    jieba.dt.tmp_dir = str(cache)
    sys.modules["jieba_fast"] = jieba
    sys.modules["jieba_fast.posseg"] = jieba.posseg
    from TTS_infer_pack.TTS import TTS
    from voice_acceleration import inference_device
    requested = 'cpu' if force_cpu else os.environ.get('RONOVA_VOICE_DEVICE', selection.get('device', 'auto'))
    acceleration = inference_device(torch, requested)
    cpu_threads = int(os.environ.get('RONOVA_VOICE_THREADS', '6'))
    if not 1 <= cpu_threads <= 16:
        raise ValueError('本地语音线程数必须在 1–16 之间')
    torch.set_num_threads(cpu_threads)
    acceleration['cpu_threads'] = cpu_threads
    if acceleration['device'] == 'cuda' and os.environ.get('RONOVA_VOICE_HALF') == '0':
        acceleration['is_half'] = False
    mode = '显卡' if acceleration['device'] == 'cuda' else 'CPU'
    emit("status", message=f"正在使用{mode}加载角色音色并静默预热")
    model = TTS({"custom": {
        "device": acceleration['device'], "is_half": acceleration['is_half'], "version": version,
        "t2s_weights_path": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "vits_weights_path": weights,
        "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base"}})
    if model.configs.version != version:
        raise RuntimeError("Loaded voice model does not match the requested version")
    from voice_features import ReferenceSpeakerCache
    speaker_cache = ReferenceSpeakerCache(model.sv_model.compute_embedding3)
    model.sv_model.compute_embedding3 = speaker_cache
    model.reference_speaker_cache = speaker_cache
    refs = json.loads((ROOT / "voice-reference/prepared/references.json").read_text(encoding="utf-8"))
    reference = next(ref for ref in refs["references"] if ref["id"] == selection.get("reference_id", "A"))
    return model, reference, time.perf_counter() - started, acceleration


def parameters(reference, text):
    return {"text": text, "text_lang": "all_zh", "ref_audio_path": str(ROOT / reference["path"]),
            "prompt_text": reference["text"], "prompt_lang": "all_zh", "top_k": 15, "top_p": 1,
            "temperature": 1, "text_split_method": "cut5", "batch_size": 1, "speed_factor": 1,
            "fragment_interval": .12, "seed": 20261003, "parallel_infer": True,
            "repetition_penalty": 1.35, "return_fragment": True, "streaming_mode": False}


def main():
    # Preserve the Windows fix: native libraries initialize on the main thread
    # BEFORE any other thread starts reading stdin or using these libraries.
    first = sys.stdin.readline()
    if not first:
        return
    first_task = json.loads(first)
    if first_task.get("action") != "prepare":
        return
    model = None
    prepare_started = time.perf_counter()
    try:
        # Failed CUDA model loads or warmups (including low VRAM) retry once on
        # CPU. Run this on the main thread before starting protocol threads.
        for force_cpu in (False, True):
            try:
                model, reference, initialize_seconds, acceleration = initialize(force_cpu)
                import numpy as np
                warm_started = time.perf_counter()
                nonzero = False
                for sr, data in model.run(parameters(reference, "凡人，坐下。")):
                    nonzero = nonzero or bool(np.any(data))
                if not nonzero:
                    raise RuntimeError("Warmup did not produce speech")
                break
            except Exception as error:
                # Retry once using CPU kernels from the same CUDA package.
                import torch
                if force_cpu or torch.version.cuda is None or os.environ.get('RONOVA_VOICE_DEVICE') == 'cpu':
                    raise
                fallback_reason = type(error).__name__
                model = None
                gc.collect()
                try: torch.cuda.empty_cache()
                except Exception: pass
                emit('status', message='显卡语音准备失败，正在切换 CPU')
        if force_cpu:
            acceleration['fallback_reason'] = fallback_reason
        if acceleration['device'] == 'cuda':
            import torch
            acceleration['gpu_memory_peak_mb'] = torch.cuda.max_memory_allocated()/1024**2
        speaker_cache = model.reference_speaker_cache
        emit("ready", initialize_seconds=initialize_seconds,
             warmup_seconds=time.perf_counter() - warm_started,
             prepare_seconds=time.perf_counter() - prepare_started,
             **acceleration,
             model_version=model.configs.version, vits_weights_path=model.configs.vits_weights_path,
             engine="gpt", reference_id=reference["id"], reference_audio=reference["path"],
             reference_warmup=True, reference_speaker_cache=True,
             speaker_cache_hits=speaker_cache.hits, speaker_cache_misses=speaker_cache.misses)
    except Exception as exc:
        # Startup tracebacks contain local model paths, never chat or keys.
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit("boot_error", message=f"语音准备失败：{type(exc).__name__}")
        return

    tasks = queue.Queue(maxsize=128)
    states = {}
    state_lock = threading.Lock()
    current = [None]

    def run():
        while True:
            task = tasks.get()
            if task is None:
                return
            job = task["job"]
            with state_lock:
                state = states.get(job)
                if state is None:
                    continue
                current[0] = job
            if task["action"] == "finish":
                if not state["cancelled"] and not state["nonzero"]:
                    emit("error", job, message="本轮没有可播放的语音")
                else:
                    emit("done", job, cancelled=state["cancelled"], fragments=state["count"],
                         device=acceleration['device'], is_half=acceleration['is_half'],
                         generation_seconds=state["generation_seconds"], load_seconds=0.0,
                         first_fragment_seconds=state["first_fragment_seconds"], segments=state["segments"],
                         queue_wait_seconds=state["queue_wait_seconds"],
                         speaker_cache_hits=state["speaker_cache_hits"],
                         speaker_cache_misses=state["speaker_cache_misses"])
                with state_lock:
                    states.pop(job, None)
                    current[0] = None
                continue
            if not state["cancelled"] and not state["failed"]:
                started = time.perf_counter()
                state["queue_wait_seconds"] += max(0.0, started - task["queued"])
                cache_hits, cache_misses = speaker_cache.hits, speaker_cache.misses
                if hasattr(model, 'set_cancel_check'):
                    model.set_cancel_check(lambda: state['cancelled'])
                generator = model.run(parameters(reference, task["text"]))
                try:
                    for sr, data in generator:
                        if state["cancelled"]:
                            break
                        pcm = np.asarray(data, dtype=np.int16)
                        if not pcm.size:
                            continue
                        audible = bool(np.any(pcm))
                        state["nonzero"] |= audible
                        if audible and state["first_fragment_seconds"] is None:
                            state["first_fragment_seconds"] = time.perf_counter() - state["created"]
                        path = ROOT / "runtime/chat-voice-tmp" / job / f"{state['count']:04d}.wav"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with wave.open(str(path), "wb") as wav:
                            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sr)
                            wav.writeframes(pcm.tobytes())
                        emit("fragment", job, index=state["count"], path=str(path), sample_rate=sr,
                             audio_seconds=pcm.size / sr, silent=not audible)
                        state["count"] += 1
                    state["segments"] += 1
                except Exception as exc:
                    if not state["cancelled"]:
                        state["failed"] = True
                        emit("error", job, terminal=False, message=f"本地合成失败：{type(exc).__name__}")
                finally:
                    generator.close()
                    state["generation_seconds"] += time.perf_counter() - started
                    state["speaker_cache_hits"] += speaker_cache.hits - cache_hits
                    state["speaker_cache_misses"] += speaker_cache.misses - cache_misses
            with state_lock:
                current[0] = None

    def read_controls():
        for line in sys.stdin:
            try:
                task = json.loads(line)
                action = task["action"]
                if action == "close":
                    break
                if action == "prepare":
                    emit("ready", initialize_seconds=0.0, warmup_seconds=0.0)
                    continue
                job = task["job"]
                if not re.fullmatch(r"[0-9a-f]{32}", job):
                    continue
                with state_lock:
                    if action == "begin":
                        states[job] = {"created": time.perf_counter(),
                                       "cancelled": False, "failed": False, "nonzero": False, "count": 0,
                                       "segments": 0, "generation_seconds": 0.0, "first_fragment_seconds": None,
                                       "queue_wait_seconds": 0.0, "speaker_cache_hits": 0,
                                       "speaker_cache_misses": 0}
                    elif action == "cancel" and job in states:
                        states[job]["cancelled"] = True
                        if current[0] == job:
                            model.stop()
                    elif action in ("append", "finish") and job in states:
                        if action == "append" and (not isinstance(task.get("text"), str) or not task["text"].strip()):
                            continue
                        task["queued"] = time.perf_counter()
                        # Do not block the stdin reader: it also carries urgent cancellation.
                        try:
                            tasks.put_nowait(task)
                        except queue.Full:
                            states[job]["cancelled"] = True
                            if current[0] == job:
                                model.stop()
                            emit("error", job, terminal=False, message="语音队列已满，文字保留")
                            if action == "finish":
                                # Acknowledgement is essential for safe cleanup. Defer it
                                # instead of blocking cancellation on the reader thread.
                                threading.Thread(target=tasks.put, args=(task,), daemon=True).start()
            except (ValueError, KeyError, TypeError):
                continue

        # Release the protocol thread before native runtimes shut down on Windows.
        with state_lock:
            for state in states.values():
                state["cancelled"] = True
            if current[0] is not None:
                model.stop()
        if SYNTHESIZE_ON_MAIN_THREAD:
            tasks.put(None)  # Reader has stopped; queued cancelled tasks drain promptly.
        else:
            try:
                tasks.put_nowait(None)
            except queue.Full:
                pass  # The owner retains a bounded forced-exit fallback.

    if SYNTHESIZE_ON_MAIN_THREAD:
        reader = threading.Thread(target=read_controls, daemon=True)
        reader.start()
        run()
        reader.join(timeout=1)
    else:
        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        read_controls()
        runner.join(timeout=5)


if __name__ == "__main__":
    main()
