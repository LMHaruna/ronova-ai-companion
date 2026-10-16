"""Local SpeexDSP AEC and a bounded, device-positioned playback reference."""
from collections import deque
import ctypes as C
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parent


class PlaybackReference:
    """Map the actual waveOut playhead to captured audio time, including gaps.

    Submitted audio is not treated as already audible. Flat playhead intervals
    produce silence. No microphone samples are stored here.
    """
    def __init__(self, seconds=30, rate=16000):
        self.seconds, self.output_rate = seconds, rate
        self.lock = threading.RLock()
        self.generation = 0
        self.rate = None
        self.chunks = deque()
        self.anchors = deque(maxlen=200)
        self.stored_frames = 0
        self.ended = True
        self.error = None
        self._clock_origin = None
        self._raw_position = None

    def begin(self, rate):
        with self.lock:
            self.generation += 1
            self.rate = rate
            self.chunks.clear()
            self.anchors.clear()
            self.stored_frames = 0
            self.ended = False
            self.error = None
            self._clock_origin = None
            self._raw_position = None

    def append(self, pcm, offset):
        import numpy as np
        with self.lock:
            samples = np.frombuffer(pcm, dtype='<i2').copy()
            self.chunks.append((offset, samples))
            self.stored_frames += len(samples)
            maximum = int(self.seconds * self.rate)
            while self.stored_frames > maximum:
                index, old = self.chunks.popleft()
                trim = min(self.stored_frames - maximum, len(old))
                self.stored_frames -= trim
                if trim < len(old):
                    self.chunks.appendleft((index + trim, old[trim:].copy()))

    def observe(self, position, now=None, ended=False, running=None):
        now = time.perf_counter() if now is None else now
        with self.lock:
            if self.anchors and (position < (self._raw_position or 0) or now <= self.anchors[-1][0]):
                raise ValueError('Playback clock moved backwards')
            raw_position = position
            if running and not ended:
                origin = now - position / self.rate
                if self._clock_origin is None:
                    self._clock_origin = origin
                elif position != self._raw_position:
                    self._clock_origin += .05 * (origin - self._clock_origin)
                total = self.chunks[-1][0] + len(self.chunks[-1][1]) if self.chunks else position
                position = min(total, max(0, (now - self._clock_origin) * self.rate))
                if self.anchors:
                    position = max(self.anchors[-1][1], position)
            else:
                self._clock_origin = None
            self._raw_position = raw_position
            self.anchors.append((now, position))
            self.ended = ended

    def unavailable(self, error):
        with self.lock:
            self.error = str(error)

    def read(self, captured, count=320):
        import numpy as np
        empty = bytes(count * 2)
        with self.lock:
            if not self.rate or self.error or len(self.anchors) < 2:
                return empty, False, self.generation
            anchors = tuple(self.anchors)
            times = captured - (count - np.arange(count) - .5) / self.output_rate
            if times[0] < anchors[0][0] or (not self.ended and times[-1] > anchors[-1][0] + .025):
                return empty, False, self.generation
            walls, positions = np.asarray(anchors, dtype=np.float64).T
            indexes = np.searchsorted(walls, times, side='right') - 1
            indexes = np.clip(indexes, 0, len(walls) - 2)
            left, right = indexes, indexes + 1
            moving = positions[right] > positions[left]
            mapped = np.interp(times, walls, positions)
            # Once drained/cancelled, the last position is stationary.
            moving[times > walls[-1]] = False
            result = np.zeros(count, dtype=np.float64)
            covered = ~moving
            for offset, samples in self.chunks:
                mask = moving & (mapped >= offset) & (mapped < offset + len(samples))
                if mask.any():
                    local = mapped[mask] - offset
                    low = local.astype(np.int64)
                    high = np.minimum(low + 1, len(samples) - 1)
                    result[mask] = samples[low] * (1 - (local - low)) + samples[high] * (local - low)
                    covered |= mask
            return np.rint(result).astype('<i2').tobytes(), bool(covered.all()), self.generation


