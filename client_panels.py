"""Main-screen chat and phone settings, scaled with the desktop scene."""
from dataclasses import replace
from fractions import Fraction
from concurrent.futures import Future
from threading import Thread
import time
import math
from datetime import datetime
from client_calls import SHANGHAI

from PySide6.QtCore import (QEvent, QEasingCurve, QPoint, QPointF, Property, QPropertyAnimation, QRectF,
                           QTimer, Qt, QUrl, Signal)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QImage, QPainter, QPainterPath,
                          QPen, QPixmap, QTransform)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QGraphicsOpacityEffect,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget,
    QVBoxLayout, QWidget)
from client_scene import PHONE_BODY, PHONE_SCREEN, PHONE_SCREEN_RADIUS, CHAT_PANEL, CHAT_PANEL_RADIUS, phone_clock
from client_motion import MotionButton
from voice_input import input_devices
from pcm_player import output_devices

PHONE_CSS = '''
QWidget {background:transparent;color:#F5F5F7;font-family:"Microsoft YaHei UI";font-size:13px;}
QWidget#phoneSurface {background:#F5F6F8;}
QWidget#chatPage {background:#F5F6F8;color:#252B35;}
QWidget#chatPage QWidget {color:#252B35;}
QWidget#chatPage QPushButton#navButton {color:#515B6B;}
QWidget#chatPage QPushButton#actionButton {color:#687487;font-size:11px;}
QWidget#chatPage QPushButton#actionButton:disabled {color:#B4BBC6;}
QWidget#chatPage QPushButton#sendButton {background:#D93652;color:white;}
QWidget#chatPage QPushButton#sendButton:disabled {background:#E8ECF2;color:#AAB3C2;}
QWidget#chatPage QWidget#composer {background:white;border:1px solid #E4E7ED;border-radius:17px;}
QWidget#chatPage QPlainTextEdit {color:#252B35;selection-background-color:#F4C7D2;}
QWidget#chatPage QLabel#noteLabel {color:#89919C;font-size:11px;}
QWidget#chatPage QScrollBar::handle:vertical {background:#CBD1DA;}
QWidget#settingsPage {background:#F5F6F8;color:#252B35;}
QWidget#settingsPage QWidget {color:#252B35;}
QWidget#settingsPage QWidget#groupCard {background:#FFFFFF;border-radius:16px;}
QWidget#settingsPage QWidget#profileCard {background:transparent;}
QWidget#settingsPage QPushButton {color:#252B35;}
QWidget#settingsPage QPushButton#navButton {color:#252B35;}
QWidget#settingsPage QPushButton#settingsRow:hover {background:transparent;border-radius:12px;}
QWidget#settingsPage QPushButton#primaryButton {background:#D93652;color:white;}
QWidget#settingsPage QPushButton#secondaryButton {background:white;}
QWidget#settingsPage QLabel#noteLabel {color:#89919C;font-size:11px;}
QWidget#settingsPage QSlider::groove:horizontal {background:#E7EAF0;height:3px;}
QWidget#settingsPage QSlider::sub-page:horizontal {background:#D93652;height:3px;}
QWidget#settingsPage QSlider::handle:horizontal {background:#FFFFFF;border:1px solid #D8DDE5;}
QWidget#settingsPage QLineEdit#modelInput {background:#F6F7F9;color:#252B35;border:1px solid #E7EAF0;border-radius:10px;padding:8px 9px;font-size:12px;selection-background-color:#F4C7D2;}
QWidget#settingsPage QLineEdit#modelInput:focus {border-color:#ABB5C4;}
QWidget#groupCard {background:#1C1C1E;border-radius:14px;}
QWidget#composer {background:#1C1C1E;border-radius:18px;}
QPushButton {background:transparent;border:0;padding:0;color:#F5F5F7;}
QPushButton:focus,QPushButton:hover {border:0;}
QPushButton:disabled {color:#66666C;}
QPushButton#navButton {color:#72AAFF;text-align:left;font-size:14px;}
QPushButton#navButton:hover,QPushButton#navButton:pressed,QPushButton#navButton:focus {background:transparent;border:0;}
QPushButton#settingsRow:hover {background:transparent;border-radius:12px;}
QPushButton#actionButton {color:#A5A5AD;font-size:11px;padding:6px 2px;}
QPushButton#actionButton:disabled {color:#515158;}
QPushButton#sendButton {background:#E52B3B;border-radius:17px;font-size:23px;font-weight:bold;}
QPushButton#sendButton:disabled {background:#3A3A40;color:#73737A;}
QPushButton#primaryButton {background:#E52B3B;border-radius:12px;padding:13px;font-weight:bold;}
QPushButton#secondaryButton {background:#242428;border-radius:12px;padding:13px;}
QLabel#sectionLabel {color:#929299;font-size:11px;padding-left:14px;}
QLabel#noteLabel {color:#929299;font-size:11px;}
QLabel#pageTitle {font-size:29px;font-weight:bold;}
QLabel#navTitle {font-size:15px;font-weight:bold;}
QPlainTextEdit {background:transparent;border:0;padding:3px;color:#F5F5F7;selection-background-color:#6B3040;}
QScrollArea {background:transparent;border:0;}
QScrollBar:vertical {background:transparent;width:3px;margin:2px 0;}
QScrollBar::handle:vertical {background:#45454B;min-height:24px;border-radius:1px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:transparent;}
QSlider::groove:horizontal {height:4px;background:#45454B;border-radius:2px;}
QSlider::sub-page:horizontal {background:#F5F5F7;border-radius:2px;}
QSlider::handle:horizontal {background:white;border:0;width:19px;margin:-8px 0;border-radius:9px;}
'''


def button(text, callback, name='navButton'):
    result = MotionButton(text)
    result.setObjectName(name)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    result.clicked.connect(callback)
    result.setMinimumHeight(34)
    return result


def note(text):
    label = QLabel(text)
    label.setObjectName('noteLabel')
    label.setWordWrap(True)
    return label


def ronova_status(state, profile, now=None):
    if profile['angry']: return '想静一静'
    if state.get('incoming'): return '想找你聊聊'
    if state.get('call_requested'): return '陪你聊天'
    if state.get('responding') or state.get('busy'): return '忙碌'
    if profile['stage'] in ('close','bond'): return '开心'
    hour=(now or datetime.now(SHANGHAI)).hour
    if 9<=hour<18: return '忙碌'
    if hour>=21 or hour<3: return '加班中'
    return '休息中'


def navigation(back_text, title, back, trailing=None, avatar=None, leading=None):
    bar = QWidget()
    bar.setFixedHeight(44)
    layout = QHBoxLayout(bar)
    layout.setContentsMargins(14, 0, 14, 0)
    back_button = leading if leading is not None else button(back_text, back)
    back_button.setFixedWidth(82)
    layout.addWidget(back_button)
    heading = QLabel(title)
    heading.setObjectName('navTitle')
    heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if avatar is None: layout.addWidget(heading, 1)
    else:
        identity = QWidget(); identity_layout = QHBoxLayout(identity)
        identity_layout.setContentsMargins(0,0,0,0); identity_layout.setSpacing(7)
        identity_layout.addStretch(); identity_layout.addWidget(avatar)
        identity_layout.addWidget(heading); identity_layout.addStretch()
        layout.addWidget(identity,1)
    if trailing is None:
        blank = QWidget(); blank.setFixedWidth(82); layout.addWidget(blank)
    else:
        trailing.setFixedWidth(82); layout.addWidget(trailing)
    return bar


class ElidedLabel(QLabel):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        if self.text()=='✓':
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor('#D93652'),2.2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
            x,y=self.width()/2,self.height()/2
            painter.drawPolyline([QPointF(x-6,y),QPointF(x-2,y+4),QPointF(x+7,y-6)])
            return
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, self.width())
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), text)


class SettingsIcon(QWidget):
    def __init__(self, name, color):
        super().__init__(); self.name=name; self.setFixedSize(24,24)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#515B6B'),1.6,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.name=='engine':
            painter.drawRoundedRect(QRectF(6,6,12,12),3,3)
            painter.drawRoundedRect(QRectF(10,10,4,4),1,1)
            for position in (9,15):
                painter.drawLine(position,3,position,6); painter.drawLine(position,18,position,21)
                painter.drawLine(3,position,6,position); painter.drawLine(18,position,21,position)
        elif self.name=='service':
            painter.drawRoundedRect(QRectF(4,9,10,7),3,3); painter.drawRoundedRect(QRectF(10,5,10,7),3,3)
        elif self.name=='mode':
            path=QPainterPath(); path.moveTo(8,4); path.cubicTo(5,4,4,7,5,10)
            path.cubicTo(7,15,11,19,16,20); path.cubicTo(19,20,21,17,19,15)
            path.lineTo(16,13); path.lineTo(13,15); path.lineTo(9,11); path.lineTo(11,8)
            path.closeSubpath(); painter.drawPath(path)
        elif self.name=='mic':
            painter.drawRoundedRect(QRectF(9,3,6,11),3,3)
            painter.drawArc(QRectF(6,7,12,11),180*16,180*16)
            painter.drawLine(12,18,12,21); painter.drawLine(9,21,15,21)
        elif self.name=='speaker':
            path=QPainterPath(); path.moveTo(5,10); path.lineTo(9,10); path.lineTo(13,6)
            path.lineTo(13,19); path.lineTo(9,15); path.lineTo(5,15); path.closeSubpath(); painter.drawPath(path)
            painter.drawArc(QRectF(12,7,9,11),-60*16,120*16)
        elif self.name=='autoplay':
            path=QPainterPath(); path.moveTo(9,6); path.lineTo(19,12.5); path.lineTo(9,19); path.closeSubpath()
            painter.drawEllipse(QRectF(3,3,18,18)); painter.drawPath(path)
        elif self.name=='memory':
            painter.drawRoundedRect(QRectF(5,4,14,17),3,3)
            painter.drawLine(9,9,15,9); painter.drawLine(9,13,15,13)
            painter.drawLine(9,17,13,17)
        else:
            painter.drawLine(5,7,19,7); painter.drawLine(9,4,15,4)
            painter.drawRoundedRect(QRectF(7,7,10,14),2,2)
            painter.drawLine(10,11,10,17); painter.drawLine(14,11,14,17)


