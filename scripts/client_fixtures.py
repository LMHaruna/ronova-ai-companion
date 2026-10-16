"""Explicitly offline service fixtures for desktop verification."""
import threading
from chat_client import ChatClient, ChatError, ChatCancelled
from local_voice import SpeechJob
from voice_backends import BackendRouter


class FakeChat(ChatClient):
    outputs_audio = True
    def __init__(self):
        super().__init__(post=lambda *args,**kwargs:None)
        self.fail = False
        self.entered = threading.Event()
        self.release = None
        self.calls = 0
    def configured(self): pass
    def reply(self,text,on_token=None,cancel=None,on_audio=None,audio_enabled=True):
        self.calls += 1
        reply='凡人，先坐下休息。'
        on_token(reply[:3]); self.entered.set()
        if self.release: self.release.wait(2)
        if cancel.is_set(): raise ChatCancelled('已停止')
        if self.fail: raise ChatError('离线模拟连接失败')
        if audio_enabled and on_audio: on_audio(b'\x10\x00'*2400,24000)
        on_token(reply[3:])
        with self._lock:
            self.history.extend([{'role':'user','content':text},{'role':'assistant','content':reply}])
        return reply
    def cancel_transport(self):
        if self.release and self.entered.is_set(): self.release.set()


class FakeVoice:
    prepare_metrics={}
    playback_active=False
    playback_level=0.0
    def __init__(self): self.jobs=[]; self.stopped=0; self.replayed=[]; self.closed=False
    def prepare(self): return threading.Event()
    def begin(self):
        job=SpeechJob(); self.jobs.append(job); return job
    def append_pcm(self,job,pcm,rate): pass
    def append(self,job,text): pass
    def end(self,job): job.input_closed=True; job.finished.set()
    def stop(self):
        self.stopped+=1
        for job in self.jobs:
            job.cancelled.set(); job.finished.set()
    def speak(self,text):
        self.replayed.append(text)
        job=self.begin(); self.end(job); return job
    def wait_idle(self,timeout=None): return True
    def close(self): self.closed=True


class FakeCall:
    def __init__(self,pipeline,voice,output,status,on_transcript=None):
        self.pipeline=pipeline; self.on_transcript=on_transcript
        self.active=False; self.mode='speakers'; self.starts=[]; self.stops=[]
    def start(self,mode,device=None,cancel_reply=True):
        self.active=True; self.mode=mode; self.starts.append(cancel_reply)
        if cancel_reply: self.pipeline.cancel()
    def stop(self,cancel_reply=True):
        self.active=False; self.stops.append(cancel_reply)
        if cancel_reply: self.pipeline.cancel()
    def reset_input(self): pass
    def timings(self): return {'state':'listening' if self.active else 'off','error':None,'mode':self.mode}


def fake_router():
    chat,voice=FakeChat(),FakeVoice()
    gpt,gpt_voice=FakeChat(),FakeVoice()
    gpt.outputs_audio=False
    router=BackendRouter(gpt,gpt_voice,qwen_factory=lambda:(chat,voice))
    router.analysis_factory=lambda backend,config:FakeAnalyzer()
    return router,chat,voice


class FakeAnalyzer:
    """Neutral structured result; feature tests inject richer explicit fixtures."""
    def analyze(self,text,reply,memories,cancel):
        return {'memories':[],'interaction':{'kind':'neutral','evidence':'','confidence':1}}
    def cancel(self): pass
    def close(self): pass
