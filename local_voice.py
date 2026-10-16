"""Prepared GPT synthesis with immediate sentence playback and drained PCM tails."""
from collections import deque
from pathlib import Path
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from echo_control import PlaybackReference

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / "runtime/voice-venv/Scripts/python.exe"
TTS_ENGINES = ('gpt-sovitsv2proplus', 'indextts-2.5')


def validate_resources(tts_engine='gpt-sovitsv2proplus'):
    if tts_engine not in TTS_ENGINES: raise ValueError('语音合成引擎无效')
    selection = json.loads((ROOT / "config/voice-selection.json").read_text(encoding="utf-8"))
    reference_audio = ROOT / selection.get("reference_audio", "voice-reference/prepared/A.wav")
    if tts_engine=='indextts-2.5':
        package = ROOT / 'runtime/indextts-2.5'
        paths = (PYTHON, ROOT/'index_voice_worker.py', reference_audio,
                 ROOT/'index_voice_backend.py', package/'source/indextts/infer_v2_5.py',
                 ROOT/'index_decode_graph.py',
                 package/'models/config.yaml', package/'models/gpt.pth',
                 package/'models/codec.pth', package/'models/s2mel.pth',
                 package/'models/feat1.pt', package/'models/feat2.pt',
                 package/'models/wav2vec2bert_stats.pt',
                 package/'models/multilingual_zh_ja_yue_char_del.tiktoken',
                 package/'models/hf_cache/campplus_cn_common.bin',
                 package/'models/hf_cache/w2v-bert-2.0/model.safetensors',
                 package/'models/hf_cache/bigvgan/bigvgan_generator.pt')
    else:
        paths = (PYTHON, ROOT / "voice_worker.py", reference_audio,
                 ROOT / "runtime/gpt-sovits/GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth")
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"{tts_engine} 缺少资源：{path.relative_to(ROOT)}")


class SpeechJob:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.created = time.perf_counter()
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.input_closed = False
        self.metrics = {"job": self.id, "fragment_order": [], "playback_calls": 0, "audio_seconds": 0.0,
                        "playback_strategy": "sentence_stream"}
        self.error = None