class Chevron(QWidget):
    def __init__(self):
        super().__init__(); self.setFixedSize(10,20)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#BAC0C9'),1.5,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        painter.drawPolyline([QPointF(3,5),QPointF(7,10),QPointF(3,15)])


class FoldoutChevron(Chevron):
    def __init__(self):
        super().__init__(); self.progress = 0.0

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#89919C'),1.5,Qt.PenStyle.SolidLine,
                           Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        painter.translate(5,10); painter.rotate(90*self.progress)
        painter.drawPolyline([QPointF(-2,-5),QPointF(2,0),QPointF(-2,5)])


class ModelFoldout(QWidget):
    """Reveal a full-size form by animating its clipped container height."""
    def __init__(self, form, chevron):
        super().__init__(); self.form = form; form.setParent(self)
        form.installEventFilter(self)
        self.chevron = chevron; self.expanded = False; self._reveal = 0.0
        self._form_width = None; self._form_height = 0
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.setFixedHeight(0); self.hide(); self.setEnabled(False)
        self.animation = QPropertyAnimation(self,b'reveal',self)
        self.animation.setDuration(360)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self.finish)

    def get_reveal(self): return self._reveal

    def set_reveal(self, value):
        # Reserve space below the full form for the expansion's visible
        # overshoot. The fields retain their full geometry throughout.
        self._reveal = min(1.10,max(0.0,value))
        self.chevron.progress = min(1.0,self._reveal); self.chevron.update()
        self.place_form()

    reveal = Property(float,get_reveal,set_reveal)

    def place_form(self):
        width = max(1,self.width())
        height = self.form.heightForWidth(width) if self.form.hasHeightForWidth() else self.form.sizeHint().height()
        height = max(height,self.form.minimumSizeHint().height())
        if self._form_width!=width or self._form_height!=height:
            self._form_height = height
            self._form_width=width
            self.form.setGeometry(0,0,width,self._form_height)
        visible_height = round(self._form_height*self._reveal)
        if self.height()!=visible_height: self.setFixedHeight(visible_height)

    def eventFilter(self, watched, event):
        if watched is self.form and event.type()==QEvent.Type.LayoutRequest:
            self.place_form()
        return super().eventFilter(watched,event)

    def resizeEvent(self, event):
        super().resizeEvent(event); self.place_form()

    def animate(self, expanded):
        self.expanded = expanded; self.animation.stop()
        self.setEnabled(expanded)
        if expanded: self.show(); self.place_form()
        distance=abs(float(expanded)-self._reveal)
        if distance<.001:
            self.set_reveal(float(expanded)); self.finish(); return
        curve=QEasingCurve(QEasingCurve.Type.OutBack if expanded else QEasingCurve.Type.OutCubic)
        if expanded: curve.setOvershoot(1.5)
        self.animation.setEasingCurve(curve)
        self.animation.setDuration(max(90,round((360 if expanded else 200)*distance)))
        self.animation.setStartValue(self._reveal)
        self.animation.setEndValue(float(expanded)); self.animation.start()

    def finish(self):
        if not self.expanded: self.hide()


class Switch(QAbstractButton):
    """An accessible binary switch with an animated thumb."""
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True); self.setChecked(checked)
        self.setFixedSize(42, 25)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName('自动播放文字回复')
        self._position = float(checked)
        self.animation = QPropertyAnimation(self, b'position', self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self.animate)

    def get_position(self): return self._position
    def set_position(self, value): self._position = value; self.update()
    position = Property(float, get_position, set_position)

    def animate(self, checked):
        self.animation.stop()
        self.animation.setStartValue(self._position)
        self.animation.setEndValue(float(checked)); self.animation.start()

    def sync(self, checked):
        if checked == self.isChecked(): return
        self.blockSignals(True); self.setChecked(checked); self.blockSignals(False)
        self.animate(checked)

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1 if self.isEnabled() else .45)
        painter.setPen(Qt.PenStyle.NoPen)
        off,on=QColor('#D9DDE4'),QColor('#D93652')
        painter.setBrush(QColor.fromRgbF(*[
            start+(end-start)*self._position
            for start,end in zip(off.getRgbF()[:3],on.getRgbF()[:3])]))
        painter.drawRoundedRect(QRectF(0, 0, 42, 25), 12.5, 12.5)
        painter.setBrush(QColor('#FFFFFF'))
        painter.drawEllipse(QRectF(2 + 17*self._position, 2, 21, 21))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush); painter.setPen(QPen(QColor('#D93652'), 1))
            painter.drawRoundedRect(QRectF(1, 1, 40, 23), 11.5, 11.5)


class PageTransition(QWidget):
    """Blend complete frames; interrupted navigation captures the visible blend."""
    def __init__(self, parent):
        super().__init__(parent); self.snapshot=None; self.incoming=None; self._progress=1.0
        self.direction=0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.animation=QPropertyAnimation(self,b'progress',self)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation.finished.connect(self.finish)
        self.hide()

    def get_progress(self): return self._progress
    def set_progress(self, value): self._progress=value; self.update()
    progress=Property(float,get_progress,set_progress)

    def start(self, snapshot, incoming, duration=280, direction=0, bounds=None, content_rect=None):
        self.animation.stop(); self.snapshot=snapshot; self.incoming=incoming
        self.direction=direction; self.set_progress(0)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic if direction else QEasingCurve.Type.InOutCubic)
        self.setGeometry(self.parentWidget().rect() if bounds is None else bounds)
        self.content_rect=QRectF(self.rect()) if content_rect is None else content_rect
        self.show(); self.raise_()
        self.animation.setDuration(duration)
        self.animation.setStartValue(0); self.animation.setEndValue(1); self.animation.start()

    def finish(self):
        self.hide(); self.snapshot=None; self.incoming=None

    def cancel(self):
        self.animation.stop(); self.finish()

    def paintEvent(self,event):
        if self.snapshot is not None:
            painter=QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.fillRect(self.rect(),QColor('#F5F6F8'))
            if not self.direction:
                # The main phone compositor already contains opaque frames.
                painter.drawPixmap(QPointF(),self.snapshot)
                painter.setOpacity(self._progress)
                painter.drawPixmap(QPointF(),self.incoming)
                return
            content=self.content_rect; width=content.width(); progress=self._progress
            painter.drawPixmap(QPointF(),self.snapshot)
            painter.setOpacity(progress); painter.drawPixmap(QPointF(),self.incoming); painter.setOpacity(1)
            painter.setClipRect(content)
            def page(frame, offset):
                painter.save(); painter.translate(offset,0)
                # Child widgets have transparent backgrounds. Every moving
                # page needs its own backing, including the blank areas, so
                # old controls are fully covered before the compositor leaves.
                painter.setClipRect(content,Qt.ClipOperation.IntersectClip)
                painter.fillRect(content,QColor('#F5F6F8'))
                painter.drawPixmap(QPointF(),frame); painter.restore()
            if self.direction>0:
                page(self.snapshot,-width*.22*progress)
                page(self.incoming,width*(1-progress))
            else:
                page(self.incoming,-width*.22*(1-progress))
                page(self.snapshot,width*progress)


class AnimatedStack(QStackedWidget):
    def __init__(self,parent=None):
        super().__init__(parent); self.transition=PageTransition(self); self._trail=[]

    def setCurrentWidget(self, widget, *, animate=True):
        if self.indexOf(widget)<0: return
        if widget is self.currentWidget(): return
        visible=animate and self.isVisible() and self.currentWidget() is not None
        ancestor=self.parentWidget(); overlay=None
        while ancestor is not None and overlay is None:
            overlay=getattr(getattr(ancestor,'main',None),'overlay',None)
            ancestor=ancestor.parentWidget()
        context=overlay.stack_context(self) if visible and overlay and overlay.isVisible() else None
        if context:
            bounds,content=context
            snapshot=overlay.main.grab(bounds)
            overlay.cancel_subpages()
            if overlay.transition.snapshot is not None:
                overlay.transition.cancel(); overlay.finished()
        else: snapshot=self.grab() if visible else None
        self.transition.cancel()
        current=self.currentWidget()
        if not self._trail and current is not None: self._trail.append(current)
        direction=-1 if widget in self._trail else 1
        if widget in self._trail: self._trail=self._trail[:self._trail.index(widget)+1]
        else: self._trail.append(widget)
        super().setCurrentIndex(self.indexOf(widget))
        if animate and snapshot is not None:
            # Scroll areas settle their ranges through posted layout requests.
            # Capture the final viewport, not a transient scrollbar/layout.
            for _ in range(2): QApplication.sendPostedEvents(None,QEvent.Type.LayoutRequest)
            if context:
                # Capture through the actual view at its display transform. A
                # widget grab inside a proxy can rasterize text at a different
                # pixel phase, then sharpen/move when the real page returns.
                incoming=overlay.main.grab(bounds)
                self.transition.setParent(overlay.main)
                self.transition.start(snapshot,incoming,duration=320,direction=direction,
                                      bounds=bounds,content_rect=content)
            else:
                self.transition.start(snapshot,self.grab(),duration=320,direction=direction)

    def setCurrentIndex(self, index):
        widget=self.widget(index)
        if widget is not None: self.setCurrentWidget(widget)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'transition'): self.transition.cancel()


