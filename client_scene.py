"""Figma 60:2 assets and geometry, rendered independently of service state."""
from pathlib import Path
import hashlib
import json
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageFilter
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtSvg import QSvgRenderer

# User-approved revision: a solid black body and a six-pixel screen bezel.
PHONE_BODY = (800,79,340,709)
PHONE_SCREEN = (806,85,328,697)
PHONE_SCREEN_RADIUS = 44
CHAT_PANEL = PHONE_SCREEN
CHAT_PANEL_RADIUS = PHONE_SCREEN_RADIUS


def phone_clock():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M')

# Original SVG assets are retained. The scene is scaled as one unit.
SLOTS = {
 'imgCallIdleBlurV10':(0,0,1280,802), 'imgVector':(0,0,1280,64),
 'imgVector1':(1160,12,86,40), 'imgVector2':(1175.1,23.1,17.8,17.8),
 'imgVector3':(1181.44,29.43,5.19995,5.2), 'imgVector4':(0,64,1280,738),
 'imgVector5':(710,87,520,520), 'imgVector6':(800,79,340,709),
 'imgVector7':(808,87,324,693), 'imgVector8':(814,93,312,681),
 'imgVector9':(785,127,370,370), 'imgVector10':(918,93,104,25),
 'imgVector11':(1009.15,103.22,5.59998,5.60001),
 'imgVector12':(1069.1,105,17.7,8.7), 'imgVector13':(1076.48,114.53,3,3),
 'imgVector14':(1092.28,103.24,26.5,14.5), 'imgVector15':(1096.06,106.99,18,7),
 'imgVector16':(871.04,194.97,198,198), 'imgVector17':(882.05,206.03,176,176),
 'imgVector18':(881.05,205.03,178,178), 'imgVector19':(878.98,478,182,61),
 'imgVector20':(836,660,60,60), 'imgVector21':(859.63,673.43,12.3,21.3),
 'imgVector22':(853.89,686.74,24.3001,20.3001), 'imgVector23':(940,660,60,60),
 'imgVector24':(954.89,684.57,30.3001,14.55), 'imgVector25':(1060,671,29.3,35.3),
 'imgVector26':(1066.5,684.507,3,3), 'imgVector27':(927,763,86,4),
 'imgPortraitFrom161923V41':(-1.106,0,597,739.431),
}


def pil_pixmap(image):
    image = image.convert('RGBA')
    raw = image.tobytes()
    return QPixmap.fromImage(QImage(raw,image.width,image.height,image.width*4,
                                   QImage.Format.Format_RGBA8888).copy())