class LocalVoice:
    def __init__(self, status=None, on_fragment=None, player=None, tts_engine='gpt-sovitsv2proplus'):
        validate_resources(tts_engine)
        self.tts_engine = tts_engine
        self.python = PYTHON
        self.worker = ROOT/'index_voice_worker.py' if tts_engine=='indextts-2.5' else ROOT/'voice_worker.py'
        self.engine = "gpt"
        self.status = status or (lambda message: None)
        self.on_fragment = on_fragment
        self._player_override = player
        self.pcm_factory = None
        self._process = None
        self._jobs = {}
        self._write_lock = threading.RLock()
        self._jobs_lock = threading.RLock()
        self._queue = queue.Queue()
        self._stderr = deque(maxlen=8)
        self._closed = False
        self.ready = threading.Event()
        self.prepare_metrics = {}
        self.prepare_error = None
        self._prepare_started = None
        self._ready_at = None
        self._read_threads = []
        self._pcm = None
        self._pcm_job = None
        self._pcm_lock = threading.RLock()
        self._echo_reference = PlaybackReference()
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._play_thread.start()

    def _start(self):
        if self._process and self._process.poll() is None:
            return False
        if self._closed:
            raise RuntimeError("Voice service is closed")
        validate_resources(self.tts_engine)
        if self._process:
            self._dispose_streams(self._process)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        env = {name: value for name, value in env.items()
               if 'api_key' not in name.casefold() and 'api_token' not in name.casefold()}
        self.ready.clear()
        self._ready_at = None
        self.prepare_error = None
        self._prepare_started = time.perf_counter()
        self._process = subprocess.Popen([str(self.python), "-s", "-u", str(self.worker)], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process = self._process
        def logs():
            for line in process.stderr:
                self._stderr.append(line.strip())
                if os.getenv("RONOVA_VOICE_DEBUG") == "1":
                    self.status("[诊断] " + line.rstrip())
        self._read_threads = [threading.Thread(target=logs, daemon=True),
                              threading.Thread(target=self._read, args=(process,), daemon=True)]
        for thread in self._read_threads:
            thread.start()
        return True

    def _dispose_streams(self, process):
        for thread in self._read_threads:
            thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
        self._read_threads = []

    def _send(self, value):
        with self._write_lock:
            self._process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self._process.stdin.flush()

    def prepare(self):
        with self._write_lock:
            if self._start():
                self._send({"action": "prepare"})
        return self.ready

    def begin(self):
        self.stop()
        self.prepare()
        job = SpeechJob()
        job.worker_process = self._process
        with self._jobs_lock:
            self._jobs[job.id] = job
            job.metrics['prepare_wait_seconds'] = max(0.0, self._ready_at-job.created) if self._ready_at is not None else None
        job.metrics["engine"] = self.engine
        job.metrics["tts_engine"] = self.tts_engine
        self._send({"action": "begin", "job": job.id})
        return job

    def append(self, job, text):
        if job.cancelled.is_set() or job.finished.is_set() or job.input_closed or not text.strip():
            return
        self._send({"action": "append", "job": job.id, "text": text})

    def end(self, job):
        if not job.input_closed and not job.finished.is_set():
            job.input_closed = True
            self._send({"action": "finish", "job": job.id})

    def speak(self, text):
        job = self.begin()
        self.append(job, text)
        self.end(job)
        return job

    def _read(self, process):
        for line in process.stdout:
            if process is not self._process:
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            kind = event.get("event")
            if kind == "ready":
                self._ready_at = time.perf_counter()
                self.prepare_metrics = {k: v for k, v in event.items() if k not in ("event", "job")}
                self.prepare_metrics["ready_seconds"] = time.perf_counter() - self._prepare_started
                self.ready.set()
                with self._jobs_lock:
                    for job in self._jobs.values():
                        if getattr(job, 'worker_process', None) is process:
                            job.metrics['prepare_wait_seconds'] = max(0.0, self._ready_at-job.created)
                self.status("语音已准备好")
                continue
            if kind == "boot_error":
                self.prepare_error = event["message"]
                self.status(event["message"] + "；文字聊天可继续")
            with self._jobs_lock:
                job = self._jobs.get(event.get("job"))
            if kind == "status":
                if job is None or not job.cancelled.is_set():
                    self.status(event["message"])
            elif job is not None:
                if kind == "done":
                    job.metrics["worker_done_received_seconds"] = time.perf_counter() - job.created
                self._queue.put(event)
        if not self._closed:
            with self._jobs_lock:
                jobs = [job for job in self._jobs.values()
                        if getattr(job, "worker_process", process) is process]
            for job in jobs:
                if job.cancelled.is_set():
                    self._queue.put({"event": "done", "job": job.id, "cancelled": True})
                    continue
                self._queue.put({"event": "error", "job": job.id,
                                 "terminal": True,
                                 "message": self.prepare_error or "本地语音进程已退出，文字保留；可重试"})

    def _finish(self, job):
        path = ROOT / "runtime/chat-voice-tmp" / job.id
        if path.exists():
            base = (ROOT / "runtime/chat-voice-tmp").resolve()
            if (not base.is_relative_to(ROOT.resolve()) or path.resolve().parent != base
                    or path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)):
                raise RuntimeError("Refusing to remove an unexpected temporary directory")
            shutil.rmtree(path)
        job.metrics["total_seconds"] = time.perf_counter() - job.created
        job.metrics["cancelled"] = job.cancelled.is_set()
        job.finished.set()
        with self._jobs_lock:
            self._jobs.pop(job.id, None)

    def _finish_pcm(self, job):
        with self._pcm_lock:
            player = self._pcm if self._pcm_job == job.id else None
        if player:
            try:
                player.finish()
                job.metrics.update(estimated_underruns=player.underruns,
                                   estimated_longest_gap_seconds=player.longest_gap_seconds,
                                   submitted_frames=player.submitted_frames,
                                   completed_frames=player.completed_frames,
                                   playback_drained=not job.cancelled.is_set() and not player.pending,
                                   playback_complete_seconds=time.perf_counter() - job.created)
                padding=getattr(player,'padding_frames',0)
                continuity=getattr(player,'continuity_silence_frames',0)
                source_frames=player.submitted_frames-padding-continuity
                job.metrics.update(padding_frames=padding,continuity_silence_frames=continuity,
                    source_submitted_frames=source_frames,
                    source_completed_frames=getattr(player,'source_completed_frames',min(player.completed_frames,source_frames)))
                if getattr(player,'continuity_error',None):
                    job.metrics['continuity_error']=player.continuity_error
            finally:
                with self._pcm_lock:
                    self._pcm = self._pcm_job = None

    def _play_fragment(self, job, event):
        if job.cancelled.is_set():
            return
        path = Path(event["path"])
        if self.on_fragment:
            self.on_fragment(event)
        if job.cancelled.is_set():
            return
        if self._player_override:
            self._player_override(path, event["audio_seconds"], job.cancelled)
        else:
            from pcm_player import PCMPlayer
            with self._pcm_lock:
                if self._pcm is None:
                    options = {'keep_alive':True} if self.tts_engine=='indextts-2.5' else {}
                    self._pcm = (self.pcm_factory or PCMPlayer)(job.cancelled, reference=self._echo_reference, **options)
                    self._pcm_job = job.id
                player = self._pcm
            if self._pcm_job != job.id:
                raise RuntimeError("Previous utterance did not finish")
            if not player.write(path):
                return
        if not event.get("silent"):
            job.metrics.setdefault("first_playback_seconds", time.perf_counter() - job.created)
        job.metrics["fragment_order"].append(event["index"])
        job.metrics["audio_seconds"] += event["audio_seconds"]
        job.metrics["playback_calls"] += 1

    def _play_loop(self):
        while True:
            event = self._queue.get()
            if event is None:
                return
            with self._jobs_lock:
                job = self._jobs.get(event.get("job"))
            if job is None:
                continue
            kind = event["event"]
            try:
                if kind == "fragment":
                    if job.cancelled.is_set():
                        continue
                    path = Path(event["path"]).resolve()
                    if path.parent != (ROOT / "runtime/chat-voice-tmp" / job.id).resolve():
                        raise RuntimeError("Unexpected audio path")
                    self._play_fragment(job, event)
                elif kind in ("done", "error"):
                    if kind == "error":
                        job.error = event["message"]
                        job.cancelled.set()
                        self.status("语音失败，文字保留：" + job.error)
                        try:
                            self._send({"action": "cancel", "job": job.id})
                            self.end(job)
                        except (OSError, ValueError, AttributeError):
                            pass
                        # Worker errors are provisional until it acknowledges finish.
                        # It may still be closing its generator or writing the last file.
                        if not event.get("terminal", True):
                            self._finish_pcm(job)
                            continue
                    else:
                        job.metrics.update({k: v for k, v in event.items() if k not in ("event", "job", "cancelled")})
                        job.metrics["synthesis_complete_seconds"] = time.perf_counter() - job.created
                    self._finish_pcm(job)
                    self._finish(job)
            except Exception as exc:
                job.error = f"播放失败：{type(exc).__name__}"
                job.cancelled.set()
                self.status(job.error + "；文字已保留")
                try:
                    self._send({"action": "cancel", "job": job.id})
                    self.end(job)
                except (OSError, ValueError, AttributeError):
                    pass
                self._finish_pcm(job)
                if kind in ("done", "error") and (kind == "done" or event.get("terminal", True)):
                    # The worker already acknowledged completion; no later event
                    # can release this job after a buffered playback failure.
                    self._finish(job)
                # Otherwise wait for worker's done before deleting files it may still write.

    def stop(self):
        with self._jobs_lock:
            jobs = [job for job in self._jobs.values() if not job.finished.is_set()]
        for job in jobs:
            job.cancelled.set()
            try:
                self._send({"action": "cancel", "job": job.id})
                self.end(job)
            except (OSError, ValueError, AttributeError):
                pass
        with self._pcm_lock:
            if self._pcm:
                self._pcm.stop()

    @property
    def preparing(self):
        return (not self._closed and self._process is not None and self._process.poll() is None
                and not self.ready.is_set() and self.prepare_error is None)

    @property
    def playback_active(self):
        """Include drain and short gaps so speaker input cannot hear our reply."""
        with self._pcm_lock:
            return self._pcm is not None and not self._pcm.cancel.is_set()

    def capture_reference(self, captured):
        with self._pcm_lock:
            player = self._pcm
        if player:
            player.poll_reference()
        return self._echo_reference.read(captured)

    @property
    def playback_level(self):
        with self._pcm_lock: player=self._pcm
        return getattr(player,'playback_level',0.0) if player is not None else 0.0

    def wait_idle(self, timeout=None):
        deadline = None if timeout is None else time.perf_counter() + timeout
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            remaining = None if deadline is None else max(0.0, deadline - time.perf_counter())
            if not job.finished.wait(remaining):
                return False
        return True

    def release_worker(self):
        """Release model VRAM while retaining the lazy service and PCM queue."""
        self.stop()
        if self._process:
            try:
                self._send({"action": "close"})
                self._process.wait(timeout=10)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                if self._process.poll() is None and sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                elif self._process.poll() is None:
                    self._process.kill()
                self._process.wait(timeout=5)
        with self._pcm_lock:
            if self._pcm:
                self._pcm.close()
                self._pcm = None
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            self._finish(job)
        if self._process:
            self._dispose_streams(self._process)
        self._process = None
        self.ready.clear()
        self.prepare_metrics = {}
        self.prepare_error = None
        self._ready_at = None

    def close(self):
        if self._closed: return
        self._closed = True
        self.release_worker()
        self._queue.put(None)
        self._play_thread.join(timeout=5)