class SettingsRow(MotionButton):
    def __init__(self, title, value='', color=None, icon='', callback=None, toggle=None, disclosure=True, subtitle=''):
        super().__init__()
        self.setObjectName('settingsRow'); self.setMinimumHeight(48)
        self.setAccessibleName(title); self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self); layout.setContentsMargins(13, 7, 13, 7); layout.setSpacing(9)
        if color:
            layout.addWidget(SettingsIcon(icon,color))
        self.title = QLabel(title)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if subtitle:
            column = QVBoxLayout(); column.setContentsMargins(0,0,0,0); column.setSpacing(3)
            column.addWidget(self.title)
            self.subtitle = QLabel(subtitle); self.subtitle.setWordWrap(True)
            self.subtitle.setStyleSheet('color:#89919C;font-size:11px;')
            self.subtitle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            column.addWidget(self.subtitle); layout.addLayout(column,1)
        else: layout.addWidget(self.title)
        if not subtitle: layout.addStretch(1)
        self.value = ElidedLabel(value); self.value.setMaximumWidth(128)
        self.value.setStyleSheet('color:#89919C;font-size:12px;')
        self.value.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.value)
        if toggle:
            layout.addWidget(toggle)
            self.clicked.connect(toggle.click)
        elif callback:
            if disclosure:
                layout.addWidget(Chevron())
            # QPushButton's checked argument must never replace a choice value.
            self.clicked.connect(lambda _checked=False:callback())


class InlineChoicePicker(QWidget):
    """An inline disclosure with the same spring finish as model cards."""
    currentIndexChanged = Signal(int)

    def __init__(self):
        super().__init__(); self._choices=[]; self._index=-1; self.options=[]
        self.setObjectName('ttsEngineInput')
        self.setStyleSheet('QWidget#ttsEngineInput {background:white;border:1px solid #E4E7ED;border-radius:10px;}')
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        self.header=SettingsRow('',callback=self.toggle,disclosure=False)
        self.setFocusProxy(self.header)
        self.header.setMinimumHeight(40);self.header.layout().setContentsMargins(10,5,10,5)
        self.header.title.setWordWrap(True);self.header.title.setStyleSheet('font-size:12px;')
        self.header.title.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        self.header.layout().setStretch(0,1);self.header.layout().setStretch(1,0)
        self.header.value.hide()
        self.chevron=FoldoutChevron();self.header.layout().addWidget(self.chevron)
        self.header.setAccessibleDescription('点击展开语音合成引擎选项')
        layout.addWidget(self.header)
        form=QWidget();self.option_layout=QVBoxLayout(form)
        self.option_layout.setContentsMargins(4,0,4,6);self.option_layout.setSpacing(0)
        self.foldout=ModelFoldout(form,self.chevron);layout.addWidget(self.foldout)

    def addItem(self, text, data):
        index=len(self._choices);self._choices.append((text,data))
        option=SettingsRow(text,callback=lambda:self.choose(index),disclosure=False)
        option.setMinimumHeight(39);option.title.setWordWrap(True)
        option.title.setStyleSheet('font-size:12px;')
        option.title.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        option.layout().setStretch(0,1);option.layout().setStretch(1,0)
        option.layout().setContentsMargins(8,5,8,5)
        option.value.setFixedWidth(18);option.value.setStyleSheet('color:#D93652;font-size:17px;')
        option.setProperty('choice',data);self.option_layout.addWidget(option);self.options.append(option)
        if self._index<0:self.setCurrentIndex(0)

    def count(self):return len(self._choices)
    def itemData(self,index):return self._choices[index][1]
    def itemText(self,index):return self._choices[index][0]
    def findData(self,data):return next((i for i,item in enumerate(self._choices) if item[1]==data),-1)
    def currentIndex(self):return self._index
    def currentData(self):return self.itemData(self._index) if self._index>=0 else None
    def currentText(self):return self.itemText(self._index) if self._index>=0 else ''

    def setCurrentIndex(self,index):
        if not 0<=index<self.count():return
        changed=index!=self._index;self._index=index
        self.header.title.setText(self.currentText());self.header.setAccessibleName(self.currentText())
        for i,option in enumerate(self.options):option.value.setText('✓' if i==index else '')
        if changed:self.currentIndexChanged.emit(index)

    def toggle(self):
        self.hidePopup() if self.foldout.expanded else self.showPopup()

    def showPopup(self):
        if not self.isEnabled():return
        self.header.setAccessibleDescription('点击收起语音合成引擎选项')
        self.foldout.animate(True)

    def hidePopup(self):
        self.header.setAccessibleDescription('点击展开语音合成引擎选项')
        self.foldout.animate(False)

    def choose(self,index):
        self.setCurrentIndex(index);self.hidePopup();self.header.setFocus()


def group(rows):
    card = QWidget(); card.setObjectName('groupCard')
    layout = QVBoxLayout(card); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
    for index, row in enumerate(rows):
        if index:
            line = QFrame(); line.setFixedHeight(1); line.setStyleSheet('background:#F0F2F5;margin-left:48px;margin-right:16px;')
            layout.addWidget(line)
        layout.addWidget(row)
    return card


class ChatAvatar(QWidget):
    def __init__(self, main, own=False, size=34, animated=False):
        super().__init__(); self.main=main; self.own=own; self.animated=animated
        self.setObjectName('userAvatar' if own else 'ronovaAvatar')
        self.setAccessibleName('你的头像' if own else '若娜瓦头像')
        self.setFixedSize(size,size)

    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.own:
            painter.setBrush(QColor('#E8ECF2')); painter.drawRoundedRect(rect,11,11)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#7E8A9D'),1.6,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
            scale=self.width()/34; painter.scale(scale,scale)
            painter.drawEllipse(QRectF(12,7,10,10))
            painter.drawArc(QRectF(7,19,20,17),0,180*16)
        else:
            if self.animated:
                self.main.speaking_motion.paint(painter,self.main.assets.avatar,rect.adjusted(3,3,-3,-3))
            else:
                clip=QPainterPath(); clip.addRoundedRect(rect,10,10); painter.setClipPath(clip)
                painter.drawPixmap(rect,self.main.assets.avatar,QRectF(self.main.assets.avatar.rect()))


