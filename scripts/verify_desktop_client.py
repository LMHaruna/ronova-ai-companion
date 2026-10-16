"""Render and click the real Qt client against offline service fixtures."""
from pathlib import Path
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'runtime/client-tools'))
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QLockFile,QPointF,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall,fake_router


def main():
    directory=Path(os.environ.get('RONOVA_PREVIEW_DIR',ROOT/'runtime/verification-records/phone-motion-20261006'))
    directory.mkdir(parents=True,exist_ok=True)
    app=QApplication([]); app.setQuitOnLastWindowClosed(False)
    checks=[]
    def wait(predicate):
        deadline=time.monotonic()+5
        while not predicate() and time.monotonic()<deadline:
            app.processEvents(); time.sleep(.01)
        assert predicate(),'UI action timed out'
        app.processEvents()
        QTest.qWait(60)
    def check_phone_frame():
        capture=window.grab().toImage(); dpr=window.devicePixelRatioF()
        for x,y in ((803,430),(1137,430),(970,82),(970,785)):
            px=round((window.offset_x+x*window.scale)*dpr)
            py=round((window.offset_y+y*window.scale)*dpr)
            assert capture.pixelColor(px,py).name()=='#040506', 'phone bezel is missing'
        px=round((window.offset_x+970*window.scale)*dpr)
        py=round((window.offset_y+105*window.scale)*dpr)
        assert capture.pixelColor(px,py).name()=='#080b0d', 'top camera block is missing'
    with tempfile.TemporaryDirectory() as temporary, \
            patch('requests.sessions.Session.request',side_effect=AssertionError('live network forbidden')), \
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('live microphone forbidden')), \
            patch('client_models.environment',return_value='ui-fixture-api-key'):
        router,chat,voice=fake_router()
        controller=DesktopController(temporary,router=router,call_factory=FakeCall)
        window=ClientWindow(controller,ROOT)
        def click(widget):
            from PySide6.QtWidgets import QScrollArea
            ancestor=widget.parentWidget()
            while ancestor:
                if isinstance(ancestor,QScrollArea): ancestor.ensureWidgetVisible(widget,0,0)
                ancestor=ancestor.parentWidget()
            app.processEvents(); QTest.qWait(50)
            point=widget.mapTo(window.overlay.surface,widget.rect().center())
            position=window.overlay.mapFromScene(QPointF(point))
            assert window.overlay.viewport().rect().contains(position), 'control is outside the visible page: '+widget.objectName()
            QTest.mouseClick(window.overlay.viewport(),Qt.MouseButton.LeftButton,pos=position)
        try:
            window.show(); app.processEvents()
            assert chat.calls==0 and not controller.call.active
            checks.append('opening does not start microphone or model')
            wait(lambda:window.overlay.phase=='shown')
            assert window.screen=='home' and window.text_pane.isVisible()
            assert window.overlay.surface.width()==328
            assert window.overlay.surface.height()==697
            assert window.overlay.geometry().x()==round(window.offset_x+806*window.scale)
            check_phone_frame()
            assert window.text_pane.mapTo(window.overlay.surface,window.text_pane.rect().topLeft()).y()>=49
            checks.append('main chat shares the top camera block with call and leaves room above navigation')
            assert window.grab().save(str(directory/'chat-home.png'))
            window.text_pane.input.setPlainText('主界面的未发送草稿')
            click(window.text_pane.call_button); wait(lambda:window.overlay.phase=='hidden')
            assert window.screen=='call' and chat.calls==0 and not controller.call.active
            check_phone_frame()
            checks.append('main chat and call share the solid black six-pixel phone bezel')
            checks.append('startup shows main-screen chat; entering the existing phone does not start capture or a request')
            QTest.mouseClick(window.text_button,Qt.MouseButton.LeftButton)
            wait(lambda:window.overlay.phase=='shown')
            assert window.text_pane.input.toPlainText()=='主界面的未发送草稿'
            window.settings(); wait(lambda:window.overlay.phase=='shown' and window.overlay.page=='settings')
            assert window.screen=='call'
            click(next(widget for widget in window.settings_pane.root.findChildren(QPushButton)
                       if widget.text()=='‹ 返回'))
            wait(lambda:window.overlay.phase=='shown' and window.overlay.page=='text')
            assert window.screen=='home' and window.text_pane.input.toPlainText()=='主界面的未发送草稿'
            assert chat.calls==0 and not controller.call.active
            checks.append('phone and settings return to main chat with its draft, without starting microphone or model')
            window.text_pane.input.clear()
            window.open_call(); wait(lambda:window.overlay.phase=='hidden')
            labels=[]; draw_label=window.label
            window.label=lambda painter,text,*args,**kwargs:(labels.append(text),draw_label(painter,text,*args,**kwargs))[-1]
            window.repaint(); app.processEvents()
            assert '凡人，你终于来了。' not in labels
            assert '死之执政' in labels
            window.label=draw_label
            checks.append('call home starts without preset conversation text')
            for width,height in ((1280,802),(960,602),(1440,900)):
                window.resize(width,height); app.processEvents()
                assert window.grab().save(str(directory/f'idle-{width}.png'))
                for button in (window.voice_button,window.end_button,window.text_button,window.settings_button):
                    assert window.rect().contains(button.geometry())
            checks.append('1280, 960 and 1440 layouts keep controls in bounds')
            window.resize(1280,802); app.processEvents()
            QTest.mousePress(window.voice_button,Qt.MouseButton.LeftButton); QTest.qWait(60)
            assert 0 < window.voice_button.feedback <= 1
            QTest.mouseRelease(window.voice_button,Qt.MouseButton.LeftButton)
            wait(lambda:controller.call.active)
            QTest.qWait(370); assert window.voice_button.feedback==0
            QTest.mouseClick(window.end_button,Qt.MouseButton.LeftButton)
            wait(lambda:not controller.call_requested)
            checks.append('call button gives animated press and release feedback')
            assert window.settings_button.feedback_shape=='none'
            before = window.settings_button.grab().toImage()
            window.settings_button.setFocus(); window.settings_button.set_feedback(1)
            assert window.settings_button.grab().toImage()==before
            window.settings_button.set_feedback(0)
            checks.append('main settings button has no filled press or focus rectangle of any color')
            QTest.mouseClick(window.voice_button,Qt.MouseButton.LeftButton)
            initial_feedback=window.voice_button.feedback
            assert .12<=initial_feedback<=1
            QTest.qWait(40)
            assert window.voice_button.feedback>initial_feedback
            assert window.grab().save(str(directory/'button-pressed.png'))
            QTest.qWait(400); assert window.voice_button.feedback==0
            QTest.mouseClick(window.end_button,Qt.MouseButton.LeftButton)
            wait(lambda:not controller.call_requested)
            checks.append('a quick click gives immediate feedback and visibly scales the painted call button')
            from PySide6.QtCore import QPoint
            from PySide6.QtGui import QImage,QPainter,QRegion
            from PySide6.QtWidgets import QWidget
            for control in (window.voice_button,window.end_button,window.text_button):
                control.setFocus(); control.set_feedback(1)
                capture=QImage(control.size(),QImage.Format.Format_ARGB32_Premultiplied)
                capture.fill(Qt.GlobalColor.transparent); painter=QPainter(capture)
                control.render(painter,QPoint(),QRegion(),QWidget.RenderFlag.DrawChildren); painter.end()
                assert not any(capture.pixelColor(x,y).alpha()>0 and
                    capture.pixelColor(x,y).red()>capture.pixelColor(x,y).green()*1.35
                    for x in range(capture.width()) for y in range(capture.height()))
                control.set_feedback(0)
            checks.append('all three call control overlays keep neutral feedback without red focus or press outlines')
            voice.playback_active=True; voice.playback_level=1; window.refresh(); QTest.qWait(350)
            assert window.speaking_motion.amount>.9
            first=window.grab().toImage(); voice.playback_level=.1; QTest.qWait(350); second=window.grab().toImage()
            assert first!=second
            voice.playback_level=1; QTest.qWait(220)
            assert window.grab().save(str(directory/'speaking.png'))
            voice.playback_level=0; window.refresh(); QTest.qWait(1100)
            assert window.speaking_motion.amount==0
            quiet=window.grab().toImage(); QTest.qWait(160); assert window.grab().toImage()==quiet
            voice.playback_active=False
            checks.append('avatar follows actual speech intensity and stays still during silent playback pauses')
            QTest.mouseClick(window.voice_button,Qt.MouseButton.LeftButton)
            wait(lambda:controller.call.active)
            QTest.mouseClick(window.voice_button,Qt.MouseButton.LeftButton)
            wait(lambda:controller.muted)
            assert not controller.call.active
            QTest.mouseClick(window.voice_button,Qt.MouseButton.LeftButton)
            wait(lambda:controller.call.active and not controller.muted)
            assert controller.call.starts[-1] is False
            checks.append('call, mute and resume work without cancelling retained response')
            QTest.mouseClick(window.text_button,Qt.MouseButton.LeftButton)
            wait(lambda:window.overlay.phase=='shown')
            assert controller.call.active and window.text_pane.call_button.text()=='通话中'
            click(window.text_pane.call_button); wait(lambda:window.overlay.phase=='hidden')
            assert controller.call.active
            checks.append('switching between text home and phone keeps an existing call active')
            QTest.mouseClick(window.end_button,Qt.MouseButton.LeftButton)
            wait(lambda:not controller.call_requested)
            QTest.mouseClick(window.text_button,Qt.MouseButton.LeftButton)
            QTest.qWait(80)
            assert 0 < window.overlay.effect.opacity() < 1
            wait(lambda:window.overlay.phase=='shown')
            dialog=window.text_pane; assert dialog.isVisible() and not dialog.isWindow()
            assert window.overlay.surface.graphicsProxyWidget() is window.overlay.proxy
            assert not window.voice_button.isVisible()
            checks.append('chat fades into the main-screen panel without a dialog window')
            assert window.overlay.surface.grab().toImage().pixelColor(50,110).name()=='#f5f6f8'
            assert dialog.avatar.isVisible() and dialog.avatar.accessibleName()=='若娜瓦头像'
            assert window.grab().save(str(directory/'chat-empty.png'))
            checks.append('main chat uses a light surface and shows an avatar beside the navigation name')
            mask=window.overlay.corner_mask
            edge=[mask.pixelColor(x,y).alpha() for x in range(44) for y in range(44)]
            assert 0 in edge and 255 in edge and any(0<alpha<255 for alpha in edge)
            assert window.overlay.mask().isEmpty()
            checks.append('main chat corner alpha mask has fractional edge coverage')
            from client_motion import MotionButton
            def check_navigation(pane):
                for nav in pane.findChildren(MotionButton):
                    if nav.objectName()=='navButton' and nav.isVisible():
                        nav.set_feedback(0); before = nav.grab().toImage()
                        nav.setFocus(); nav.set_feedback(1)
                        assert nav.grab().toImage()==before
                        nav.set_feedback(0)
            check_navigation(dialog)
            checks.append('chat top call and settings navigation has identical pixels before and during press')
            dialog.input.setPlainText('今天有点累。<script>保留文字</script>')
            click(dialog.send)
            wait(lambda:len(controller.records())==1 and controller.records()[0]['state']=='completed')
            dialog.refresh()
            assert '<script>保留文字</script>' in dialog.history.toPlainText()
            assert '凡人，先坐下休息。' in dialog.history.toPlainText()
            assert window.grab().save(str(directory/'reply.png'))
            assert window.grab().save(str(directory/'text-history.png'))
            checks.append('send button streams Chinese and escapes user markup')
            from client_panels import ChatAvatar
            avatars=dialog.history.findChildren(ChatAvatar)
            assert any(avatar.own for avatar in avatars) and any(not avatar.own for avatar in avatars)
            assert all(avatar.isVisible() for avatar in avatars)
            checks.append('QQ-style message rows show Ronova and user avatars beside selectable plain text bubbles')
            old_end=voice.end
            def hold_tail(job): job.input_closed=True; voice.playback_active=True; voice.playback_level=1
            voice.end=hold_tail
            dialog.input.setPlainText('文字结束后不要关闭播放设备。'); click(dialog.send)
            wait(lambda:len(controller.records())==2 and controller.records()[-1]['state']=='completed')
            job=controller.pipeline.last_turn.voice_job; stopped=voice.stopped
            assert not job.finished.is_set() and controller.pipeline.responding
            QTest.qWait(300); first=dialog.avatar.grab().toImage(); voice.playback_level=.05; QTest.qWait(400)
            second=dialog.avatar.grab().toImage(); assert first!=second
            for _ in range(20): dialog.refresh(); window.refresh()
            assert voice.stopped==stopped and not job.cancelled.is_set() and not job.finished.is_set()
            assert dialog.history.action_buttons[controller.records()[-1]['id']]['stop'].isEnabled()
            assert window.grab().save(str(directory/'chat-speaking.png'))
            job.finished.set(); voice.playback_active=False; voice.playback_level=0; voice.end=old_end
            checks.append('completed text and repeated chat refresh keep the device tail alive and animate the chat avatar')
            chat.fail=True; dialog.input.setPlainText('失败后继续。')
            click(dialog.send)
            wait(lambda:len(controller.records())==3 and controller.records()[-1]['state']=='failed')
            chat.fail=False; dialog.refresh()
            click(dialog.history.action_buttons[controller.records()[-1]['id']]['retry'])
            wait(lambda:controller.records()[-1]['state']=='completed')
            assert len(controller.records())==3
            checks.append('failed request can retry without duplicate user message')
            dialog.input.setPlainText('使用键盘发送。'); click(dialog.input)
            QTest.keyClick(window.overlay.viewport(),Qt.Key.Key_Return,Qt.KeyboardModifier.ShiftModifier)
            assert '\n' in dialog.input.toPlainText() and len(controller.records())==3
            QTest.keyClick(window.overlay.viewport(),Qt.Key.Key_Return)
            wait(lambda:len(controller.records())==4 and controller.records()[-1]['state']=='completed')
            assert dialog.input.toPlainText()==''
            checks.append('Enter sends from the embedded editor and Shift+Enter inserts a newline')
            from PySide6.QtGui import QInputMethodEvent
            dialog.input.setPlainText('输入法选字'); calls=chat.calls
            app.sendEvent(dialog.input,QInputMethodEvent('候选词',[]))
            QTest.keyClick(dialog.input,Qt.Key.Key_Return)
            assert chat.calls==calls
            app.sendEvent(dialog.input,QInputMethodEvent('',[])); dialog.input.clear()
            checks.append('Enter selecting an active Chinese IME composition does not send prematurely')
            from client_motion import MotionButton
            navs=[b for b in dialog.findChildren(MotionButton) if b.objectName()=='navButton']
            assert len(navs)==2
            for nav in navs:
                nav.setFocus(); nav.set_feedback(1)
                capture=nav.grab().toImage()
                assert not any(capture.pixelColor(x,y).red()>150 and
                    capture.pixelColor(x,y).red()>capture.pixelColor(x,y).green()*1.35
                    for x in range(capture.width()) for y in range(capture.height()))
                nav.set_feedback(0)
            checks.append('chat navigation press and focus use neutral fill without a red outline')
            window.close_phone(); wait(lambda:window.overlay.phase=='hidden')
            labels=[]; window.label=lambda painter,text,*args,**kwargs:(labels.append(text),draw_label(painter,text,*args,**kwargs))[-1]
            window.repaint(); app.processEvents()
            assert '凡人，先坐下休息。' not in labels
            window.label=draw_label
            assert window.grab().save(str(directory/'call-after-chat.png'))
            checks.append('returning to call home does not display old chat text')
            with patch('client_panels.input_devices',return_value=[{'id':7,'name':'测试麦克风'}]), \
                    patch('client_panels.output_devices',return_value=[{'id':1,'name':'测试扬声器'}]):
                window.settings(); wait(lambda:window.overlay.phase=='shown' and window.overlay.page=='settings')
                settings=window.settings_pane
                assert settings.isVisible() and not settings.isWindow()
                check_navigation(settings)
                checks.append('settings top call navigation also draws no gray or colored press rectangle')
                assert window.grab().save(str(directory/'settings.png'))
                for nav in settings.findChildren(MotionButton):
                    if nav.objectName()=='navButton' and nav.isVisible():
                        nav.setFocus(); nav.set_feedback(1)
                        capture=nav.grab().toImage()
                        assert not any(capture.pixelColor(x,y).red()>150 and
                            capture.pixelColor(x,y).red()>capture.pixelColor(x,y).green()*1.35
                            for x in range(capture.width()) for y in range(capture.height()))
                        nav.set_feedback(0)
                checks.append('settings navigation also has no red press or focus outline')
                settings.volume.setValue(35)
                wait(lambda:controller.preferences.volume==.35)
                click(settings.autoplay)
                wait(lambda:not controller.preferences.autoplay and settings.pending is None)
                click(settings.mode)
                assert settings.stack.currentWidget() is settings.selector
                assert window.grab().save(str(directory/'settings-options.png'))
                click(settings.selection_buttons[-1])
                wait(lambda:controller.preferences.call_mode=='headphones' and settings.pending is None)
                assert settings.selection_buttons[-1].value.text()=='✓'
                for field,row,expected in [('microphone',settings.mic,7),('speaker',settings.speaker,1)]:
                    settings.stack.setCurrentWidget(settings.root); click(row)
                    click(settings.selection_buttons[-1])
                    wait(lambda:getattr(controller.preferences,field)==expected and settings.pending is None)
                settings.stack.setCurrentWidget(settings.root); click(settings.engine)
                app.processEvents(); QTest.qWait(80)
                assert len(settings.selection_buttons)==3 and settings.expanded_model is None
                assert all(detail.height()==0 and not detail.isVisible() for detail in settings.model_details.values())
                assert all(not edit.isVisible() for inputs in settings.model_fields.values() for edit in inputs.values())
                assert all(row.isVisible() for row in settings.selection_buttons)
                assert window.grab().save(str(directory/'models-collapsed.png'))
                checks.append('model page displays three options with GPT removed and all API Key fields hidden')
                qwen_detail=settings.model_details['qwen']; second_y=settings.selection_buttons[-1].mapTo(settings.selector,settings.selection_buttons[-1].rect().topLeft()).y()
                click(settings.selection_buttons[0]); QTest.qWait(85)
                assert 0<qwen_detail.get_reveal()<1 and qwen_detail.height()>0
                assert settings.selection_buttons[-1].mapTo(settings.selector,settings.selection_buttons[-1].rect().topLeft()).y()>second_y
                assert 0<qwen_detail.chevron.progress<1
                assert window.grab().save(str(directory/'models-expanding.png'))
                wait(lambda:qwen_detail.get_reveal()==1 and settings.pending is None)
                checks.append('model details slide downward through intermediate heights and rotate the disclosure icon')
                draft=settings.model_fields['qwen']['api_key']; prior_key=draft.text(); draft.setText('fixture-draft-key')
                click(settings.selection_buttons[0]); QTest.qWait(70)
                assert 0<qwen_detail.get_reveal()<1
                click(settings.selection_buttons[0]); wait(lambda:qwen_detail.get_reveal()==1)
                assert draft.text()=='fixture-draft-key'; draft.setText(prior_key); draft.setCursorPosition(0)
                checks.append('rapid collapse and reopen reverses the animation without losing an edited draft')
                draft.setFocus(); click(settings.selection_buttons[0]); wait(lambda:not qwen_detail.isVisible())
                assert qwen_detail.height()==0 and not draft.isVisible() and not draft.hasFocus()
                checks.append('collapsed model fields are hidden and cannot retain keyboard focus')
                click(settings.selection_buttons[-1])
                wait(lambda:controller.preferences.engine=='gpt' and settings.pending is None)
                wait(lambda:settings.model_details['gpt'].get_reveal()==1)
                assert not qwen_detail.isVisible() and settings.expanded_model=='gpt'
                click(settings.selection_buttons[0])
                wait(lambda:controller.preferences.engine=='qwen' and settings.pending is None)
                wait(lambda:qwen_detail.get_reveal()==1 and not settings.model_details['gpt'].isVisible())
                checks.append('choosing another model opens its fields and fully collapses the previous model')
                QTest.keyClick(settings.selection_buttons[0],Qt.Key.Key_Space)
                wait(lambda:not qwen_detail.isVisible())
                QTest.keyClick(settings.selection_buttons[0],Qt.Key.Key_Space)
                wait(lambda:qwen_detail.get_reveal()==1)
                checks.append('model selection and foldout also work through keyboard activation')
                from PySide6.QtWidgets import QLineEdit,QScrollArea
                assert set(settings.model_fields)=={'qwen','gpt','deepseek'}
                for engine,inputs in settings.model_fields.items():
                    assert set(inputs)=={'api_key'}
                    assert inputs['api_key'].echoMode()==QLineEdit.EchoMode.Password
                    assert inputs['api_key'].text()=='ui-fixture-api-key'
                    assert inputs['api_key'].displayText()!=inputs['api_key'].text()
                    assert all(edit.height()>=37 for edit in inputs.values())
                assert all(edit.isVisible() and edit.isEnabled() for edit in settings.model_fields['qwen'].values())
                assert all(not edit.isVisible() and not edit.isEnabled() for edit in settings.model_fields['gpt'].values())
                assert controller.models.snapshot()['gpt'].model=='qwen3.8-flash'
                assert controller.models.snapshot()['gpt'].url.endswith('/chat/completions')
                checks.append('all three options show actual model IDs and expand only masked API Key fields')
                assert settings.model_rows[0].title.text()=='qwen-audio-3.1-realtime-plus'
                assert settings.model_rows[1].title.text()=='deepseek-flash'
                assert settings.model_rows[2].title.text()=='qwen3.8-flash'
                scroll=settings.selector.findChild(QScrollArea)
                scroll.verticalScrollBar().setValue(0); app.processEvents()
                assert window.grab().save(str(directory/'models-qwen.png'))
                click(settings.selection_buttons[-1]); wait(lambda:settings.pending is None and settings.model_details['gpt'].get_reveal()==1)
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum()); app.processEvents()
                assert window.grab().save(str(directory/'models-gpt.png'))
                calls=chat.calls
                gpt=settings.model_fields['gpt']
                prior_profile=controller.models.snapshot()['gpt']
                gpt['api_key'].setText('ui-customer-gpt-key'); click(settings.model_save_buttons['gpt'])
                wait(lambda:settings.pending is None)
                assert controller.router.selected=='gpt'
                assert controller.router.backends['gpt'][0].config is not None, settings.status_label.text()+' / '+controller.snapshot()['message']
                assert controller.router.backends['gpt'][0].config.key()=='ui-customer-gpt-key'
                saved_profile=controller.models.snapshot()['gpt']
                assert (saved_profile.model,saved_profile.url)==(prior_profile.model,prior_profile.url)
                assert controller.router.backends['gpt'][0].config.model==saved_profile.model
                assert controller.router.backends['gpt'][0].config.endpoint()==saved_profile.url
                assert settings.selection_buttons[-1].value.text()=='✓'
                assert 'ui-customer-gpt-key' not in controller.models.path.read_bytes().decode('latin1')
                checks.append('qwen3.8-flash saves its key while retaining the built-in model and URL, with no network request')
                click(settings.selection_buttons[0]); wait(lambda:settings.pending is None and qwen_detail.get_reveal()==1)
                qwen=settings.model_fields['qwen']
                qwen['api_key'].setText('ui-customer-qwen-key')
                click(settings.model_save_buttons['qwen']); wait(lambda:settings.pending is None)
                assert controller.router.selected=='qwen'
                assert chat.config is voice.config and chat.config.key()=='ui-customer-qwen-key'
                assert chat.config.model==controller.models.presets['qwen'].model
                assert chat.config.url==controller.models.presets['qwen'].url
                assert chat.calls==calls and not controller.call.active
                checks.append('save-and-use applies the built-in Qwen model and URL plus customer key to dialogue and replay without network or capture')
                qwen['api_key'].clear(); click(settings.model_save_buttons['qwen']); wait(lambda:settings.pending is None)
                assert controller.models.snapshot()['qwen'].api_key=='ui-customer-qwen-key'
                assert settings.status_label.text()=='保存未完成，请检查输入'
                qwen['api_key'].setText('ui-customer-qwen-key')
                checks.append('invalid model draft stays editable and preserves the last saved customer credential')
                settings.stack.setCurrentWidget(settings.root)
                click(settings.engine)
                assert settings.expanded_model is None
                assert all(detail.height()==0 and not detail.isVisible() for detail in settings.model_details.values())
                checks.append('returning to model settings resets to the compact two-option view')
                settings.stack.setCurrentWidget(settings.root)
                settings.confirm_clear()
                assert controller.records()
                settings.stack.setCurrentWidget(settings.root)
            checks.append('light settings with monochrome icons auto-save slider, switch and in-phone choice pages')
            checks.append('device and model choice rows store their actual values')
            checks.append('clearing history requires an in-phone confirmation and leaves history until confirmed')
            window.close_phone(); QTest.qWait(60)
            assert 0 < window.overlay.effect.opacity() < 1
            wait(lambda:window.overlay.phase=='hidden')
            assert window.voice_button.isVisible() and not window.overlay.isVisible()
            checks.append('closing fades out before restoring call controls')
            window.text(); wait(lambda:window.overlay.phase=='shown')
            dialog.input.setPlainText('未发送的草稿')
            for width,height in ((960,602),(1440,900)):
                window.resize(width,height); app.processEvents()
                assert window.rect().contains(window.overlay.geometry())
                mask=window.overlay.corner_mask
                assert (mask.width(),mask.height())==(round(window.overlay.width()*window.overlay.devicePixelRatioF()),
                                                     round(window.overlay.height()*window.overlay.devicePixelRatioF()))
                assert mask.pixelColor(0,0).alpha()==0
                assert mask.pixelColor(mask.width()//2,mask.height()//2).alpha()==255
                check_phone_frame()
                assert window.grab().save(str(directory/f'text-{width}.png'))
            checks.append('main chat scales correctly at 960 and 1440 without changing hit targets')
            window.close_phone(); window.settings(); window.close_phone()
            wait(lambda:window.overlay.phase=='hidden')
            window.text(); wait(lambda:window.overlay.phase=='shown')
            assert dialog.input.toPlainText()=='未发送的草稿'
            checks.append('main chat, phone and settings resize and rapid transitions preserve unsent drafts')
            assert window.assets.geometries['phone_bezel']=={'width':6,'color':'#040506'}
            assert '私人陪伴通话' not in (ROOT/'client_app.py').read_text(encoding='utf-8')
            checks.append('solid six-pixel bezel and removed subtitle are installed')
            window.close()
            wait(lambda:controller.closed)
            assert voice.closed and not controller.call.active
            checks.append('closing client releases capture and voice')
            router,new_chat,new_voice=fake_router()
            restored=DesktopController(temporary,router=router,call_factory=FakeCall)
            assert len(restored.records())==4 and len(new_chat.history)==9
            assert new_chat.calls==0
            assert restored.models.snapshot()['gpt'].model=='qwen3.8-flash'
            assert restored.models.snapshot()['qwen'].api_key=='ui-customer-qwen-key'
            checks.append('customer model settings survive a client restart without using the owner environment key')
            restored.close().result(5)
            checks.append('restart restores history without re-sending old messages')
            assert not list(Path(temporary).rglob('*.wav'))
            first,second=QLockFile(str(Path(temporary)/'test.lock')),QLockFile(str(Path(temporary)/'test.lock'))
            assert first.tryLock(0) and not second.tryLock(0)
            first.unlock()
            checks.append('only one client can own the local history session')
            report={'checks':checks,'passed':len(checks),'cloud_requests':0,'physical_microphone_started':False,
                    'physical_aec_verified':False,'assets':len(window.assets.manifest['assets']),
                    'render_geometry':window.assets.geometries,
                    'screenshots':[str(path) for path in directory.glob('*.png')]}
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'ui_checks':len(checks),'cloud_requests':0,'report':str(directory/'report.json')},ensure_ascii=False))
        finally:
            if not controller.closed: controller.close().result(5)
            window.close(); app.processEvents()


if __name__=='__main__': main()
