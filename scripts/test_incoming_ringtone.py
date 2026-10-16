"""Real call lifecycle and ringtone writer, with simulated PCM and no hardware."""
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from client_controller import DesktopController
from client_ringtone import IncomingRingtone
from client_fixtures import FakeCall, fake_router
from test_incoming_calls import Clock

ASSET = Path(__file__).resolve().parents[1] / 'design/client-audio/incoming-call.wav'


def wait(predicate):
    deadline = time.monotonic()+2
    while not predicate() and time.monotonic() < deadline: time.sleep(.005)
    assert predicate(), 'audio fixture did not settle'


class Player:
    def __init__(self, cancel, **options):
        self.cancel, self.options = cancel, options
        self.writes = []; self.closed = False; self.resets = 0
    def write_pcm(self, pcm, rate):
        if self.cancel.is_set(): return False
        self.writes.append((pcm, rate, self.options['volume']()))
        return True
    def stop(self): self.resets += 1
    def close(self): self.closed = True


class RingtoneTests(unittest.TestCase):
    def setUp(self):
        self.players = []; self.errors = []; self.volume = .8
        def factory(cancel, **options):
            player = Player(cancel, **options); self.players.append(player); return player
        self.ringer = IncomingRingtone(ASSET, lambda:self.volume, lambda:2,
            failed=lambda *args:self.errors.append(args), player_factory=factory)
    def tearDown(self): self.ringer.close()
    def start(self):
        self.assertTrue(self.ringer.start('one'))
        wait(lambda:self.players and self.players[0].writes)
    def test_loop_repeats_with_bounded_chunks_and_current_volume_device(self):
        self.start(); player = self.players[0]
        self.assertEqual(player.options['device'], 2)
        self.assertEqual(player.options['end_padding_seconds'], 0)
        self.assertLessEqual(player.options['max_buffer_seconds'], .12)
        self.volume = 0
        wait(lambda:any(write[2] == 0 for write in player.writes))
        self.volume = .25
        wait(lambda:any(write[2] == .25 for write in player.writes))
        self.assertTrue(all(len(pcm)/(rate*2) <= .04 for pcm,rate,_ in player.writes))
        self.assertTrue(self.ringer.stop('one')); self.assertTrue(player.closed)
        self.assertGreaterEqual(player.resets, 1)
        count = len(player.writes); time.sleep(.06); self.assertEqual(len(player.writes), count)
    def test_duplicate_start_and_stale_stop_cannot_duplicate_or_silence_new_call(self):
        self.start(); self.assertTrue(self.ringer.start('one'))
        self.assertTrue(self.ringer.stop('old')); self.assertFalse(self.players[0].closed)
        self.assertEqual(len(self.players), 1)
        self.assertTrue(self.ringer.start('two')); wait(lambda:len(self.players)==2)
        self.assertTrue(self.players[0].closed)
    def test_stop_before_player_publication_prevents_late_audio(self):
        entered, release = threading.Event(), threading.Event()
        def factory(cancel, **options):
            entered.set(); release.wait(2)
            player=Player(cancel, **options); self.players.append(player); return player
        self.ringer.player_factory = factory
        self.ringer.start('one'); self.assertTrue(entered.wait(1))
        done=[]
        worker=threading.Thread(target=lambda:done.append(self.ringer.stop('one')))
        worker.start(); self.assertTrue(self.ringer.job.cancel.wait(1)); release.set(); worker.join(1)
        self.assertEqual(done, [True]); self.assertEqual(self.players[0].writes, [])
        self.assertTrue(self.players[0].closed)
    def test_missing_asset_is_nonfatal_and_does_not_open_audio(self):
        self.ringer.path = ASSET.with_name('missing.wav')
        self.ringer.start('one'); wait(lambda:self.ringer.job.done.is_set())
        self.assertEqual(self.players, []); self.assertEqual(len(self.errors), 1)
        self.assertTrue(self.ringer.stop())
    def test_close_prevents_future_ring_jobs(self):
        self.start(); self.assertTrue(self.ringer.close())
        self.assertFalse(self.ringer.start('two')); self.assertEqual(len(self.players), 1)

    def test_clip_repeats_without_growing_the_audio_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            clip=Path(directory)/'short.wav'
            with wave.open(str(clip),'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000)
                output.writeframes(b'\x01\x00'*960+b'\x02\x00'*960+b'\x03\x00'*960)
            self.ringer.path=clip; self.start()
            wait(lambda:len(self.players[0].writes)>=7)
            chunks=[pcm for pcm,_,_ in self.players[0].writes]
            self.assertEqual(chunks[:3],chunks[3:6])
            self.assertTrue(self.ringer.stop())

    def test_thread_start_failure_can_still_be_stopped(self):
        with patch('client_ringtone.threading.Thread.start',side_effect=RuntimeError('fixture thread failure')):
            self.assertFalse(self.ringer.start('one'))
        self.assertTrue(self.ringer.stop()); self.assertEqual(self.players,[])
        self.assertEqual(len(self.errors),1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.root=Path(self.directory.name)
        target=self.root/'design/client-audio/incoming-call.wav'; target.parent.mkdir(parents=True)
        shutil.copy2(ASSET,target)
        self.players=[]; self.clock=Clock()
        def player_factory(cancel, **options):
            player=Player(cancel, **options); self.players.append(player); return player
        def ringtone_factory(*args, **kwargs):
            return IncomingRingtone(*args, **kwargs, player_factory=player_factory)
        owner=self
        class GuardedCall(FakeCall):
            def start(self,*args,**kwargs):
                owner.assertTrue(all(player.closed for player in owner.players))
                return super().start(*args,**kwargs)
        self.router,self.chat,self.voice=fake_router()
        self.controller=DesktopController(self.root,router=self.router,call_factory=GuardedCall,
            call_clock=self.clock,ringtone_factory=ringtone_factory)
        self.calls=self.controller.incoming_calls
    def tearDown(self): self.controller.close().result(5); self.directory.cleanup()
    def ring(self):
        count=len(self.players)
        identifier=self.controller.test_incoming_call().result(3)
        wait(lambda:len(self.players)>count and self.players[-1].writes)
        self.assertEqual(self.controller.call.starts,[])
        self.assertEqual(self.chat.calls,0)
        return identifier
    def assert_stopped(self):
        self.assertTrue(all(player.closed for player in self.players))
        self.assertIsNone(self.controller.ringtone.job)
    def test_answer_closes_device_before_microphone_and_opening(self):
        identifier=self.ring()
        begin=self.voice.begin
        def guarded_begin(*args,**kwargs):
            self.assert_stopped(); return begin(*args,**kwargs)
        with patch.object(self.voice,'begin',side_effect=guarded_begin):
            self.controller.answer_call(identifier).result(3)
            self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.assert_stopped(); self.assertTrue(self.controller.call.active)
    def test_decline_snooze_and_cancel_stop_without_capture(self):
        for mode in ('decline','snooze','cancel'):
            with self.subTest(mode=mode):
                identifier=self.ring()
                if mode=='cancel': self.controller.cancel().result(3)
                else: self.controller.decline_call(identifier,later=mode=='snooze').result(3)
                self.assert_stopped(); self.assertIsNone(self.calls.snapshot())
                self.assertEqual(self.controller.call.starts,[])
    def test_timeout_and_sleep_resume_stop(self):
        for seconds in (46,120):
            identifier=self.ring(); self.clock.advance(seconds); self.controller.poll()
            self.assert_stopped(); self.assertIsNone(self.calls.snapshot())
            self.assertEqual(self.calls.store.find(identifier)['state'],'missed')
    def test_all_direct_dismiss_paths_stop(self):
        for state in ('busy','withdrawn','interrupted'):
            identifier=self.ring(); self.calls.dismiss(state,identifier); self.assert_stopped()
    def test_invalidate_settings_and_changed_speaker_stop_and_next_ring_uses_device(self):
        self.ring(); self.calls.invalidate(); self.assert_stopped()
        self.ring()
        self.controller.apply_preferences(replace(self.controller.preferences,speaker=2)).result(3)
        self.assert_stopped(); self.ring()
        self.assertEqual(self.players[-1].options['device'],2)
        self.controller.apply_preferences(replace(self.controller.preferences,proactive_calls=False)).result(3)
        self.assert_stopped()
    def test_volume_change_keeps_ring_and_silences_at_zero(self):
        self.ring(); player=self.players[-1]
        self.controller.apply_preferences(replace(self.controller.preferences,volume=0)).result(3)
        wait(lambda:any(write[2]==0 for write in player.writes))
        self.assertIsNotNone(self.calls.snapshot()); self.assertFalse(player.closed)
    def test_stale_answer_does_not_stop_current_ringtone(self):
        self.ring()
        self.assertFalse(self.controller.answer_call('old').result(3))
        self.assertFalse(self.players[-1].closed)
        self.assertEqual(self.controller.call.starts,[])
    def test_close_silences_ring_before_queued_action_finishes(self):
        self.ring(); entered,release=threading.Event(),threading.Event()
        def pending(): entered.set(); release.wait(3)
        self.controller.action(pending); self.assertTrue(entered.wait(1))
        try:
            future=self.controller.close(); self.assert_stopped()
            self.assertFalse(future.done())
        finally: release.set()
        future.result(3)
    def test_ringtone_failure_does_not_prevent_answer(self):
        self.controller.ringtone.path=ASSET.with_name('missing.wav')
        identifier=self.controller.test_incoming_call().result(3)
        wait(lambda:self.controller.ringtone.job.done.is_set())
        self.controller.answer_call(identifier).result(3)
        self.assertTrue(self.controller.call.active)
    def test_unclean_audio_close_blocks_capture_and_opening(self):
        self.ring(); player=self.players[-1]
        with patch.object(player,'close',side_effect=OSError('fixture close failure')):
            self.assertFalse(self.controller.answer_call(self.calls.snapshot()['id']).result(3))
        self.assertEqual(self.controller.call.starts,[])
        self.assertEqual(self.voice.jobs,[])
        # Permit fixture cleanup after demonstrating that a failure cannot overlap.
        player.close(); self.controller.ringtone.job.closed_cleanly=True


if __name__=='__main__': unittest.main()
