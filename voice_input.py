"""Local Chinese streaming input. Audio exists only in bounded RAM buffers."""
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import queue
import sys
import threading
import time
from native_resources import native_resource_path

ROOT = Path(__file__).resolve().parent
MODEL_NAME = "sherpa-onnx-streaming-zipformer-ctc-zh-int8-2025-06-30"


@dataclass(frozen=True)
class InputConfig:
    rate: int = 16000
    frame_ms: int = 20
    onset_ms: int = 120
    silence_ms: int = 900
    minimum_ms: int = 200
    maximum_ms: int = 20000
    prefix_ms: int = 240
    queue_frames: int = 100
    calibration_ms: int = 800
    onset_noise_ratio: float = 2.0
    sustain_noise_ratio: float = 1.45
    minimum_rms: float = 90.0
    interrupt_ms: int = 400


CALL_CONFIG = InputConfig(onset_ms=240, minimum_ms=300, silence_ms=1100,
                          prefix_ms=600, sustain_noise_ratio=1.2)


class SpeechEnergyGate:
    """Combine VAD with a calibrated noise floor; retain only numeric history."""
    def __init__(self, config=None):
        self.config = config or InputConfig()
        self.levels = deque(maxlen=5000 // self.config.frame_ms)
        self.remaining = self.config.calibration_ms
        self.noise_rms = 60.0
        self.rms = 0.0
        self.threshold_rms = self.config.minimum_rms
        self.energy_pass = False
        self.frames = self.rejected_vad_frames = 0

    @property
    def ready(self):
        return self.remaining == 0

    def process(self, pcm, vad_speech, active=False, learn=True):
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
        self.rms = float(np.sqrt(np.mean(samples * samples)))
        self.frames += 1
        if not self.ready:
            if not learn:
                self.energy_pass = False
                return False
            self.levels.append(self.rms)
            self.remaining = max(0, self.remaining - self.config.frame_ms)
            if self.ready:
                self.noise_rms = max(60.0, self._floor())
            self.energy_pass = False
            return False
        ratio = self.config.sustain_noise_ratio if active else self.config.onset_noise_ratio
        self.threshold_rms = max(self.config.minimum_rms, self.noise_rms * ratio)
        self.energy_pass = self.rms >= self.threshold_rms
        speech = bool(vad_speech and self.energy_pass)
        if vad_speech and not speech:
            self.rejected_vad_frames += 1
        # Never train the background level on an active utterance. Quiet frames
        # rejected by VAD/energy may update it slowly between turns.
        if learn and not active and not speech:
            self.levels.append(self.rms)
            self.noise_rms += .02 * (max(60.0, self._floor()) - self.noise_rms)
        return speech

    def _floor(self):
        levels = sorted(self.levels)
        return levels[len(levels) // 5] if levels else 60.0

    def timings(self):
        return dict(calibration_remaining_ms=self.remaining, noise_rms=self.noise_rms,
                    frame_rms=self.rms, threshold_rms=self.threshold_rms,
                    frames=self.frames, rejected_vad_frames=self.rejected_vad_frames)


class TurnDetector:
    """Debounce speech onset, preserve its prefix and retain short pauses."""
    def __init__(self, config=None):
        self.config = config or InputConfig()
        self.reset()

    def reset(self):
        self.prefix = deque(maxlen=self.config.prefix_ms // self.config.frame_ms)
        self.active = False
        self.run = self.silent = self.voiced = self.frames = 0
        self.last_speech = None

    def push(self, pcm, speech, captured):
        c = self.config
        if not self.active:
            self.prefix.append(pcm)
            self.run = self.run + 1 if speech else 0
            if self.run * c.frame_ms < c.onset_ms:
                return []
            self.active = True
            self.voiced = self.run
            self.frames = len(self.prefix)
            self.last_speech = captured
            return [("start", tuple(self.prefix))]
        self.frames += 1
        if speech:
            self.voiced += 1
            self.last_speech = captured
            self.silent = 0
        else:
            self.silent += 1
        events = [("audio", pcm)]
        if self.frames * c.frame_ms >= c.maximum_ms:
            # Never send a chopped long utterance as a complete user message.
            events.append(("discard", "说话超过20秒，本段未发送；请停顿后重新说。"))
            self.reset()
        elif self.silent * c.frame_ms >= c.silence_ms:
            if self.voiced * c.frame_ms >= c.minimum_ms:
                events.append(("end", {"vad_last_speech_at": self.last_speech,
                                       "audio_seconds": self.frames * c.frame_ms / 1000,
                                       "endpoint_silence_seconds": c.silence_ms / 1000}))
            else:
                events.append(("discard", None))
            self.reset()
        return events


class ChineseRecognizer:
    def __init__(self, model_name=MODEL_NAME):
        import numpy as np
        tools = ROOT / "runtime/asr-tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import sherpa_onnx
        model = ROOT / "runtime/models" / model_name
        for name in ("model.int8.onnx", "tokens.txt"):
            if not (model / name).is_file():
                raise FileNotFoundError("缺少中文流式识别模型，请运行 scripts/setup_streaming_asr.py")
        self.np = np
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
            tokens=str(native_resource_path(model / "tokens.txt")),
            model=str(native_resource_path(model / "model.int8.onnx")),
            num_threads=2, sample_rate=16000, provider="cpu",
            enable_endpoint_detection=False)

    def create(self):
        return self.recognizer.create_stream()

    def feed(self, stream, pcm):
        samples = self.np.frombuffer(pcm, dtype="<i2").astype(self.np.float32) / 32768
        stream.accept_waveform(16000, samples)
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return self.recognizer.get_result(stream).strip()

    def finish(self, stream):
        stream.accept_waveform(16000, self.np.zeros(4800, dtype=self.np.float32))
        stream.input_finished()
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return self.recognizer.get_result(stream).strip()


class InputSession:
    """Pure streaming state machine shared by microphone and file verification."""
    def __init__(self, recognizer, on_start, on_partial, on_final, status,
                 config=None, clock=time.perf_counter, on_confirmed=None):
        self.recognizer = recognizer
        self.on_start, self.on_partial, self.on_final = on_start, on_partial, on_final
        self.status, self.clock = status, clock
        self.on_confirmed = on_confirmed or (lambda: None)
        self.detector = TurnDetector(config)
        self.stream = None
        self.text = ""
        self.decode_seconds = 0
        self.last_partial = 0
        self.quiet_needed = 0
        self.confirmed = False

    def reset(self, wait_for_silence=False):
        self.detector.reset()
        self.stream = None
        self.text = ""
        self.decode_seconds = 0
        self.confirmed = False
        self.quiet_needed = self.detector.config.silence_ms if wait_for_silence else 0

    def process(self, pcm, speech, captured):
        if self.quiet_needed:
            if speech:
                self.quiet_needed = self.detector.config.silence_ms
            else:
                self.quiet_needed = max(0, self.quiet_needed - self.detector.config.frame_ms)
            return
        for kind, value in self.detector.push(pcm, speech, captured):
            if kind == "start":
                self.on_start()
                self.stream = self.recognizer.create()
                self.text = ""
                self.decode_seconds = 0
                self.confirmed = False
                value = b"".join(value)
            if kind in ("start", "audio"):
                started = self.clock()
                text = self.recognizer.feed(self.stream, value)
                self.decode_seconds += self.clock() - started
                if text and text != self.text and self.clock() - self.last_partial >= .3:
                    self.on_partial(text)
                    self.last_partial = self.clock()
                self.text = text
                if (not self.confirmed and speech
                        and self.detector.voiced * self.detector.config.frame_ms >= self.detector.config.interrupt_ms
                        and sum('\u4e00' <= char <= '\u9fff' for char in text) >= 2):
                    self.confirmed = True
                    self.on_confirmed()
            elif kind == "end":
                started = self.clock()
                text = self.recognizer.finish(self.stream)
                value["asr_decode_seconds"] = self.decode_seconds + self.clock() - started
                value["speech_end_to_submit_seconds"] = max(0, self.clock() - value.pop("vad_last_speech_at"))
                self.stream = None
                if text and any('\u4e00' <= char <= '\u9fff' for char in text):
                    self.on_final(text, value)
                else:
                    self.status("本段未识别到中文，请继续说。")
            elif kind == "discard":
                self.stream = None
                if value:
                    self.status(value)
                    self.reset(wait_for_silence=True)


def input_devices():
    import sounddevice as sd
    return [{"id": index, "name": device["name"],
             "hostapi": sd.query_hostapis(device["hostapi"])["name"]}
            for index, device in enumerate(sd.query_devices()) if device["max_input_channels"]]


class LiveMicrophone:
    def __init__(self, on_start, on_partial, on_final, status, blocked=lambda: False,
                 config=None, recognizer_factory=ChineseRecognizer, stream_factory=None,
                 vad_factory=None, processor_factory=None, on_confirmed=None):
        self.config = config or InputConfig()
        self.on_start, self.on_partial, self.on_final = on_start, on_partial, on_final
        self.status, self.blocked = status, blocked
        self.on_confirmed = on_confirmed or (lambda: None)
        self.recognizer_factory = recognizer_factory
        self.stream_factory, self.vad_factory = stream_factory, vad_factory
        self.processor_factory = processor_factory
        self.processor = None
        self.processor_metrics = None
        self.energy_gate = None
        self.input_metrics = {}
        self._lock = threading.RLock()
        self._thread = self._stream = None
        self._stop = threading.Event()
        self._reset = threading.Event()
        self._overrun = threading.Event()
        self._queue = queue.Queue(maxsize=self.config.queue_frames)
        self.state = "off"
        self.error = None

    def start(self, device=None):
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("收音尚未停止，请稍后重试")
            self._stop = threading.Event()
            self._reset.clear()
            self._overrun.clear()
            self._drain()
            self.error = None
            self.input_metrics = {}
            self.energy_gate = None
            self.state = "preparing"
            self._thread = threading.Thread(target=self._run, args=(device,), daemon=True)
            self._thread.start()

    def _drain(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def reset(self):
        self._reset.set()

    def _callback(self, data, frames, timing, status):
        # No inference, cloud call or console IO on PortAudio's callback thread.
        if self._stop.is_set():
            return
        if status:
            self._overrun.set()
        lag = max(0, timing.currentTime - timing.inputBufferAdcTime)
        arrived = time.perf_counter()
        captured = min(arrived, arrived - lag + self.config.frame_ms / 1000)
        try:
            self._queue.put_nowait((bytes(data), captured))
        except queue.Full:
            self._overrun.set()

    def _run(self, device):
        session = stream = processor = None
        try:
            import sounddevice as sd
            import webrtcvad
            recognizer = self.recognizer_factory()
            if self._stop.is_set():
                return
            if self.processor_factory:
                processor = self.processor = self.processor_factory()
            vad = self.vad_factory() if self.vad_factory else webrtcvad.Vad(2)
            factory = self.stream_factory or sd.RawInputStream
            c = self.config
            gate = self.energy_gate = SpeechEnergyGate(c)
            sd.check_input_settings(device=device, channels=1, dtype="int16", samplerate=c.rate)
            def emit(callback, *args):
                if not self._stop.is_set() and not self._reset.is_set():
                    callback(*args)
            session = InputSession(recognizer, lambda: emit(self.on_start),
                                   lambda text: emit(self.on_partial, text),
                                   lambda text, metrics: emit(self.on_final, text, metrics),
                                   self.status, c, on_confirmed=lambda: emit(self.on_confirmed))
            stream = factory(device=device, samplerate=c.rate, channels=1, dtype="int16",
                             blocksize=c.rate * c.frame_ms // 1000, callback=self._callback)
            with self._lock:
                self._stream = stream
                if self._stop.is_set():
                    return
                stream.start()
                self.state = "listening"
            self.status("麦克风已开启，前0.8秒测量背景音，请先保持安静。")
            was_blocked = False
            while not self._stop.is_set():
                if not stream.active:
                    raise RuntimeError("麦克风已断开")
                try:
                    pcm, captured = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                if self._stop.is_set():
                    break
                if self._overrun.is_set() or self._reset.is_set():
                    overflow = self._overrun.is_set()
                    self._overrun.clear()
                    self._reset.clear()
                    self._drain()
                    session.reset(wait_for_silence=True)
                    if processor:
                        processor.reset()
                    if overflow:
                        self.status("收音缓冲溢出，本段未发送；停顿后可继续。")
                    continue
                if self.blocked():
                    if not was_blocked:
                        session.reset()
                    was_blocked = True
                    continue
                if was_blocked:
                    session.reset()
                    was_blocked = False
                    self._drain()
                    continue
                expected = c.rate * c.frame_ms // 1000 * 2
                if len(pcm) != expected:
                    raise RuntimeError("麦克风帧长度异常")
                meter_pcm = pcm
                if processor:
                    pcm, admissible = processor.process(pcm, captured)
                    if not admissible:
                        session.reset()
                        continue
                was_ready = gate.ready
                # Meter the microphone level before AEC attenuates quiet words;
                # VAD and ASR still use the echo-cleaned signal. Never learn our
                # own speaker playback as a new background noise floor.
                learn = not processor or not processor.audible()
                speech = gate.process(meter_pcm, vad.is_speech(pcm, c.rate),
                                      session.detector.active, learn=learn)
                if not was_ready:
                    if gate.ready:
                        self.status(f"背景音测量完成，可以说话；停顿约{c.silence_ms / 1000:g}秒自动发送，识别出中文后才打断。")
                    continue
                session.process(pcm, speech, captured)
                self.input_metrics = dict(utterance_active=session.detector.active,
                                          voiced_ms=session.detector.voiced * c.frame_ms,
                                          silence_ms=session.detector.silent * c.frame_ms,
                                          waiting_for_quiet_ms=session.quiet_needed,
                                          recognized_chinese_chars=sum('\u4e00' <= char <= '\u9fff' for char in session.text),
                                          interruption_confirmed=session.confirmed)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.status("收音失败，已关闭麦克风；文字聊天可继续：" + self.error)
        finally:
            if session:
                session.reset()
            if processor:
                processor.close()
                self.processor_metrics = processor.timings()
            if stream:
                try:
                    stream.abort()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception as exc:
                    self.error = f"关闭麦克风失败：{type(exc).__name__}: {exc}"
                    self.status(self.error)
            with self._lock:
                self._stream = None
                self.state = "off"
            self._drain()

    def stop(self):
        self._stop.set()
        with self._lock:
            stream = self._stream
        if stream:
            try:
                stream.abort()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._drain()


class VoiceCall:
    """Admission lock rejects callbacks from closed or superseded calls."""
    def __init__(self, pipeline, voice, output, status, microphone_factory=LiveMicrophone, on_transcript=None):
        self.pipeline, self.voice = pipeline, voice
        self.output, self.status = output, status
        self.microphone_factory = microphone_factory
        self.on_transcript = on_transcript
        self.microphone = None
        self.mode = "speakers"
        self._active = False
        self._revision = 0
        self._lock = threading.RLock()
        self._echo_until = 0
        self._text_echo_until = 0
        self._speech_revision = None

    @property
    def active(self):
        return self._active and self.microphone and self.microphone.state != "off"

    def _blocked(self):
        now = time.perf_counter()
        if getattr(self.pipeline, 'text_reply_active', False) is True:
            self._text_echo_until = now + .35
            return True
        if now < self._text_echo_until:
            return True
        if self.mode in ("headphones", "speakers-aec"):
            return False
        playing = self.voice.playback_active
        if playing:
            self._echo_until = now + .35
        return playing or now < self._echo_until

    def start(self, mode="speakers", device=None, cancel_reply=True):
        if mode not in ("speakers", "headphones", "speakers-aec"):
            raise ValueError("通话模式必须是 speakers、speakers-aec 或 headphones")
        self.stop(cancel_reply=cancel_reply)
        with self._lock:
            revision = self._revision
            self.mode = mode
            self._active = True
            self._echo_until = 0
            self._text_echo_until = 0

            def admit(action):
                with self._lock:
                    if self._active and revision == self._revision:
                        action()

            def start_speech():
                self._speech_revision = None if self._blocked() else revision

            def confirmed_speech():
                if self._speech_revision != revision or self._blocked():
                    return
                if self.pipeline.responding:
                    self.pipeline.cancel()
                    self.status("已确认中文开口，停止上一轮回应。")

            def final(text, metrics):
                if self._speech_revision != revision or self._blocked():
                    return
                self.output("\n你（语音）: " + text + "\n")
                if self.on_transcript: self.on_transcript('final', text)
                self.pipeline.submit(text, input_metrics=metrics)

            def partial(text):
                self.status("识别中：" + text)
                if self.on_transcript: self.on_transcript('partial', text)

            extra = {'config': CALL_CONFIG}
            if mode == 'speakers-aec':
                from echo_control import SpeakerInput
                extra['processor_factory'] = lambda: SpeakerInput(
                    self.voice.capture_reference, lambda: self.voice.playback_active,
                    lambda text: admit(lambda: self.status(text)))
            self.microphone = self.microphone_factory(
                on_start=lambda: admit(start_speech),
                on_confirmed=lambda: admit(confirmed_speech),
                on_partial=lambda text: admit(lambda: partial(text)),
                on_final=lambda text, metrics: admit(lambda: final(text, metrics)),
                status=lambda text: admit(lambda: self.status(text)), blocked=self._blocked, **extra)
            try:
                self.microphone.start(device)
            except Exception:
                self._active = False
                raise
        messages = {'headphones': '正在准备中文识别。耳机模式确认中文开口后打断。',
                    'speakers': '正在准备中文识别。扬声器模式在播放期间暂停识别；可用 /stop 打断。',
                    'speakers-aec': '正在准备中文识别与回声处理。扬声器模式，播放开始约0.5秒用于适应回声。'}
        self.status(messages[mode])

    def reset_input(self):
        with self._lock:
            self._speech_revision = None
            if self.microphone:
                self.microphone.reset()

    def stop(self, cancel_reply=True):
        with self._lock:
            self._active = False
            self._revision += 1
            microphone = self.microphone
        if microphone:
            microphone.stop()
        if cancel_reply:
            self.pipeline.cancel()

    def timings(self):
        result = {"state": self.microphone.state if self.microphone else "off",
                  "mode": self.mode, "error": self.microphone.error if self.microphone else None}
        if self.microphone:
            gate = getattr(self.microphone, 'energy_gate', None)
            if gate:
                result['speech_gate'] = gate.timings()
            result['input'] = dict(getattr(self.microphone, 'input_metrics', {}))
            processor = getattr(self.microphone, 'processor', None)
            if processor:
                result['echo'] = processor.timings()
        return result
