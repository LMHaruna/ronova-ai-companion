"""Local incoming-call loop, independent of voice replies and microphone capture."""
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
import wave

from pcm_player import PCMPlayer


@dataclass
class RingJob:
    identifier: str
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    player: object = None
    thread: object = None
    closed_cleanly: bool = True


class IncomingRingtone:
    def __init__(self, path, volume, device, failed=None, player_factory=None):
        self.path = Path(path)
        self.volume, self.device, self.failed = volume, device, failed
        self.player_factory = player_factory or PCMPlayer
        self.lock = threading.RLock()
        self.operations = threading.RLock()
        self.job = None
        self.closed = False

    def _report(self, identifier):
        if self.failed:
            try: self.failed(identifier, '来电铃声未能播放或停止，请检查播放设备')
            except Exception: pass  # Notifications must not interrupt audio cleanup.

    def start(self, identifier):
        with self.operations:
            if self.closed: return False
            with self.lock:
                current = self.job
                if current and current.identifier == identifier and not current.cancel.is_set() and not current.done.is_set():
                    return True
            if not self.stop(): return False
            job = RingJob(identifier)
            job.thread = threading.Thread(target=self._run, args=(job,), name='incoming-ringtone', daemon=True)
            with self.lock: self.job = job
            try: job.thread.start()
            except Exception:
                job.cancel.set(); job.done.set(); self._report(identifier)
                return False
            return True

    def _run(self, job):
        player = None
        try:
            with wave.open(str(self.path), 'rb') as clip:
                rate = clip.getframerate()
                if (clip.getnchannels() != 1 or clip.getsampwidth() != 2 or clip.getcomptype() != 'NONE'
                        or not 8000 <= rate <= 48000 or not 0 < clip.getnframes() <= rate * 10):
                    raise ValueError('来电铃声格式无效')
                pcm = clip.readframes(clip.getnframes())
                if len(pcm) != clip.getnframes() * 2: raise ValueError('来电铃声不完整')
            if job.cancel.is_set(): return
            player = self.player_factory(job.cancel, max_buffer_seconds=.12, volume=self.volume,
                                         device=self.device(), end_padding_seconds=0)
            with self.lock: job.player = player
            cursor = 0
            chunk_size = max(2, round(rate * .04) * 2)
            deadline = time.monotonic()
            while not job.cancel.is_set():
                chunk = pcm[cursor:cursor + chunk_size]
                if not player.write_pcm(chunk, rate): break
                cursor = (cursor + len(chunk)) % len(pcm)
                deadline += len(chunk) / (rate * 2)
                job.cancel.wait(max(0, deadline - time.monotonic()))
        except Exception:
            if not job.cancel.is_set(): self._report(job.identifier)
        finally:
            try:
                if player: player.close()
            except Exception:
                job.closed_cleanly = False
                self._report(job.identifier)
            finally:
                job.done.set()

    def stop(self, identifier=None):
        """Reset queued audio now; return only after the writer/device is closed."""
        with self.operations:
            with self.lock:
                job = self.job
                if not job or identifier is not None and job.identifier != identifier: return True
                job.cancel.set()
                player = job.player
            if player:
                try: player.stop()
                except Exception: self._report(job.identifier)
            stopped = job.done.wait(2) and job.closed_cleanly
            if stopped:
                with self.lock:
                    if self.job is job: self.job = None
            else:
                self._report(job.identifier)
            return stopped

    def close(self):
        with self.operations:
            self.closed = True
            return self.stop()