class ReplyActionButton(MotionButton):
    """Compact, accessible icons under the reply they operate on."""
    labels = {'retry':'重试回复', 'replay':'重播语音', 'stop':'停止语音'}

    def __init__(self, action, record_id, callback):
        super().__init__('', shape='none')
        self.action = action
        self.setObjectName('replyActionButton')
        self.setProperty('recordId',record_id); self.setProperty('replyAction',action)
        self.setFixedSize(28,28)
        self.setAccessibleName(self.labels[action]); self.setToolTip(self.labels[action])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(callback)

    def paintEvent(self, event):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isEnabled() and (self.underMouse() or self.hasFocus() or self.feedback>.001):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#F4DCE3' if self.feedback>.001 else '#E8ECF2'))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),6,6)
        painter.translate(self.width()/2,self.height()/2)
        color=QColor('#687487' if self.isEnabled() else '#BBC2CC')
        painter.setPen(QPen(color,1.5,Qt.PenStyle.SolidLine,
                           Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.action=='retry':
            painter.drawArc(QRectF(-7,-7,14,14),40*16,295*16)
            path=QPainterPath(); path.moveTo(2,-7); path.lineTo(7,-7); path.lineTo(7,-2)
            painter.drawPath(path)
        elif self.action=='replay':
            # The speaker spans x=-7..14; center those bounds inside 28px.
            # The old translation put the outer wave on the clipping edge.
            painter.translate(-3.5,0)
            path=QPainterPath(); path.moveTo(-7,-3); path.lineTo(-3,-3)
            path.lineTo(1,-7); path.lineTo(1,7); path.lineTo(-3,3)
            path.lineTo(-7,3); path.closeSubpath(); painter.drawPath(path)
            painter.drawArc(QRectF(0,-5,9,10),-65*16,130*16)
            painter.drawArc(QRectF(0,-8,14,16),-60*16,120*16)
        else:
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(color)
            painter.drawRoundedRect(QRectF(-5,-5,10,10),1.5,1.5)


class MessageHistory(QScrollArea):
    def __init__(self, main):
        super().__init__(); self.main=main; self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget(); self.setWidget(self.content)
        self.rows = QVBoxLayout(self.content); self.rows.setContentsMargins(14, 8, 14, 8); self.rows.setSpacing(14)
        self.last = None; self.plain_text = ''; self.active_avatar=None
        self.action_buttons = {}
        self.reply_avatars = {}
        self._messages = {}; self._empty_mode = None
        self._scroll_target = None; self._restoring_scroll = False
        self._scroll_timer = QTimer(self); self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self.restore_scroll)
        self.verticalScrollBar().rangeChanged.connect(lambda *_:self._scroll_timer.start(0))
        self.verticalScrollBar().valueChanged.connect(self.remember_scroll)

    def toPlainText(self): return self.plain_text

    def remember_scroll(self, value):
        if self._restoring_scroll or self._scroll_timer.isActive(): return
        self._scroll_target = None if value >= self.verticalScrollBar().maximum()-15 else value

    def restore_scroll(self):
        scrollbar = self.verticalScrollBar()
        self._restoring_scroll = True
        try:
            scrollbar.setValue(scrollbar.maximum() if self._scroll_target is None else self._scroll_target)
        finally: self._restoring_scroll = False

    def refresh(self, records):
        signature = tuple((row['id'], row['user'], row['assistant'], row['state']) for row in records)
        if signature == self.last:
            self.sync_actions(records)
            if self.active_avatar: self.active_avatar.update()
            return
        latest_changed = self.last is None or signature[-1:] != self.last[-1:]
        self.last = signature
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum()-15
        self._scroll_target = None if latest_changed or at_bottom else scrollbar.value()
        # Bubble wrapping can update the range after this refresh. Follow that
        # settled range too, so a growing reply stays at the newest line.
        self._scroll_timer.start(0)
        self.viewport().setUpdatesEnabled(False)
        try:
            empty_mode=not records
            if self._empty_mode!=empty_mode:
                while self.rows.count():
                    item=self.rows.takeAt(0)
                    if item.widget():item.widget().hide();item.widget().deleteLater()
                self._messages.clear();self._empty_mode=empty_mode
                if empty_mode:
                    empty=note('凡人，想说什么就写下来。')
                    empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.rows.addStretch(1);self.rows.addWidget(empty);self.rows.addStretch(2)
                else:self.rows.addStretch(1)
            desired=[(row['id'],own) for row in records for own in (True,False) if not own or row['user']]
            retained=set(desired)
            for key in list(self._messages):
                if key not in retained:
                    message=self._messages.pop(key)['widget']
                    self.rows.removeWidget(message);message.hide();message.deleteLater()
            self.plain_text='';position=0
            labels={'pending':'回应中…','failed':'回复失败 · 可重试','cancelled':'已中断','interrupted':'已中断','completed':''}
            for row in records:
                for own,text in ((True,row['user']),(False,row['assistant'] or '…')):
                    if own and not text:continue
                    key=(row['id'],own)
                    if key not in self._messages:self._messages[key]=self.build_message(row,own,text)
                    item=self._messages[key];message=item['widget']
                    if self.rows.indexOf(message)!=position:
                        self.rows.removeWidget(message);self.rows.insertWidget(position,message)
                    position+=1
                    if item['bubble'].text()!=text:item['bubble'].setText(text)
                    if not own:
                        status='来电开场未完成' if row.get('source')=='incoming' and row['state']=='failed' else labels[row['state']]
                        if item['status'].text()!=status:item['status'].setText(status)
                        item['status'].setVisible(bool(status))
                    self.plain_text+=('你' if own else '若娜瓦')+'\n'+text+'\n'
            self.action_buttons={key[0]:item['buttons'] for key,item in self._messages.items() if not key[1]}
            self.reply_avatars={key[0]:item['avatar'] for key,item in self._messages.items() if not key[1]}
            self.rows.activate()
            self.restore_scroll()
        finally:self.viewport().setUpdatesEnabled(True)
        self.sync_actions(records)

    def build_message(self,row,own,text):
        wrapper=QWidget();vertical=QVBoxLayout(wrapper)
        vertical.setContentsMargins(0,0,0,0);vertical.setSpacing(4)
        name=QLabel('你' if own else '若娜瓦');name.setStyleSheet('color:#8792A2;font-size:10px')
        name.setAlignment(Qt.AlignmentFlag.AlignRight if own else Qt.AlignmentFlag.AlignLeft)
        vertical.addWidget(name)
        bubble=QLabel(text);bubble.setTextFormat(Qt.TextFormat.PlainText)
        bubble.setObjectName('userBubble' if own else 'assistantBubble');bubble.setProperty('recordId',row['id'])
        bubble.setWordWrap(True);bubble.setMaximumWidth(217)
        bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Preferred)
        bubble.setStyleSheet(f'color:#252B35;background:{"#FCE7EC" if own else "#FFFFFF"};border:1px solid {"#F7D7E0" if own else "#E8ECF1"};border-radius:14px;padding:10px 11px;font-size:13px;')
        line=QHBoxLayout();line.setContentsMargins(0,0,0,0)
        if own:line.addStretch(1)
        line.addWidget(bubble)
        if not own:line.addStretch(1)
        vertical.addLayout(line)
        status=None;buttons={}
        if not own:
            status=note('');status.setWordWrap(False)
            actions=QWidget();actions.setObjectName('replyActions');actions.setProperty('recordId',row['id'])
            tools=QHBoxLayout(actions);tools.setContentsMargins(0,0,0,0);tools.setSpacing(5)
            for action,callback in (('retry',self.main.controller.retry_failed),
                                    ('replay',self.main.controller.replay),('stop',self.main.controller.stop_playback)):
                item=ReplyActionButton(action,row['id'],lambda _checked=False,record_id=row['id'],operation=callback:
                    self.main.safe(lambda:operation(record_id)))
                tools.addWidget(item);buttons[action]=item
            tools.addWidget(status);tools.addStretch(1);vertical.addWidget(actions)
        message=QWidget();horizontal=QHBoxLayout(message)
        horizontal.setContentsMargins(0,0,0,0);horizontal.setSpacing(8)
        avatar=ChatAvatar(self.main,own,animated=not own)
        if not own:horizontal.addWidget(avatar,0,Qt.AlignmentFlag.AlignTop)
        else:horizontal.addStretch(1)
        wrapper.setMaximumWidth(217);horizontal.addWidget(wrapper)
        if own:horizontal.addWidget(avatar,0,Qt.AlignmentFlag.AlignTop)
        else:horizontal.addStretch(1)
        return {'widget':message,'bubble':bubble,'status':status,'buttons':buttons,'avatar':avatar}

    def sync_actions(self, records):
        state=self.main.state
        ready=not (state.get('busy') or state.get('responding') or state.get('oath_active'))
        selected=self.main.controller.router.selected
        playing=state.get('playing',False)
        owner=state.get('playback_record_id')
        active_id=owner if playing else records[-1]['id'] if records else None
        self.active_avatar=self.reply_avatars.get(active_id)
        for record_id,avatar in self.reply_avatars.items():
            animated=record_id==active_id
            if avatar.animated!=animated:
                avatar.animated=animated; avatar.update()
        for row in records:
            buttons=self.action_buttons.get(row['id'])
            if not buttons: continue
            current_backend=row.get('backend',selected)==selected
            buttons['retry'].setEnabled(ready and current_backend and row.get('source')!='incoming' and row['state'] in ('failed','cancelled','interrupted'))
            buttons['replay'].setEnabled(ready and current_backend and row['state']=='completed' and bool(row['assistant'].strip()))
            buttons['stop'].setEnabled(playing and owner==row['id'] and not state.get('busy'))


class SendButton(MotionButton):
    def __init__(self, callback):
        super().__init__('', shape='circle')
        self.setObjectName('sendButton'); self.setFixedSize(34,34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName('发送消息'); self.setToolTip('Enter 发送 · Shift+Enter 换行')
        self.clicked.connect(callback)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.rect().center()); painter.scale(self.width()/34,self.height()/34)
        painter.setPen(QPen(self.palette().color(self.foregroundRole()),2,
            Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        path = QPainterPath(); path.moveTo(7,-6); path.lineTo(7,4); path.lineTo(-7,4)
        path.moveTo(-2,-1); path.lineTo(-7,4); path.lineTo(-2,9)
        painter.drawPath(path)


class ChatInput(QPlainTextEdit):
    send_requested = Signal()

    def __init__(self):
        super().__init__(); self.composing = False
        self.empty_placeholder = '给若娜瓦发消息…'
        self.textChanged.connect(self.update_placeholder)
        self.update_placeholder()

    def update_placeholder(self):
        self.setPlaceholderText(self.empty_placeholder if self.document().isEmpty() and not self.composing else '')

    def inputMethodEvent(self,event):
        self.composing = bool(event.preeditString())
        # IME preedit text is visible before it becomes document text.
        self.update_placeholder()
        super().inputMethodEvent(event)
        self.update_placeholder()

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter)
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier and not self.composing):
            event.accept()
            if not event.isAutoRepeat(): self.send_requested.emit()
        else: super().keyPressEvent(event)


