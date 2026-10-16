"""One cancellable chat turn feeds complete clauses into one appendable voice job."""
import queue
import threading
import time
import uuid
from chat_client import ChatError, ChatCancelled


class SentenceBuffer:
    def __init__(self, emit, minimum=8, maximum=80):
        self.emit = emit
        self.minimum = minimum
        self.maximum = maximum
        self.buffer = ""

    def feed(self, token):
        self.buffer += token
        while self.buffer:
            boundary = None
            for index, char in enumerate(self.buffer, 1):
                strong = char in "。！？!?\n"
                weak = char in "，,；;：:"
                if strong and index >= 5 or weak and index >= self.minimum:
                    boundary = index
                    break
            if boundary is None and len(self.buffer) >= self.maximum:
                boundary = self.maximum
            if boundary is None:
                return
            text, self.buffer = self.buffer[:boundary], self.buffer[boundary:]
            if text.strip():
                self.emit(text.strip())

    def finish(self):
        if self.buffer.strip():
            self.emit(self.buffer.strip())
        self.buffer = ""


class Turn:
    def __init__(self, text):
        self.id = uuid.uuid4().hex
        self.text = text
        self.reply = ''
        self.terminal_notified = False
        self.created = time.perf_counter()
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.voice_job = None
        self.metrics = {}
        self.error = None


