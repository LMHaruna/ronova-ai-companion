"""Local text history and non-secret desktop preferences."""
from pathlib import Path
from dataclasses import asdict, dataclass, fields
import json
import math
import sqlite3
import threading
import time


@dataclass
class Preferences:
    engine: str = 'qwen'
    gpt_tts_engine: str = 'gpt-sovitsv2proplus'
    deepseek_tts_engine: str = 'gpt-sovitsv2proplus'
    call_mode: str = 'speakers-aec'
    microphone: int | None = None
    speaker: int | None = None
    volume: float = .8
    autoplay: bool = True
    proactive_calls: bool = True
    call_start_hour: int = 9
    call_end_hour: int = 23
    call_daily_limit: int = 2
    call_cooldown_hours: int = 3
    call_idle_minutes: int = 15

    def validate(self):
        if self.engine not in ('qwen', 'gpt', 'openai', 'deepseek'): raise ValueError('对话模型选项无效')
        for field in ('gpt_tts_engine','deepseek_tts_engine'):
            if getattr(self,field) not in ('gpt-sovitsv2proplus','indextts-2.5'):
                raise ValueError('语音合成引擎无效')
        # Migrate the retired desktop choice; CLI voice modes remain independent.
        if self.call_mode == 'speakers': self.call_mode = 'speakers-aec'
        if self.call_mode not in ('speakers-aec', 'headphones'): raise ValueError('通话模式无效')
        for value in (self.microphone, self.speaker):
            if value is not None and (type(value) is not int or value < 0): raise ValueError('设备编号无效')
        if type(self.volume) not in (int,float) or not math.isfinite(self.volume) or not 0 <= self.volume <= 1:
            raise ValueError('音量无效')
        if type(self.autoplay) is not bool: raise ValueError('自动播放配置无效')
        if type(self.proactive_calls) is not bool: raise ValueError('主动来电开关无效')
        for name,low,high in (('call_start_hour',0,23),('call_end_hour',0,23),
                             ('call_daily_limit',1,3),('call_cooldown_hours',1,12),('call_idle_minutes',5,120)):
            value = getattr(self,name)
            if type(value) is not int or not low<=value<=high: raise ValueError('主动来电设置无效')
        if self.call_start_hour==self.call_end_hour: raise ValueError('来电开始和结束时间不能相同')
        return self

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists(): return cls()
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        allowed = {field.name for field in fields(cls)}
        return cls(**{name: value[name] for name in allowed if name in value}).validate()

    def save(self, path):
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name+'.tmp')
        temporary.write_text(json.dumps(asdict(self),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(path)


class HistoryStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA secure_delete=ON')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, generation TEXT NOT NULL, backend TEXT NOT NULL,
            source TEXT NOT NULL, user TEXT NOT NULL, assistant TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, updated REAL NOT NULL)''')
        # A prior process cannot leave a record pretending to be in flight.
        self.connection.execute("UPDATE turns SET state='interrupted' WHERE state='pending'")
        self.connection.commit()

    def begin(self, generation, backend, text, source='text', retry_id=None):
        if backend not in ('gpt','qwen','openai','deepseek') or (not text.strip() and source!='incoming'): raise ValueError('历史话轮无效')
        with self.lock, self.connection:
            if retry_id:
                existing = self.connection.execute('SELECT state FROM turns WHERE id=?',(retry_id,)).fetchone()
                if not existing or existing['state'] not in ('failed','interrupted','cancelled'):
                    raise ValueError('该话轮无需重试')
                self.connection.execute('''UPDATE turns SET generation=?, assistant='', state='pending',
                    error=NULL, attempts=attempts+1, updated=? WHERE id=?''',(generation,time.time(),retry_id))
                return retry_id
            self.connection.execute('''INSERT INTO turns(id,generation,backend,source,user,state,created,updated)
                VALUES(?,?,?,?,?,'pending',?,?)''',(generation,generation,backend,source,text,time.time(),time.time()))
            return generation

    def update(self, generation, reply, state='pending', error=None):
        if state not in ('pending','completed','failed','cancelled','interrupted'): raise ValueError('历史状态无效')
        with self.lock, self.connection:
            self.connection.execute('''UPDATE turns SET assistant=?, state=?, error=?, updated=?
                WHERE generation=? AND state='pending' ''',(reply,state,error,time.time(),generation))

    def recent(self, limit=100):
        with self.lock:
            rows = self.connection.execute('SELECT * FROM turns ORDER BY created DESC, rowid DESC LIMIT ?',
                                          (max(1,min(limit,500)),)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def context(self, backend, limit=20, since=0):
        with self.lock:
            rows = self.connection.execute("SELECT user,assistant FROM turns WHERE backend=? AND state='completed' AND created>? ORDER BY created DESC, rowid DESC LIMIT ?",
                                           (backend,since,max(1,min(limit,20)))).fetchall()
            return [message for row in reversed(rows) for message in
                    ([{'role':'user','content':row['user']}] if row['user'] else [])+
                    [{'role':'assistant','content':row['assistant']}]]

    def clear(self):
        with self.lock:
            with self.connection: self.connection.execute('DELETE FROM turns')
            self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.connection.execute('VACUUM')

    def close(self):
        with self.lock: self.connection.close()
