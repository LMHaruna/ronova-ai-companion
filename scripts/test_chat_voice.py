"""Regression checks for interrupted streams, history, ordered playback and cancellation."""
from pathlib import Path
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_client import ChatClient, ChatError
from local_voice import LocalVoice, SpeechJob, ROOT
import requests


class Response:
    def __init__(self, lines=(), status=200):
        self.lines, self.status_code = lines, status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def iter_lines(self, **kwargs):
        yield from self.lines


def token(text, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": finish}]})


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.dict(os.environ, {"ALI_API_Key": "test-only-key"})
        self.key.start()
    def tearDown(self):
        self.key.stop()
    def test_successful_stream_commits_only_complete_pair(self):
        received = []
        chat = ChatClient(post=lambda *a, **k: Response([token("凡人，"), token("坐下。", "stop"), "data: [DONE]"]))
        self.assertEqual(chat.reply("你好", received.append), "凡人，坐下。")
        self.assertEqual(received, ["凡人，", "坐下。"])
        self.assertEqual([m["role"] for m in chat.history], ["system", "user", "assistant"])
    def test_truncated_stream_keeps_old_history(self):
        chat = ChatClient(post=lambda *a, **k: Response([token("已经显示的片段")]))
        before = list(chat.history)
        with self.assertRaises(ChatError):
            chat.reply("你好")
        self.assertEqual(chat.history, before)
    def test_http_errors_hide_response_body_and_key(self):
        chat = ChatClient(post=lambda *a, **k: Response(status=401))
        with self.assertRaises(ChatError) as error:
            chat.reply("你好")
        self.assertNotIn("test-only-key", str(error.exception))
        self.assertEqual(len(chat.history), 1)
    def test_network_failure_can_retry(self):
        def fail(*a, **kw):
            raise requests.ConnectionError("sensitive remote detail")
        chat = ChatClient(post=fail)
        with self.assertRaises(ChatError):
            chat.reply("你好")
        chat.post = lambda *a, **kw: Response([token("重试成功。", "stop")])
        self.assertEqual(chat.reply("你好"), "重试成功。")
    def test_bad_json_does_not_commit_partial_text(self):
        chat = ChatClient(post=lambda *a, **k: Response([token("部分"), "data: broken"]))
        with self.assertRaises(ChatError):
            chat.reply("你好")
        self.assertEqual(len(chat.history), 1)
    def test_invalid_stream_shape_recovers_without_crashing(self):
        for data in (None, [], {"choices": "invalid"}, {"choices": [{"delta": []}]}):
            chat = ChatClient(post=lambda *a, value=data, **k: Response(["data: " + json.dumps(value)]))
            with self.assertRaises(ChatError):
                chat.reply("你好")
            self.assertEqual(len(chat.history), 1)


class PlaybackTests(unittest.TestCase):
    def test_two_fragments_play_in_order_and_temp_is_deleted(self):
        played = []
        voice = LocalVoice(player=lambda path, duration, cancel: played.append(path.name))
        job = SpeechJob()
        voice._jobs[job.id] = job
        directory = ROOT / "runtime/chat-voice-tmp" / job.id
        directory.mkdir(parents=True)
        try:
            for i in range(2):
                path = directory / f"{i:04d}.wav"
                path.write_bytes(b"test")
                voice._queue.put({"event": "fragment", "job": job.id, "index": i,
                                  "path": str(path), "audio_seconds": .1})
            voice._queue.put({"event": "done", "job": job.id})
            self.assertTrue(job.finished.wait(3))
            self.assertEqual(played, ["0000.wav", "0001.wav"])
            self.assertFalse(directory.exists())
        finally:
            voice.close()
    def test_cancel_skips_queued_fragment(self):
        played = []
        voice = LocalVoice(player=lambda *args: played.append(True))
        job = SpeechJob()
        voice._jobs[job.id] = job
        job.cancelled.set()
        voice._queue.put({"event": "fragment", "job": job.id})
        voice._queue.put({"event": "done", "job": job.id})
        self.assertTrue(job.finished.wait(3))
        self.assertEqual(played, [])
        voice.close()
    def test_tts_failure_reports_error_and_allows_next_job(self):
        voice = LocalVoice(player=lambda *args: None)
        job = SpeechJob()
        voice._jobs[job.id] = job
        voice._queue.put({"event": "error", "job": job.id, "message": "model unavailable"})
        self.assertTrue(job.finished.wait(3))
        self.assertEqual(job.error, "model unavailable")
        self.assertNotIn(job.id, voice._jobs)
        voice.close()


if __name__ == "__main__":
    unittest.main()
