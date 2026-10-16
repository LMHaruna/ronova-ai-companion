"""Validate local recording input before any verification side effects."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
import verify_text_playback as playback


class RecordingInputTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir=ROOT/'runtime')
        self.path=Path(self.directory.name)/'测试录音.wav'

    def tearDown(self):
        self.directory.cleanup()

    def write_wav(self,channels=1,width=2,rate=24000,frames=2400):
        pcm=b'\x01'*(channels*width*frames)
        with wave.open(str(self.path),'wb') as recording:
            recording.setnchannels(channels)
            recording.setsampwidth(width)
            recording.setframerate(rate)
            recording.writeframes(pcm)
        return pcm

    def test_selected_unicode_wav_preserves_pcm_bytes(self):
        expected=self.write_wav()
        self.assertEqual(playback.read_recording(self.path),expected)

    def test_wrong_format_and_empty_recording_are_rejected(self):
        for options in ({'channels':2},{'width':1},{'rate':32000},{'frames':0}):
            with self.subTest(options=options):
                self.write_wav(**options)
                with self.assertRaises(ValueError):
                    playback.read_recording(self.path)

    def test_truncated_wav_is_rejected(self):
        self.write_wav()
        self.path.write_bytes(self.path.read_bytes()[:-2])
        with self.assertRaisesRegex(ValueError,'不完整'):
            playback.read_recording(self.path)

    def test_bad_cli_inputs_fail_before_devices_or_live_requests(self):
        cases=([],['--play-existing-audio'],
               ['--play-existing-audio','--audio',str(self.path)],
               ['--live-fixed-turn','--audio',str(self.path)],
               ['--live-fixed-turn','--play-existing-audio'])
        with patch.object(playback,'DesktopController') as controller, \
                patch.object(playback,'live_fixed_turn') as live:
            for arguments in cases:
                with self.subTest(arguments=arguments),redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as failure:
                        playback.main(arguments)
                    self.assertEqual(failure.exception.code,2)
            controller.assert_not_called()
            live.assert_not_called()


if __name__=='__main__': unittest.main()