class TextPane(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main; self.setObjectName('chatPage')
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        settings = button('设置', main.settings); settings.setStyleSheet('color:#515B6B;text-align:right;')
        self.avatar=ChatAvatar(main,size=26,animated=True)
        self.call_button=button('通话', main.open_call)
        self.call_button.setAccessibleName('进入通话界面')
        self.call_button.setToolTip('进入手机通话界面')
        outer.addWidget(navigation('通话', '若娜瓦', main.open_call, settings,
                                  avatar=self.avatar,leading=self.call_button))
        self.history = MessageHistory(main); outer.addWidget(self.history, 1)
        composer = QWidget(); composer.setObjectName('composer')
        entry = QHBoxLayout(composer); entry.setContentsMargins(8, 5, 8, 5); entry.setSpacing(5)
        self.input = ChatInput()
        self.input.setFixedHeight(67)
        self.input.setAccessibleName('消息内容')
        self.input.setTabChangesFocus(True)
        entry.addWidget(self.input, 1)
        self.send = SendButton(self.submit)
        entry.addWidget(self.send, 0, Qt.AlignmentFlag.AlignBottom)
        bottom = QHBoxLayout(); bottom.setContentsMargins(12, 0, 12, 7); bottom.addWidget(composer)
        outer.addLayout(bottom)
        self.input.send_requested.connect(self.submit)
        self.input.textChanged.connect(self.update_send)
        self.refresh()

    def update_send(self): self.send.setEnabled(bool(self.input.toPlainText().strip()))

    def submit(self):
        text = self.input.toPlainText().strip()
        if text and self.main.safe(lambda:self.main.controller.submit(text)):
            self.input.clear()

    def refresh(self):
        self.avatar.update()
        self.call_button.setText('通话中' if self.main.state['call_requested'] else '通话')
        rows = self.main.controller.records(); self.history.refresh(rows)
        self.update_send()


class SettingsPane(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main; self.setObjectName('settingsPage')
        self.pending = None; self.selection_buttons = []; self.selection_field = None
        self.model_fields = {}; self.model_save_buttons = {}; self.pending_model = False
        self.model_tts_combos = {}; self.model_key_links = {}
        self.model_details = {}; self.expanded_model = None
        self.queued_model=None; self.device_future=None
        self.values = replace(main.controller.preferences)
        self.mics, self.speakers = [(None,'系统默认')], [(None,'系统默认')]
        self.volume_timer = QTimer(self); self.volume_timer.setSingleShot(True)
        self.volume_timer.timeout.connect(self.save_volume)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        self.stack = AnimatedStack(); outer.addWidget(self.stack)
        self.root = QWidget(); self.stack.addWidget(self.root)
        layout = QVBoxLayout(self.root); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        layout.addWidget(navigation('‹ 返回', '设置', self.leave))
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(); scroll.setWidget(body); layout.addWidget(scroll, 1)
        body_layout = QVBoxLayout(body); body_layout.setContentsMargins(14, 4, 14, 14); body_layout.setSpacing(12)
        profile = QWidget(); profile.setObjectName('profileCard')
        profile_layout = QHBoxLayout(profile); profile_layout.setContentsMargins(8, 10, 8, 14); profile_layout.setSpacing(13)
        avatar = QLabel(); avatar.setFixedSize(48, 48)
        from PySide6.QtGui import QPixmap
        portrait = QPixmap(48,48); portrait.fill(Qt.GlobalColor.transparent)
        painter = QPainter(portrait); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        clip=QPainterPath(); clip.addRoundedRect(QRectF(0,0,48,48),15,15); painter.setClipPath(clip)
        painter.drawPixmap(QRectF(0,0,48,48),main.assets.avatar,QRectF(main.assets.avatar.rect())); painter.end()
        avatar.setPixmap(portrait)
        profile_layout.addWidget(avatar)
        identity = QVBoxLayout(); identity.setSpacing(3)
        name = QLabel('若娜瓦'); name.setStyleSheet('font-size:17px;font-weight:bold;')
        self.profile_status=note(''); self.profile_status.setObjectName('ronovaStatus')
        self.profile_status.setStyleSheet('color:#89919C;font-size:11px;')
        identity.addWidget(name); identity.addWidget(self.profile_status)
        profile_layout.addLayout(identity, 1); body_layout.addWidget(profile)
        self.engine = SettingsRow('对话模型', color='#515B6B', icon='engine', callback=lambda:self.choose('engine','对话模型'))
        self.engine.setMinimumHeight(76)
        self.engine.title.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        self.engine.layout().setStretch(1,1); self.engine.layout().setStretch(2,0)
        self.engine.value.hide()
        self.add_section(body_layout, '对话', group([self.engine]))
        self.mode = SettingsRow('通话方式', color='#E99432', icon='mode', callback=lambda:self.choose('call_mode','通话方式'))
        self.mic = SettingsRow('麦克风', color='#E85465', icon='mic', callback=lambda:self.choose('microphone','麦克风'))
        self.speaker = SettingsRow('播放设备', color='#5B85DE', icon='speaker', callback=lambda:self.choose('speaker','播放设备'))
        self.autoplay = Switch(self.values.autoplay)
        self.autoplay.toggled.connect(lambda checked:self.apply_change('autoplay',checked))
        autoplay_row = SettingsRow('自动播放回复', color='#37AA7A', icon='autoplay', toggle=self.autoplay)
        volume_card = QWidget(); volume_layout = QVBoxLayout(volume_card)
        volume_layout.setContentsMargins(13, 9, 13, 12); volume_layout.setSpacing(10)
        volume_header = QHBoxLayout(); volume_header.addWidget(QLabel('播放音量')); volume_header.addStretch()
        self.volume_label = note(''); volume_header.addWidget(self.volume_label); volume_layout.addLayout(volume_header)
        self.volume = QSlider(Qt.Orientation.Horizontal); self.volume.setRange(0, 100)
        self.volume.setAccessibleName('播放音量')
        self.volume.valueChanged.connect(self.volume_changed)
        self.volume.sliderReleased.connect(self.save_volume)
        volume_layout.addWidget(self.volume)
        self.add_section(body_layout, '声音', group([self.mode,self.mic,self.speaker,autoplay_row,volume_card]))
        self.proactive_row=SettingsRow('主动来电',color='#37AA7A',icon='mode',callback=self.open_call_settings)
        self.add_section(body_layout,'来电',group([self.proactive_row]))
        self.call_page=QWidget(); self.stack.addWidget(self.call_page)
        call_layout=QVBoxLayout(self.call_page); call_layout.setContentsMargins(0,0,0,0); call_layout.setSpacing(0)
        call_layout.addWidget(navigation('‹ 设置','主动来电',lambda:self.stack.setCurrentWidget(self.root)))
        call_scroll=QScrollArea(); call_scroll.setWidgetResizable(True)
        call_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        call_body=QWidget(); call_scroll.setWidget(call_body); call_layout.addWidget(call_scroll,1)
        call_content=QVBoxLayout(call_body); call_content.setContentsMargins(14,8,14,14); call_content.setSpacing(12)
        self.proactive = Switch(self.values.proactive_calls)
        self.proactive.setAccessibleName('允许若娜瓦主动来电')
        self.proactive.toggled.connect(lambda checked:self.apply_change('proactive_calls',checked))
        self.call_rows = {}
        calls = [SettingsRow('允许主动来电',icon='mode',toggle=self.proactive)]
        for field,title in (('call_start_hour','允许来电开始'),('call_end_hour','允许来电结束'),
                            ('call_daily_limit','每日最多来电'),('call_cooldown_hours','来电间隔'),('call_idle_minutes','空闲后判断')):
            row = SettingsRow(title,icon='mode',callback=lambda f=field,t=title:self.choose(f,t))
            self.call_rows[field]=row; calls.append(row)
        self.test_call_row=SettingsRow('测试来电',icon='mode',callback=lambda:main.safe(main.controller.test_incoming_call))
        self.call_history_row=SettingsRow('来电记录',icon='records',callback=self.open_call_history)
        call_content.addWidget(group(calls))
        call_content.addWidget(group([self.test_call_row,self.call_history_row]))
        call_content.addWidget(note('接听后才打开麦克风。关闭窗口后在托盘等待来电，托盘菜单可退出。'))
        call_content.addStretch(1)
        self.memory_row = SettingsRow('记忆与关系',color='#515B6B',icon='memory',callback=self.open_memory)
        self.add_section(body_layout, '记忆', group([self.memory_row]))
        clear = SettingsRow('清除聊天记录', color='#8D8D95', icon='records', callback=self.confirm_clear)
        clear.title.setStyleSheet('color:#D93652;')
        self.add_section(body_layout, '记录', group([clear]))
        connection='模型密钥已配置' if main.controller.models.snapshot()[self.values.engine].api_key else '请填写模型 API Key'
        footer=note('聊天记录仅保存在本机 · '+connection)
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter); body_layout.addWidget(footer)
        body_layout.addStretch(1)
        self.selector = QWidget(); self.stack.addWidget(self.selector)
        self.choice_page=self.selector
        self.model_page=QWidget(); self.stack.addWidget(self.model_page)
        self.model_rows=[]
        self.confirmation = QWidget(); self.stack.addWidget(self.confirmation)
        self.call_history_page = QWidget(); self.stack.addWidget(self.call_history_page)
        from client_memory_panel import MemoryPane
        self.memory_page = MemoryPane(main,lambda:self.stack.setCurrentWidget(self.root))
        self.stack.addWidget(self.memory_page); self.memory_revision=-1
        self.status_label = note(''); self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFixedHeight(28)
        outer.addWidget(self.status_label)
        self.refresh_values()
        self.build_models()

    @staticmethod
    def add_section(layout, title, card):
        layout.addWidget(card)

    def prepare(self):
        if self.pending and not self.pending.done(): return
        self.values = replace(self.main.controller.preferences)
        if self.device_future is None:
            # A driver query must block neither the GUI nor the operation queue.
            # This worker touches no widgets and never starts audio capture.
            future=self.device_future=Future(); scan=self.scan_devices
            def discover():
                try: future.set_result(scan())
                except Exception as exc: future.set_exception(exc)
            Thread(target=discover,name='Ronova-device-list',daemon=True).start()
        self.stack.setCurrentWidget(self.root); self.refresh_values()

    @staticmethod
    def scan_devices():
        return ([(None,'系统默认')]+[(d['id'],d['name']) for d in input_devices()],
                [(None,'系统默认')]+[(d['id'],d['name']) for d in output_devices()])

    def options(self, field):
        return {'engine':[('qwen','千问 3.1'),('deepseek','DeepSeek'),('gpt','千问')],
                'call_mode':[('speakers-aec','扬声器'),('headphones','耳机')],
                'microphone':self.mics,'speaker':self.speakers,
                'call_start_hour':[(n,f'{n:02d}:00') for n in range(24)],
                'call_end_hour':[(n,f'{n:02d}:00') for n in range(24)],
                'call_daily_limit':[(n,f'{n}次') for n in (1,2,3)],
                'call_cooldown_hours':[(n,f'{n}小时') for n in (1,2,3,6,12)],
                'call_idle_minutes':[(n,f'{n}分钟') for n in (5,15,30,60,120)]}[field]

    def refresh_values(self):
        prefs = self.values
        profile = self.main.controller.models.snapshot()[prefs.engine]
        self.engine.title.setText('对话模型\n'+profile.model)
        self.engine.title.setWordWrap(True)
        self.engine.value.setText('')
        self.engine.setToolTip(profile.model)
        self.mode.value.setText({'speakers-aec':'扬声器','headphones':'耳机'}[prefs.call_mode])
        for field, row in [('microphone',self.mic),('speaker',self.speaker)]:
            row.value.setText(dict(self.options(field)).get(getattr(prefs,field),'系统默认'))
            row.setToolTip(row.value.text())
        self.autoplay.sync(prefs.autoplay)
        self.proactive.sync(prefs.proactive_calls)
        self.proactive_row.value.setText('已开启' if prefs.proactive_calls else '已关闭')
        self.profile_status.setText(ronova_status(self.main.state,self.main.controller.memory.profile()))
        for field,row in self.call_rows.items(): row.value.setText(dict(self.options(field)).get(getattr(prefs,field),str(getattr(prefs,field))))
        self.volume.blockSignals(True); self.volume.setValue(round(prefs.volume*100)); self.volume.blockSignals(False)
        self.volume_label.setText(f'{self.volume.value()}%')
        self.refresh_model_voices()

    def model_subtitle(self, engine, title):
        if engine=='qwen': return title+' · 原生语音'
        tts = getattr(self.values,engine+'_tts_engine')
        label = 'IndexTTS-2.5' if tts=='indextts-2.5' else 'GPT-SoVITS V2ProPlus'
        return title+' · '+label+' · 需要 NVIDIA 独立显卡'

    def refresh_model_voices(self):
        for engine,combo in self.model_tts_combos.items():
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(getattr(self.values,engine+'_tts_engine')))
            combo.blockSignals(False)
            combo.setEnabled(self.pending is None)
        titles = dict(self.options('engine'))
        for row in self.model_rows:
            row.subtitle.setText(self.model_subtitle(row.property('choice'),titles[row.property('choice')]))

    @staticmethod
    def empty_page(page):
        old = page.layout()
        if old:
            while old.count():
                item = old.takeAt(0)
                if item.widget():
                    # Deferred deletion happens after the incoming frame is
                    # captured; hide the old controls before rebuilding.
                    item.widget().hide(); item.widget().deleteLater()
            QWidget().setLayout(old)

    def choose(self, field, title):
        if self.pending and not self.pending.done(): return
        self.save_volume()
        if field=='engine':
            self.choose_models(); return
        self.selector=self.choice_page
        self.empty_page(self.selector)
        layout = QVBoxLayout(self.selector); layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)
        call_field=field.startswith('call_') and field!='call_mode'
        parent=self.call_page if call_field else self.root
        layout.addWidget(navigation('‹ 主动来电' if call_field else '‹ 设置', title, lambda:self.stack.setCurrentWidget(parent)))
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(); content = QVBoxLayout(body); content.setContentsMargins(16,12,16,12); content.setSpacing(10)
        self.selection_buttons = []; self.selection_field = field
        choices=[]
        for value, label in self.options(field):
            row = SettingsRow(label, '✓' if value==getattr(self.values,field) else '',
                              callback=lambda selected=value:self.apply_change(field,selected),disclosure=False)
            row.title.setWordWrap(True); row.setMinimumHeight(54)
            row.value.setStyleSheet('color:#D93652;font-size:19px;')
            row.setProperty('choice',value)
            self.selection_buttons.append(row); choices.append(row)
        content.addWidget(group(choices))
        if field=='call_mode':
            content.addWidget(note('扬声器：处理播放回声，确认你开口后中断回复。\n耳机：确认你开口后中断回复。'))
        content.addStretch(1); scroll.setWidget(body); layout.addWidget(scroll,1)
        self.stack.setCurrentWidget(self.selector)

    def choose_models(self):
        # Build these persistent forms at startup, not inside a click handler.
        # Each visit still starts collapsed and uses the saved configurations.
        self.selector=self.model_page; self.selection_field='engine'
        self.selection_buttons=self.model_rows; self.expanded_model=None
        profiles=self.main.controller.models.snapshot()
        for engine,detail in self.model_details.items():
            if engine in self.model_tts_combos:self.model_tts_combos[engine].hidePopup()
            detail.animation.stop(); detail.expanded=False; detail.set_reveal(0)
            detail.setEnabled(False); detail.hide()
            for name,edit in self.model_fields[engine].items():
                edit.setText(getattr(profiles[engine],name)); edit.setCursorPosition(0)
        for row in self.model_rows:
            row.value.setText('✓' if row.property('choice')==self.values.engine else '')
            row.setAccessibleDescription('点击选择模型并展开配置')
            engine = row.property('choice')
            row.title.setText(profiles[engine].model)
        self.stack.setCurrentWidget(self.selector)
        self.refresh_model_voices()

    def build_models(self):
        layout = QVBoxLayout(self.model_page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)
        layout.addWidget(navigation('‹ 设置','对话模型',lambda:self.stack.setCurrentWidget(self.root)))
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(); content = QVBoxLayout(body)
        content.setContentsMargins(14,8,14,16); content.setSpacing(14)
        self.model_fields = {}; self.model_save_buttons = {}
        self.model_details = {}; self.expanded_model = None
        profiles = self.main.controller.models.snapshot()
        for engine,title in self.options('engine'):
            profile = profiles[engine]
            row = SettingsRow(profile.model,'✓' if engine==self.values.engine else '',
                              callback=lambda selected=engine:self.select_model(selected),disclosure=False,
                              subtitle=self.model_subtitle(engine,title))
            row.title.setWordWrap(True); row.setMinimumHeight(62); row.setProperty('choice',engine)
            row.value.setFixedWidth(22)
            row.value.setStyleSheet('color:#D93652;font-size:19px;'); self.model_rows.append(row)
            chevron = FoldoutChevron(); row.layout().addWidget(chevron)
            row.setAccessibleDescription('点击选择模型并展开配置')
            form = QWidget(); fields = QVBoxLayout(form)
            fields.setContentsMargins(13,6,13,13); fields.setSpacing(6)
            fields.addWidget(note('文字与语音由千问生成' if engine=='qwen' else
                                  '大语言模型生成回复，本地语音引擎合成声音。\n需要 NVIDIA 独立显卡及兼容驱动。'))
            if engine!='qwen':
                caption = note('语音合成引擎'); fields.addWidget(caption)
                combo = InlineChoicePicker(); combo.setMinimumHeight(40)
                combo.addItem('IndexTTS-2.5','indextts-2.5')
                combo.addItem('GPT-SoVITS V2ProPlus','gpt-sovitsv2proplus')
                combo.setCurrentIndex(combo.findData(getattr(self.values,engine+'_tts_engine')))
                combo.setAccessibleName(title+' · 语音合成引擎'); caption.setBuddy(combo)
                combo.currentIndexChanged.connect(lambda _index,selected=engine:self.change_model_voice(selected))
                fields.addWidget(combo); self.model_tts_combos[engine] = combo
            key_url = ('https://platform.deepseek.com/api_keys' if engine=='deepseek' else
                       'https://platform.qianwenai.com/home/api-keys')
            link = note(''); link.setTextFormat(Qt.TextFormat.RichText); link.setOpenExternalLinks(False)
            link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            display = key_url.replace('https://','').replace('/api_keys','/<wbr>api_keys').replace('/home/','/<wbr>home/<wbr>')
            link.setText('API Key 获取网址：<br><a style="color:#89919C" href="'+key_url+'">'+display+'</a>')
            link.linkActivated.connect(lambda _url,target=key_url:QDesktopServices.openUrl(QUrl(target)))
            link.setAccessibleName(title+' · API Key 获取网址 '+key_url)
            fields.addWidget(link); self.model_key_links[engine] = link
            inputs = {}
            labels = [('api_key','API Key')]
            for name,label in labels:
                caption = note(label); fields.addWidget(caption)
                edit = QLineEdit(); edit.setObjectName('modelInput'); edit.setMinimumHeight(37)
                edit.setMaxLength({'model':200,'url':2048,'api_key':4096}[name])
                edit.setAccessibleName(title+' · '+label); caption.setBuddy(edit)
                if name=='api_key':
                    edit.setEchoMode(QLineEdit.EchoMode.Password)
                    edit.setPlaceholderText('填写你的 API Key')
                else: edit.setPlaceholderText('调用的模型名字' if name=='model' else '完整接口地址')
                edit.setText(getattr(profiles[engine],name)); edit.setCursorPosition(0)
                inputs[name] = edit; fields.addWidget(edit)
            self.model_fields[engine] = inputs
            fields.addSpacing(5)
            save = button('保存并使用',lambda _checked=False,selected=engine:self.save_model(selected),'primaryButton')
            save.setMinimumHeight(39); fields.addWidget(save); self.model_save_buttons[engine] = save
            detail = ModelFoldout(form,chevron); self.model_details[engine] = detail
            card = group([row]); card.layout().addWidget(detail); content.addWidget(card)
        content.addStretch(1); scroll.setWidget(body); layout.addWidget(scroll,1)

    def select_model(self, engine):
        selected = None if self.expanded_model==engine else engine
        self.expanded_model = selected
        for row in self.selection_buttons:
            choice = row.property('choice'); expanded = choice==selected
            detail = self.model_details[choice]
            focus = QApplication.focusWidget()
            if not expanded and focus and detail.isAncestorOf(focus): row.setFocus()
            detail.animate(expanded)
            row.setAccessibleDescription('点击收起配置' if expanded else '点击选择模型并展开配置')
        if selected:
            if self.pending is not None: self.queued_model=selected
            else: self.apply_change('engine',selected)

    def save_model(self, engine):
        if self.pending and not self.pending.done(): return
        inputs = self.model_fields[engine]
        try:
            values = {name:edit.text() for name,edit in inputs.items()}
            profile = self.main.controller.models.snapshot()[engine]
            values.update(model=profile.model,url=profile.url)
            self.pending = self.main.controller.configure_model(engine,**values)
            self.pending_model = True
            self.refresh_model_voices()
            self.model_save_buttons[engine].setEnabled(False)
            self.status_label.setText('正在保存模型配置…')
        except Exception as exc: self.main.show_error(exc)

    def change_model_voice(self, engine):
        if self.pending is not None: return
        tts = self.model_tts_combos[engine].currentData()
        if tts==getattr(self.main.controller.preferences,engine+'_tts_engine'): return
        try:
            self.pending = self.main.controller.set_tts_engine(engine,tts)
            self.pending_model = True
            self.values = replace(self.values,**{engine+'_tts_engine':tts})
            self.status_label.setText('正在切换语音引擎…')
            self.refresh_model_voices()
        except Exception as exc:
            self.main.show_error(exc); self.refresh_model_voices()

    def apply_change(self, field, value):
        if self.pending and not self.pending.done():
            self.refresh_values(); return
        proposed = replace(self.values, **{field:value})
        if proposed == self.main.controller.preferences: return
        try:
            proposed.validate()
            self.pending = self.main.controller.apply_preferences(proposed)
            self.values = proposed; self.refresh_values()
            self.status_label.setText('正在保存…')
        except Exception as exc:
            self.main.show_error(exc); self.values = replace(self.main.controller.preferences); self.refresh_values()

    def volume_changed(self, value):
        self.volume_label.setText(f'{value}%')
        if not self.volume.isSliderDown(): self.volume_timer.start(220)

    def save_volume(self):
        self.volume_timer.stop()
        if self.volume.isSliderDown(): return
        self.apply_change('volume', self.volume.value()/100)

    def refresh(self):
        status=ronova_status(self.main.state,self.main.controller.memory.profile())
        if status!=self.profile_status.text(): self.profile_status.setText(status)
        self.memory_page.refresh()
        if self.memory_revision!=self.main.controller.memory.revision:
            from client_memory import STAGES
            profile=self.main.controller.memory.snapshot()
            self.memory_row.value.setText(STAGES[profile['stage']]+' · '+str(len(profile['memories']))+' 条')
            self.memory_revision=profile['revision']
        if self.device_future and self.device_future.done():
            future=self.device_future; self.device_future=None
            try:
                self.mics,self.speakers=future.result(); self.refresh_values()
                if (self.stack.currentWidget() is self.selector and
                        self.selection_field in ('microphone','speaker')):
                    self.choose(self.selection_field,'麦克风' if self.selection_field=='microphone' else '播放设备')
            except Exception:
                self.status_label.setText('设备列表暂时不可用')
        if self.pending and self.pending.done():
            saved = self.pending.result() if self.pending_model else None
            self.pending = None
            self.values = replace(self.main.controller.preferences); self.refresh_values()
            for save in self.model_save_buttons.values(): save.setEnabled(True)
            self.status_label.setText(('配置已保存' if saved else '保存未完成，请检查输入') if self.pending_model else '')
            self.pending_model = False
            for row in self.selection_buttons:
                row.value.setText('✓' if row.property('choice')==getattr(self.values,self.selection_field) else '')
                if self.selection_field=='engine':
                    row.title.setText(self.main.controller.models.snapshot()[row.property('choice')].model)
            queued,self.queued_model=self.queued_model,None
            if queued: self.apply_change('engine',queued)

    def leave(self):
        self.save_volume(); self.main.close_phone()

    def open_memory(self):
        self.save_volume(); self.memory_page.open(); self.stack.setCurrentWidget(self.memory_page)

    def open_call_settings(self):
        self.save_volume(); self.refresh_values(); self.stack.setCurrentWidget(self.call_page)

    def open_call_history(self):
        from datetime import datetime
        from client_calls import SHANGHAI
        self.empty_page(self.call_history_page)
        layout=QVBoxLayout(self.call_history_page); layout.setContentsMargins(0,0,0,0)
        layout.addWidget(navigation('‹ 主动来电','来电记录',self.open_call_settings))
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body=QWidget(); content=QVBoxLayout(body); content.setContentsMargins(16,10,16,16)
        states={'ringing':'来电中','connecting':'正在接通','connected':'已接听','ended':'已结束',
                'declined':'已拒接','missed':'未接','snoozed':'稍后提醒','expired':'提醒已过期',
                'withdrawn':'已取消','busy':'已推迟','interrupted':'已中断','failed':'连接失败'}
        rows=self.main.controller.incoming_calls.store.recent()
        for row in rows:
            date=datetime.fromtimestamp(row['created'],SHANGHAI).strftime('%m-%d %H:%M')
            label=QLabel(date+' · '+states.get(row['state'],'已结束')+(' · 测试' if row['test'] else '')+'\n'+row['reason'])
            label.setTextFormat(Qt.TextFormat.PlainText); label.setWordWrap(True)
            label.setStyleSheet('color:#252B35;padding:12px;background:white;border-radius:12px;')
            content.addWidget(label)
        if not rows: content.addWidget(note('还没有来电记录'))
        content.addWidget(note('拒接、未接不会减少好感度。清除聊天记录时同时清除来电记录。'))
        content.addStretch(); scroll.setWidget(body); layout.addWidget(scroll,1)
        self.stack.setCurrentWidget(self.call_history_page)

    def confirm_clear(self):
        self.empty_page(self.confirmation)
        layout = QVBoxLayout(self.confirmation); layout.setContentsMargins(16,0,16,16); layout.setSpacing(15)
        layout.addWidget(navigation('‹ 设置','',lambda:self.stack.setCurrentWidget(self.root)))
        layout.addStretch(1)
        title = QLabel('清除聊天记录？'); title.setStyleSheet('font-size:22px;font-weight:bold;')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(title)
        detail = note('本机保存的聊天文字及当前对话上下文将被清除。')
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(detail)
        self.clear_memory_switch=Switch(False)
        self.clear_memory_switch.setAccessibleName('同时清除长期记忆')
        layout.addWidget(group([SettingsRow('同时清除记忆',toggle=self.clear_memory_switch)]))
        layout.addWidget(note('开启后也会清除全部长期记忆，好感度、关系状态与誓约仍保留。'))
        layout.addSpacing(12)
        self.clear_confirm = button('清除记录', self.clear_history, 'primaryButton'); layout.addWidget(self.clear_confirm)
        layout.addWidget(button('保留记录',lambda:self.stack.setCurrentWidget(self.root),'secondaryButton'))
        layout.addStretch(2); self.stack.setCurrentWidget(self.confirmation)

    def clear_history(self):
        if self.main.safe(lambda:self.main.controller.clear_history(include_memory=self.clear_memory_switch.isChecked())):
            self.stack.setCurrentWidget(self.root); self.main.overlay.notify('正在清除聊天记录…')


