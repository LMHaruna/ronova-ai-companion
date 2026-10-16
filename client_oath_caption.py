"""Voice-timed character captions drawn beside the ceremony portrait."""
from dataclasses import dataclass
from PySide6.QtCore import QRectF,Qt
from PySide6.QtGui import QColor,QFont,QFontMetricsF,QPainter,QPainterPath,QPen
from PySide6.QtWidgets import QWidget

def ease(value):
    value=max(0.0,min(1.0,value));return value*value*(3-2*value)

@dataclass(frozen=True)
class CaptionState:
    index: int
    text: str
    events: tuple
    opacity: float

class DialogueTimeline:
    def __init__(self,lines): self.lines=lines
    def sample(self,elapsed):
        index=-1
        for i,line in enumerate(self.lines):
            if elapsed>=line["start"]: index=i
        if index<0: return CaptionState(-1,"",(),0.0)
        line=self.lines[index]
        events=tuple(event for event in line["characters"] if event["at"]<=elapsed)
        opacity=1.0 if index==len(self.lines)-1 else 1-ease((elapsed-line["end"]-.85)/.45)
        return CaptionState(index,"".join(event["text"] for event in events),events,opacity)

class OathCaption(QWidget):
    def __init__(self,scene):
        super().__init__(scene);self.scene=scene
        self.timeline=DialogueTimeline(scene.assets.captions) if scene.assets else None
        self.mode="preview";self.elapsed=0.0;self.replay=False
        self.visible_text="";self.state=CaptionState(-1,"",(),0.0)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAccessibleName("誓约逐字对白")

    def preview(self,replay):
        self.mode="preview";self.replay=replay;self.visible_text="";self.update()

    def show_at(self,elapsed):
        self.mode="play";self.elapsed=elapsed
        if self.timeline:self.state=self.timeline.sample(elapsed)
        self.visible_text=self.state.text if self.state.opacity>0 else ""
        self.setAccessibleDescription(self.visible_text);self.update()

    def show_all(self,replay=False):
        self.mode="all";self.replay=replay
        self.visible_text="\n".join(line["text"] for line in self.timeline.lines) if self.timeline else ""
        self.setAccessibleDescription(self.visible_text);self.update()

    def show_last(self):
        self.mode="last"
        self.visible_text=self.timeline.lines[-1]["text"] if self.timeline else ""
        self.setAccessibleDescription(self.visible_text);self.update()

    def text_area(self):
        picture=self.scene.picture_rect()
        x=picture.right()+18
        right=self.width()-28
        return QRectF(x,self.height()*.34,max(200,right-x),self.height()*.38)

    def font(self):
        font=QFont("Microsoft YaHei")
        font.setPixelSize(round(max(22,min(30,self.height()*.036))))
        font.setWeight(QFont.Weight.Medium)
        return font

    def placements(self,text):
        area=self.text_area();metrics=QFontMetricsF(self.font())
        row=0;x=0.0;result=[];line_height=metrics.height()*1.65
        # Give the spoken hesitation its own row; wrapping uses the complete
        # sentence, so newly appearing letters never move the previous text.
        for index,char in enumerate(text):
            width=metrics.horizontalAdvance(char)+1.5
            if x+width>area.width() and x>0:row+=1;x=0
            result.append((area.x()+x,area.y()+metrics.ascent()+row*line_height))
            x+=width
            if char=="…" and index+1<len(text) and text[index+1]!="…":row+=1;x=0
        return result

    def draw_char(self,painter,char,position,age,opacity=1.0,animated=True):
        if opacity<=0:return
        appear=ease(age/.18) if animated else 1
        if appear<=0:return
        painter.save();painter.setOpacity(opacity*appear)
        painter.translate(position[0],position[1]+4*(1-appear))
        path=QPainterPath();path.addText(0,0,self.font(),char)
        if animated and age<.45:
            strength=1-ease(age/.45)
            for width,alpha in ((9,35),(5,65)):
                painter.setPen(QPen(QColor(231,142,161,round(alpha*strength)),width,
                                    Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
                painter.setBrush(Qt.BrushStyle.NoBrush);painter.drawPath(path)
        painter.strokePath(path,QPen(QColor(20,16,23,210),2.4))
        painter.fillPath(path,QColor("#FFF0DE") if animated and age<.4 else QColor("#F4EAE4"))
        painter.restore()

    def paintEvent(self,event):
        if not self.timeline:return
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        area=self.text_area()
        if self.mode=="preview":
            font=self.font();painter.setFont(font);painter.setPen(QColor("#EEE3DE"))
            painter.drawText(QRectF(area.x(),area.y(),area.width(),45),Qt.AlignmentFlag.AlignLeft,"掌心相接")
            font.setPixelSize(14);painter.setFont(font);painter.setPen(QColor("#A7A0A7"))
            painter.drawText(QRectF(area.x(),area.y()+60,area.width(),100),
                Qt.AlignmentFlag.AlignLeft|Qt.TextFlag.TextWordWrap,
                "你先伸出掌心，\n她主动将手交给你。\n\n"+("重温保留原缔结日期。" if self.replay else "完成仪式后，留下这次约定。"))
        elif self.mode=="play" and self.state.index>=0:
            line=self.timeline.lines[self.state.index]
            positions=self.placements(line["text"])
            for index,item in enumerate(self.state.events):
                self.draw_char(painter,item["text"],positions[index],self.elapsed-item["at"],self.state.opacity)
        elif self.mode=="last":
            for char,position in zip(self.visible_text,self.placements(self.visible_text)):
                self.draw_char(painter,char,position,1,animated=False)
        elif self.mode=="all":
            font=QFont("Microsoft YaHei");font.setPixelSize(14);painter.setFont(font)
            painter.setPen(QColor("#B49EA4"))
            painter.drawText(QRectF(area.x(),area.y()-40,area.width(),30),Qt.AlignmentFlag.AlignLeft,
                             "重温结束" if self.replay else "誓约已缔结")
            shift=0.0
            for line in self.timeline.lines:
                positions=self.placements(line["text"])
                for char,position in zip(line["text"],positions):
                    self.draw_char(painter,char,(position[0],position[1]+shift),1,animated=False)
                if positions:shift+=positions[-1][1]-positions[0][1]+QFontMetricsF(self.font()).height()*2.25
        painter.end()