class ReplyPipeline:
    def __init__(self, chat, voice, output, status, voice_enabled=True, on_event=None, protect_text_replies=False):
        self.chat, self.voice = chat, voice
        self.output, self.status = output, status
        self.voice_enabled = voice_enabled
        self.on_event = on_event
        self.protect_text_replies = protect_text_replies
        self.last_reply = None
        self.last_turn = None
        self._current = None
        self._queue = queue.Queue(maxsize=1)
        self._lock = threading.RLock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit_opening(self, text, *, call_id=None):
        return self.submit('', opening=text, call_id=call_id)

    def submit(self, text, input_metrics=None, *, opening=None, call_id=None):
        self.cancel(reason='new_message')
        turn = Turn(text)
        turn.source = 'incoming' if opening is not None else 'voice' if input_metrics is not None else 'text'
        turn.opening = opening
        turn.call_id = call_id
        if input_metrics:
            turn.metrics["input"] = dict(input_metrics)
        with self._lock:
            if self._closed:
                raise RuntimeError("Reply pipeline is closed")
            turn.chat = self.chat.snapshot() if getattr(type(self.chat), 'snapshot', None) else self.chat
            turn.voice = self.voice.snapshot() if getattr(type(self.voice), 'snapshot', None) else self.voice
            turn.backend = getattr(turn.chat,'backend',None) or ('qwen' if getattr(turn.chat,'outputs_audio',False) is True else 'gpt')
            self._current = self.last_turn = turn
            self._event('submitted', turn, source=turn.source)
            self._queue.put_nowait(turn)
        self.status("正在回应…可输入 /stop 停止")
        return turn

    def _event(self, kind, turn, **extra):
        if self.on_event:
            try:
                self.on_event({'kind': kind, 'id': turn.id, 'backend': turn.backend,
                               'text': turn.text, 'reply': turn.reply, 'source':turn.source,
                               'call_id':turn.call_id, **extra})
            except Exception as exc:
                self.status('界面记录未更新（'+type(exc).__name__+'），当前对话继续。')

    def _terminal(self, turn):
        with self._lock:
            if not turn.terminal_notified:
                turn.terminal_notified = True
                kind = 'cancelled' if turn.cancel.is_set() else 'failed' if turn.error else 'completed'
                self._event(kind, turn, error=turn.error)

    @property
    def text_reply_active(self):
        with self._lock:
            return bool(self.protect_text_replies and self._current and
                        self._current.source=='text' and self.responding)

    @property
    def responding(self):
        with self._lock:
            turn = self._current
            return bool(turn and not turn.cancel.is_set() and
                        (not turn.done.is_set() or
                         turn.voice_job and not turn.voice_job.finished.is_set()))

    def cancel(self, reason='cancel'):
        with self._lock:
            if self._current and self.responding:
                self._current.metrics['stop_reason']=reason
                self._current.cancel.set()
                if self._current.done.is_set():
                    self._event('playback_stopped', self._current)
                else:
                    self._terminal(self._current)
            while True:
                try:
                    pending = self._queue.get_nowait()
                    if pending:
                        pending.cancel.set()
                        self._terminal(pending)
                        pending.done.set()
                except queue.Empty:
                    break
        self.chat.cancel_transport()
        self.voice.stop()

    def clear(self):
        self.cancel(reason='clear_history')
        self.chat.clear()
        self.last_reply = None

    def _run(self):
        while True:
            turn = self._queue.get()
            if turn is None:
                return
            if turn.cancel.is_set():
                with self._lock:
                    self._terminal(turn)
                    turn.done.set()
                continue
            started = time.perf_counter()
            chat, voice = turn.chat, turn.voice
            if turn.opening is not None:
                self._opening(turn)
                continue
            direct_audio = getattr(chat, 'outputs_audio', False) is True
            header_printed = False
            voice_failed = False
            def send_sentence(text):
                nonlocal voice_failed
                with self._lock:
                    if turn.cancel.is_set() or not self.voice_enabled or voice_failed:
                        return
                    turn.metrics.setdefault("first_sentence_seconds", time.perf_counter() - turn.created)
                    if direct_audio:
                        return
                    try:
                        if turn.voice_job is None:
                            turn.voice_job = voice.begin()
                        voice.append(turn.voice_job, text)
                    except Exception as exc:
                        voice_failed = True
                        self.status(f"语音未启动（{type(exc).__name__}），文字保留")
            sentences = SentenceBuffer(send_sentence)
            def token(text):
                nonlocal header_printed
                with self._lock:
                    if turn.cancel.is_set():
                        return
                    if not header_printed:
                        turn.metrics['first_text_seconds'] = time.perf_counter() - turn.created
                        self.output("\n若娜瓦: ")
                        header_printed = True
                    self.output(text)
                    turn.reply += text
                    self._event('delta', turn)
                    sentences.feed(text)
            try:
                # Start local model preparation while waiting for cloud text.
                # prepare() only schedules the worker; it never blocks on warmup.
                if not direct_audio and self.voice_enabled:
                    prepare = getattr(voice, 'prepare', None)
                    if callable(prepare):
                        try:
                            prepare()
                            turn.metrics['voice_prepare_requested_seconds'] = time.perf_counter() - turn.created
                        except Exception as exc:
                            voice_failed = True
                            self.status(f"语音未启动（{type(exc).__name__}），文字保留")
                if direct_audio:
                    with self._lock:
                        if turn.cancel.is_set():
                            raise ChatCancelled('本轮已停止')
                        audio_enabled = self.voice_enabled
                        if audio_enabled:
                            turn.voice_job = voice.begin()
                    def audio(pcm, rate):
                        if not turn.cancel.is_set() and turn.voice_job:
                            voice.append_pcm(turn.voice_job, pcm, rate)
                    reply = chat.reply(turn.text, on_token=token, cancel=turn.cancel,
                                       on_audio=audio, audio_enabled=audio_enabled)
                else:
                    reply = chat.reply(turn.text, on_token=token, cancel=turn.cancel)
                with self._lock:
                    if turn.cancel.is_set():
                        raise ChatCancelled("本轮已停止")
                    self.last_reply = reply
                    turn.reply = reply
                    sentences.finish()
                    self.output("\n")
            except ChatCancelled:
                pass
            except ChatError as exc:
                turn.error = str(exc)
                turn.metrics['stop_reason']='response_error'
                self.status(str(exc) + "；部分语音可能已播放，本轮未提交上下文")
                # Already played speech cannot be undone; stop unplayed partial output.
                with self._lock:
                    if not turn.cancel.is_set():
                        voice.stop()
            except Exception as exc:
                turn.error = type(exc).__name__
                turn.metrics['stop_reason']='response_error'
                self.status(f"本轮失败（{type(exc).__name__}），可重试")
                with self._lock:
                    if not turn.cancel.is_set():
                        voice.stop()
            finally:
                if turn.voice_job and not turn.voice_job.input_closed:
                    try:
                        voice.end(turn.voice_job)
                    except (OSError, ValueError):
                        pass
                turn.metrics["chat"] = dict(chat.metrics)
                turn.metrics['backend'] = turn.backend
                turn.metrics["chat_queue_seconds"] = started - turn.created
                turn.metrics["text_total_seconds"] = time.perf_counter() - turn.created
                turn.metrics["cancelled"] = turn.cancel.is_set()
                with self._lock:
                    self._terminal(turn)
                    turn.done.set()

    def _opening(self, turn):
        chat,voice = turn.chat,turn.voice
        try:
            with chat._lock: revision = chat._revision
            with self._lock:
                if turn.cancel.is_set(): raise ChatCancelled('来电已结束')
                turn.reply = turn.opening
                self._event('delta',turn)
                turn.voice_job = voice.speak(turn.opening)
            if turn.voice_job is None: raise ChatError('来电语音未启动')
            while not turn.voice_job.finished.wait(.1):
                if turn.cancel.is_set(): raise ChatCancelled('来电已结束')
            if turn.voice_job.error: raise ChatError(turn.voice_job.error)
            if turn.cancel.is_set() or turn.voice_job.cancelled.is_set(): raise ChatCancelled('来电已结束')
            with chat._lock:
                if revision!=chat._revision or turn.cancel.is_set(): raise ChatCancelled('上下文已更新')
                chat.history.append({'role':'assistant','content':turn.reply})
            self.last_reply = turn.reply
        except ChatCancelled:
            turn.cancel.set()
        except Exception as exc:
            turn.error = str(exc) if isinstance(exc,ChatError) else '来电开场未完成（'+type(exc).__name__+'）'
            self.status(turn.error)
        finally:
            with self._lock:
                self._terminal(turn); turn.done.set()

    def timings(self):
        with self._lock:
            turn = self.last_turn
        result = {"voice_prepare": dict(self.voice.prepare_metrics)}
        if turn:
            result.update(turn.metrics)
            if turn.voice_job:
                result["voice"] = dict(turn.voice_job.metrics)
                first = turn.voice_job.metrics.get("first_playback_seconds")
                if first is not None:
                    result["request_to_first_playback_seconds"] = turn.voice_job.created - turn.created + first
                    if "input" in turn.metrics:
                        result["vad_speech_end_to_first_playback_seconds"] = (
                            turn.metrics["input"]["speech_end_to_submit_seconds"]
                            + result["request_to_first_playback_seconds"])
        return result

    def wait_finished(self, timeout=None):
        """Wait for text, the final flush and actual playback without cancelling."""
        with self._lock:
            turn = self.last_turn
        if turn is None:
            return True
        deadline = None if timeout is None else time.perf_counter() + timeout
        if not turn.done.wait(timeout):
            return False
        if turn.voice_job:
            remaining = None if deadline is None else max(0.0, deadline - time.perf_counter())
            return turn.voice_job.finished.wait(remaining)
        return True

    def close(self):
        self._closed = True
        self.cancel(reason='client_close')
        self._queue.put(None)
        self._thread.join(timeout=2)
        self.chat.close()
