"""Windows companion: idle incoming calls and explicit-answer voice capture."""
from pathlib import Path
import argparse
import json
import math
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'runtime/client-tools'))
from PySide6.QtCore import QEvent, QLockFile, QPoint, QRectF, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication,QMenu,QMessageBox,QPushButton,QSystemTrayIcon,QToolTip,QWidget
from client_controller import DesktopController
from client_scene import SceneAssets, phone_clock
from client_panels import PhoneOverlay
from client_motion import HeartButton, MotionButton, SpeakingMotion
from client_affinity import AffinityBar


def load_fonts():
    # The offscreen Qt renderer has no system font database on Windows.
    # Register the same installed fonts for normal and test rendering.
    import os
    directory = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    for name in ('msyh.ttc','msyhbd.ttc','segoeui.ttf','segoeuib.ttf'):
        path = directory/name
        if path.is_file(): QFontDatabase.addApplicationFont(str(path))


def stylesheet(colors):
    return f'''QDialog,QWidget{{background:{colors['surface']};color:{colors['text']};font-family:"Microsoft YaHei UI";font-size:14px}}
        QPushButton{{background:{colors['elevated']};border:1px solid {colors['border']};border-radius:10px;padding:9px 14px}}
        QPushButton:hover{{border-color:{colors['primary']}}} QPushButton:disabled{{color:{colors['muted']}}}
        QPlainTextEdit,QTextBrowser,QComboBox{{background:{colors['background']};border:1px solid {colors['border']};border-radius:8px;padding:8px}}
        QTabWidget::pane{{border:0}} QTabBar::tab{{padding:12px 18px;background:{colors['elevated']}}}
        QTabBar::tab:selected{{color:{colors['primary']}}} QSlider::groove:horizontal{{height:5px;background:{colors['border']}}}
        QSlider::handle:horizontal{{width:16px;margin:-6px 0;border-radius:8px;background:{colors['primary']}}}'''


