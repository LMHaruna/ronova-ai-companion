"""One bounded background queue analyzes completed text, never plays audio."""
import json
import queue
import threading
from chat_client import ChatClient, ChatCancelled
from qwen_realtime import QwenRealtime
from client_relationship import ANALYSIS_PROMPT, parse_analysis


class SemanticAnalyzer:
    def __init__(self, backend, config, client=None, lore=None):
        self.backend=backend
        self.lore=lore
        self.client=client or (QwenRealtime(config=config,instructions=ANALYSIS_PROMPT) if backend=='qwen' else ChatClient(config=config))
        self.client.history=[{'role':'system','content':ANALYSIS_PROMPT}]
        self.client.context_provider=None

    def analyze(self, text, reply, memories, cancel):
        data={'当前用户文字':text,'当前角色回复':reply[:2000],
            '已保存的相关信息':[{key:row[key] for key in ('id','content','category','slot','updated')} for row in memories]}
        if self.lore:
            hints=self.lore.joke_hint(text)
            if hints: data['玩家调侃背景']=hints
        payload=json.dumps(data,ensure_ascii=False)
        if self.backend=='qwen':
            raw=self.client.reply(payload,cancel=cancel,audio_enabled=False)
        else: raw=self.client.reply(payload,cancel=cancel,max_tokens=1800)
        if cancel.is_set(): raise ChatCancelled('后台分析已取消')
        return parse_analysis(raw,text)

    def cancel(self): self.client.cancel_transport()
    def close(self): self.client.close()


class CompanionWorker:
    def __init__(self, store, factory=SemanticAnalyzer, changed=lambda result:None):
        self.store=store; self.factory=factory; self.changed=changed
        self.queue=queue.Queue(maxsize=8); self.lock=threading.RLock()
        self.active=None; self.closed=False; self.pending=0
        self.message=''; self.completed=0; self.failed=0; self.dropped=0
        self.thread=threading.Thread(target=self._run,name='Ronova-companion-analysis',daemon=True)
        self.thread.start()

    def enqueue(self, event, config, ticket):
        with self.lock:
            if self.closed: return
            item={'event':dict(event),'config':config,'ticket':ticket,'cancel':threading.Event()}
            if self.queue.full():
                old=self.queue.get_nowait(); old['cancel'].set()
                self.pending-=1; self.dropped+=1
            self.queue.put_nowait(item); self.pending+=1
            self.message='正在整理记忆与关系…'

    def snapshot(self):
        with self.lock: return {'pending':self.pending,'message':self.message,
            'completed':self.completed,'failed':self.failed,'dropped':self.dropped}

    def cancel_pending(self):
        with self.lock:
            if self.active:
                self.active['cancel'].set()
                client=self.active.get('client')
                if client: client.cancel()
            while True:
                try:
                    item=self.queue.get_nowait()
                    if item is None: self.queue.put_nowait(None); break
                    item['cancel'].set(); self.pending-=1
                except queue.Empty: break
            self.message=''

    def _run(self):
        while True:
            item=self.queue.get()
            if item is None: return
            client=None; result=None
            try:
                with self.lock:
                    if self.closed or item['cancel'].is_set(): continue
                    self.active=item
                event=item['event']; text=event['text']
                snapshot=self.store.snapshot()
                # Recent facts plus identity/boundaries give corrections a
                # stable key without transmitting the full chat archive.
                memories=sorted(snapshot['memories'],key=lambda row:(row['category'] in ('fact','boundary'),row['updated']),reverse=True)[:12]
                if not snapshot['enabled'] or not snapshot['auto_memory']: memories=[]
                client=self.factory(event['backend'],item['config'])
                with self.lock:
                    item['client']=client
                    if self.closed or item['cancel'].is_set(): continue
                data=client.analyze(text,event['reply'],memories,item['cancel'])
                with self.lock:
                    if self.closed or item['cancel'].is_set(): continue
                    result=self.store.apply_analysis(event['id'],text,data,**item['ticket'])
                    self.completed+=1
                    self.message=('记忆已满，请整理已有内容' if result and result['capacity'] else
                        '已自动整理记忆与关系')
            except Exception:
                with self.lock:
                    if not item['cancel'].is_set() and not self.closed:
                        self.failed+=1; self.message='本轮自动整理未完成，后续对话继续尝试'
            finally:
                if client:
                    try: client.close()
                    except Exception: pass
                with self.lock:
                    self.pending=max(0,self.pending-1)
                    if self.active is item: self.active=None
                # Never acquire the controller's observer lock while holding
                # the worker lock: completed turns enqueue in the other order.
                if result:
                    try: self.changed(result)
                    except Exception: pass

    def wait_idle(self, timeout=5):
        import time
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            with self.lock:
                if self.pending==0: return True
            time.sleep(.005)
        return False

    def close(self):
        with self.lock: self.closed=True
        self.cancel_pending(); self.queue.put_nowait(None)
        self.thread.join(timeout=2)