class PhoneSurface(QWidget):
    def __init__(self, main):
        super().__init__(); self.main = main
        self.setObjectName('phoneSurface'); self.setFixedSize(328,697)
        self.mode='settings'
        self.setStyleSheet(PHONE_CSS)
        layout = QVBoxLayout(self); layout.setContentsMargins(0,49,0,29); layout.setSpacing(0)
        self.pages = AnimatedStack(); layout.addWidget(self.pages)
        self.text = TextPane(main); self.settings = SettingsPane(main)
        self.pages.addWidget(self.text); self.pages.addWidget(self.settings)
        self.toast = QLabel(self); self.toast.setWordWrap(True)
        self.toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast.setStyleSheet('background:#36363C;color:white;border-radius:12px;padding:10px;font-size:12px;')
        self.toast.setGeometry(16,88,296,76); self.toast.hide()
        self.toast_timer = QTimer(self); self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.toast.hide)

    def configure(self, name):
        self.mode=name
        _,_,width,height=CHAT_PANEL if name=='text' else PHONE_SCREEN
        self.setFixedSize(width,height)
        self.layout().setContentsMargins(0,49,0,8 if name=='text' else 29)
        self.toast.setGeometry(16,70 if name=='text' else 88,width-32,76)

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        light=True
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor('#F5F6F8' if light else '#101013'))
        radius=CHAT_PANEL_RADIUS if self.mode=='text' else PHONE_SCREEN_RADIUS
        painter.drawRoundedRect(QRectF(self.rect()),radius,radius)
        painter.save(); painter.translate(-PHONE_SCREEN[0],-PHONE_SCREEN[1])
        self.main.assets.paint_phone_header(painter,light=light)
        if self.mode!='text' and light:
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor('#252B35'))
            painter.drawRoundedRect(QRectF(927,763,86,4),2,2)
        elif self.mode!='text': self.main.assets.svg(painter,'imgVector27')
        painter.restore()


