"""Generate public short phrases for Index boundary diagnosis, without playback."""
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
OUTPUT=ROOT/'runtime/verification-records/index-edges-20261015'
AUDITIONS=ROOT/'voice-audition/index-edges-20261015'
TEXTS=[
    '你先忙，等事情处理完再来找我。',
    '不必急着解释，我听着。',
    '今天就到这里。早点休息，明天再说。',
    '嗯，知道了。你先把手边的事做好。',
    '我想与你说说话。',
    '别逞强，累了就休息一会儿。',
]

def stats(data,rate):
    import numpy as np
    x=data.astype('float64')/32768
    def rms_db(a):return float(20*np.log10(max(float(np.sqrt(np.mean(a*a))),1e-10)))
    window=max(1,round(rate*.02))
    return {'frames':len(data),'audio_seconds':len(data)/rate,'first_sample':int(data[0]),
            'last_sample':int(data[-1]),'peak':int(np.abs(data.astype('int32')).max()),
            'head_20ms_rms_dbfs':rms_db(x[:window]),'tail_20ms_rms_dbfs':rms_db(x[-window:]),
            'tail_20_60ms_rms_dbfs':rms_db(x[-3*window:-window]),
            'max_last_100ms_step':float(np.max(np.abs(np.diff(x[-5*window:]))))}

def main():
    OUTPUT.mkdir(parents=True,exist_ok=True);AUDITIONS.mkdir(parents=True,exist_ok=True)
    from index_voice_backend import initialize
    from reply_pipeline import SentenceBuffer
    results=[]
    with (OUTPUT/'synthesis.log').open('w',encoding='utf-8') as log, \
            contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        model,reference,initialization,acceleration=initialize()
        if acceleration['device']!='cuda':raise RuntimeError('Expected local CUDA')
        for _ in model.run({'text':'凡人，坐下。','ref_audio_path':str(ROOT/reference['path'])}):pass
        for case,text in enumerate(TEXTS,1):
            clauses=[];buffer=SentenceBuffer(clauses.append);buffer.feed(text);buffer.finish()
            chunks=[];fragments=[];started=time.perf_counter()
            for clause in clauses:
                for rate,data in model.run({'text':clause,'ref_audio_path':str(ROOT/reference['path'])}):
                    part=AUDITIONS/f'case-{case}-part-{len(chunks)+1}-raw.wav'
                    with wave.open(str(part),'wb') as wav:
                        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(data.tobytes())
                    fragments.append(dict(path=str(part.relative_to(ROOT)),**stats(data,rate)))
                    chunks.append(data)
            if not chunks:raise RuntimeError('No generated audio')
            full=AUDITIONS/f'case-{case}-raw.wav'
            with wave.open(str(full),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate)
                wav.writeframes(b''.join(p.tobytes() for p in chunks))
            results.append({'text':text,'path':str(full.relative_to(ROOT)),
                            'generation_seconds':time.perf_counter()-started,'fragments':fragments})
            (OUTPUT/'probe.json').write_text(json.dumps({'passed':False,'partial':True,'results':results},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print('CASE',case,json.dumps(fragments),flush=True)
        report={'passed':True,'reference':reference['path'],
                'reference_sha256':hashlib.sha256((ROOT/reference['path']).read_bytes()).hexdigest(),
                'initialize_seconds':initialization,'acceleration':acceleration,'results':results,
                'cloud_requests':0,'microphone_started':False,'physical_playback':False}
        (OUTPUT/'probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':True,'cases':len(results),'fragments':sum(len(r['fragments']) for r in results)}),flush=True)

if __name__=='__main__':main()
