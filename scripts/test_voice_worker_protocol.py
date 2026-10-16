"""Real worker transport and shutdown with a CPU fixture, no model/device/cloud."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
SOURCE=r'''
import sys,time,threading
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import voice_worker as worker
worker.ROOT=Path(sys.argv[2])
worker.SYNTHESIZE_ON_MAIN_THREAD=sys.argv[1]=='1'
class Model:
    def __init__(self):
        self.owner=threading.get_ident();self.warm=True
        self.reference_speaker_cache=SimpleNamespace(hits=0,misses=0)
        self.configs=SimpleNamespace(version='fixture',vits_weights_path='fixture')
        self.stopped=threading.Event();self.cancel_check=lambda:False
    def stop(self):self.stopped.set()
    def set_cancel_check(self,check):self.cancel_check=check
    def run(self,parameters):
        if not self.warm:
            assert (threading.get_ident()==self.owner)==worker.SYNTHESIZE_ON_MAIN_THREAD
        self.warm=False;self.stopped.clear()
        for i in range(2):
            for _ in range(5):
                if self.stopped.is_set() or self.cancel_check():return
                time.sleep(.002)
            yield 22050,np.array([100,-100,50],dtype=np.int16)
def initialize(force_cpu=False):
    return Model(),{'id':'C','path':'C.wav','text':'fixture'},0.0,{'device':'cpu','is_half':False}
worker.initialize=initialize
worker.main()
'''


class ProtocolTests(unittest.TestCase):
    def run_worker(self,main_thread,exit_by_eof=False):
        with tempfile.TemporaryDirectory() as folder:
            env=dict(os.environ,PYTHONUTF8='1',PYTHONNOUSERSITE='1')
            for key in list(env):
                if 'api_key' in key.casefold() or 'api_token' in key.casefold():env.pop(key)
            process=subprocess.Popen([sys.executable,'-s','-u','-c',SOURCE,'1' if main_thread else '0',folder],
                cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            events=queue.Queue()
            def reader():
                for line in process.stdout:
                    try:events.put(json.loads(line))
                    except ValueError:pass
                events.put({'event':'exit'})
            thread=threading.Thread(target=reader,daemon=True);thread.start()
            def send(action,**values):
                process.stdin.write(json.dumps(dict(action=action,**values))+'\n');process.stdin.flush()
            def receive():
                event=events.get(timeout=5)
                self.assertNotIn(event['event'],('error','boot_error','exit'),event)
                return event
            try:
                send('prepare');self.assertEqual(receive()['event'],'ready')
                job=uuid.uuid4().hex
                send('begin',job=job);send('append',job=job,text='first');send('finish',job=job)
                fragments=0
                while True:
                    event=receive()
                    if event['event']=='fragment':fragments+=1
                    if event['event']=='done':break
                self.assertEqual(fragments,2);self.assertFalse(event['cancelled'])
                job=uuid.uuid4().hex
                send('begin',job=job);send('append',job=job,text='cancel')
                send('cancel',job=job);send('finish',job=job)
                while True:
                    event=receive()
                    if event['event']=='done':break
                self.assertTrue(event['cancelled'])
                if exit_by_eof:process.stdin.close()
                else:send('close')
                self.assertEqual(process.wait(timeout=5),0)
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=3)
                thread.join(timeout=1)
                for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def test_index_main_thread_jobs_cancel_and_close(self):self.run_worker(True)
    def test_index_main_thread_eof_exits(self):self.run_worker(True,True)
    def test_gpt_background_protocol_is_preserved(self):self.run_worker(False)

if __name__=='__main__':unittest.main()
