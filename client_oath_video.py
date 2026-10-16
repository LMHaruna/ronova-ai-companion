"""Bounded local video decoding, painted by Qt and timed by cached PCM playback."""
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
import wave
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor,QImage,QPainter,QPixmap
from PySide6.QtWidgets import QWidget
from client_oath import LINES
import pcm_player

ASSET_ROOT=Path(__file__).resolve().parent/'design/oath-video'
FFMPEG=Path(pcm_player.__file__).resolve().parent/'runtime/gpt-sovits/ffmpeg.exe'


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): digest.update(chunk)
    return digest.hexdigest()


class OathVideoAssets:
    def __init__(self,root=ASSET_ROOT):
        self.root=Path(root)
        try:
            saved=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))
            if saved['version']!=1 or tuple(saved['lines'])!=LINES: raise ValueError('script mismatch')
            self.canvas=tuple(saved['canvas']); self.decode_size=tuple(saved['decode_size'])
            self.fps=saved['fps']; self.frame_count=saved['frame_count']; self.duration=saved['duration_seconds']
            self.line_times=tuple(saved['line_times']); self.beats=tuple(saved['beats'])
            if (self.canvas!=(1288,1608) or self.decode_size!=(644,804) or self.fps!=24 or
                self.frame_count!=384 or self.duration!=16 or len(self.line_times)!=3 or
                not 0<=self.line_times[0]<self.line_times[1]<self.line_times[2]<self.duration):
                raise ValueError('invalid timeline')
            for name in ('video','audio','poster','ending'):
                spec=saved[name]; path=self.confined(spec['file'])
                if sha(path)!=spec['sha256']: raise ValueError('asset hash mismatch')
                setattr(self,name,path)
            with wave.open(str(self.audio),'rb') as sound:
                if (sound.getnchannels()!=1 or sound.getsampwidth()!=2 or sound.getframerate()!=24000 or
                    sound.getnframes()!=384000): raise ValueError('invalid mixed PCM')
            self.poster_image=QPixmap(str(self.poster)); self.ending_image=QPixmap(str(self.ending))
            if self.poster_image.isNull() or self.ending_image.isNull(): raise ValueError('invalid still')
            spec=saved['captions']; path=self.confined(spec['file'])
            if sha(path)!=spec['sha256']: raise ValueError('caption hash mismatch')
            captions=json.loads(path.read_text(encoding='utf-8'))
            if captions['version']!=1 or captions['source_audio_sha256']!=saved['audio']['sha256']:
                raise ValueError('caption source mismatch')
            self.captions=captions['lines']
            if len(self.captions)!=len(LINES): raise ValueError('caption lines incomplete')
            for text,start,line in zip(LINES,self.line_times,self.captions):
                events=line['characters']
                if (line['text']!=text or line['start']!=start or
                    ''.join(item['text'] for item in events)!=text or
                    not start<line['end']<=self.duration):
                    raise ValueError('invalid caption script')
                times=[item['at'] for item in events]
                if any(not isinstance(t,(int,float)) or not start<=t<=line['end'] for t in times):
                    raise ValueError('invalid caption timing')
                if times!=sorted(times): raise ValueError('unordered caption timing')
            if not FFMPEG.is_file(): raise ValueError('decoder missing')
        except (OSError,ValueError,KeyError,TypeError,IndexError,wave.Error) as exc:
            raise RuntimeError('仪式素材未就绪，请重新安装完整素材后重试。') from exc

    def confined(self,relative):
        path=(self.root/relative).resolve()
        if not path.is_relative_to(self.root.resolve()): raise ValueError('asset outside package')
        return path


