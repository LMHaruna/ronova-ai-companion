"""Noise-contaminated endpoints and confirmed interruption; no devices/network."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_input import CALL_CONFIG, SpeechEnergyGate, InputSession, LiveMicrophone


def frame(level):
    # The gate operates on RMS, independent of this synthetic signal's shape.
    return np.full(320, level, dtype='<i2').tobytes()


class GateTests(unittest.TestCase):
    def gate(self, level):
        gate = SpeechEnergyGate()
        for _ in range(40):
            self.assertFalse(gate.process(frame(level), True))
        self.assertTrue(gate.ready)
        return gate

    def test_vad_positive_background_does_not_start_speech(self):
        gate = self.gate(180)
        for _ in range(300):
            self.assertFalse(gate.process(frame(200), True))
        self.assertGreater(gate.rejected_vad_frames, 0)
        self.assertGreater(gate.threshold_rms, 200)

    def test_quiet_room_keeps_soft_speech_above_minimum(self):
        gate = self.gate(0)
        self.assertTrue(gate.process(frame(150), True))
        self.assertFalse(gate.process(frame(80), True, active=True))

    def test_hysteresis_preserves_quieter_syllables_after_onset(self):
        gate = self.gate(180)
        self.assertFalse(gate.process(frame(300), True, active=False))
        self.assertTrue(gate.process(frame(300), True, active=True))

    def test_active_voice_never_raises_background_threshold(self):
        gate = self.gate(180)
        for _ in range(500):
            self.assertTrue(gate.process(frame(5000), True, active=True))
        self.assertAlmostEqual(gate.noise_rms, 180)

    def test_no_audio_retained_and_numeric_history_is_bounded(self):
        gate = self.gate(100)
        for _ in range(1000):
            gate.process(frame(100), False)
        self.assertEqual(len(gate.levels), 250)
        self.assertTrue(all(isinstance(value, float) for value in gate.levels))
        self.assertTrue(all(isinstance(value, (float, int)) for value in gate.timings().values()))

    def test_speaker_playback_cannot_train_or_complete_background_calibration(self):
        gate = SpeechEnergyGate()
        for _ in range(100): gate.process(frame(5000), False, learn=False)
        self.assertFalse(gate.ready)
        self.assertEqual(len(gate.levels), 0)
        for _ in range(40): gate.process(frame(180), False)
        self.assertTrue(gate.ready)
        for _ in range(1000): gate.process(frame(5000), False, learn=False)
        self.assertAlmostEqual(gate.noise_rms, 180)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.config = CALL_CONFIG
        self.started, self.confirmed, self.final = Mock(), Mock(), Mock()
        self.recognizer = Mock()
        self.recognizer.feed.return_value = '今天有点累'
        self.recognizer.finish.return_value = '今天有点累'
        self.session = InputSession(self.recognizer, self.started, Mock(), self.final,
                                    Mock(), self.config, on_confirmed=self.confirmed)

    def feed(self, speech, count):
        for _ in range(count):
            self.session.process(frame(900), speech, 1.)

    def test_short_candidate_noise_cannot_interrupt(self):
        self.feed(True, 12)
        self.started.assert_called_once()
        self.confirmed.assert_not_called()
        self.feed(False, 55)
        self.final.assert_not_called()

    def test_chinese_and_four_hundred_ms_are_both_required(self):
        self.feed(True, 19)
        self.confirmed.assert_not_called()
        self.feed(True, 1)
        self.confirmed.assert_called_once()
        self.feed(True, 25)
        self.confirmed.assert_called_once()

    def test_non_chinese_noise_never_confirms_interruption(self):
        for text in ('', '...', 'noise', '嗯'):
            self.session.reset()
            self.recognizer.feed.return_value = text
            self.feed(True, 50)
        self.confirmed.assert_not_called()

    def test_reset_rejects_incomplete_previous_confirmation(self):
        self.feed(True, 19)
        self.session.reset(wait_for_silence=True)
        self.feed(True, 100)
        self.confirmed.assert_not_called()
        self.feed(False, 55)
        self.feed(True, 20)
        self.confirmed.assert_called_once()

    def test_noise_positive_vad_after_user_speech_still_sends_automatically(self):
        gate = SpeechEnergyGate(self.config)
        for _ in range(40): gate.process(frame(180), True)
        for level, count in ((200, 100), (900, 40), (200, 100)):
            for _ in range(count):
                raw = frame(level)
                speech = gate.process(raw, True, self.session.detector.active)
                self.session.process(raw, speech, 1.)
        self.started.assert_called_once()
        self.confirmed.assert_called_once()
        self.final.assert_called_once()
        self.assertEqual(self.final.call_args.args[0], '今天有点累')
        self.assertEqual(self.final.call_args.args[1]['endpoint_silence_seconds'], 1.1)

    def test_a_short_pause_does_not_split_speech_even_with_background(self):
        gate = SpeechEnergyGate(self.config)
        for _ in range(40): gate.process(frame(180), True)
        for level, count in ((900, 30), (180, 30), (900, 30), (180, 55)):
            for _ in range(count):
                raw = frame(level)
                speech = gate.process(raw, True, self.session.detector.active)
                self.session.process(raw, speech, 1.)
        self.final.assert_called_once()
        self.confirmed.assert_called_once()


class WorkerTests(unittest.TestCase):
    def test_live_worker_calibrates_confirms_and_ends_despite_background_vad(self):
        self.run_worker()

    def test_aec_attenuation_does_not_raise_the_microphone_speech_gate(self):
        processor = Mock()
        processor.audible.return_value = False
        processor.process.side_effect = lambda data, captured: ((np.frombuffer(data, '<i2') * .1).astype('<i2').tobytes(), True)
        self.run_worker(processor)
        processor.close.assert_called_once()

    def run_worker(self, processor=None):
        stream = Mock(active=True)
        recognizer = Mock()
        recognizer.feed.return_value = '今天有点累'
        recognizer.finish.return_value = '今天有点累'
        done = threading.Event()
        started, confirmed, final = Mock(), Mock(), Mock(side_effect=lambda *args: done.set())
        vad = Mock()
        vad.is_speech.return_value = True  # Reproduce the user's stuck endpoint.
        mic = LiveMicrophone(started, Mock(), final, Mock(), config=CALL_CONFIG,
                             recognizer_factory=lambda: recognizer,
                             stream_factory=lambda **kwargs: stream,
                             vad_factory=lambda: vad, on_confirmed=confirmed,
                             processor_factory=(lambda: processor) if processor else None)
        def send(level, count, target):
            for _ in range(count):
                mic._callback(frame(level), 320, Mock(currentTime=1, inputBufferAdcTime=1), False)
            deadline = time.perf_counter() + 2
            while mic.energy_gate.frames < target and time.perf_counter() < deadline:
                time.sleep(.005)
            self.assertEqual(mic.energy_gate.frames, target)
        try:
            with patch('sounddevice.check_input_settings'):
                mic.start()
                deadline = time.perf_counter() + 2
                while mic.state == 'preparing' and time.perf_counter() < deadline: time.sleep(.005)
                self.assertEqual(mic.state, 'listening')
                send(180, 40, 40)
                recognizer.feed.assert_not_called()
                send(900, 30, 70)
                confirmed.assert_called_once()
                send(180, 55, 125)
                self.assertTrue(done.wait(1))
                started.assert_called_once()
                final.assert_called_once()
                self.assertEqual(final.call_args.args[0], '今天有点累')
                quiet = frame(18 if processor else 180)
                self.assertTrue(any(quiet in call.args[1] for call in recognizer.feed.call_args_list))
        finally:
            mic.stop()
        self.assertEqual(mic.state, 'off')
        self.assertFalse(mic._thread.is_alive())
        self.assertTrue(mic._queue.empty())
        stream.close.assert_called_once()


if __name__ == '__main__': unittest.main()
