"""Small, local press feedback and playback-driven avatar motion."""
import math
import time
from PySide6.QtCore import Property, QPropertyAnimation, QEasingCurve, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QPushButton


class MotionButton(QPushButton):
    feedback_changed = Signal()
    def __init__(self, text='', parent=None, shape='rect'):
        super().__init__(text, parent)
        self.feedback_shape = shape
        self._feedback = 0.0
        self.feedback_animation = QPropertyAnimation(self, b'feedback', self)
        self.feedback_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._release_pending = False
        self.feedback_animation.finished.connect(self.finish_feedback)
        self.pressed.connect(self.press_feedback)
        self.released.connect(self.release_feedback)

    def get_feedback(self): return self._feedback
    def set_feedback(self, value):
        self._feedback = value; self.update(); self.feedback_changed.emit()
    feedback = Property(float, get_feedback, set_feedback)

    def press_feedback(self):
        # Keep the current position on a rapid re-press. A small initial hint
        # makes a zero-duration click visible without jumping to full scale.
        self._release_pending = False
        if self._feedback < .12: self.set_feedback(.12)
        self.animate_feedback(1, 65)

    def release_feedback(self):
        if self.feedback_animation.endValue()==1 and self._feedback<1:
            self._release_pending=True
        else: self.animate_feedback(0,180)

    def finish_feedback(self):
        if self._release_pending:
            self._release_pending=False; self.animate_feedback(0,180)

    def animate_feedback(self, value, duration):
        self.feedback_animation.stop()
        self.feedback_animation.setDuration(duration)
        self.feedback_animation.setStartValue(self._feedback)
        self.feedback_animation.setEndValue(value)
        self.feedback_animation.start()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._feedback <= .001 or self.feedback_shape=='none' or self.objectName()=='navButton': return
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor('#FFFFFF' if self.feedback_shape=='circle' else '#758194')
        color.setAlphaF(.14*self._feedback)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(color)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.feedback_shape == 'circle':
            inset = (1-self._feedback)*3
            painter.drawEllipse(rect.adjusted(inset, inset, -inset, -inset))
        else: painter.drawRoundedRect(rect, 12, 12)


class HeartButton(MotionButton):
    """A painted heart with matching pointer hit area and normal button access."""
    def __init__(self, text='', parent=None):
        super().__init__(text,parent,shape='none')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet('QPushButton{background:transparent;border:none;padding:0}')

    def heart_path(self):
        rect=QRectF(self.rect()).adjusted(2,2,-2,-2)
        path=QPainterPath()
        def point(x,y): return QPointF(rect.left()+x*rect.width(),rect.top()+y*rect.height())
        path.moveTo(point(.5,.22))
        path.cubicTo(point(.30,-.04),point(0,.06),point(0,.34))
        path.cubicTo(point(0,.60),point(.23,.78),point(.5,1))
        path.cubicTo(point(.77,.78),point(1,.60),point(1,.34))
        path.cubicTo(point(1,.06),point(.70,-.04),point(.5,.22))
        path.closeSubpath()
        return path

    def hitButton(self, position):
        return self.heart_path().contains(QPointF(position))

    def paintEvent(self, event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center=QRectF(self.rect()).center()
        painter.translate(center)
        painter.scale(1-.08*self._feedback,1-.08*self._feedback)
        painter.translate(-center)
        color='#E5435F' if self.underMouse() else '#D93652'
        if not self.isEnabled(): color='#8C4554'
        painter.setBrush(QColor(color))
        painter.setPen(QPen(QColor('#F8BEC7' if self.hasFocus() else '#EB5E76'),1.3))
        painter.drawPath(self.heart_path())
        font=self.font(); font.setPixelSize(max(9,round(self.height()*.19))); font.setBold(True)
        painter.setFont(font); painter.setPen(QColor('#FFFFFF'))
        label='重温' if self.text()=='重温誓约' else self.text()
        painter.drawText(QRectF(0,self.height()*.28,self.width(),self.height()*.36),
                         int(Qt.AlignmentFlag.AlignCenter),label)


class SpeakingMotion:
    """Follow the audible PCM envelope; pauses have no free-running pulse."""
    def __init__(self):
        self.amount = 0.0
        self.last = time.monotonic()

    def tick(self, playing, level=0.0, now=None):
        now = time.monotonic() if now is None else now
        delta = max(0.0,min(.1,now-self.last)); self.last = now
        target = max(0.0,min(1.0,level)) if playing and math.isfinite(level) else 0.0
        speed = .045 if target>self.amount else .14
        self.amount += (target-self.amount)*(1-math.exp(-delta/speed))
        if self.amount < .001: self.amount = 0.0

    def paint(self, painter, avatar, rect):
        painter.save()
        center = rect.center(); strength = self.amount
        if self.amount > .001:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            unit = rect.width()/144
            for index in range(2):
                margin = (7+11*index+(19+7*index)*strength)*unit
                color = QColor('#F26A82'); color.setAlphaF((.62-.22*index)*strength)
                width = max(1.2,(2.6-.8*index)*unit)
                painter.setPen(QPen(color, width))
                painter.drawEllipse(rect.adjusted(-margin, -margin, margin, margin))
        size = rect.width()*(1+.09*strength)
        animated = QRectF(center.x()-size/2, center.y()-size/2, size, size)
        clip = QPainterPath(); clip.addEllipse(animated); painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        painter.drawPixmap(animated, avatar, QRectF(avatar.rect()))
        painter.restore()
        if self.amount > .001:
            painter.save(); painter.setBrush(Qt.BrushStyle.NoBrush)
            color = QColor('#FFCCD6'); color.setAlphaF(.7*strength)
            painter.setPen(QPen(color,max(1.2,rect.width()/72)))
            painter.drawEllipse(animated.adjusted(1,1,-1,-1)); painter.restore()