class SpeexAEC:
    """One 20 ms CPU frame; native state belongs to the microphone worker."""
    def __init__(self, rate=16000, frame_samples=320, tail_ms=200, library=None):
        self.frame_samples = frame_samples
        self.echo = self.preprocess = None
        self.lib = library or C.CDLL(str(ROOT / 'runtime/echo-tools/libspeexdsp-1.dll'))
        lib = self.lib
        short_p = C.POINTER(C.c_int16)
        signatures = {
            'speex_echo_state_init': ([C.c_int, C.c_int], C.c_void_p),
            'speex_echo_state_destroy': ([C.c_void_p], None),
            'speex_echo_state_reset': ([C.c_void_p], None),
            'speex_echo_ctl': ([C.c_void_p, C.c_int, C.c_void_p], C.c_int),
            'speex_echo_cancellation': ([C.c_void_p, short_p, short_p, short_p], None),
            'speex_preprocess_state_init': ([C.c_int, C.c_int], C.c_void_p),
            'speex_preprocess_state_destroy': ([C.c_void_p], None),
            'speex_preprocess_ctl': ([C.c_void_p, C.c_int, C.c_void_p], C.c_int),
            'speex_preprocess_run': ([C.c_void_p, short_p], C.c_int),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
        try:
            self.echo = lib.speex_echo_state_init(frame_samples, rate * tail_ms // 1000)
            if not self.echo:
                raise MemoryError('Unable to allocate echo filter')
            value = C.c_int(rate)
            self._check(lib.speex_echo_ctl(self.echo, 24, C.byref(value)))
            self.preprocess = lib.speex_preprocess_state_init(frame_samples, rate)
            if not self.preprocess:
                raise MemoryError('Unable to allocate residual echo filter')
            self._check(lib.speex_preprocess_ctl(self.preprocess, 24, self.echo))
            for request, setting in ((0, 0), (2, 0), (20, -50), (22, -12)):
                value = C.c_int(setting)
                self._check(lib.speex_preprocess_ctl(self.preprocess, request, C.byref(value)))
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check(code):
        if code:
            raise RuntimeError(f'SpeexDSP control error {code}')

    def process(self, pcm, reference):
        expected = self.frame_samples * 2
        if not self.echo or len(pcm) != expected or len(reference) != expected:
            raise ValueError('AEC requires one complete PCM frame')
        array = C.c_int16 * self.frame_samples
        recorded, played, output = array.from_buffer_copy(pcm), array.from_buffer_copy(reference), array()
        self.lib.speex_echo_cancellation(self.echo, recorded, played, output)
        self.lib.speex_preprocess_run(self.preprocess, output)
        return bytes(output)

    def reset(self):
        if self.echo:
            self.lib.speex_echo_state_reset(self.echo)

    def close(self):
        if self.preprocess:
            self.lib.speex_preprocess_state_destroy(self.preprocess)
            self.preprocess = None
        if self.echo:
            self.lib.speex_echo_state_destroy(self.echo)
            self.echo = None


class SpeakerInput:
    """AEC admission fails closed during playback if its reference is unsafe."""
    def __init__(self, reference, audible, status, factory=SpeexAEC):
        self.reference, self.audible, self.status = reference, audible, status
        self.aec = None
        self.error = None
        self.state = 'preparing'
        self.frames = self.blocked_frames = 0
        self.process_seconds = self.maximum_frame_seconds = 0.
        self._generation = None
        self._warmup = 0
        self._echo_until = 0.
        self._far_history = deque(maxlen=14)
        self._raw_history = deque(maxlen=4)
        self.echo_dominant_frames = 0
        try:
            self.aec = factory()
            self.state = 'active'
        except Exception as exc:
            self._fallback(exc)

    def _fallback(self, exc):
        self.error = f'{type(exc).__name__}: {exc}'
        self.state = 'fallback'
        if self.aec:
            self.aec.close()
            self.aec = None
        self.status('回声处理不可用，已回退到播放期间暂停识别：' + self.error)

    def process(self, pcm, captured):
        now = time.perf_counter()
        playing = self.audible()
        if playing:
            self._echo_until = now + .35
        guarded = playing or now < self._echo_until
        if not self.aec:
            return pcm, not guarded
        started = time.perf_counter()
        try:
            reference, valid, generation = self.reference(captured)
            if generation != self._generation:
                self._generation = generation
                self._warmup = 25  # First 500 ms of each playback learns its echo path.
                self._far_history.clear()
                self._raw_history.clear()
            if not valid:
                if guarded:
                    self.blocked_frames += 1
                    return pcm, False
                reference = bytes(len(pcm))
            cleaned = self.aec.process(pcm, reference)
            self.frames += 1
            self._far_history.append(reference)
            self._raw_history.append(pcm)
            if guarded and self._warmup:
                import numpy as np
                far = np.frombuffer(reference, '<i2').astype(np.float64)
                if np.mean(far * far) > 80**2:
                    self._warmup -= 1
                self.blocked_frames += 1
                return bytes(len(cleaned)), True
            if guarded:
                import numpy as np
                raw = np.frombuffer(pcm, '<i2').astype(np.float64)
                residual = np.frombuffer(cleaned, '<i2').astype(np.float64)
                raw_power = float(np.mean(raw * raw))
                residual_power = float(np.mean(residual * residual))
                correlated_echo = self._echo_dominates()
                # VAD can classify even heavily attenuated echo as speech.
                # A frame dominated by the removed echo cannot begin a turn.
                if correlated_echo or residual_power < max(90**2, raw_power * .08):
                    self.blocked_frames += 1
                    # Preserve an active user's pauses and let its endpoint fire.
                    return bytes(len(cleaned)), True
            return cleaned, True
        except Exception as exc:
            self._fallback(exc)
            return pcm, not guarded
        finally:
            seconds = time.perf_counter() - started
            self.process_seconds += seconds
            self.maximum_frame_seconds = max(self.maximum_frame_seconds, seconds)

    def _echo_dominates(self):
        import numpy as np
        from scipy.signal import correlate
        if len(self._raw_history) < 4:
            return False
        recorded = np.frombuffer(b''.join(self._raw_history), '<i2').astype(np.float64)
        played = np.frombuffer(b''.join(self._far_history), '<i2').astype(np.float64)
        if len(played) < len(recorded):
            return False
        power = float(np.dot(recorded, recorded))
        sums = np.concatenate(([0.], np.cumsum(played * played)))
        far_power = sums[len(recorded):] - sums[:-len(recorded)]
        dots = correlate(played, recorded, mode='valid', method='fft')
        coherence = np.abs(dots) / np.sqrt(np.maximum(power * far_power, 1.))
        dominant = bool(coherence.max(initial=0) > .82)
        self.echo_dominant_frames += int(dominant)
        return dominant

    def reset(self):
        if self.aec:
            self.aec.reset()
            self._warmup = 25
            self._far_history.clear()
            self._raw_history.clear()

    def close(self):
        if self.aec:
            self.aec.close()
            self.aec = None
        self.state = 'off'

    def timings(self):
        return dict(state=self.state, error=self.error, frames=self.frames,
                    blocked_frames=self.blocked_frames, process_seconds=self.process_seconds,
                    maximum_frame_seconds=self.maximum_frame_seconds,
                    echo_dominant_frames=self.echo_dominant_frames)
