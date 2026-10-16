"""Play three cached lines without invoking a model or opening a microphone."""
from pathlib import Path
import threading
import time
from pcm_player import PCMPlayer


class OathSound:
    def __init__(self, clips, starts, *, enabled, volume, device, player_factory=None,
                 duration=None, end_padding_seconds=.4):
        self.clips=tuple(Path(path) for path in clips); self.starts=tuple(starts)
        self.enabled=enabled and volume>0; self.volume=volume; self.device=device
        self.factory=player_factory or PCMPlayer
        self.cancelled=threading.Event(); self.done=threading.Event()
        self.lock=threading.Lock(); self.player=None; self.thread=None; self.error=''
        self.started=None; self.played=[]; self.duration=duration
        self.end_padding_seconds=end_padding_seconds; self.last_position=0.0

    def start(self):
        if self.started is not None: return
        self.started=time.monotonic()
        if not self.enabled:
            self.done.set(); return
        self.thread=threading.Thread(target=self.run,daemon=True,name='oath-cached-audio')
        self.thread.start()

    def run(self):
        try:
            for index,(path,start) in enumerate(zip(self.clips,self.starts)):
                wait=max(0.0,self.started+start-time.monotonic())
                if self.cancelled.wait(wait): return
                player=self.factory(self.cancelled,volume=self.volume,device=self.device,
                    end_padding_seconds=self.end_padding_seconds)
                with self.lock:
                    if self.cancelled.is_set(): player.close(); return
                    self.player=player
                try:
                    if not player.write(path): return
                    self.played.append(index); player.finish()
                finally:
                    player.close()
                    with self.lock: self.player=None
                if self.cancelled.is_set(): return
        except Exception:
            self.error='声音暂不可用，仪式将按字幕继续。'
        finally:
            self.done.set()

    def stop(self):
        self.cancelled.set()
        with self.lock:
            if self.player:
                try: self.player.stop()
                except Exception: pass

    def finished(self): return self.done.is_set()

    def position(self):
        """The single mixed track drives video with actual waveOut device time."""
        if self.started is None: return 0.0
        if not self.enabled or self.error or self.duration is None:
            return max(0.0,time.monotonic()-self.started)
        if self.cancelled.is_set(): return self.last_position
        if self.done.is_set(): return self.duration
        with self.lock:
            player=self.player
            if player:
                try:
                    with player.lock:
                        if player.handle and player.rate:
                            self.last_position=min(self.duration,max(0.0,
                                player._device_position()/player.rate))
                except (OSError,ValueError):
                    self.error='声音暂不可用，仪式将按字幕继续。'
        return self.last_position
