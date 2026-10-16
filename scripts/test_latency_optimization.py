"""Offline regression checks for incremental replies, cancellation and request budgets."""
from pathlib import Path
import json
import os
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_client import ChatClient, ChatCancelled
from local_voice import LocalVoice, SpeechJob
from reply_pipeline import SentenceBuffer, ReplyPipeline


def token(text):
    return 'data: ' + json.dumps({'choices':[{'delta':{'content':text}, 'finish_reason':None}]})


class Response:
    status_code = 200
    def __init__(self, lines): self.lines = lines; self.options = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_lines(self, **options): self.options=options; yield from self.lines
    def close(self): pass


class StreamingTests(unittest.TestCase):
    def test_hidden_reasoning_timing_is_recorded_without_exposing_the_reasoning(self):
        hidden='sensitive-reasoning-fixture'
        reasoning='data: '+json.dumps({'choices':[{'delta':{'reasoning_content':hidden},'finish_reason':None}]})
        response=Response([reasoning,token('凡人，坐下。'),'data: [DONE]'])
        with patch.dict(os.environ,{'ALI_API_Key':'test-secret'}):
            chat=ChatClient(post=lambda *a,**k:response); received=[]
            try:
                chat.reply('private-input-fixture',received.append)
                self.assertEqual(received,['凡人，坐下。'])
                self.assertEqual(chat.metrics['reasoning_characters'],len(hidden))
                self.assertGreaterEqual(chat.metrics['first_content_seconds'],chat.metrics['last_reasoning_seconds'])
                for private in (hidden,'private-input-fixture','test-secret'):
                    self.assertNotIn(private,json.dumps(chat.metrics))
            finally: chat.close()

    def test_real_loopback_sse_arrives_early_and_reuses_tcp_connection(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import time
        peers=[]; tail_sent=threading.Event(); early=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                peers.append(self.client_address)
                first=(token('首句已经到达。')+'\n\n').encode()
                tail=(token('后句。')+'\n\ndata: [DONE]\n\n').encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(first)+len(tail)));self.end_headers()
                self.wfile.write(first);self.wfile.flush()
                time.sleep(.2);tail_sent.set();self.wfile.write(tail);self.wfile.flush()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        chat=ChatClient()
        try:
            with patch.dict(os.environ,{'ALI_API_Key':'local-only-test'}),patch('chat_client.CHAT_COMPLETION_URL',f'http://127.0.0.1:{server.server_port}/chat'):
                for i in range(2):
                    tail_sent.clear()
                    chat.reply('local synthetic test',on_token=lambda value:early.append(not tail_sent.is_set()))
            self.assertTrue(early[0]);self.assertTrue(early[2])
            self.assertEqual(len(peers),2);self.assertEqual(peers[0],peers[1])
        finally:
            chat.close();server.shutdown();server.server_close();thread.join(2)

    def test_small_sse_reads_and_content_free_metrics(self):
        response = Response([token('凡人，坐下。'), 'data: [DONE]'])
        with patch.dict(os.environ, {'ALI_API_Key':'secret-for-test'}):
            chat = ChatClient(post=lambda *a, **k: response)
            chat.reply('private-for-test')
        self.assertEqual(response.options['chunk_size'], 1)
        self.assertIn('headers_seconds', chat.metrics)
        self.assertEqual(chat.metrics['content_events'], 1)
        self.assertNotIn('private-for-test', json.dumps(chat.metrics))
        self.assertNotIn('secret-for-test', json.dumps(chat.metrics))

    def test_connection_session_is_owned_and_reused(self):
        with patch('chat_client.requests.Session') as factory:
            chat=ChatClient()
            self.assertEqual(factory.call_count,1)
            self.assertEqual(chat.post, factory.return_value.post)
            chat.close()
            factory.return_value.close.assert_called_once()

    def test_cancelled_partial_reply_does_not_commit(self):
        cancel=threading.Event()
        response=Response([token('部分回复。'),token('应被取消。'),'data: [DONE]'])
        with patch.dict(os.environ, {'ALI_API_Key':'test'}):
            chat=ChatClient(post=lambda *a, **k: response)
            with self.assertRaises(ChatCancelled):
                chat.reply('测试',on_token=lambda value:cancel.set(),cancel=cancel)
        self.assertEqual(len(chat.history),1)

    def test_request_budget_keeps_system_latest_user_and_whole_pairs(self):
        chat=ChatClient(post=lambda *a, **k:None, history_turn_limit=2)
        for i in range(4):
            chat.history.extend([{'role':'user','content':str(i)}, {'role':'assistant','content':'a'+str(i)}])
        messages, omitted=chat._messages('now')
        self.assertEqual([m['role'] for m in messages],['system','user','assistant','user','assistant','user'])
        self.assertEqual([m['content'] for m in messages[1:]],['2','a2','3','a3','now'])
        self.assertEqual(omitted,2)
        self.assertEqual(len(chat.history),9)
        chat.clear();self.assertEqual(len(chat.history),1)

    def test_long_turn_does_not_split_a_history_pair(self):
        chat=ChatClient(post=lambda *a, **k:None, history_char_budget=700)
        chat.history.extend([{'role':'user','content':'x'*80},{'role':'assistant','content':'y'*80}])
        messages,omitted=chat._messages('最新')
        self.assertEqual(len(messages),2);self.assertEqual(omitted,1)


