"""Index output remains clocked across synthesis gaps; no physical audio."""
import ctypes
import struct
import sys
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pcm_player import PCMPlayer
from local_voice import LocalVoice, SpeechJob


class SimulatedDevice:
    def __init__(self):
        self.now=0.0;self.end=0.0;self.rate=1000;self.buffers=[];self.received=[];self.gaps=[]
        self.api=Mock()
        self.api.waveOutOpen.side_effect=self.open
        self.api.waveOutWrite.side_effect=self.write
        self.api.waveOutGetPosition.side_effect=self.position
        self.api.waveOutReset.side_effect=self.reset
        for name in ('waveOutPrepareHeader','waveOutUnprepareHeader','waveOutClose'):
            getattr(self.api,name).return_value=0
    def open(self,handle,device,fmt,*args):
        handle._obj.value=1;self.rate=fmt._obj.rate;return 0
    def write(self,handle,header,size):
        h=header._obj
        if self.buffers and self.now>self.end+.001:self.gaps.append(self.now-self.end)
        start=max(self.now,self.end);self.end=start+h.length/(2*self.rate)
        self.buffers.append((h,start,self.end))
        self.received.append((h.source_frames,ctypes.string_at(h.data,h.length)))
        return 0
    def advance(self,now):
        self.now=now
        for header,start,end in self.buffers:
            if now>=end:header.flags|=1
    def position(self,handle,value,size):
        value._obj.kind=2
        value._obj.data.value=sum(min(h.length//2,max(0,round((self.now-start)*self.rate)))
                                 for h,start,end in self.buffers)
        return 0
    def reset(self,handle):
        for h,start,end in self.buffers:h.flags|=1
        return 0
    def clock_wait(self,delay):self.advance(self.now+delay);return False


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.device=SimulatedDevice();self.cancel=threading.Event()
        with patch('pcm_player.C.WinDLL',return_value=self.device.api), \
                patch('pcm_player.threading.Thread.start'):
            self.player=PCMPlayer(self.cancel,keep_alive=True,end_padding_seconds=.4,reference=Mock())
        self.player.reference.observe.return_value=None
        self.clock=patch('pcm_player.time.perf_counter',side_effect=lambda:self.device.now);self.clock.start()
    def tearDown(self):
        self.cancel.set();self.player.close();self.clock.stop()
    def pump(self,until):
        while self.device.now+.01<until:
            self.device.advance(self.device.now+.01)
            self.player._supply_continuity_silence()
        self.device.advance(until)
    def test_slow_next_fragment_has_no_device_gap_and_speech_is_bit_exact(self):
        first=struct.pack('<h',3456)*100;second=struct.pack('<h',7890)*100
        self.player.write_pcm(first,1000)
        self.pump(3.1)
        self.assertGreater(self.player.continuity_silence_frames,0)
        self.player.write_pcm(second,1000)
        with patch.object(self.cancel,'wait',side_effect=self.device.clock_wait):self.player.finish()
        self.assertEqual(self.device.gaps,[])
        self.assertEqual(b''.join(data for frames,data in self.device.received if frames),first+second)
        self.assertEqual(self.player.source_submitted_frames,200)
        self.assertEqual(self.player.source_completed_frames,200)
        self.assertEqual(self.player.submitted_frames,200+self.player.continuity_silence_frames+400)
        self.assertEqual(self.player.completed_frames,self.player.submitted_frames)
        self.player.reference.append.assert_any_call(first,0)
        self.device.api.waveOutReset.assert_not_called()
    def test_keepalive_uses_device_remaining_time_for_long_pending_headers(self):
        self.player.write_pcm(b'\x10\0'*3000,1000)
        self.device.advance(2.975)
        self.player._supply_continuity_silence()
        self.assertAlmostEqual(self.device.end,3.055,places=3)
        self.assertEqual(self.player.continuity_silence_frames,55)
    def test_ready_chunks_and_no_first_audio_never_add_silence(self):
        self.player._supply_continuity_silence()
        self.device.api.waveOutOpen.assert_not_called()
        self.player.write_pcm(b'\x10\0'*100,1000)
        self.device.advance(.03);self.player._supply_continuity_silence()
        self.player.write_pcm(b'\x20\0'*100,1000)
        self.assertEqual(self.player.continuity_silence_frames,0)
    def test_completion_stops_refill_and_preserves_separate_eof_guard(self):
        self.player.write_pcm(b'\x10\0'*100,1000)
        self.pump(.2)
        with patch.object(self.cancel,'wait',side_effect=self.device.clock_wait):self.player.finish()
        count=len(self.device.received)
        self.player._supply_continuity_silence();self.player.finish()
        self.assertEqual(len(self.device.received),count)
        self.assertEqual(self.player.padding_frames,400)
        self.assertEqual(self.player.source_completed_frames,100)
    def test_cancellation_does_not_refill_or_wait_for_silence(self):
        self.player.write_pcm(b'\x10\0'*100,1000);self.pump(.2)
        count=len(self.device.received)
        self.cancel.set();self.player._supply_continuity_silence();self.player.finish()
        self.assertEqual(len(self.device.received),count)
        self.assertEqual(self.player.padding_frames,0)
        self.device.api.waveOutReset.assert_called_once()
    def test_silence_cannot_drive_avatar_or_echo_as_speech(self):
        from pcm_player import PlaybackEnvelope
        self.player.envelope=PlaybackEnvelope()
        self.player.write_pcm(b'\x10\0'*100,1000);self.pump(.2)
        self.assertEqual(self.player.playback_level,0)
        silence=[data for frames,data in self.device.received if not frames]
        self.assertTrue(silence);self.assertTrue(all(data==bytes(len(data)) for data in silence))
    def test_clock_failure_stops_refill_without_cancelling_speech(self):
        self.player.write_pcm(b'\x10\0'*100,1000)
        self.device.api.waveOutGetPosition.side_effect=OSError('simulated clock failure')
        self.player._continuity_stop.wait=Mock(side_effect=lambda timeout:self.player._continuity_stop.is_set())
        self.player._maintain_stream()
        self.assertEqual(self.player.continuity_error,'OSError')
        self.assertFalse(self.cancel.is_set())
        self.player.write_pcm(b'\x20\0'*100,1000)
        self.assertEqual(self.player.source_submitted_frames,200)

    def test_background_refill_thread_runs_and_stops_with_the_player(self):
        import time
        self.player.write_pcm(b'\x10\0'*100,1000)
        self.device.advance(.08)
        self.player._continuity_thread.start()
        deadline=time.monotonic()+1
        while self.player.continuity_silence_frames==0 and time.monotonic()<deadline:time.sleep(.005)
        self.assertGreater(self.player.continuity_silence_frames,0)
        self.player.stop()
        self.assertFalse(self.player._continuity_thread.is_alive())


class RoutingTests(unittest.TestCase):
    def test_only_index_requests_continuous_device_silence(self):
        for engine in ('indextts-2.5','gpt-sovitsv2proplus'):
            voice=LocalVoice(tts_engine=engine)
            player=Mock();voice.pcm_factory=Mock(return_value=player)
            job=SpeechJob()
            try:
                voice._play_fragment(job,{'path':'unused-fixture.wav','audio_seconds':.1,'index':0})
                self.assertEqual(voice.pcm_factory.call_args.kwargs.get('keep_alive',False),engine=='indextts-2.5')
            finally:
                voice._pcm=None;voice.close()


if __name__=='__main__':unittest.main()
