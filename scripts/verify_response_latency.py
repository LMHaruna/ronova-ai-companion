"""Two bounded text-only timing probes; never send customer chats or memories."""
import json
from pathlib import Path
import sys
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from chat_client import ChatClient
from client_models import ClientModels
from client_store import Preferences

def main():
    output=ROOT/'runtime/verification-records/response-latency-20261014/live-text.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    engine=Preferences.load(ROOT/'config/client-preferences.json').engine
    models=ClientModels(ROOT)
    config=models.config(engine)
    if engine=='qwen' or config.provider!='custom' or config.model!='qwen3.8-flash':
        raise RuntimeError('The selected model is not the Qwen Flash route being optimized.')
    config.validate()
    mode=['original_default']
    session=requests.Session()
    def post(url,**kwargs):
        if mode[0]=='original_default': kwargs['json'].pop('enable_thinking',None)
        else: kwargs['json']['enable_thinking']=False
        return session.post(url,**kwargs)
    chat=ChatClient(post=post,config=config)
    rows=[]
    try:
        for selected in ('original_default','thinking_disabled'):
            mode[0]=selected; chat.clear()
            print('Text timing probe: '+selected,flush=True)
            reply=chat.reply('你好。我今天有一点累，陪我说一句话，不超过20个汉字。',max_tokens=512)
            metrics={name:chat.metrics[name] for name in (
                'transport_wait_seconds','headers_seconds','first_sse_seconds','first_reasoning_seconds',
                'last_reasoning_seconds','first_content_seconds','total_seconds','reasoning_characters',
                'request_characters','content_events') if name in chat.metrics}
            rows.append({'mode':selected,'enable_thinking_requested':False if selected=='thinking_disabled' else None,
                         'reply_characters':len(reply),**metrics})
            print(json.dumps(rows[-1],ensure_ascii=False),flush=True)
    finally:
        chat.close(); session.close()
    report={'passed':len(rows)==2,'model':'qwen3.8-flash','cloud_requests':len(rows),'runs':rows,
            'physical_playback':False,'microphone_started':False,'customer_history_used':False,
            'customer_memories_used':False,'limits':'Short synthetic text, not the user full conversation or end-to-end physical call.'}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0

if __name__=='__main__': raise SystemExit(main())