def paint_call_control(painter, key, x, amount, muted=False):
    """One 24px rounded-stroke icon family, drawn at the current scene scale."""
    painter.save(); painter.translate(x+30,690+2.5*amount)
    painter.scale(1-.11*amount,1-.11*amount)
    painter.setPen(Qt.PenStyle.NoPen)
    background='#37AA7A' if key in ('answer','voice') else '#D93652' if key=='end' else '#303038'
    painter.setBrush(QColor(background))
    painter.drawEllipse(QRectF(-30,-30,60,60))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor('#FFFFFF'),1.9,Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
    if key=='voice':
        painter.drawRoundedRect(QRectF(-4,-12,8,15),4,4)
        painter.drawArc(QRectF(-8,-6,16,15),180*16,180*16)
        painter.drawLine(QPointF(0,9),QPointF(0,13))
        painter.drawLine(QPointF(-4,13),QPointF(4,13))
        if muted:
            painter.setPen(QPen(QColor(background),5,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(-11,-11),QPointF(11,11))
            painter.setPen(QPen(QColor('#FFFFFF'),1.9,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(-11,-11),QPointF(11,11))
    elif key in ('end','answer'):
        if key=='answer': painter.rotate(-125)
        path=QPainterPath(); path.moveTo(-13,0)
        path.cubicTo(-6,-7,6,-7,13,0); path.lineTo(12,5)
        path.quadTo(11,6,10,5); path.lineTo(6,3); path.lineTo(6,-1)
        path.quadTo(0,-3,-6,-1); path.lineTo(-6,3); path.lineTo(-10,5)
        path.quadTo(-11,6,-12,5); path.closeSubpath(); painter.drawPath(path)
    elif key=='later':
        painter.drawEllipse(QRectF(-12,-12,24,24))
        painter.drawLine(QPointF(0,-7),QPointF(0,1)); painter.drawLine(QPointF(0,1),QPointF(6,4))
    else:
        path=QPainterPath(); path.moveTo(-8,-9); path.lineTo(8,-9)
        path.quadTo(12,-9,12,-5); path.lineTo(12,4); path.quadTo(12,8,8,8)
        path.lineTo(-2,8); path.lineTo(-8,12); path.lineTo(-8,8)
        path.quadTo(-12,8,-12,4); path.lineTo(-12,-5); path.quadTo(-12,-9,-8,-9)
        painter.drawPath(path)
        painter.drawLine(QPointF(-6,-2),QPointF(6,-2))
        painter.drawLine(QPointF(-6,3),QPointF(2,3))
    painter.restore()


class SceneAssets:
    def __init__(self, root):
        self.path = Path(root)/'design/client-assets'
        self.manifest = json.loads((self.path/'manifest.json').read_text(encoding='utf-8'))
        self.svgs = {}
        for asset in self.manifest['assets']:
            path = self.path/asset['file']
            if not path.is_file() or path.stat().st_size == 0 or hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']:
                raise RuntimeError('界面素材缺失或校验失败：'+asset['file'])
            if asset['file'].endswith('.svg'):
                renderer = QSvgRenderer(str(path))
                if not renderer.isValid(): raise RuntimeError('界面矢量素材无法读取')
                self.svgs[asset['name']] = renderer
        files={asset['name']:asset['file'] for asset in self.manifest['assets']}
        mapping=self.manifest.get('relationship_portraits',{'acquaintance':'imgPortraitFrom161923V42'})
        if 'acquaintance' not in mapping: raise RuntimeError('缺少相识立绘')
        self.portrait_sources={}
        self.portraits={}
        for stage,name in mapping.items():
            if stage not in ('acquaintance','familiar','close','bond','angry') or name not in files:
                raise RuntimeError('立绘状态配置无效')
            path=self.path/files[name]
            self.portrait_sources[stage]=path
            self.portraits[stage]=self._portrait_layer(path,stage=='acquaintance',stage in ('familiar','close','bond'),
                                                       820 if stage in ('familiar','close') else None)
        self.stage='acquaintance'
        self.background=self.portraits[self.stage]['background']
        self.foreground=self.portraits[self.stage]['foreground']
        self.avatar = pil_pixmap(Image.open(self.path/files['imgAvatarRonovaOfficial']).resize(
            (144,144),Image.Resampling.LANCZOS))
        self.geometries = {**SLOTS,'imgPortraitFrom161923V42':[(334,-304,1200,1499),(30.08,63.57,596,745)],
                           'imgAvatarRonovaOfficial':(898,222,144,144),
                           'phone_body':PHONE_BODY,'phone_screen':PHONE_SCREEN,
                           'phone_bezel':{'width':6,'color':'#040506'}}

    def _portrait_layer(self, path, original=False, suppress_background_overlap=False, portrait_height=None):
        """Prepare native display effects once; never alter the source PNG."""
        portrait=Image.open(path).convert('RGBA')
        background=portrait.resize((1200,1499),Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(32))
        background.putalpha(background.getchannel('A').point(lambda a:round(a*.4)))
        background=pil_pixmap(background)
        if suppress_background_overlap:
            # Keep the enlarged duplicate outside the foreground portrait slot.
            # Its lower fade must reveal the scene, not another pair of legs or
            # the blurred bitmap's hard left boundary at scene x=334.
            painter=QPainter(background)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            fade=QLinearGradient(30.08+596-334,0,PHONE_BODY[0]-334,0)
            fade.setColorAt(0,QColor(255,255,255,0))
            fade.setColorAt(1,QColor(255,255,255,255))
            painter.fillRect(background.rect(),fade)
            painter.end()
        if original:
            foreground=pil_pixmap(portrait.resize((596,745),Image.Resampling.LANCZOS))
        else:
            # Different seated/standing crops retain their actual proportions.
            image=QImage(str(path))
            if image.isNull(): raise RuntimeError('无法读取立绘：'+path.name)
            # A slightly wider view of the seated poses reveals more
            # lower leg within the same slot, while keeping its proportions.
            target=(596,portrait_height) if portrait_height is not None else (596,745)
            aspect=Qt.AspectRatioMode.KeepAspectRatio if portrait_height is not None else Qt.AspectRatioMode.KeepAspectRatioByExpanding
            scaled=QPixmap.fromImage(image).scaled(*target,aspect,
                                                   Qt.TransformationMode.SmoothTransformation)
            foreground=QPixmap(596,745); foreground.fill(Qt.GlobalColor.transparent)
            painter=QPainter(foreground)
            painter.drawPixmap((596-scaled.width())//2,0,scaled)
            painter.end()
        mask = QImage(596,745,QImage.Format.Format_ARGB32_Premultiplied)
        mask.fill(Qt.GlobalColor.transparent)
        painter = QPainter(mask)
        self.svgs['imgPortraitFrom161923V41'].render(painter,QRectF(*SLOTS['imgPortraitFrom161923V41']))
        if not original:
            # The original right-side fade plus soft crop boundaries for the
            # newly supplied illustrations, especially their lower-leg cut.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            vertical=QLinearGradient(0,0,0,745)
            for position,alpha in ((0,0),(.035,255),(.82,255),(1,0)):
                vertical.setColorAt(position,QColor(255,255,255,alpha))
            painter.fillRect(mask.rect(),vertical)
            left_edge=(596-scaled.width())//2 if portrait_height is not None else 0
            left=QLinearGradient(left_edge,0,left_edge+596,0)
            for position,alpha in ((0,0),(.025,255),(1,255)):
                left.setColorAt(position,QColor(255,255,255,alpha))
            painter.fillRect(mask.rect(),left)
            if portrait_height is not None:
                right_edge=left_edge+scaled.width()
                # Fade to the resized image's own edge so its rectangular
                # boundary blends into the scene at every window scale.
                right=QLinearGradient(right_edge-72,0,right_edge,0)
                for position,alpha in ((0,255),(.3,230),(.7,90),(1,0)):
                    right.setColorAt(position,QColor(255,255,255,alpha))
                painter.fillRect(mask.rect(),right)
            caption=QRadialGradient(80,670,230)
            for position,alpha in ((0,40),(.5,100),(.85,220),(1,255)):
                caption.setColorAt(position,QColor(255,255,255,alpha))
            painter.fillRect(mask.rect(),caption)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(QRectF(0,0,596,1),Qt.GlobalColor.transparent)
            painter.fillRect(QRectF(0,0,1,745),Qt.GlobalColor.transparent)
            painter.fillRect(QRectF(595,0,1,745),Qt.GlobalColor.transparent)
        painter.end()
        faded=QPixmap(foreground.size())
        faded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(faded)
        painter.drawPixmap(0,0,foreground)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0,0,mask)
        painter.end()
        return {'background':background,'foreground':faded}

    def set_stage(self, stage):
        stage='close' if stage=='bond' and 'bond' not in self.portraits else stage
        stage=stage if stage in self.portraits else 'acquaintance'
        if stage==self.stage: return False
        self.stage=stage
        self.background=self.portraits[stage]['background']
        self.foreground=self.portraits[stage]['foreground']
        return True

    def svg(self,painter,name,position=None):
        self.svgs[name].render(painter,QRectF(*(position or SLOTS[name])))

    def paint_phone_header(self,painter,light=False):
        """The same camera, clock, signal and battery geometry on every page."""
        painter.save()
        for name in ('imgVector10','imgVector11'): self.svg(painter,name)
        color=QColor('#252B35' if light else '#F5F5F7')
        painter.setBrush(Qt.BrushStyle.NoBrush); painter.setPen(QPen(color,1.4))
        painter.drawRoundedRect(QRectF(1092,104,25,12),3,3)
        painter.fillRect(QRectF(1095,107,17,6),color)
        painter.drawLine(1119,108,1119,112)
        painter.drawArc(QRectF(1067,104,20,15),35*16,110*16)
        painter.drawArc(QRectF(1071,108,12,9),35*16,110*16)
        painter.drawPoint(QPointF(1077,115))
        font=QFont('Segoe UI'); font.setPixelSize(12); font.setBold(True); painter.setFont(font)
        painter.drawText(QRectF(835,104,70,22),phone_clock())
        painter.restore()

    def paint(self,painter,motion=None,feedback=None,muted=False):
        self.paint_static(painter)
        self.paint_dynamic(painter,motion,feedback,muted)
        self.paint_phone_header(painter)

    def paint_static(self,painter,phone=True):
        """One relationship state's reusable portrait, background and phone."""
        self.svg(painter,'imgCallIdleBlurV10')
        self.svg(painter,'imgVector4')
        painter.drawPixmap(334,-304,self.background)
        # Keep the ambient portrait layer below the solid title bar.
        self.svg(painter,'imgVector')
        painter.drawPixmap(QPointF(30.08,63.57),self.foreground)
        for name in ['imgVector1','imgVector2','imgVector3']:
            self.svg(painter,name)
        if phone: self.svg(painter,'imgVector5')
        painter.setBrush(QColor('#040506')); painter.setPen(QPen(QColor('#36393C'),1))
        painter.drawRoundedRect(QRectF(*PHONE_BODY),50,50)
        if not phone: return
        painter.save()
        screen = QPainterPath(); screen.addRoundedRect(QRectF(*PHONE_SCREEN),PHONE_SCREEN_RADIUS,PHONE_SCREEN_RADIUS)
        painter.setClipPath(screen)
        self.svg(painter,'imgVector8',PHONE_SCREEN)
        for name in ['imgVector9',*[f'imgVector{i}' for i in range(16,20)]]: self.svg(painter,name)
        self.svg(painter,'imgVector27')
        painter.restore()

    def paint_dynamic(self,painter,motion=None,feedback=None,muted=False,incoming=False):
        feedback=feedback or {}
        painter.save()
        screen = QPainterPath(); screen.addRoundedRect(QRectF(*PHONE_SCREEN),PHONE_SCREEN_RADIUS,PHONE_SCREEN_RADIUS)
        painter.setClipPath(screen,Qt.ClipOperation.IntersectClip)
        if motion is not None:
            motion.paint(painter,self.avatar,QRectF(898,222,144,144))
        else:
            painter.save()
            clip = QPainterPath(); clip.addEllipse(QRectF(898,222,144,144))
            painter.setClipPath(clip,Qt.ClipOperation.IntersectClip)
            painter.drawPixmap(QRectF(898,222,144,144),self.avatar,QRectF(self.avatar.rect()))
            painter.restore()
        for key,x in (('voice',836),('end',940),('text',1044)):
            icon='answer' if incoming and key=='voice' else 'later' if incoming and key=='text' else key
            paint_call_control(painter,icon,x,feedback.get(key,0),muted)
        painter.restore()
