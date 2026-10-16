"""Per-reply actions and Qt geometry, without network, devices or user data."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
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


class ReplyActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.guards=[
            patch("requests.sessions.Session.request",side_effect=AssertionError("live network forbidden")),
            patch("voice_input.LiveMicrophone.start",side_effect=AssertionError("live microphone forbidden")),
            patch("client_models.environment",return_value="offline-reply-action-fixture"),
        ]
        for guard in self.guards: guard.start()
        self.directory=tempfile.TemporaryDirectory()
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall)
        self.window=None

    def tearDown(self):
        self.controller.close().result(5)
        if self.window:
            self.window.timer.stop(); self.window.motion_timer.stop()
            self.window.close(); self.app.processEvents()
        self.directory.cleanup()
        for guard in reversed(self.guards): guard.stop()

    def seed(self, record_id, state="completed", reply=None, backend="qwen"):
        self.controller.store.begin(record_id,backend,"消息 "+record_id)
        self.controller.store.update(record_id,reply if reply is not None else "回复 "+record_id,state)

    def settle(self):
        self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.controller.poll()

    def open_window(self):
        self.window=ClientWindow(self.controller,ROOT)
        self.window.timer.stop(); self.window.motion_timer.stop()
        self.window.show(); self.app.processEvents()
        self.window.text(); QTest.qWait(260)
        self.window.state=self.controller.snapshot()
        self.window.text_pane.refresh(); QTest.qWait(30)
        return self.window.text_pane.history

    def ui_wait(self, predicate):
        deadline=time.monotonic()+4
        while not predicate() and time.monotonic()<deadline:
            self.app.processEvents(); QTest.qWait(10)
        self.assertTrue(predicate())

    def test_replay_older_reply_uses_its_text_instead_of_latest(self):
        self.seed("old",reply="第一条专属内容。")
        self.seed("new",reply="第二条不同的内容。")
        self.controller.replay("old").result(2)
        self.assertEqual(self.voice.replayed,["第一条专属内容。"])
        self.assertEqual(self.chat.calls,0)
        self.assertEqual(len(self.controller.records()),2)

    def test_retry_older_failure_preserves_ids_and_newer_failure(self):
        self.seed("old","failed")
        self.seed("new","failed")
        self.controller.retry_failed("old").result(2); self.settle()
        rows={row["id"]:row for row in self.controller.records()}
        self.assertEqual(set(rows),{"old","new"})
        self.assertEqual(rows["old"]["state"],"completed")
        self.assertEqual(rows["old"]["attempts"],2)
        self.assertEqual(rows["new"]["state"],"failed")
        self.assertEqual(rows["new"]["attempts"],1)
        self.assertEqual(self.chat.history[-2]["content"],"消息 old")
        self.voice.playback_active=True
        self.assertEqual(self.controller.snapshot()["playback_record_id"],"old")

    def test_missing_and_cross_backend_records_do_not_fall_back_to_latest(self):
        self.seed("current")
        self.seed("other",backend="gpt")
        self.seed("failed","failed")
        for target in ("missing","other"):
            with self.assertRaises(ValueError): self.controller.replay(target)
            with self.assertRaises(ValueError): self.controller.retry_failed(target)
        self.assertEqual(self.voice.replayed,[])
        self.assertEqual(self.chat.calls,0)

    def test_successful_reply_is_not_retried_as_failure(self):
        self.seed("done")
        with self.assertRaises(ValueError): self.controller.retry_failed("done")
        self.assertEqual(self.chat.calls,0)

    def test_stop_for_another_reply_does_not_stop_current_audio(self):
        self.seed("one"); self.seed("two")
        self.voice.playback_active=True; self.controller.playback_record_id="two"
        stopped=self.voice.stopped
        self.controller.stop_playback("one").result(2)
        self.assertEqual(self.voice.stopped,stopped)
        self.assertEqual(self.controller.snapshot()["playback_record_id"],"two")
        self.controller.stop_playback("two").result(2)
        self.assertEqual(self.voice.stopped,stopped+1)
        self.assertIsNone(self.controller.snapshot()["playback_record_id"])

    def test_queued_old_stop_cannot_stop_a_new_playback_owner(self):
        self.seed("one"); self.seed("two")
        gate=threading.Event()
        blocking=self.controller.action(lambda:gate.wait(2))
        self.controller.playback_record_id="one"; self.voice.playback_active=True
        stop=self.controller.stop_playback("one")
        self.controller.playback_record_id="two"
        stopped=self.voice.stopped; gate.set()
        blocking.result(2); stop.result(2)
        self.assertEqual(self.voice.stopped,stopped)

    def test_action_icons_are_small_and_below_each_assistant_bubble(self):
        self.seed("done"); self.seed("failed","failed")
        history=self.open_window()
        self.assertEqual(set(history.action_buttons),{"done","failed"})
        self.assertEqual(len(history.findChildren(QLabel,"assistantBubble")),2)
        for record_id,buttons in history.action_buttons.items():
            bubble=next(label for label in history.findChildren(QLabel,"assistantBubble")
                        if label.property("recordId")==record_id)
            for action,item in buttons.items():
                self.assertEqual((item.width(),item.height()),(28,28))
                self.assertEqual(item.text(),"")
                self.assertTrue(item.toolTip() and item.accessibleName())
                self.assertGreater(item.mapTo(history.content,QPoint(0,0)).y(),
                                   bubble.mapTo(history.content,bubble.rect().bottomLeft()).y())
            self.assertEqual(buttons["retry"].isEnabled(),record_id=="failed")
            self.assertEqual(buttons["replay"].isEnabled(),record_id=="done")
            self.assertFalse(buttons["stop"].isEnabled())
        self.assertFalse(hasattr(self.window.text_pane,"retry"))
        self.assertEqual(self.chat.calls,0)

    def test_click_older_replay_and_stop_target_the_clicked_row(self):
        self.seed("one",reply="点击较早回复。"); self.seed("two",reply="最新回复。")
        history=self.open_window()
        def speak(text):
            self.voice.replayed.append(text); self.voice.playback_active=True
        def stop():
            self.voice.stopped+=1; self.voice.playback_active=False
        with patch.object(self.voice,"speak",side_effect=speak),patch.object(self.voice,"stop",side_effect=stop):
            QTest.mouseClick(history.action_buttons["one"]["replay"],Qt.MouseButton.LeftButton)
            self.ui_wait(lambda:bool(self.voice.replayed) and not self.controller.snapshot()["busy"])
            self.window.state=self.controller.snapshot(); self.window.text_pane.refresh()
            self.assertEqual(self.voice.replayed,["点击较早回复。"])
            self.assertTrue(history.action_buttons["one"]["stop"].isEnabled())
            self.assertFalse(history.action_buttons["two"]["stop"].isEnabled())
            QTest.mouseClick(history.action_buttons["one"]["stop"],Qt.MouseButton.LeftButton)
            self.ui_wait(lambda:not self.voice.playback_active and not self.controller.snapshot()["busy"])
            self.window.state=self.controller.snapshot(); self.window.text_pane.refresh()
            self.assertFalse(history.action_buttons["one"]["stop"].isEnabled())

    def test_click_older_failed_reply_retries_that_row(self):
        self.seed("old","failed"); self.seed("new","failed")
        history=self.open_window()
        QTest.mouseClick(history.action_buttons["old"]["retry"],Qt.MouseButton.LeftButton)
        self.ui_wait(lambda:self.controller.records()[0]["state"]=="completed")
        self.settle()
        self.assertEqual(self.controller.records()[1]["state"],"failed")
        self.assertEqual(self.controller.records()[0]["attempts"],2)

    def test_playback_state_changes_update_buttons_without_rebuilding_history(self):
        self.seed("one"); self.seed("two")
        history=self.open_window()
        before=history.action_buttons["one"]["replay"]
        self.voice.playback_active=True; self.controller.playback_record_id="one"
        self.window.state=self.controller.snapshot(); self.window.text_pane.refresh()
        self.assertIs(history.action_buttons["one"]["replay"],before)
        self.assertTrue(history.action_buttons["one"]["stop"].isEnabled())
        self.assertIs(history.active_avatar,history.reply_avatars["one"])
        self.assertFalse(history.reply_avatars["two"].animated)
        self.controller.playback_record_id="two"
        self.window.state=self.controller.snapshot(); self.window.text_pane.refresh()
        self.assertIs(history.action_buttons["one"]["replay"],before)
        self.assertFalse(history.action_buttons["one"]["stop"].isEnabled())
        self.assertTrue(history.action_buttons["two"]["stop"].isEnabled())

    def test_other_backend_and_inflight_reply_disable_retry_replay(self):
        self.seed("other",backend="gpt")
        self.seed("pending","pending")
        history=self.open_window()
        for buttons in history.action_buttons.values():
            self.assertFalse(buttons["retry"].isEnabled())
            self.assertFalse(buttons["replay"].isEnabled())


if __name__=="__main__": unittest.main()

