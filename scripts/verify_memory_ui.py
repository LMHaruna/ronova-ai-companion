"""Click the real memory pages with isolated stores; never cloud or physical audio."""
from pathlib import Path
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'): sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea
from client_app import ClientWindow, load_fonts
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router, FakeAnalyzer
from client_panels import SettingsRow


def main():
    output=Path(sys.argv[1]); output.mkdir(parents=True,exist_ok=True)
    app=QApplication([]); app.setQuitOnLastWindowClosed(False); load_fonts()
    checks=[]
    def wait(predicate):
        deadline=time.monotonic()+5
        while not predicate() and time.monotonic()<deadline: QTest.qWait(10)
        assert predicate(),'memory UI action timed out'
        QTest.qWait(210)
    with tempfile.TemporaryDirectory() as temporary, \
            patch('requests.sessions.Session.request',side_effect=AssertionError('live network forbidden')), \
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('live microphone forbidden')), \
            patch('client_models.environment',return_value='memory-ui-fixture'), \
            patch('client_panels.input_devices',return_value=[]), \
            patch('client_panels.output_devices',return_value=[]):
        values={
            '我很信任你':('trust',[]),'你真没用':('insult',[]),'对不起，我刚才冒犯了你':('apology',[]),
            '我是一名大学生，学计算机。':('sharing',[('identity.role','我是一名大学生'),('identity.major','学计算机')]),
            '我已经毕业不再是大学生，开始工作了':('sharing',[('identity.role','我已经毕业不再是大学生，开始工作了')])}
        class Analysis(FakeAnalyzer):
            def analyze(self,text,reply,memories,cancel):
                kind,facts=values.get(text,('neutral',[]))
                items=[]
                for key,content in facts:
                    prior=next((row for row in memories if row['slot']==key),None) if '已经毕业' in text else None
                    items.append({'key':key,'category':'fact','content':content,'evidence':text,'confidence':.99,
                        'operation':'update' if prior else 'add','replace_id':prior['id'] if prior else '',
                        'previous_content':prior['content'] if prior else '', 'previous_updated':prior['updated'] if prior else 0})
                return {'memories':items,
                    'interaction':{'kind':kind,'evidence':text if kind!='neutral' else '', 'confidence':1}}
        router,chat,voice=fake_router(); controller=DesktopController(temporary,router=router,call_factory=FakeCall,
            analysis_factory=lambda backend,config:Analysis())
        window=ClientWindow(controller,ROOT); window.show(); QTest.qWait(300)
        def click(widget):
            ancestor=widget.parentWidget()
            while ancestor:
                if isinstance(ancestor,QScrollArea): ancestor.ensureWidgetVisible(widget,0,0)
                ancestor=ancestor.parentWidget()
            app.processEvents(); QTest.qWait(50)
            point=widget.mapTo(window.overlay.surface,widget.rect().center())
            position=window.overlay.mapFromScene(QPointF(point))
            QTest.mouseClick(window.overlay.viewport(),Qt.MouseButton.LeftButton,pos=position)
        def send(text):
            controller.submit(text).result(3)
            wait(lambda:controller.pipeline.last_turn.done.is_set())
            controller.poll(); wait(lambda:controller.companion.snapshot()['pending']==0)
            window.refresh()
        def seed(score,stage):
            with controller.memory.connection: controller.memory.connection.execute('UPDATE profile SET affinity=?,stage=?',(score,stage))
            controller.memory.revision+=1; pane.refresh(force=True)
        try:
            window.settings(); wait(lambda:window.overlay.phase=='shown')
            settings=window.settings_pane; click(settings.memory_row)
            pane=settings.memory_page
            assert settings.stack.currentWidget() is pane
            assert pane.stack.currentWidget() is pane.home and not pane.isWindow()
            assert '0 条' in pane.count_label.text()
            checks.append('memory page opens inside phone and starts empty without model or microphone')
            pane.edit(None); wait(lambda:pane.stack.currentWidget() is pane.editor)
            pane.content.setPlainText('我喜欢喝热茶，不喜欢太甜。')
            pane.category.setCurrentIndex(pane.category.findData('preference')); click(pane.save_button)
            wait(lambda:pane.pending is None and len(controller.memory.snapshot()['memories'])==1)
            assert pane.stack.currentWidget() is pane.home
            row=controller.memory.snapshot()['memories'][0]
            checks.append('save persists a categorized memory and returns to updated list')
            list_row=pane.list_body.findChild(SettingsRow); click(list_row)
            wait(lambda:pane.stack.currentWidget() is pane.editor)
            assert pane.content.toPlainText()==row['content']
            pane.content.setPlainText('我喜欢清茶。'); click(pane.save_button)
            wait(lambda:pane.pending is None and controller.memory.snapshot()['memories'][0]['content']=='我喜欢清茶。')
            assert '清茶' in controller.router.backends['qwen'][0]._messages('茶')[0][0]['content']
            checks.append('editing updates existing id and next actual model context')
            pane.edit(row); pane.content.setPlainText('字'*301); click(pane.save_button)
            assert '1–300' in pane.status_label.text() and pane.stack.currentWidget() is pane.editor
            pane.show_home(); checks.append('overlength content is rejected locally without leaving editor')
            click(pane.stage_row)
            assert pane.stack.currentWidget() is pane.stages and not hasattr(pane,'stage_buttons')
            pane.close_name.setText('我的偏爱'); click(pane.name_save)
            wait(lambda:pane.pending is None and controller.memory.profile()['close_name']=='我的偏爱')
            assert controller.memory.profile()['appellation']=='你'
            seed(29,'acquaintance'); send('今天的天气很舒服')
            wait(lambda:controller.memory.profile()['stage']=='familiar')
            assert pane.stage_row.value.text()=='熟悉'
            assert pane.affinity_bar.value()==30
            checks.append('manual stage switches are removed and affinity automatically advances to familiar')
            seed(68,'familiar'); send('我很信任你')
            assert controller.memory.profile()['stage']=='close' and controller.memory.profile()['appellation']=='我的偏爱'
            seed(2,'acquaintance')
            send('你真没用')
            assert pane.stage_row.value.text()=='生气' and controller.memory.profile()['appellation']=='凡人'
            assert '清茶' in controller.memory.context('茶')
            send('对不起，我刚才冒犯了你')
            assert pane.stage_row.value.text()=='相识' and controller.memory.profile()['appellation']=='你'
            checks.append('close nickname and angry address follow automatic state while retaining the same memory')
            seed(70,'close')
            send('我是一名大学生，学计算机。')
            assert len(controller.memory.snapshot()['memories'])==3
            assert any(record['source']=='automatic' for record in controller.memory.snapshot()['memories'])
            checks.append('normal conversation automatically saves identity and major without a remember command')
            controller.start_call().result(3); before_score=controller.memory.profile()['affinity']
            click(pane.reset_button); assert controller.memory.profile()['affinity']==before_score
            pane.show_home(); assert controller.memory.profile()['affinity']==before_score
            click(pane.reset_button); QTest.qWait(220); click(pane.confirm_button)
            wait(lambda:pane.pending is None and controller.memory.profile()['affinity']==0)
            assert len(controller.memory.snapshot()['memories'])==3 and len(controller.records())==5
            assert controller.call.active and controller.memory.profile()['appellation']=='你'
            checks.append('reset confirmation returns to acquaintance without clearing facts history or active call')
            click(pane.automatic); wait(lambda:pane.pending is None and not controller.memory.profile()['auto_memory'])
            send('我已经毕业不再是大学生，开始工作了')
            assert any(record['content']=='我是一名大学生' for record in controller.memory.snapshot()['memories'])
            click(pane.automatic); wait(lambda:pane.pending is None and controller.memory.profile()['auto_memory'])
            send('我已经毕业不再是大学生，开始工作了')
            assert not any(record['content']=='我是一名大学生' for record in controller.memory.snapshot()['memories'])
            assert len(controller.memory.snapshot()['memories'])==3
            checks.append('automatic memory switch blocks extraction and reenabling permits correction of existing identity')
            click(pane.enabled); wait(lambda:pane.pending is None and not controller.memory.snapshot()['enabled'])
            assert '清茶' not in chat._messages('茶')[0][0]['content']
            assert len(controller.memory.snapshot()['memories'])==3
            click(pane.enabled); wait(lambda:pane.pending is None and controller.memory.snapshot()['enabled'])
            checks.append('memory switch stops inclusion but retains data and can be reenabled')
            layouts=[]
            for width,height in ((1280,802),(960,602)):
                window.resize(width,height); QTest.qWait(220)
                for page,name in ((pane.home,'home'),(pane.editor,'editor'),(pane.stages,'stages'),(pane.confirmation,'delete')):
                    if page is pane.editor: pane.edit(controller.memory.snapshot()['memories'][0])
                    elif page is pane.confirmation: pane.confirm_delete(row['id'])
                    else: pane.stack.setCurrentWidget(page)
                    QTest.qWait(220)
                    assert pane.width()>250 and pane.height()>500
                    assert not pane.findChild(QScrollArea).horizontalScrollBar().isVisible()
                    assert window.grab().save(str(output/f'memory-{name}-{width}.png'))
                    layouts.append({'page':name,'window':[width,height],'dpr':window.devicePixelRatioF()})
            checks.append('home editor relationship and delete pages render at both window sizes')
            pane.show_home(); pane.confirm_delete(row['id'])
            assert len(controller.memory.snapshot()['memories'])==3
            pane.show_home(); assert len(controller.memory.snapshot()['memories'])==3
            pane.confirm_delete(row['id']); QTest.qWait(220); click(pane.confirm_button)
            wait(lambda:pane.pending is None and len(controller.memory.snapshot()['memories'])==2)
            assert '清茶' not in chat._messages('茶')[0][0]['content']
            checks.append('deletion confirmation cancels safely and confirmed delete removes model memory')
            controller.save_memory('<b>文字原样显示</b>','fact').result(3); pane.refresh(force=True)
            assert all(row.title.textFormat()==Qt.TextFormat.PlainText for row in pane.list_body.findChildren(SettingsRow))
            checks.append('memory markup renders as literal text')
            settings.stack.setCurrentWidget(settings.root); settings.confirm_clear()
            assert not settings.clear_memory_switch.isChecked()
            click(settings.clear_memory_switch); assert settings.clear_memory_switch.isChecked()
            score=controller.memory.profile()['affinity']
            click(settings.clear_confirm); wait(lambda:not controller.memory.snapshot()['memories'])
            assert controller.memory.profile()['affinity']==score
            checks.append('clear chat defaults to retaining memory and provides explicit clear-both switch')
            assert chat.calls==7 and not controller.call.active and len(voice.jobs)==chat.calls
            report={'checks':checks,'passed':len(checks),'layout_checks':layouts,'cloud_requests':0,
                    'physical_microphone_started':False,'physical_audio_played':False}
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))
        finally: controller.close().result(5); window.close()


if __name__=='__main__': main()
