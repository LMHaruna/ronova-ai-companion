"""The relationship meter beside the phone, shared by every main page."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget
from client_relationship import MIN_AFFINITY, MAX_AFFINITY


class AffinityBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self.setObjectName('homeAffinityBar')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAccessibleName('好感度进度')
        self.base_geometry = (712,314,70,346)
        self.setValue(0)

    def value(self): return self._value
    def minimum(self): return MIN_AFFINITY
    def maximum(self): return MAX_AFFINITY

    def setValue(self, value):
        value = min(MAX_AFFINITY,max(MIN_AFFINITY,int(value)))
        description = f'好感度 {value} / 100' + (' · 生气中' if value<0 else '')
        self.setAccessibleDescription(description)
        self.setToolTip(description+'\n范围 −20～100；红色表示好感，紫色表示生气。')
        if value!=self._value:
            self._value = value
            self.update()

    def layout_geometry(self):
        # Keep the rail aligned with the heart while reserving room on its
        # right for the score and above/below for the endpoint text.
        scale = self.height()/346
        width = 8*scale
        rail = QRectF(18*scale,10*scale,width,self.height()-20*scale)
        bound = abs(MIN_AFFINITY) if self._value<0 else MAX_AFFINITY
        fill = rail.height()*abs(self._value)/bound
        endpoint = rail.bottom()-fill
        left = rail.right()+6*scale
        label = QRectF(left,endpoint-8.5*scale,max(0,self.width()-left),17*scale)
        return rail,label,fill

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = self.height()/346
        rail,label,fill = self.layout_geometry()
        width = rail.width()
        color = QColor('#A989B9' if self._value<0 else '#D93652')
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(235,225,230,65))
        painter.drawRoundedRect(rail,width/2,width/2)
        if fill:
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(rail.x(),rail.bottom()-fill,width,fill),width/2,width/2)
        painter.setPen(color if self._value else QColor('#D8D1D4'))
        font = QFont('Microsoft YaHei UI'); font.setPixelSize(max(9,round(11*scale)))
        painter.setFont(font)
        painter.drawText(label,Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,str(self._value))
        painter.end()
