"""Click provenance/history/restore in the real phone, no cloud or audio devices."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
if os.environ.get('RONOVA_CODE_ROOT'):sys.path.insert(0,os.environ['RONOVA_CODE_ROOT'])
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPointF,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QScrollArea,QPushButton,QLabel
from client_app import ClientWindow,load_fonts
from client_controller import DesktopController
from client_fixtures import FakeCall,fake_router
from client_panels import SettingsRow
from test_memory_safety import data


def main():
    output=Path(sys.argv[1]);output.mkdir(parents=True,exist_ok=True)
    app=QApplication([]);app.setQuitOnLastWindowClosed(False);load_fonts()
    checks=[];layouts=[]
    def wait(predicate):
        deadline=time.monotonic()+5
        while not predicate() and time.monotonic()<deadline:QTest.qWait(10)
        assert predicate(),'history UI action timed out'
        QTest.qWait(220)
    with tempfile.TemporaryDirectory() as temporary, \
            patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')), \
            patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('microphone forbidden')), \
            patch('client_models.environment',return_value='memory-history-fixture'), \
            patch('client_panels.input_devices',return_value=[]),patch('client_panels.output_devices',return_value=[]):
        router,chat,voice=fake_router();controller=DesktopController(temporary,router=router,call_factory=FakeCall)
        window=ClientWindow(controller,ROOT);window.show();QTest.qWait(300)
        def click(widget):
            app.processEvents()
            ancestor=widget.parentWidget()
            while ancestor:
                if isinstance(ancestor,QScrollArea):ancestor.ensureWidgetVisible(widget,0,0)
                ancestor=ancestor.parentWidget()
            app.processEvents();QTest.qWait(50)
            point=widget.mapTo(window.overlay.surface,widget.rect().center())
            QTest.mouseClick(window.overlay.viewport(),Qt.MouseButton.LeftButton,pos=window.overlay.mapFromScene(QPointF(point)))
        def record(identifier):return next(row for row in controller.memory.snapshot()['memories'] if row['id']==identifier)
        def restore_button(index):return pane.history_layout.itemAt(index).widget().findChild(QPushButton)
        try:
            text='我的名字叫<b>小林</b>';profile=controller.memory.profile()
            controller.memory.apply_analysis('voice-source',text,data(text,key='identity.name'),profile['memory_epoch'],profile['relationship_epoch'])
            identifier=controller.memory.snapshot()['memories'][0]['id']
            baseline=controller.memory.versions(identifier)[0]
            controller.save_memory('我的名字叫小李','fact',identifier).result(3)
            controller.save_memory('我的名字叫小陈','fact',identifier).result(3)
            window.settings();wait(lambda:window.overlay.phase=='shown')
            click(window.settings_pane.memory_row);pane=window.settings_pane.memory_page
            wait(lambda:window.settings_pane.stack.currentWidget() is pane)
            click(pane.list_body.findChild(SettingsRow));wait(lambda:pane.stack.currentWidget() is pane.editor)
            assert pane.edit_id==identifier and '手动编辑' in pane.source_label.text()
            assert pane.history_button.isVisible()
            checks.append('each editor exposes current source timestamp and a history entry')
            click(pane.history_button);wait(lambda:pane.stack.currentWidget() is pane.history)
            assert pane.history_layout.count()==3 and not restore_button(0).isEnabled()
            labels=pane.history_body.findChildren(QLabel)
            assert all(label.textFormat()==Qt.TextFormat.PlainText for label in labels)
            assert any('原话依据：'+text in label.text() for label in labels)
            checks.append('versions show chronology source original evidence and literal markup')
            click(restore_button(2));wait(lambda:pane.stack.currentWidget() is pane.confirmation)
            assert record(identifier)['content']=='我的名字叫小陈'
            pane.cancel_confirmation();wait(lambda:pane.stack.currentWidget() is pane.history)
            assert record(identifier)['content']=='我的名字叫小陈'
            checks.append('restore preview and cancellation never change the current record')
            click(restore_button(2));wait(lambda:pane.stack.currentWidget() is pane.confirmation)
            click(pane.confirm_button);wait(lambda:pane.pending is None and record(identifier)['content']==text)
            assert len(controller.memory.versions(identifier))==4
            assert record(identifier)['source']=='restored' and record(identifier)['evidence']==text
            assert text in chat._messages('名字')[0][0]['content'] and '小陈' not in chat._messages('名字')[0][0]['content']
            checks.append('confirmed restore keeps all versions and updates the next model context')
            pane.edit(record(identifier));pane.show_versions();wait(lambda:pane.stack.currentWidget() is pane.history)
            click(restore_button(1));wait(lambda:pane.stack.currentWidget() is pane.confirmation)
            click(pane.confirm_button);wait(lambda:pane.pending is None and record(identifier)['content']=='我的名字叫小陈')
            assert len(controller.memory.versions(identifier))==5
            checks.append('a restored record can recover the value from before restoration')
            pane.edit(record(identifier));controller.save_memory('我的名字叫小周','fact',identifier).result(3)
            pane.content.setPlainText('我的名字叫小王');click(pane.save_button)
            wait(lambda:pane.pending is None)
            assert record(identifier)['content']=='我的名字叫小周' and '已更新' in pane.status_label.text()
            assert window.overlay.surface.toast.isVisible()
            assert len(controller.memory.versions(identifier))==6
            checks.append('stale editor is rejected with an understandable message and no overwrite')
            # A second isolated record supplies enough versions to exercise lazy paging.
            paged=controller.memory.save('分页0')
            for index in range(1,44):controller.memory.save('分页'+str(index),'fact',paged)
            pane.edit(record(paged));pane.show_versions();wait(lambda:pane.stack.currentWidget() is pane.history)
            assert pane.history_layout.count()==20 and pane.load_versions_button.isVisible()
            click(pane.load_versions_button);wait(lambda:pane.history_layout.count()==40)
            click(pane.load_versions_button);wait(lambda:pane.history_layout.count()==44)
            assert not pane.load_versions_button.isVisible()
            checks.append('older versions load in bounded pages without dropping original records')
            window.overlay.surface.toast_timer.stop();window.overlay.surface.toast.hide()
            for width,height in ((1280,802),(960,602)):
                window.resize(width,height);QTest.qWait(220)
                for name in ('editor','history','restore'):
                    pane.edit(record(identifier))
                    if name!='editor':pane.show_versions()
                    if name=='restore':pane.confirm_restore(controller.memory.versions(identifier)[1])
                    QTest.qWait(220)
                    page=pane.stack.currentWidget();scroll=page.findChild(QScrollArea)
                    assert not scroll.horizontalScrollBar().isVisible()
                    assert pane.width()>250 and pane.height()>500
                    assert window.grab().save(str(output/f'history-{name}-{width}.png'))
                    layouts.append({'page':name,'window':[width,height],'dpr':window.devicePixelRatioF()})
            checks.append('source history and restore pages remain in the phone at both window sizes')
            pane.show_home();pane.confirm_delete(identifier);QTest.qWait(220);click(pane.confirm_button)
            wait(lambda:pane.pending is None and all(row['id']!=identifier for row in controller.memory.snapshot()['memories']))
            assert controller.memory.connection.execute('SELECT COUNT(*) FROM memory_versions WHERE memory_id=?',(identifier,)).fetchone()[0]==0
            checks.append('confirmed deletion purges the selected record and all its versions')
            assert chat.calls==0 and not voice.jobs
            report={'passed':len(checks),'checks':checks,'layouts':layouts,'cloud_requests':0,
                'physical_microphone_started':False,'physical_audio_played':False,'production_database_modified':False}
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))
        finally:controller.close().result(5);window.close()


if __name__=='__main__':main()
