"""Bounded realtime PCM playback with the same drain and echo reference as GPT."""
import queue
import threading
import time
from echo_control import PlaybackReference
from local_voice import SpeechJob
from pcm_player import PCMPlayer
from qwen_realtime import QwenRealtime, RealtimeConfig

READ_INSTRUCTIONS = '你是中文朗读器。只逐字朗读用户提供的文字，保持标点停顿，不增删、不解释。'


class RealtimeVoice:
    engine = 'qwen'

    def __init__(self, status=None, player_factory=None, on_pcm=None, config=None):
        self.status = status or (lambda text: None)
        self.player_factory = player_factory or PCMPlayer
        self.on_pcm = on_pcm
        self.config = config
        self.ready = threading.Event()
        self.prepare_error = None
        self.prepare_metrics = {}
        self._lock = threading.RLock()
        self._job = None
        self._player = None
        self._closed = False
        self._reference = PlaybackReference()

    def prepare(self):
        config = self.config or RealtimeConfig.load()
        config.validate()
        self.prepare_metrics = {'model': config.model, 'voice': config.voice,
                                'configuration_checked': True, 'network_prewarm': False}
        self.ready.set()
        return self.ready

    def begin(self):
        self.stop()
        if not self.wait_idle(2):
            raise RuntimeError('上一轮实时声音尚未停止')
        self.prepare()
        with self._lock:
            if self._closed: raise RuntimeError('Voice service is closed')
            job = SpeechJob()
            job.metrics.update(engine='qwen', playback_strategy='native_pcm_stream')
            job.audio_queue = queue.Queue(maxsize=32)
            job.play_thread = threading.Thread(target=self._play, args=(job,), daemon=True)
            self._job = job
            job.play_thread.start()
            return job

    def append_pcm(self, job, pcm, rate=24000):
        if rate != 24000 or not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError('Qwen output must be mono 24kHz 16-bit PCM')
        if job.input_closed or job.finished.is_set() or job.cancelled.is_set(): return
        job.metrics.setdefault('first_fragment_seconds', time.perf_counter() - job.created)
        if self.on_pcm: self.on_pcm(job, pcm, rate)
        # At most 3.2s queue plus 1.5s waveOut buffers; no per-chunk files.
        for offset in range(0, len(pcm), 4800):
            self._put(job, pcm[offset:offset + 4800])

    @staticmethod
    def _put(job, value):
        while not job.cancelled.is_set() and not job.finished.is_set():
            try:
                job.audio_queue.put(value, timeout=.05)
                return
            except queue.Full: pass

    def append(self, job, text):
        raise RuntimeError('Qwen 直接输出语音，不能再次逐句合成')

    def end(self, job):
        if not job.input_closed:
            job.input_closed = True
            job.metrics['synthesis_complete_seconds'] = time.perf_counter() - job.created
            self._put(job, None)

    def _play(self, job):
        player = None
        try:
            while not job.cancelled.is_set():
                try: pcm = job.audio_queue.get(timeout=.1)
                except queue.Empty: continue
                if pcm is None: break
                with self._lock:
                    if job.cancelled.is_set(): break
                    if player is None:
                        player = self.player_factory(job.cancelled, reference=self._reference)
                        self._player = player
                if not player.write_pcm(pcm, 24000): break
                job.metrics.setdefault('first_playback_seconds', time.perf_counter() - job.created)
                job.metrics['playback_calls'] += 1
                job.metrics['audio_seconds'] += len(pcm) / 48000
            if player:
                player.finish()
                job.metrics.update(estimated_underruns=player.underruns,
                    estimated_longest_gap_seconds=player.longest_gap_seconds,
                    submitted_frames=player.submitted_frames, completed_frames=player.completed_frames,
                    playback_drained=not job.cancelled.is_set() and not player.pending,
                    playback_complete_seconds=time.perf_counter() - job.created)
                padding=getattr(player,'padding_frames',0)
                job.metrics.update(padding_frames=padding,
                    source_submitted_frames=player.submitted_frames-padding,
                    source_completed_frames=min(player.completed_frames,player.submitted_frames-padding))
                job.metrics['playback_write_events'] = list(player.write_events)
        except Exception as exc:
            job.error = f'实时声音播放失败（{type(exc).__name__}）'
            job.cancelled.set()
            self.status(job.error)
        finally:
            if player:
                try: player.close()
                except Exception: pass
            with self._lock:
                if self._job is job: self._player = None
            while True:
                try: job.audio_queue.get_nowait()
                except queue.Empty: break
            job.metrics.update(total_seconds=time.perf_counter() - job.created, cancelled=job.cancelled.is_set())
            job.finished.set()

    def speak(self, text):
        job = self.begin()
        client = QwenRealtime(config=self.config, instructions=READ_INSTRUCTIONS)
        job.synth_client = client
        def run():
            try:
                client.reply(text, cancel=job.cancelled,
                             on_audio=lambda pcm, rate: self.append_pcm(job, pcm, rate))
            except Exception as exc:
                if not job.cancelled.is_set():
                    job.error = f'实时朗读失败（{type(exc).__name__}）'
                    self.status(job.error)
                    job.cancelled.set()
            finally:
                self.end(job)
                client.close()
        job.synth_thread = threading.Thread(target=run, daemon=True)
        job.synth_thread.start()
        return job

    def stop(self):
        with self._lock:
            job, player = self._job, self._player
            if job and not job.finished.is_set():
                job.cancelled.set()
                if hasattr(job, 'synth_client'): job.synth_client.cancel_transport()
            if player: player.stop()

    @property
    def playback_active(self):
        with self._lock:
            return self._player is not None and self._job is not None and not self._job.cancelled.is_set()

    def capture_reference(self, captured):
        with self._lock:
            if self._player: self._player.poll_reference()
        return self._reference.read(captured)

    @property
    def playback_level(self):
        with self._lock: player=self._player
        return getattr(player,'playback_level',0.0) if player is not None else 0.0

    def wait_idle(self, timeout=None):
        with self._lock: job = self._job
        if job is None: return True
        started = time.perf_counter()
        if not job.finished.wait(timeout): return False
        thread = getattr(job, 'synth_thread', None)
        if thread and thread is not threading.current_thread():
            thread.join(None if timeout is None else max(0, timeout - (time.perf_counter() - started)))
            return not thread.is_alive()
        return True

    def close(self):
        self._closed = True
        self.stop()
        self.wait_idle(3)