class SegmentTests(unittest.TestCase):
    def test_complete_clauses_emit_before_stream_ends_without_duplicates(self):
        sent=[]; buffer=SentenceBuffer(sent.append)
        buffer.feed('凡人倒是');self.assertEqual(sent,[])
        buffer.feed('会喊累。去睡');self.assertEqual(sent,['凡人倒是会喊累。'])
        buffer.feed('一觉。');buffer.finish()
        self.assertEqual(''.join(sent),'凡人倒是会喊累。去睡一觉。')

    def test_short_clauses_and_decimals_remain_intact(self):
        sent=[]; buffer=SentenceBuffer(sent.append)
        buffer.feed('你来了，凡人。金额12.5元，先收下。');buffer.finish()
        self.assertEqual(sent[0],'你来了，凡人。')
        self.assertTrue(any('12.5' in v for v in sent))

    def test_text_without_punctuation_has_bounded_chunks(self):
        sent=[];buffer=SentenceBuffer(sent.append,maximum=40)
        text='字'*103;buffer.feed(text);buffer.finish()
        self.assertEqual(''.join(sent),text);self.assertLessEqual(max(map(len,sent)),40)


class FakeVoice:
    def __init__(self):
        self.prepare_metrics={};self.job=None;self.sent=[];self.begins=0;self.first=threading.Event()
    def stop(self):
        if self.job: self.job.cancelled.set()
    def begin(self):
        self.begins+=1;self.job=SpeechJob();return self.job
    def append(self,job,text): self.sent.append(text);self.first.set()
    def end(self,job): job.input_closed=True;job.finished.set()