class VideoDecoder:
    def __init__(self,assets):
        self.assets=assets; self.frames=queue.Queue(maxsize=12)
        self.cancelled=threading.Event(); self.done=threading.Event()
        self.process=None; self.error=''; self.decoded=0
        self.thread=threading.Thread(target=self.run,daemon=True,name='oath-local-video')

    def start(self): self.thread.start()

    def run(self):
        try:
            if self.cancelled.is_set(): return
            width,height=self.assets.decode_size; size=width*height*3
            self.process=subprocess.Popen([str(FFMPEG),'-hide_banner','-loglevel','error','-nostdin',
                '-threads','2','-i',str(self.assets.video),'-an','-sn','-dn',
                '-vf',f'scale={width}:{height}','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],
                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if self.cancelled.is_set(): self.process.terminate(); return
            while not self.cancelled.is_set():
                frame=self.process.stdout.read(size)
                if not frame: break
                if len(frame)!=size: raise ValueError('incomplete video frame')
                item=(self.decoded,frame); self.decoded+=1
                while not self.cancelled.is_set():
                    try: self.frames.put(item,timeout=.05); break
                    except queue.Full: continue
            if not self.cancelled.is_set():
                if self.process.wait(timeout=3)!=0 or self.decoded!=self.assets.frame_count:
                    raise ValueError('video incomplete')
        except (OSError,ValueError,subprocess.TimeoutExpired):
            if not self.cancelled.is_set(): self.error='动画暂无法播放，可取消后重试或跳过动画。'
        finally:
            if self.process:
                if self.process.poll() is None:
                    self.process.kill(); self.process.wait()
                if self.process.stdout: self.process.stdout.close()
            self.done.set()

    def stop(self):
        self.cancelled.set()
        if self.process and self.process.poll() is None:
            try: self.process.terminate()
            except OSError: pass
        if self.thread.is_alive(): self.thread.join(timeout=.25)


class OathVideoScene(QWidget):
    def __init__(self,assets,parent=None):
        super().__init__(parent); self.assets=assets; self.elapsed=None
        self.decoder=None; self.future_frame=None; self.last_index=-1
        self.picture=assets.poster_image if assets else QPixmap()
        self.read_started=None; self.error=''
        self.setMinimumSize(300,400); self.setAccessibleName('掌心相接仪式画面')

    @property
    def ready(self): return self.last_index>=0

    def begin(self):
        self.stop(); self.last_index=-1; self.future_frame=None; self.error=''
        self.picture=self.assets.poster_image; self.read_started=time.monotonic()
        self.decoder=VideoDecoder(self.assets); self.decoder.start()

    def advance(self,elapsed):
        self.elapsed=elapsed
        if self.decoder:
            target=min(self.assets.frame_count-1,max(0,int(elapsed*self.assets.fps)))
            selected=None
            while True:
                if self.future_frame is not None: item=self.future_frame; self.future_frame=None
                else:
                    try: item=self.decoder.frames.get_nowait()
                    except queue.Empty: break
                index,data=item
                if index>target: self.future_frame=item; break
                selected=item
            if selected is not None:
                index,data=selected; width,height=self.assets.decode_size
                image=QImage(data,width,height,width*3,QImage.Format.Format_RGB888).copy()
                self.picture=QPixmap.fromImage(image); self.last_index=index
            self.error=self.decoder.error
            if not self.ready and self.read_started and time.monotonic()-self.read_started>8:
                self.error='动画载入超时，可取消后重试或跳过动画。'
        if self.assets and elapsed>=self.assets.duration: self.picture=self.assets.ending_image
        self.update()

    def finished(self):
        return bool(self.decoder and self.decoder.done.is_set() and not self.decoder.error and
            not self.decoder.cancelled.is_set() and self.last_index==self.assets.frame_count-1)

    def stop(self):
        if self.decoder: self.decoder.stop()

    def picture_rect(self):
        if self.picture.isNull(): return QRectF()
        scale=min((self.width()-40)/self.picture.width(),self.height()/self.picture.height())
        width=self.picture.width()*scale; height=self.picture.height()*scale
        return QRectF(self.width()*.41-width/2,(self.height()-height)/2,width,height)

    def paintEvent(self,event):
        painter=QPainter(self); painter.fillRect(self.rect(),QColor('#1d1c22'))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self.picture.isNull():
            painter.drawPixmap(self.picture_rect(),
                               self.picture,QRectF(self.picture.rect()))
        painter.end()
