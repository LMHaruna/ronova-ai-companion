"""Driver completion cannot close the output until the separate EOF guard drains."""
from pathlib import Path
import ctypes
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pcm_player import PCMPlayer


class EndGuardTests(unittest.TestCase):
    def setUp(self):
        self.cancel=threading.Event(); self.api=Mock(); self.headers=[]; self.data=[]
        def opened(handle,*args): handle._obj.value=1; return 0
        def written(handle,header,size):
            self.headers.append(header._obj)
            self.data.append(ctypes.string_at(header._obj.data,header._obj.length)); return 0
        def reset(handle):
            for header in self.headers: header.flags|=1
            return 0
        self.api.waveOutOpen.side_effect=opened; self.api.waveOutWrite.side_effect=written
        self.api.waveOutReset.side_effect=reset
        for name in ('waveOutPrepareHeader','waveOutUnprepareHeader','waveOutClose'):
            getattr(self.api,name).return_value=0
        with patch('pcm_player.C.WinDLL',return_value=self.api):
            self.player=PCMPlayer(self.cancel,end_padding_seconds=.4)
        self.thread=None

    def tearDown(self):
        self.cancel.set()
        if self.thread: self.thread.join(2)
        self.player.close()

    def finish_async(self):
        self.finished=threading.Event()
        def run(): self.player.finish(); self.finished.set()
        self.thread=threading.Thread(target=run); self.thread.start()

    def wait_guard(self):
        deadline=time.monotonic()+1
        while len(self.headers)<3 and time.monotonic()<deadline: time.sleep(.005)
        self.assertEqual(len(self.headers),3)

    def source(self):
        self.head=b'\x31\x00'*2400; self.tail=b'\x72\x00'*256
        self.player.write_pcm(self.head,24000); self.player.write_pcm(self.tail,24000)
        self.assertEqual(self.data,[self.head,self.tail])

    def test_short_source_tail_survives_and_close_waits_for_guard_once(self):
        self.source(); self.finish_async(); self.wait_guard()
        self.assertEqual(self.data,[self.head,self.tail,b'\0\0'*9600])
        self.headers[0].flags|=1; self.headers[1].flags|=1
        self.assertFalse(self.finished.wait(.04))
        self.api.waveOutClose.assert_not_called(); self.api.waveOutReset.assert_not_called()
        self.headers[2].flags|=1; self.assertTrue(self.finished.wait(1))
        self.assertEqual(self.player.submitted_frames,self.player.completed_frames)
        self.assertEqual(self.player.submitted_frames-self.player.padding_frames,2656)
        self.api.waveOutClose.assert_called_once(); self.api.waveOutReset.assert_not_called()
        self.player.finish(); self.assertEqual(len(self.data),3)

    def test_cancellation_before_eof_skips_guard(self):
        self.source(); self.cancel.set(); self.player.finish()
        self.assertEqual(self.data,[self.head,self.tail]); self.assertEqual(self.player.padding_frames,0)
        self.api.waveOutReset.assert_called_once()

    def test_cancellation_while_guard_is_pending_remains_immediate(self):
        self.source(); self.finish_async(); self.wait_guard()
        self.cancel.set(); self.assertTrue(self.finished.wait(.2))
        self.api.waveOutReset.assert_called_once(); self.api.waveOutClose.assert_called_once()


if __name__=='__main__': unittest.main()