class PipelineTests(unittest.TestCase):
    def test_local_preparation_starts_before_cloud_text_and_does_not_wait_for_ready(self):
        prepared=threading.Event(); ready=threading.Event()
        class Voice(FakeVoice):
            def prepare(self): prepared.set(); return ready
        voice=Voice()
        class Chat:
            metrics={}
            def reply(self,text,on_token,cancel):
                self_test.assertTrue(prepared.is_set())
                self_test.assertFalse(ready.is_set())
                on_token('凡人，先坐下。'); return '凡人，先坐下。'
            def cancel_transport(self): pass
            def close(self): pass
        self_test=self
        pipeline=ReplyPipeline(Chat(),voice,lambda text:None,lambda text:None)
        try:
            turn=pipeline.submit('测试')
            self.assertTrue(turn.done.wait(2)); self.assertIsNone(turn.error)
            self.assertTrue(voice.sent)
            self.assertIn('voice_prepare_requested_seconds',turn.metrics)
            self.assertLessEqual(turn.metrics['first_text_seconds'],turn.metrics['first_sentence_seconds'])
        finally: pipeline.close()

    def test_failed_early_preparation_keeps_the_text_reply(self):
        class Voice(FakeVoice):
            def prepare(self): raise RuntimeError('unavailable')
        voice=Voice()
        class Chat:
            metrics={}
            def reply(self,text,on_token,cancel): on_token('文字仍然可用。'); return '文字仍然可用。'
            def cancel_transport(self): pass
            def close(self): pass
        pipeline=ReplyPipeline(Chat(),voice,lambda text:None,lambda text:None)
        try:
            turn=pipeline.submit('测试')
            self.assertTrue(turn.done.wait(2)); self.assertIsNone(turn.error)
            self.assertEqual(turn.reply,'文字仍然可用。'); self.assertIsNone(turn.voice_job)
        finally: pipeline.close()

    def test_failed_playback_does_not_kill_player_loop(self):
        voice=LocalVoice(player=lambda *args:None)
        job=SpeechJob();voice._jobs[job.id]=job
        try:
            with patch.object(voice,'_send'):
                voice._queue.put({'event':'fragment','job':job.id,'index':0,'path':'D:/outside.wav','audio_seconds':.1})
                import time
                deadline=time.perf_counter()+1
                while not job.error and time.perf_counter()<deadline:time.sleep(.005)
                self.assertTrue(job.cancelled.is_set());self.assertTrue(voice._play_thread.is_alive())
                voice._queue.put({'event':'done','job':job.id})
                self.assertTrue(job.finished.wait(1))
        finally:voice.close()

    def test_provisional_voice_error_waits_for_worker_acknowledgement(self):
        voice=LocalVoice(player=lambda *args:None)
        job=SpeechJob();voice._jobs[job.id]=job
        try:
            with patch.object(voice,'_send') as send:
                voice._queue.put({'event':'error','job':job.id,'terminal':False,'message':'failed'})
                import time
                deadline=time.perf_counter()+1
                while not job.cancelled.is_set() and time.perf_counter()<deadline:time.sleep(.005)
                self.assertTrue(job.cancelled.is_set());self.assertFalse(job.finished.is_set())
                voice._queue.put({'event':'done','job':job.id})
                self.assertTrue(job.finished.wait(1));self.assertTrue(job.input_closed)
        finally:voice.close()

    def test_first_sentence_is_scheduled_before_remaining_text_arrives(self):
        voice=FakeVoice()
        class Chat:
            metrics={}
            def reply(self,text,on_token,cancel):
                on_token('凡人倒是会喊累。')
                if not voice.first.wait(1): raise RuntimeError('First sentence was held until completion')
                on_token('去睡一觉。');return '凡人倒是会喊累。去睡一觉。'
            def cancel_transport(self): pass
            def close(self): pass
        pipeline=ReplyPipeline(Chat(),voice,lambda text:None,lambda text:None)
        try:
            turn=pipeline.submit('测试')
            self.assertTrue(turn.done.wait(2));self.assertIsNone(turn.error)
            self.assertEqual(voice.begins,1)
            self.assertEqual(voice.sent,['凡人倒是会喊累。','去睡一觉。'])
            self.assertTrue(turn.voice_job.input_closed)
        finally: pipeline.close()

    def test_one_voice_job_accepts_multiple_segments_without_cancelling(self):
        voice=LocalVoice(player=lambda *args:None)
        commands=[]
        try:
            with patch.object(voice,'prepare'),patch.object(voice,'_send',side_effect=lambda item:commands.append(item)):
                job=voice.begin();voice.append(job,'第一句。');voice.append(job,'第二句。');voice.end(job)
                self.assertFalse(job.cancelled.is_set())
                self.assertEqual([v['action'] for v in commands],['begin','append','append','finish'])
                voice._queue.put({'event':'done','job':job.id})
                self.assertTrue(job.finished.wait(2))
        finally:voice.close()

    def test_stop_is_responsive_while_chat_waits(self):
        entered=threading.Event(); release=threading.Event(); voice=FakeVoice()
        class Chat:
            metrics={}
            def reply(self,text,on_token,cancel):
                entered.set();release.wait(2)
                if cancel.is_set():raise ChatCancelled('stop')
                return '结果。'
            def cancel_transport(self): pass
            def close(self):pass
        pipeline=ReplyPipeline(Chat(),voice,lambda text:None,lambda text:None)
        try:
            turn=pipeline.submit('测试');self.assertTrue(entered.wait(1))
            pipeline.cancel();self.assertTrue(turn.cancel.is_set())
            release.set();self.assertTrue(turn.done.wait(2));self.assertIsNone(pipeline.last_reply)
        finally:release.set();pipeline.close()


if __name__=='__main__':unittest.main()
