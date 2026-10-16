"""Local ceremony video, floating voice captions and atomic completion."""
import time
from PySide6.QtCore import QEvent, QPoint, QPropertyAnimation, QRect, QTimer, Qt
from PySide6.QtWidgets import QDialog, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QWidget
from client_oath import validate_ceremony
from client_oath_audio import OathSound
from client_oath_caption import OathCaption
from client_oath_video import OathVideoAssets as OathAssets, OathVideoScene as OathScene


class OathDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main=main; self.controller=main.controller
        self.replay=self.controller.memory.profile()['oath_completed']
        self.token=None; self.pending=None; self.pending_kind=None
        self.started=None; self.finished_ceremony=False; self.skip_requested=False
        self.close_requested=False; self.assets_ready=False
        self.auto_started=False; self.auto_start_scheduled=False
        self.last_error=''; self.assets=None; self.sound=None; self.outro_started=None
        self.setWindowTitle('重温誓约' if self.replay else '誓约')
        self.setWindowFlags(Qt.WindowType.Dialog|Qt.WindowType.FramelessWindowHint)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(900,560)
        self.setGeometry(QRect(main.mapToGlobal(QPoint(0,0)),main.size()))
        self.setStyleSheet('''QDialog{background:transparent;color:#F7F0F2;}
            QLabel{background:transparent;color:#F7F0F2;font-family:"Microsoft YaHei UI";font-size:16px;}
            QPushButton{background:rgba(40,37,45,210);color:#DDD5D9;border:1px solid #59505B;border-radius:9px;padding:4px 8px;font-size:13px;}
            QPushButton:hover{background:#39323D;border-color:#B27B8D;}
            QPushButton:disabled{color:#8C8690;}
            QPushButton#oathPrimary{background:#87384F;color:#FFF2F0;border:1px solid #B26176;}
            QPushButton#oathPrimary:hover{background:#A44763;}
            QPushButton#oathPrimary:disabled{background:#493943;color:#8C8690;}''')
        error=''
        try: validate_ceremony(); self.assets=OathAssets(); self.assets_ready=True
        except RuntimeError as exc: error=str(exc)
        outer=QHBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        self.content=QWidget(self); outer.addWidget(self.content)
        video_layout=QHBoxLayout(self.content); video_layout.setContentsMargins(0,0,0,0)
        self.scene=OathScene(self.assets); video_layout.addWidget(self.scene)
        self.caption=OathCaption(self.scene); self.caption.show_at(0)
        self.status=QLabel(error,self.scene); self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignBottom)
        self.status.setStyleSheet('font-size:13px;color:#D0AAB5;')
        self.primary=QPushButton('跳过动画',self.scene); self.primary.setObjectName('oathPrimary')
        self.secondary=QPushButton('取消仪式',self.scene)
        for button in (self.primary,self.secondary): button.setFixedSize(96,36)
        self.primary.clicked.connect(self.primary_action); self.secondary.clicked.connect(self.reject)
        self.scene.installEventFilter(self); self.layout_overlays()
        self.opacity=QGraphicsOpacityEffect(self.content); self.content.setGraphicsEffect(self.opacity)
        self.open_animation=QPropertyAnimation(self.opacity,b'opacity',self)
        self.open_animation.setDuration(220); self.open_animation.setStartValue(0.0); self.open_animation.setEndValue(1.0); self.open_animation.start()
        self.timer=QTimer(self); self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.refresh); self.timer.start(16)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.auto_started and not self.auto_start_scheduled:
            self.auto_start_scheduled=True
            QTimer.singleShot(0,self.auto_start)

    def auto_start(self):
        self.auto_start_scheduled=False
        if not self.isVisible() or self.close_requested or self.auto_started:
            return
        self.auto_started=True
        self.primary_action()

    def layout_overlays(self):
        width,height=self.scene.width(),self.scene.height()
        margin=round(max(18,min(26,height*.03))); gap=10
        self.caption.setGeometry(self.scene.rect())
        self.primary.move(width-margin-self.primary.width(),height-margin-self.primary.height())
        self.secondary.move(self.primary.x()-gap-self.secondary.width(),self.primary.y())
        self.status.setGeometry(width-margin-min(360,width-2*margin),self.primary.y()-82,
                                min(360,width-2*margin),68)
        self.caption.raise_(); self.status.raise_(); self.primary.raise_(); self.secondary.raise_()

    def eventFilter(self,watched,event):
        if watched is self.scene and event.type()==QEvent.Type.Resize: self.layout_overlays()
        return super().eventFilter(watched,event)

    def launch(self, kind, operation):
        if self.pending: return
        try:
            self.last_error=''; self.status.setText('')
            self.pending=operation(); self.pending_kind=kind
            self.primary.setEnabled(False); self.secondary.setEnabled(kind=='start')
        except (RuntimeError,ValueError) as exc:
            self.last_error=str(exc); self.status.setText(self.last_error)

    def primary_action(self):
        if self.pending: return
        if self.finished_ceremony: self.reject(); return
        if not self.token:
            if not self.assets_ready: return
            self.launch('start',lambda:self.controller.begin_oath(replay=self.replay,assets_ready=self.assets_ready))
        else:
            self.skip_requested=True
            if self.sound: self.sound.stop()
            self.scene.stop()
            self.scene.advance(self.assets.duration)
            self.show_all_lines()
            self.launch('finish',lambda:self.controller.finish_oath(self.token,skip=True))

    def show_all_lines(self):
        self.caption.show_all(self.replay)

    def refresh(self):
        if self.controller.closed or self.controller.closing:
            if self.sound: self.sound.stop()
            self.scene.stop()
            self.timer.stop(); super().reject(); return
        if self.pending and self.pending.done():
            result=self.pending.result(); kind=self.pending_kind; self.pending=None
            if kind=='start' and result:
                self.token=result; self.started=None
                if self.close_requested:
                    self.reject(); return
                preferences=self.controller.preferences
                self.sound=OathSound((self.assets.audio,),(0,),enabled=preferences.autoplay,
                    volume=preferences.volume,device=preferences.speaker,
                    duration=self.assets.duration,end_padding_seconds=0)
                self.scene.begin()
                self.caption.show_at(0)
                self.primary.setText('跳过动画'); self.secondary.setText('取消仪式')
            elif kind=='finish' and result:
                self.finished_ceremony=True; self.show_all_lines()
                if self.sound: self.sound.stop()
                self.scene.stop()
                self.scene.advance(self.assets.duration)
                if not self.skip_requested: self.caption.show_last()
                self.primary.setText('返回日常'); self.secondary.hide()
                self.status.setText('')
                if not self.skip_requested and not self.close_requested:
                    self.outro_started=time.monotonic(); QTimer.singleShot(800,self.reject)
            elif kind=='cancel':
                self.token=None; self.timer.stop(); super().reject(); return
            if not result:
                self.last_error=self.controller.snapshot()['message'] or '未完成，请重试'
                self.status.setText(self.last_error)
                if kind=='finish': self.primary.setText('重试保存')
                elif kind=='start': self.primary.setText('重试播放')
            if self.close_requested and not self.pending:
                self.reject(); return
        if self.pending: return
        if not self.token:
            if self.auto_started and self.last_error: self.primary.setText('重试播放')
            profile=self.controller.memory.profile()
            eligible=profile['oath_completed'] if self.replay else not profile['oath_completed'] and profile['affinity']==100
            available=self.controller.oath_available()
            self.primary.setEnabled(self.auto_started and self.assets_ready and eligible and available)
            self.secondary.setEnabled(True)
            if self.assets_ready:
                self.status.setText(self.last_error or ('' if eligible and available else
                    '请结束当前通话、回复、播放或保存操作后再开始' if not available else '好感度达到100后可开启')
                )
            return
        self.secondary.setEnabled(True); self.primary.setEnabled(True)
        if self.finished_ceremony:
            if self.outro_started is not None:
                self.opacity.setOpacity(max(0.0,1-(time.monotonic()-self.outro_started)/.8))
            return
        if self.started is None and not self.skip_requested:
            self.scene.advance(0)
            if self.scene.error:
                self.status.setText(self.scene.error); return
            if not self.scene.ready:
                self.status.setText('正在载入仪式画面…'); return
            self.started=time.monotonic(); self.sound.start(); self.status.setText('')
        elapsed=self.sound.position() if self.sound and self.sound.started is not None else 0
        if not self.skip_requested:
            self.scene.advance(elapsed)
            self.caption.show_at(elapsed)
        if self.scene.error:
            if self.sound: self.sound.stop()
            self.scene.stop(); self.status.setText(self.scene.error); return
        if self.sound and self.sound.error and not self.last_error: self.status.setText(self.sound.error)
        if (elapsed>=self.assets.duration and self.scene.finished() and
            (not self.sound or self.sound.finished()) and not self.last_error):
            self.launch('finish',lambda:self.controller.finish_oath(self.token))

    def reject(self):
        # A close arriving during an atomic save waits for its result. Closing
        # before save cancels, while closing after successful save preserves it.
        if self.sound: self.sound.stop()
        self.scene.stop()
        if self.pending:
            self.close_requested=True; return
        if self.token:
            self.launch('cancel',lambda:self.controller.cancel_oath(self.token)); return
        self.timer.stop(); super().reject()

    def closeEvent(self, event):
        event.ignore(); self.reject()
