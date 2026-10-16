"""Continuous Windows waveOut playback; no third-party audio runtime required."""
import ctypes as C
from ctypes import wintypes as W
from collections import deque
import threading
import time
import wave
from array import array
import sys
import math


def scale_pcm(pcm, volume):
    if not isinstance(volume, (int, float)) or not 0 <= volume <= 1:
        raise ValueError('音量须在 0 到 1 之间')
    if volume == 1: return pcm
    values = array('h')
    values.frombytes(pcm)
    if sys.byteorder != 'little': values.byteswap()
    values = array('h', (round(value*volume) for value in values))
    if sys.byteorder != 'little': values.byteswap()
    return values.tobytes()


def output_devices():
    class Caps(C.Structure):
        _fields_ = [('manufacturer',W.WORD),('product',W.WORD),('version',W.DWORD),
                   ('name',W.WCHAR*32),('formats',W.DWORD),('channels',W.WORD),
                   ('reserved',W.WORD),('support',W.DWORD)]
    api = C.WinDLL('winmm')
    api.waveOutGetDevCapsW.argtypes = [C.c_size_t,C.POINTER(Caps),W.UINT]
    result = []
    for index in range(api.waveOutGetNumDevs()):
        caps = Caps()
        if api.waveOutGetDevCapsW(index,C.byref(caps),C.sizeof(caps)) == 0:
            result.append({'id':index,'name':caps.name})
    return result


class Format(C.Structure):
    _fields_ = [("tag", W.WORD), ("channels", W.WORD), ("rate", W.DWORD),
                ("bytes_sec", W.DWORD), ("align", W.WORD), ("bits", W.WORD), ("extra", W.WORD)]


class Header(C.Structure):
    _fields_ = [("data", C.c_void_p), ("length", W.DWORD), ("recorded", W.DWORD),
                ("user", C.c_size_t), ("flags", W.DWORD), ("loops", W.DWORD),
                ("next", C.c_void_p), ("reserved", C.c_size_t)]


class PositionValue(C.Union):
    _fields_ = [("value", W.DWORD), ("storage", C.c_byte * 8)]


class Position(C.Structure):
    _fields_ = [("kind", W.UINT), ("data", PositionValue)]


class PlaybackEnvelope:
    """Bounded 20ms RMS windows; retain levels, never the utterance recording."""
    def __init__(self):
        self.windows = deque(maxlen=2000)
        self.partial = array('h'); self.partial_rms = 0.0
        self.processed = 0

    @staticmethod
    def rms(samples):
        return math.sqrt(sum(value*value for value in samples)/len(samples))/32768 if samples else 0.0

    def append(self,pcm,rate):
        samples=array('h'); samples.frombytes(pcm)
        if sys.byteorder!='little': samples.byteswap()
        samples=self.partial+samples; size=max(1,round(rate*.02))
        end=len(samples)//size*size
        for start in range(0,end,size):
            self.windows.append((self.processed,self.processed+size,self.rms(samples[start:start+size])))
            self.processed+=size
        self.partial=samples[end:]; self.partial_rms=self.rms(self.partial)

    def at(self,frame):
        if self.processed<=frame<self.processed+len(self.partial): return self.partial_rms
        for start,end,level in reversed(self.windows):
            if start<=frame<end: return level
        return 0.0


