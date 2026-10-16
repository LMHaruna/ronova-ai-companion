"""Layered palm-touch ceremony; all images and audio are local assets."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import wave
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget
from client_oath import DURATION, LINES, LINE_TIMES

ASSET_ROOT=Path(__file__).resolve().parent/'design/oath-motion'
REQUIRED=('waiting','body','her-arm','viewer-hand','contact-cover','contact')


def ease(value):
    value=min(1.0,max(0.0,value)); return value*value*(3-2*value)


@dataclass(frozen=True)
class MotionState:
    invitation: float
    reach: float
    contact: float
    zoom: float


def motion_at(elapsed):
    return MotionState(ease((elapsed-.8)/1.2),ease((elapsed-2)/4),
        ease((elapsed-5.85)/.5),1+.025*ease((elapsed-11)/2))


class OathAssets:
    def __init__(self, root=ASSET_ROOT):
        self.root=Path(root); self.images={}; self.layers={}; self.clips=[]
        try:
            saved=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))
            if tuple(saved['lines'])!=LINES: raise ValueError('script mismatch')
            self.canvas=tuple(saved['canvas'])
            if self.canvas!=(1122,1402): raise ValueError('canvas mismatch')
            for name in REQUIRED:
                spec=saved['images'][name]
                path=self.confined(spec['file'])
                if hashlib.sha256(path.read_bytes()).hexdigest()!=spec['sha256']: raise ValueError('image hash mismatch')
                picture=QPixmap(str(path))
                if picture.isNull() or not picture.hasAlphaChannel(): raise ValueError('image missing alpha')
                self.images[name]=picture; self.layers[name]=spec
            ends=[]; starts=[]
            for index,spec in enumerate(saved['audio']):
                path=self.confined(spec['file'])
                if hashlib.sha256(path.read_bytes()).hexdigest()!=spec['sha256']: raise ValueError('audio hash mismatch')
                with wave.open(str(path),'rb') as sound:
                    if sound.getnchannels()!=1 or sound.getsampwidth()!=2: raise ValueError('invalid PCM')
                    seconds=sound.getnframes()/sound.getframerate()
                    if not 0<seconds<30: raise ValueError('invalid audio duration')
                start=max(LINE_TIMES[index],ends[-1]+.45 if ends else 0)
                starts.append(start); ends.append(start+seconds+.4)
                self.clips.append(path)
            if len(self.clips)!=3: raise ValueError('audio incomplete')
            self.line_times=tuple(starts); self.duration=max(DURATION,ends[-1]+.8)
        except (OSError,ValueError,KeyError,TypeError,wave.Error,json.JSONDecodeError,IndexError) as exc:
            raise RuntimeError('仪式素材未就绪，请重新安装完整素材后重试。') from exc

    def confined(self, relative):
        path=(self.root/relative).resolve()
        if not path.is_relative_to(self.root.resolve()): raise ValueError('asset outside package')
        return path


class OathScene(QWidget):
    def __init__(self, assets, parent=None):
        super().__init__(parent); self.assets=assets; self.elapsed=None; self.fade=1.0
        self.setMinimumSize(300,400)
        self.setAccessibleName('掌心相接仪式画面')

    def advance(self, elapsed):
        self.elapsed=elapsed; self.update()

    def paintEvent(self, event):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        backdrop=QLinearGradient(0,0,self.width(),self.height())
        backdrop.setColorAt(0,QColor('#222127')); backdrop.setColorAt(1,QColor('#111116'))
        painter.fillRect(self.rect(),backdrop)
        if self.assets:
            width,height=self.assets.canvas
            scale=min(self.width()/width,self.height()/height)
            painter.translate((self.width()-width*scale)/2,(self.height()-height*scale)/2)
            painter.scale(scale,scale)
            state=motion_at(self.elapsed or 0)
            painter.translate(width/2,height/2); painter.scale(state.zoom,state.zoom); painter.translate(-width/2,-height/2)
            if self.elapsed is None:
                self.draw(painter,'waiting',1)
            elif state.reach<.08:
                self.draw(painter,'waiting',1)
                self.draw(painter,'viewer-hand',state.invitation,dy=340*(1-state.invitation))
            else:
                blend=ease(state.reach/.16)
                self.draw(painter,'waiting',1-blend)
                self.draw(painter,'body',blend*(1-state.contact))
                self.draw(painter,'viewer-hand',state.invitation*(1-state.contact))
                self.draw(painter,'her-arm',blend*(1-state.contact),angle=-30*(1-state.reach))
                self.draw(painter,'contact',state.contact)
                self.draw(painter,'contact-cover',state.contact)
        painter.end()

    def draw(self,painter,name,opacity,dy=0,angle=0):
        if opacity<=0: return
        spec=self.assets.layers[name]; pixmap=self.assets.images[name]
        painter.save(); painter.setOpacity(opacity*self.fade)
        if angle:
            x,y=spec['pivot']; painter.translate(x,y); painter.rotate(angle); painter.translate(-x,-y)
        # A native render mask reuses the exact contact thumb pixels. It prevents
        # cut rectangle edges without producing or editing another raster image.
        if spec.get('clip_polygon'):
            clip=QPainterPath(); points=spec['clip_polygon']; clip.moveTo(*points[0])
            for point in points[1:]: clip.lineTo(*point)
            clip.closeSubpath(); painter.setClipPath(clip)
        target=QRectF(*spec['target']); target.translate(0,dy)
        painter.drawPixmap(target,pixmap,QRectF(*spec.get('source',(0,0,pixmap.width(),pixmap.height()))))
        painter.restore()
