"""Motion uses device position rather than future buffered PCM; no audio devices."""
from pathlib import Path
from types import SimpleNamespace
import ctypes
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pcm_player import PCMPlayer,PlaybackEnvelope
from realtime_voice import RealtimeVoice
from local_voice import LocalVoice
from client_controller import DesktopController
from client_fixtures import fake_router,FakeCall


def pcm(value,count): return struct.pack('<h',value)*count


class EnvelopeTests(unittest.TestCase):
    def test_chunk_boundaries_preserve_levels_and_short_final_syllable(self):
        whole,split=PlaybackEnvelope(),PlaybackEnvelope()
        audio=pcm(0,20)+pcm(2000,20)+pcm(6000,7)
        whole.append(audio,1000)
        for start,end in ((0,13),(13,31),(31,43),(43,47)):
            split.append(audio[start*2:end*2],1000)
        for frame in range(47): self.assertAlmostEqual(whole.at(frame),split.at(frame))
        self.assertAlmostEqual(split.at(46),6000/32768)
        self.assertEqual(split.at(47),0)

    def test_storage_is_bounded_to_levels_and_less_than_one_pcm_window(self):
        envelope=PlaybackEnvelope(); envelope.append(pcm(1000,100001),1000)
        self.assertEqual(len(envelope.windows),2000)
        self.assertEqual(len(envelope.partial),1)
        self.assertEqual(envelope.at(0),0)
        self.assertAlmostEqual(envelope.at(100000),1000/32768)


class DeviceLevelTests(unittest.TestCase):
    def setUp(self):
        self.cancel=threading.Event(); self.api=Mock(); self.headers=[]; self.received=[]
        self.position=0; self.kind=2
        self.api.waveOutOpen.side_effect=lambda handle,*args:setattr(handle._obj,'value',1) or 0
        def write(handle,header,size):
            self.headers.append(header._obj)
            self.received.append(ctypes.string_at(header._obj.data,header._obj.length)); return 0
        def position(handle,value,size):
            value._obj.kind=self.kind; value._obj.data.value=self.position; return 0
        def reset(handle):
            for header in self.headers: header.flags|=1
            return 0
        self.api.waveOutWrite.side_effect=write; self.api.waveOutGetPosition.side_effect=position
        self.api.waveOutReset.side_effect=reset
        for name in ('waveOutPrepareHeader','waveOutUnprepareHeader','waveOutClose'):
            getattr(self.api,name).return_value=0
        with patch('pcm_player.C.WinDLL',return_value=self.api):
            self.player=PCMPlayer(self.cancel,track_levels=True)

    def tearDown(self): self.cancel.set(); self.player.close()

    def test_future_queued_speech_does_not_animate_a_current_silence(self):
        audio=pcm(0,40)+pcm(1500,40)+pcm(6000,40)+pcm(0,400)
        self.player.write_pcm(audio,1000)
        self.position=20; self.assertEqual(self.player.playback_level,0)
        self.position=60; soft=self.player.playback_level
        self.position=100; loud=self.player.playback_level
        self.assertGreater(soft,0); self.assertGreater(loud,soft)
        self.position=180; self.assertEqual(self.player.playback_level,0)
        self.assertEqual(self.received,[audio]); self.api.waveOutReset.assert_not_called()

    def test_device_samples_bytes_and_milliseconds_map_to_same_syllable(self):
        self.player.write_pcm(pcm(0,40)+pcm(6000,40),1000)
        levels=[]
        for self.kind,self.position in ((2,60),(4,120),(1,60)):
            levels.append(self.player.playback_level)
        self.assertTrue(all(level==levels[0] for level in levels)); self.assertGreater(levels[0],.9)

    def test_muted_pcm_cancel_drain_and_position_fault_produce_no_motion(self):
        self.player.volume=0; self.player.write_pcm(pcm(6000,80),1000)
        self.position=40; self.assertEqual(self.player.playback_level,0)
        self.player.volume=1; self.player.write_pcm(pcm(6000,80),1000)
        self.position=120; self.assertGreater(self.player.playback_level,.9)
        self.api.waveOutGetPosition.side_effect=OSError('device clock unavailable')
        self.assertEqual(self.player.playback_level,0); self.api.waveOutReset.assert_not_called()
        self.api.waveOutGetPosition.side_effect=lambda *args:0
        self.cancel.set(); self.assertEqual(self.player.playback_level,0)
        self.cancel.clear()
        for header in self.headers: header.flags|=1
        self.assertEqual(self.player.playback_level,0)
        self.player.finish(); self.assertEqual(self.player.playback_level,0)
        self.api.waveOutReset.assert_not_called()


class RouteTests(unittest.TestCase):
    def test_both_voice_engines_expose_the_device_envelope_without_a_microphone(self):
        remote=object.__new__(RealtimeVoice); remote._lock=threading.RLock(); remote._player=None
        local=object.__new__(LocalVoice); local._pcm_lock=threading.RLock(); local._pcm=None
        self.assertEqual(remote.playback_level,0); self.assertEqual(local.playback_level,0)
        remote._player=local._pcm=SimpleNamespace(playback_level=.73)
        self.assertEqual(remote.playback_level,.73); self.assertEqual(local.playback_level,.73)

    def test_controller_snapshot_passes_selected_voice_level_without_starting_capture(self):
        with tempfile.TemporaryDirectory() as root:
            router,chat,voice=fake_router(); voice.playback_level=.37
            controller=DesktopController(root,router=router,call_factory=FakeCall)
            try:
                self.assertEqual(controller.snapshot()['playback_level'],.37)
                self.assertFalse(controller.call.active); self.assertEqual(chat.calls,0)
            finally: controller.close().result(5)


if __name__=='__main__': unittest.main()