class ClientWindow(QWidget):
    def __init__(self,controller,asset_root=ROOT):
        super().__init__()
        self.controller = controller
        load_fonts()
        self.assets = SceneAssets(asset_root)
        token_path = Path(asset_root)/'design/tokens.json'
        self.colors = json.loads(token_path.read_text(encoding='utf-8'))['colors']
        self.setStyleSheet(stylesheet(self.colors))
        self.setWindowTitle('若娜瓦 · RONOVA')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(1280,802); self.setMinimumSize(960,602)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.state = self.controller.snapshot()
        self.screen = 'home'
        self.settings_origin = 'home'
        self.assets.set_stage(self.controller.memory.profile()['stage'])
        self._oath_unlock_notified=self.controller.memory.profile()['affinity']==100
        self._oath_unlock_pending=False
        self.call_started = None
        self.speaking_motion = SpeakingMotion()
        self._scene_cache = None; self._scene_cache_key = None
        self._scene_cache_signature = None; self._scene_variants = {}
        self._portrait_transition_from=None
        self._portrait_transition_started=0.0
        self._portrait_transition_duration=.3
        self._display_clock = None; self._display_second = None
        self.scale, self.offset_x,self.offset_y = 1,0,0
        self.settings_button = self.button('设置',(1160,12,86,40),self.settings)
        self.voice_button = self.button('语音通话',(836,660,60,60),self.voice)
        self.end_button = self.button('结束通话',(940,660,60,60),self.end_or_decline)
        self.text_button = self.button('文字聊天',(1044,660,60,60),self.text_or_later)
        self.status_button = self.button('停止回应',(879,478,182,61),self.status_action)
        self.oath_dialog=None
        self.oath_button=HeartButton('誓约',self)
        self.oath_button.base_geometry=(700,665,68,64)
        self.oath_button.setObjectName('oathHomeButton')
        self.oath_button.clicked.connect(self.open_oath)
        self.affinity_bar=AffinityBar(self)
        self.affinity_bar.setValue(self.controller.memory.profile()['affinity'])
        self.sync_oath_entry()
        self.overlay = PhoneOverlay(self)
        self.text_pane = self.overlay.surface.text
        self.settings_pane = self.overlay.surface.settings
        self.tray = None; self._quitting = False
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon(self.assets.avatar),self)
            self.tray.setToolTip('若娜瓦 · 等待来电')
            menu = QMenu(self)
            for title,callback in (('打开若娜瓦',self.restore_window),('测试来电',lambda:self.safe(controller.test_incoming_call)),('退出',self.quit_client)):
                item = QAction(title,menu); item.triggered.connect(callback); menu.addAction(item)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason:self.restore_window() if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick) else None)
            self.tray.messageClicked.connect(self.restore_window)
            self.tray.show()
        QApplication.instance().installEventFilter(self)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(50)
        self.motion_timer = QTimer(self); self.motion_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.motion_timer.timeout.connect(self.animate_scene); self.motion_timer.start(16)
        self.text()

    def eventFilter(self,receiver,event):
        if event.type() in (QEvent.Type.KeyPress,QEvent.Type.MouseButtonPress) and isinstance(receiver,QWidget):
            if receiver is self or self.isAncestorOf(receiver) or self.overlay.surface.isAncestorOf(receiver):
                if not self.controller.closing: self.controller.incoming_calls.activity()
        return super().eventFilter(receiver,event)

    def restore_window(self):
        if self.controller.closing: return
        self.showNormal(); self.raise_(); self.activateWindow()
        if self.controller.snapshot()['incoming']: self.open_call()

    def quit_client(self):
        self._quitting = True
        if self.tray: self.tray.hide()
        self.close()

    def button(self,name,geometry,action):
        button = MotionButton('',self,shape='circle' if geometry[2]==geometry[3] else 'rect')
        if name=='设置': button.feedback_shape='none'
        button.setAccessibleName(name); button.setToolTip(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet('QPushButton{background:transparent;border:none;padding:0}QPushButton:focus{border:none}QPushButton:hover{border:none}')
        button.base_geometry = geometry
        button.feedback_changed.connect(lambda:self.update(button.geometry().adjusted(-3,-3,3,4)))
        button.clicked.connect(action)
        return button

    def resizeEvent(self,event):
        # A cached source frame belongs to one viewport and device scale.
        if hasattr(self,'_portrait_transition_from'): self._portrait_transition_from=None
        self.scale = min(self.width()/1280,self.height()/802)
        self.offset_x=(self.width()-1280*self.scale)/2; self.offset_y=(self.height()-802*self.scale)/2
        self.place_controls()
        if hasattr(self,'overlay'): self.overlay.place()

    def place_controls(self):
        for button in self.findChildren(QPushButton,options=Qt.FindChildOption.FindDirectChildrenOnly):
            x,y,w,h=button.base_geometry
            button.setGeometry(round(self.offset_x+x*self.scale),round(self.offset_y+y*self.scale),round(w*self.scale),round(h*self.scale))
        if hasattr(self,'affinity_bar'):
            x,y,w,h=self.affinity_bar.base_geometry
            self.affinity_bar.setGeometry(round(self.offset_x+x*self.scale),round(self.offset_y+y*self.scale),round(w*self.scale),round(h*self.scale))

    def showEvent(self,event):
        super().showEvent(event)
        if hasattr(self,'timer'): self.timer.setInterval(50); self.motion_timer.start(16)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def hideEvent(self,event):
        super().hideEvent(event)
        if hasattr(self,'timer'): self.timer.setInterval(1000); self.motion_timer.stop()

    def safe(self,action):
        try: action(); return True
        except Exception as exc:
            self.show_error(exc)
            return False

    def show_error(self,exc):
        message = str(exc) if isinstance(exc,(ValueError,RuntimeError)) else '操作未完成（'+type(exc).__name__+'）'
        self.controller.status(message)
        self.overlay.notify(message)

    def voice(self):
        state = self.controller.snapshot()
        if not state['busy']:
            if state['incoming']: self.safe(lambda:self.controller.answer_call(state['incoming']['id']))
            else: self.safe(self.controller.mute if state['call_requested'] else self.controller.start_call)

    def end_or_decline(self):
        row = self.controller.snapshot()['incoming']
        self.safe(lambda:self.controller.decline_call(row['id'])) if row else self.safe(self.controller.end_call)

    def text_or_later(self):
        row = self.controller.snapshot()['incoming']
        if row: self.safe(lambda:self.controller.decline_call(row['id'],later=True))
        else: self.text()

    def status_action(self):
        if self.state['incoming']: return
        if self.state['responding']: self.safe(self.controller.cancel)
        elif self.state['microphone_error']: self.safe(self.controller.start_call)
        else: self.text()

    def text(self):
        self.overlay.open_page('text')

    def set_screen(self, name):
        if self.screen == name: return
        self.screen = name
        self._portrait_transition_from = None
        self.place_controls()
        self.update()

    def open_call(self):
        # Entering the phone is navigation; the microphone has its own button.
        self.overlay.close_page()

    def settings(self):
        if self.overlay.target != 'settings': self.settings_origin = self.screen
        self.overlay.open_page('settings')

    def sync_oath_entry(self, profile=None):
        profile=self.controller.memory.profile() if profile is None else profile
        completed=profile['oath_completed']
        eligible=profile['affinity']==100
        available=self.controller.oath_available()
        label='重温誓约' if completed else '誓约'
        if completed:
            hint='重温誓约 · 缔结于 '+datetime.fromtimestamp(profile['oath_completed_at']).strftime('%Y-%m-%d %H:%M')
        else:
            hint='开始掌心相接的誓约' if eligible else '好感度达到100后可开启'
        if not available: hint+='\n请结束当前通话、回复、播放或保存操作后再开始'
        self.oath_button.setText(label); self.oath_button.setAccessibleName(label)
        self.oath_button.setToolTip(hint); self.oath_button.setAccessibleDescription(hint)
        self.oath_button.setVisible(eligible)
        self.oath_button.setEnabled(eligible and available)

    def open_oath(self):
        if self.oath_dialog and self.oath_dialog.isVisible():
            self.oath_dialog.raise_(); self.oath_dialog.activateWindow(); return
        self.sync_oath_entry()
        if not self.oath_button.isEnabled(): return
        from client_oath_panel import OathDialog
        if self.oath_dialog: self.oath_dialog.deleteLater()
        self.oath_dialog=OathDialog(self)
        self.oath_dialog.finished.connect(self.oath_closed)
        self.oath_dialog.open()

    def oath_closed(self, _result):
        if not self.controller.closed and not self.controller.closing:
            self.sync_oath_entry()
            self.settings_pane.memory_page.refresh(force=True)

    def close_phone(self):
        if self.overlay.page=='settings' and self.settings_origin=='home': self.text()
        else: self.open_call()

    def phone_controls(self,visible):
        for button in (self.voice_button,self.end_button,self.text_button,self.status_button):
            button.setVisible(visible and self.screen=='call')

    def refresh(self):
        events = self.controller.poll()
        if self.controller.closed:
            self.timer.stop(); self.motion_timer.stop(); self.close()
            if self._quitting: QApplication.instance().quit()
            return
        if self.controller.closing:
            self.update(); return
        prior = self.state
        self.state=self.controller.snapshot()
        self.sync_portrait(self.controller.memory.profile()['stage'])
        if self.state['call_requested'] and self.call_started is None: self.call_started=time.monotonic()
        if not self.state['call_requested']: self.call_started=None
        ringing = bool(self.state['incoming'])
        if self.tray:
            self.tray.setToolTip('若娜瓦 · '+('来电中' if ringing else '通话中' if self.state['call_requested'] else '等待来电' if self.controller.preferences.proactive_calls else '主动来电已关闭'))
        voice_label='接听' if ringing else '恢复麦克风' if self.state['muted'] else '静音' if self.state['call_requested'] else '语音通话'
        self.voice_button.setToolTip(voice_label); self.voice_button.setAccessibleName(voice_label)
        self.end_button.setToolTip('拒接' if ringing else '结束通话'); self.end_button.setAccessibleName('拒接' if ringing else '结束通话')
        self.text_button.setToolTip('30分钟后提醒一次，仍遵守免打扰和次数限制' if ringing else '文字聊天')
        self.text_button.setAccessibleName('稍后' if ringing else '文字聊天')
        self.end_button.setEnabled(ringing or self.state['call_requested'] or self.state['responding'] or self.state['playing'])
        self.voice_button.setEnabled(not self.state['busy']); self.text_button.setEnabled(not self.state['busy'])
        self.overlay.refresh()
        profile=self.controller.memory.profile()
        self.affinity_bar.setValue(profile['affinity'])
        self.sync_oath_entry(profile)
        if profile['affinity']==100 and not profile['oath_completed'] and not self._oath_unlock_notified:
            self._oath_unlock_pending=True
        if self._oath_unlock_pending and (profile['affinity']<100 or profile['oath_completed']):
            self._oath_unlock_pending=False
        if self._oath_unlock_pending and self.isActiveWindow() and not self.state['responding'] and not self.state['oath_active']:
            QToolTip.showText(self.oath_button.mapToGlobal(QPoint(0,-30)),
                             '好感度已满，点击手机左侧的爱心“誓约”开启仪式',self.oath_button,
                             self.oath_button.rect(),5000)
            self._oath_unlock_notified=True; self._oath_unlock_pending=False
        for event in events:
            if event['kind']=='incoming_call' and self.state['incoming'] and event['id']==self.state['incoming']['id']:
                self.open_call(); self.restore_window()
                if self.tray: self.tray.showMessage('若娜瓦来电','点击打开客户端接听',QSystemTrayIcon.MessageIcon.Information,10000)
            elif event['kind']=='incoming_closed':
                self.controller.status({'missed':'未接来电','declined':'来电已拒接','snoozed':'稍后提醒已设置'}.get(event['state'],'来电已结束'))
            elif event['kind']=='incoming_ringtone_failed' and self.state['incoming'] and event['id']==self.state['incoming']['id']:
                self.overlay.notify(event['message'])
            elif event['kind']=='incoming_decision_failed':
                self.controller.status(event['message'])
                self.settings_pane.status_label.setText('主动来电判断未完成，请检查模型设置')
            elif event['kind']=='operation_failed':
                self.overlay.notify(event['message'])
            elif event['kind']=='history_cleared':
                self.overlay.notify('聊天记录已清除')
        clock=phone_clock()
        second=int(time.monotonic()-self.call_started) if self.call_started else None
        changed=any(prior.get(key)!=value for key,value in self.state.items() if key!='playback_level')
        if changed or clock!=self._display_clock or second!=self._display_second:
            self.update_scene_rect((806,85,328,697))
        self._display_clock=clock; self._display_second=second

    def update_scene_rect(self, rect):
        x,y,w,h=rect
        dirty=QRectF(self.offset_x+x*self.scale,self.offset_y+y*self.scale,w*self.scale,h*self.scale)
        self.update(dirty.toAlignedRect().adjusted(-2,-2,2,2))

    def animate_scene(self):
        if self._portrait_transition_from is not None:
            if time.monotonic()-self._portrait_transition_started>=self._portrait_transition_duration:
                self._portrait_transition_from=None
            self.update()
        previous=self.speaking_motion.amount
        self.speaking_motion.tick(self.state['playing'],self.state['playback_level'])
        if abs(previous-self.speaking_motion.amount)>.0001:
            self.update_scene_rect((844,168,252,252))
            if self.overlay.isVisible() and self.overlay.page=='text':
                self.text_pane.avatar.update()
                if self.text_pane.history.active_avatar: self.text_pane.history.active_avatar.update()

    def scene_cache(self):
        dpr=self.devicePixelRatioF()
        signature=(self.width(),self.height(),dpr,self.scale,self.offset_x,self.offset_y,self.assets.stage)
        key=(*signature,self.screen)
        if self._scene_cache_signature!=signature:
            self._scene_variants.clear(); self._scene_cache_signature=signature
        phone=self.screen=='call'
        # Keep the two navigation backgrounds for this viewport and portrait.
        # Switching pages reuses them; resize/DPI/relationship changes rebuild.
        if phone not in self._scene_variants:
            cache=QPixmap(math.ceil(self.width()*dpr),math.ceil(self.height()*dpr))
            cache.setDevicePixelRatio(dpr); cache.fill(QColor(self.colors['background']))
            painter=QPainter(cache)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.translate(self.offset_x,self.offset_y); painter.scale(self.scale,self.scale)
            self.assets.paint_static(painter,phone=phone); painter.end()
            self._scene_variants[phone]=cache
        self._scene_cache=self._scene_variants[phone]; self._scene_cache_key=key
        return self._scene_cache

    def portrait_progress(self):
        amount=min(1,max(0,(time.monotonic()-self._portrait_transition_started)/self._portrait_transition_duration))
        return amount*amount*(3-2*amount)

    def paint_scene_base(self, painter):
        target=self.scene_cache()
        if self._portrait_transition_from is None:
            painter.drawPixmap(0,0,target)
        else:
            painter.drawPixmap(0,0,self._portrait_transition_from)
            painter.save(); painter.setOpacity(self.portrait_progress())
            painter.drawPixmap(0,0,target); painter.restore()

    def sync_portrait(self, stage):
        stage='close' if stage=='bond' and 'bond' not in self.assets.portraits else stage
        stage=stage if stage in self.assets.portraits else 'acquaintance'
        if stage==self.assets.stage: return False
        source=self.scene_cache()
        if self._portrait_transition_from is not None:
            # A second change starts from what is actually on screen, rather
            # than flashing either the old or the unfinished target state.
            source=QPixmap(source.size()); source.setDevicePixelRatio(self.devicePixelRatioF())
            source.fill(Qt.GlobalColor.transparent)
            painter=QPainter(source); self.paint_scene_base(painter); painter.end()
        self.assets.set_stage(stage)
        self._portrait_transition_from=source
        self._portrait_transition_started=time.monotonic()
        self.update()
        return True

    def label(self,painter,text,rect,size=14,color='text',bold=False,center=False):
        font=QFont('Microsoft YaHei UI'); font.setPixelSize(round(size)); font.setBold(bold)
        painter.setFont(font); painter.setPen(QColor(self.colors.get(color,color)))
        alignment=Qt.AlignmentFlag.AlignTop|(Qt.AlignmentFlag.AlignHCenter if center else Qt.AlignmentFlag.AlignLeft)|Qt.TextFlag.TextWordWrap
        painter.drawText(QRectF(*rect),int(alignment),text)

    def paintEvent(self,event):
        painter=QPainter(self); self.paint_scene_base(painter)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(self.offset_x,self.offset_y); painter.scale(self.scale,self.scale)
        if self.screen=='call':
            self.assets.paint_dynamic(painter,self.speaking_motion,{'voice':self.voice_button.feedback,
                'end':self.end_button.feedback,'text':self.text_button.feedback},muted=self.state['muted'],incoming=bool(self.state['incoming']))
        self.label(painter,'RONOVA',(32,22,100,24),15,bold=True)
        self.label(painter,'若娜瓦',(140,24,90,24),13,'muted')
        self.label(painter,'设置',(1202,22,48,24),14)
        self.label(painter,'死之执政',(39,618,180,24),14,'gold')
        self.label(painter,'若娜瓦',(36,648,320,62),42)
        self.label(painter,'RONOVA',(39,713,160,22),12,'gold')
        if self.screen!='call': painter.end(); return
        self.assets.paint_phone_header(painter)
        self.label(painter,'若娜瓦',(870,399,200,46),33,bold=True,center=True)
        state=self.state
        calibrating=bool(state['active'] and '测量背景音' in state['message'])
        if self.controller.closing: status='正在退出'
        elif state['incoming']: status='若娜瓦来电'
        elif state['microphone_error']: status='麦克风不可用'
        elif state['muted']: status='麦克风已静音'
        elif state['playing']: status='正在说话'
        elif state.get('voice_preparing') and (state['responding'] or state['call_requested']): status='正在准备语音'
        elif state['responding']: status='正在回应'
        elif calibrating: status='测量背景声音'
        elif state['microphone_state']=='preparing': status='正在准备收音'
        elif state['active']: status='正在聆听'
        elif state['message'] in ('未接来电','来电已拒接','稍后提醒已设置'): status=state['message']
        else: status='等待通话'
        self.label(painter,status,(879,483,182,28),17,center=True)
        elapsed=int(time.monotonic()-self.call_started) if self.call_started else None
        timer='等待接听' if state['incoming'] else f'{elapsed//60:02d}:{elapsed%60:02d}' if elapsed is not None else '--:--'
        self.label(painter,timer,(879,511,182,28),19,center=True)
        # Conversation text belongs to the text page. The call home stays quiet.
        if calibrating:
            self.label(painter,'先保持安静，约一秒后开始说话。',(846,572,248,24),10,'#DEE1DF',center=True)
        elif state['incoming']:
            self.label(painter,state['incoming']['reason'],(846,563,248,65),13,'#DEE1DF',center=True)
        labels=((836,'接听'),(940,'拒接'),(1044,'稍后')) if state['incoming'] else ((836,'恢复' if state['muted'] else '静音' if state['call_requested'] else '语音通话'),(940,'结束通话'),(1044,'文字聊天'))
        for x,label in labels:
            self.label(painter,label,(x-5,730,70,24),10.5,center=True)
        painter.end()

    def closeEvent(self,event):
        if self.controller.closed: event.accept(); return
        if self.tray and not self._quitting:
            event.ignore(); self.hide(); return
        self._quitting = True
        event.ignore()
        self.overlay.cancel_subpages(); self.overlay.transition.cancel(); self.overlay.hide()
        self.controller.close()
        self.setEnabled(False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='检查依赖和素材后退出，不打开麦克风或调用模型')
    args=parser.parse_args()
    app=QApplication(sys.argv[:1]); app.setApplicationName('Ronova')
    if args.check:
        assets=SceneAssets(ROOT)
        from client_lore import LoreStore
        lore=LoreStore(ROOT)
        print(json.dumps({'qt':'ready','assets':len(assets.manifest['assets']),
                         'portrait_states':list(assets.portraits),'knowledge_ready':lore.enabled,
                         'knowledge_chunks':len(lore.chunks),'knowledge_stage':lore.stage,
                         'microphone_started':False,'cloud_requests':0}))
        return 0
    (ROOT/'data').mkdir(exist_ok=True)
    lock=QLockFile(str(ROOT/'data/client.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None,'若娜瓦','客户端已经在运行。')
        return 0
    controller=None
    try:
        controller=DesktopController(ROOT)
        app.setQuitOnLastWindowClosed(False)
        window=ClientWindow(controller); window.show()
        return app.exec()
    except Exception as exc:
        QMessageBox.critical(None,'启动未完成','客户端启动失败（'+type(exc).__name__+'）。请检查本地配置与依赖。')
        return 1
    finally:
        if controller and not controller.closed: controller.close().result(timeout=20)
        lock.unlock()


if __name__=='__main__': sys.exit(main())