class PCMPlayer:
    def __init__(self, cancel, max_buffer_seconds=1.5, reference=None, volume=1.0, device=None, end_padding_seconds=0.0, track_levels=False, keep_alive=False):
        self.cancel = cancel
        self.max_buffer_seconds = max_buffer_seconds
        self.pending = deque()
        self.handle = W.HANDLE()
        self.rate = None
        self.reference = reference
        self.envelope = PlaybackEnvelope() if track_levels else None
        self.volume, self.device = volume, device
        if type(end_padding_seconds) not in (int,float) or not 0 <= end_padding_seconds <= 1:
            raise ValueError('播放尾部保护须在 0 到 1 秒之间')
        self.end_padding_seconds = end_padding_seconds
        self.padding_frames = 0
        self._tail_appended = False
        if device is not None and (not isinstance(device, int) or device < 0):
            raise ValueError('播放设备编号无效')
        self._reference_ended = False
        self.lock = threading.RLock()
        self.underruns = 0
        self.writes = 0
        self.submitted_frames = 0
        self.completed_frames = 0
        self.source_submitted_frames = 0
        self.source_completed_frames = 0
        self.continuity_silence_frames = 0
        self.continuity_error = None
        self._continuity_stop = threading.Event()
        self._continuity_thread = None
        self.last_end = None
        self.longest_gap_seconds = 0.0
        self.write_events = deque(maxlen=2048)
        self.api = C.WinDLL("winmm")
        self.api.waveOutOpen.argtypes = [C.POINTER(W.HANDLE), W.UINT, C.POINTER(Format), C.c_size_t, C.c_size_t, W.DWORD]
        self.api.waveOutGetPosition.argtypes = [W.HANDLE, C.POINTER(Position), W.UINT]
        for name in ("waveOutPrepareHeader", "waveOutWrite", "waveOutUnprepareHeader"):
            getattr(self.api, name).argtypes = [W.HANDLE, C.POINTER(Header), W.UINT]
        for name in ("waveOutReset", "waveOutClose"):
            getattr(self.api, name).argtypes = [W.HANDLE]
        if keep_alive:
            self._continuity_thread = threading.Thread(target=self._maintain_stream, daemon=True)
            self._continuity_thread.start()

    def _maintain_stream(self):
        while not self._continuity_stop.wait(.01) and not self.cancel.is_set():
            try:
                self._supply_continuity_silence()
            except Exception as exc:
                # A clock/driver failure must not discard the actual speech.
                self.continuity_error = type(exc).__name__
                self._continuity_stop.set()

    def _supply_continuity_silence(self):
        with self.lock:
            if self._continuity_stop.is_set() or self.cancel.is_set() or not self.handle:
                return
            remaining = max(0, self.submitted_frames - self._device_position()) / self.rate
            if remaining >= .04:
                return
            frames = max(1, round((.08-remaining)*self.rate))
            # Use the device clock rather than whole pending headers: a long
            # source header may have only milliseconds left before it drains.
            self.write_pcm(b'\0\0'*frames, self.rate, _kind='continuity')

    def _stop_continuity(self):
        self._continuity_stop.set()
        thread = self._continuity_thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=.2)

    @staticmethod
    def _check(code):
        if code:
            raise OSError(f"waveOut error {code}")

    def _device_position(self):
        position=Position(2,PositionValue())  # TIME_SAMPLES, with driver fallback.
        self._check(self.api.waveOutGetPosition(self.handle,C.byref(position),C.sizeof(position)))
        if position.kind==2: return position.data.value
        if position.kind==1: return round(position.data.value*self.rate/1000)
        if position.kind==4: return position.data.value//2
        raise OSError('Playback position format unsupported')

    @property
    def playback_level(self):
        with self.lock:
            if self.envelope is None or not self.handle or self.cancel.is_set(): return 0.0
            try:
                frames=self._device_position()
                if not 0<frames<self.submitted_frames or not any(not (item[0].flags&1) for item in self.pending):
                    return 0.0
                rms=self.envelope.at(frames-1)
                # Suppress digital silence and compress the speech range for
                # legible motion. Use the same volume-scaled PCM as waveOut.
                return max(0.0,min(1.0,(rms-.002)/.178))**.6
            except Exception:
                return 0.0  # Visual diagnostics must never interrupt speech.

    def poll_reference(self, ended=False):
        # Reference failures disable AEC admission, never interrupt PCM playback.
        if self.reference is None or self._reference_ended:
            return
        with self.lock:
            if not self.handle:
                return
            try:
                frames = self._device_position()
                running = frames < self.submitted_frames and any(not (item[0].flags & 1) for item in self.pending)
                self.reference.observe(min(frames, self.submitted_frames), ended=ended, running=running)
            except Exception as exc:
                self.reference.unavailable(f'{type(exc).__name__}: {exc}')
            if ended:
                self._reference_ended = True

    def _reap(self):
        while self.pending and self.pending[0][0].flags & 1:
            header, data, seconds = self.pending.popleft()
            self._check(self.api.waveOutUnprepareHeader(self.handle, C.byref(header), C.sizeof(header)))
            if not self.cancel.is_set():
                self.completed_frames += header.length // 2
                self.source_completed_frames += getattr(header, 'source_frames', header.length // 2)

    def write(self, path):
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("Expected mono 16-bit PCM")
            rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
        return self.write_pcm(pcm, rate)

    def write_pcm(self, pcm, rate, *, _kind='source'):
        if not isinstance(pcm, bytes) or len(pcm) % 2 or not pcm or rate <= 0:
            raise ValueError("Expected nonempty mono 16-bit PCM")
        seconds = len(pcm) / (rate * 2)
        # Limit queued device audio and keep cancel responsive while waiting.
        while _kind == 'source' and not self.cancel.is_set():
            with self.lock:
                self._reap()
                queued = sum(v[2] for v in self.pending)
            if queued < self.max_buffer_seconds:
                break
            self.cancel.wait(.005)
        with self.lock:
            if self.cancel.is_set():
                return False
            if not self.handle:
                fmt = Format(1, 1, rate, rate * 2, 2, 16, 0)
                self._check(self.api.waveOutOpen(C.byref(self.handle), 0xFFFFFFFF if self.device is None else self.device, C.byref(fmt), 0, 0, 0))
                self.rate = rate
                if self.reference is not None:
                    self.reference.begin(rate)
                    self.poll_reference()
            elif rate != self.rate:
                raise ValueError("Sample rate changed during one utterance")
            self._reap()
            now = time.perf_counter()
            gap = max(0.0, now - self.last_end) if self.last_end is not None else 0.0
            if self.last_end is not None and now > self.last_end + .025:
                self.underruns += 1
                self.longest_gap_seconds = max(self.longest_gap_seconds, now - self.last_end)
            pcm = scale_pcm(pcm, self.volume() if callable(self.volume) else self.volume)
            data = C.create_string_buffer(pcm)
            header = Header(C.cast(data, C.c_void_p), len(pcm), 0, 0, 0, 0, None, 0)
            header.source_frames = len(pcm)//2 if _kind=='source' else 0
            self._check(self.api.waveOutPrepareHeader(self.handle, C.byref(header), C.sizeof(header)))
            self.pending.append((header, data, seconds))  # Keep buffers alive until WHDR_DONE.
            code = self.api.waveOutWrite(self.handle, C.byref(header), C.sizeof(header))
            if code:
                self.pending.pop()
                self.api.waveOutUnprepareHeader(self.handle, C.byref(header), C.sizeof(header))
                self._check(code)
            self.last_end = max(now, self.last_end or now) + seconds
            self.write_events.append({"submitted_at": now, "offset_frames": self.submitted_frames,
                                      "frames": len(pcm) // 2, "gap_seconds": gap})
            self.writes += 1
            if self.envelope is not None:
                try: self.envelope.append(pcm,rate)
                except Exception: self.envelope=None
            if self.reference is not None:
                try:
                    self.reference.append(pcm, self.submitted_frames)
                except Exception as exc:
                    self.reference.unavailable(f'{type(exc).__name__}: {exc}')
            self.submitted_frames += len(pcm) // 2
            self.source_submitted_frames += header.source_frames
            if _kind=='continuity':
                self.continuity_silence_frames += len(pcm)//2
            return True

    def stop(self):
        self._stop_continuity()
        with self.lock:
            if self.handle:
                self.poll_reference(ended=True)
                self._check(self.api.waveOutReset(self.handle))

    def finish(self):
        self._stop_continuity()
        # Keep the device running beyond the last phoneme. Buffer completion
        # alone cannot test what a downstream speaker/driver actually emitted.
        # Queue silence once at utterance EOF, never between streaming chunks.
        if self.handle and not self.cancel.is_set() and not self._tail_appended:
            self._tail_appended = True
            frames = round(self.rate*self.end_padding_seconds)
            if frames and self.write_pcm(b'\0\0'*frames,self.rate,_kind='eof'): self.padding_frames=frames
        while not self.cancel.is_set():
            with self.lock:
                self._reap()
                if not self.pending:
                    break
            self.cancel.wait(.005)
        self.close()

    def close(self):
        self._stop_continuity()
        with self.lock:
            if self.handle:
                self.poll_reference(ended=True)
                # Normal finish has already drained every WHDR_DONE header.
                # Reset only outstanding audio during cancellation/forced close.
                self._reap()
                if self.pending:
                    self._check(self.api.waveOutReset(self.handle))
                self._reap()
                # Reset completes outstanding buffers before their memory is released.
                if self.pending:
                    raise RuntimeError("waveOut reset did not release pending buffers")
                self._check(self.api.waveOutClose(self.handle))
                self.handle = W.HANDLE()