class PhoneOverlay(QGraphicsView):
    """Proxy scaling keeps the embedded pages and hit targets aligned on resize."""
    def __init__(self, main):
        super().__init__(main); self.main = main
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignTop)
        self.setStyleSheet('QGraphicsView{background:transparent;border:0;padding:0}')
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.scene = QGraphicsScene(self); self.setScene(self.scene)
        self.surface = PhoneSurface(main)
        self.proxy = self.scene.addWidget(self.surface)
        self.setSceneRect(0,0,328,697)
        self.effect = QGraphicsOpacityEffect(self); self.setGraphicsEffect(self.effect)
        # One compositor covers the painted call screen and both widget pages.
        # Capturing before any layout/model changes avoids a one-frame jump.
        self.transition = PageTransition(main)
        self.animation = self.transition.animation
        self.animation.finished.connect(self.finished)
        self.phase = 'hidden'; self.page = None; self.target = None
        self.effect.setOpacity(0); self.hide()
        self._clock=phone_clock()

    def place(self):
        self.cancel_subpages()
        if self.transition.snapshot is not None:
            self.transition.cancel(); self.finished()
        x,y,w,h = CHAT_PANEL if self.page=='text' else PHONE_SCREEN
        scale=self.main.scale
        self.setGeometry(round(self.main.offset_x+x*scale),round(self.main.offset_y+y*scale),round(w*scale),round(h*scale))
        self.setSceneRect(0,0,w,h)
        # Qt retains the fixed-size constraints from when the proxy was bound.
        # Update both the widget and its proxy when changing page dimensions.
        self.proxy.setMinimumSize(w,h); self.proxy.setMaximumSize(w,h)
        self.proxy.resize(w,h)
        # Before the parent window is shown, Qt has not resized the viewport.
        # This frameless view's contents already have the final page geometry.
        sx,sy = self.contentsRect().width()/w,self.contentsRect().height()/h
        self.setTransform(QTransform().scale(sx,sy))
        # A QWidget/QRegion mask has only on/off pixels. Composite an alpha
        # mask after rendering the whole page, including child backgrounds.
        dpr=self.devicePixelRatioF()
        mask=QImage(math.ceil(self.width()*dpr),math.ceil(self.height()*dpr),QImage.Format.Format_ARGB32_Premultiplied)
        mask.setDevicePixelRatio(dpr); mask.fill(Qt.GlobalColor.transparent)
        painter=QPainter(mask); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(Qt.GlobalColor.white)
        radius=CHAT_PANEL_RADIUS if self.page=='text' else PHONE_SCREEN_RADIUS
        painter.drawRoundedRect(QRectF(self.rect()),radius*sx,radius*sy)
        painter.end(); self.effect.setOpacityMask(QBrush(QPixmap.fromImage(mask)))
        self.corner_mask=mask

    def navigation_bounds(self):
        x,y,w,h=PHONE_BODY; scale=self.main.scale
        bounds=QRectF(self.main.offset_x+(x-12)*scale,self.main.offset_y+(y-12)*scale,
                      (w+24)*scale,(h+24)*scale)
        return self.aligned_bounds(bounds)

    def aligned_bounds(self,bounds):
        # Fractional Windows scaling needs both ends on device pixels. Otherwise
        # a 733px crop at 150% becomes 1100 pixels and gets resampled on replay.
        grid=Fraction(self.devicePixelRatioF()).limit_denominator(64).denominator
        left=math.floor(bounds.left()/grid)*grid; top=math.floor(bounds.top()/grid)*grid
        right=math.ceil(bounds.right()/grid)*grid; bottom=math.ceil(bounds.bottom()/grid)*grid
        return QRectF(left,top,right-left,bottom-top).toRect().intersected(self.main.rect())

    def stack_context(self,stack):
        origin=stack.mapTo(self.surface,QPoint(0,0))
        content=self.viewportTransform().mapRect(QRectF(QPointF(origin),stack.size()))
        content.translate(QPointF(self.viewport().mapTo(self.main,QPoint(0,0))))
        # Keep the entire graphics view in the capture. Cropping the view at a
        # child boundary changes Qt's opacity-effect raster origin at 150%.
        bounds=self.navigation_bounds()
        return bounds,content.translated(-bounds.x(),-bounds.y())

    def cancel_subpages(self):
        for stack in self.surface.findChildren(AnimatedStack): stack.transition.cancel()

    def capture_navigation(self):
        bounds=self.navigation_bounds()
        # grab() includes an unfinished compositor, so reversing starts at the
        # actual displayed frame instead of either page's original endpoint.
        snapshot=self.main.grab(bounds) if self.main.isVisible() else None
        self.cancel_subpages()
        self.transition.cancel()
        return bounds,snapshot

    def animate_navigation(self, bounds, snapshot):
        if snapshot is None:
            self.finished(); return
        incoming=self.main.grab(bounds)
        self.transition.start(snapshot,incoming,bounds=bounds)

    def open_page(self, name):
        if name not in ('text','settings'): raise ValueError('界面不存在')
        if self.target==name and self.phase in ('in','shown'):
            if self.phase=='shown': self.focus_page()
            return
        bounds,snapshot=self.capture_navigation()
        self.target = name
        self.install_page(name); self.place()
        self.effect.setOpacity(1); self.show(); self.raise_()
        self.main.phone_controls(False); self.phase='in'
        self.animate_navigation(bounds,snapshot)

    def install_page(self, name):
        self.main.set_screen('home' if name=='text' else 'call')
        self.page=name
        self.surface.configure(name)
        if name=='settings': self.surface.settings.prepare()
        else: self.surface.text.refresh()
        self.surface.pages.setCurrentWidget(getattr(self.surface,name),animate=False)
        # Preparation may reset a nested settings page. The outer compositor
        # already contains that change; do not run another animation beneath it.
        self.cancel_subpages()
        self.surface.setEnabled(True)

    def close_page(self):
        if self.phase in ('hidden','out'):
            self.main.set_screen('call'); return
        bounds,snapshot=self.capture_navigation()
        self.target=None; self.phase='out'; self.surface.setEnabled(False)
        self.hide(); self.effect.setOpacity(0); self.main.set_screen('call')
        self.main.phone_controls(True)
        self.animate_navigation(bounds,snapshot)

    def finished(self):
        if self.phase=='out':
            self.page=None; self.phase='hidden'
            self.main.phone_controls(True); self.main.setFocus()
            self.main.update()
        elif self.phase=='in':
            self.phase='shown'; self.focus_page()

    def focus_page(self):
        self.setFocus()
        if self.page=='text': self.surface.text.input.setFocus()

    def notify(self, message):
        if self.target is None: self.main.text()
        self.surface.toast.setText(message[:400]); self.surface.toast.show(); self.surface.toast.raise_()
        self.surface.toast_timer.start(5000)

    def refresh(self):
        if self.page=='text': self.surface.text.refresh()
        self.surface.settings.refresh()
        clock=phone_clock()
        if clock!=self._clock:
            self._clock=clock; self.surface.update(0,0,328,49)
