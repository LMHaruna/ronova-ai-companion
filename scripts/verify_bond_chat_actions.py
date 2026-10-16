"""Render installed bond art and per-reply icons with entirely offline fixtures."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(os.environ.get("RONOVA_ASSET_ROOT",Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/"scripts"),str(ROOT/"runtime/client-tools")]
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router

def main():
    output=Path(os.environ.get("RONOVA_PREVIEW_DIR",ROOT/"runtime/verification-records/bond-chat-actions-20261010"))
    output.mkdir(parents=True,exist_ok=True)
    app=QApplication([]); app.setQuitOnLastWindowClosed(False)
    checks=[]; captures=[]
    with tempfile.TemporaryDirectory(dir=output) as temporary, \
            patch("requests.sessions.Session.request",side_effect=AssertionError("live network forbidden")), \
            patch("voice_input.LiveMicrophone.start",side_effect=AssertionError("live microphone forbidden")), \
            patch("client_models.environment",return_value="offline-layout-fixture"):
        router,chat,voice=fake_router()
        controller=DesktopController(temporary,router=router,call_factory=FakeCall)
        with controller.memory.connection:
            controller.memory.connection.execute("UPDATE profile SET affinity=100,stage='close' WHERE id=1")
        epoch=controller.memory.prepare_oath()
        controller.memory.complete_oath(epoch)
        controller.store.begin("first","qwen","今天忙了一整天，有些累。")
        controller.store.update("first","我的眷属，先坐下休息。今天的事，慢慢告诉我。","completed")
        controller.store.begin("second","qwen","还有什么想对我说的吗？")
        controller.store.update("second","回应暂时中断。","failed")
        window=ClientWindow(controller,ROOT)
        window.timer.stop(); window.motion_timer.stop()
        try:
            window.show(); app.processEvents()
            for width,height in ((1280,802),(960,602)):
                window.overlay.close_page(); QTest.qWait(250)
                window.resize(width,height); app.processEvents()
                window.refresh(); window._portrait_transition_from=None
                assert window.assets.stage=="bond"
                assert window.assets.portrait_sources["bond"].name=="portrait-bond-final.png"
                assert window.grab().save(str(output/("bond-home-"+str(width)+".png")))
                window.text(); QTest.qWait(260)
                window.state=controller.snapshot(); window.text_pane.refresh(); QTest.qWait(80)
                history=window.text_pane.history
                for record_id,buttons in history.action_buttons.items():
                    bubble=next(item for item in history.findChildren(QLabel,"assistantBubble")
                                if item.property("recordId")==record_id)
                    for item in buttons.values():
                        assert item.width()==28 and item.height()==28
                        assert item.mapTo(history.content,QPoint()).y()>bubble.mapTo(history.content,bubble.rect().bottomLeft()).y()
                        assert item.parentWidget().rect().contains(item.geometry())
                assert history.horizontalScrollBar().maximum()==0
                filename="bond-chat-"+str(width)+".png"
                assert window.grab().save(str(output/filename))
                captures.append({"width":width,"height":height,"dpr":window.devicePixelRatioF(),"file":filename})
                voice.playback_active=True; controller.playback_record_id="first"
                window.state=controller.snapshot(); window.text_pane.refresh()
                assert history.action_buttons["first"]["stop"].isEnabled()
                assert not history.action_buttons["second"]["stop"].isEnabled()
                assert window.grab().save(str(output/("bond-chat-playing-"+str(width)+".png")))
                voice.playback_active=False
                window.state=controller.snapshot(); window.text_pane.refresh()
                window.overlay.close_page(); QTest.qWait(250)
                with controller.memory.connection:
                    controller.memory.connection.execute("UPDATE profile SET affinity=89,stage='close' WHERE id=1")
                controller.memory.revision+=1; window.refresh()
                assert window.assets.stage=="close"
                with controller.memory.connection:
                    controller.memory.connection.execute("UPDATE profile SET affinity=90,stage='close' WHERE id=1")
                controller.memory.revision+=1; window.refresh()
                assert window.assets.stage=="bond"
            assert chat.calls==0 and not controller.call.active
            checks.extend(["selected bond source matches v3", "icons under their assistant bubble",
                           "compact 28px hit areas with no horizontal overflow",
                           "stop enabled only for playback owner",
                           "89 falls back to close and 90 restores bond after oath",
                           "two supported window sizes"])
            report={"passed":len(checks),"checks":checks,"captures":captures,
                    "cloud_requests":0,"physical_microphone_started":False,"physical_audio_played":False}
            (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            print(json.dumps(report,ensure_ascii=False))
        finally:
            controller.close().result(5)
            window.close(); app.processEvents()

if __name__=="__main__": main()

