"""Explicit local-only Index decode/diffusion comparison; no playback or cloud."""
import argparse
import contextlib
import json
from pathlib import Path
import sys
import time
import wave
import os
import socket

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
TEXT='凡人，先把眼前的事做好。其他的，稍后再说。'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--auditions',type=Path,required=True)
    parser.add_argument('--runs',type=int,default=2)
    args=parser.parse_args()
    for name in list(os.environ):
        if 'api_key' in name.casefold() or 'api_token' in name.casefold(): os.environ.pop(name)
    def offline(*args,**kwargs): raise RuntimeError('Network forbidden during Index comparison')
    socket.create_connection=offline; socket.socket.connect=offline
    args.output=args.output.resolve();args.auditions=args.auditions.resolve()
    args.output.parent.mkdir(parents=True,exist_ok=True);args.auditions.mkdir(parents=True,exist_ok=True)
    from index_voice_backend import initialize
    def parameters(reference,text,steps,graph):
        return {'text':text,'ref_audio_path':str(ROOT/reference['path']),
                'index_diffusion_steps':steps,'index_cuda_graph':graph}
    with args.output.with_suffix('.log').open('w',encoding='utf-8') as log, \
            contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        started=time.perf_counter();model,reference,init,acceleration=initialize()
        if acceleration['device']!='cuda': raise RuntimeError('CUDA comparison requires a supported GPU')
        # Warm both paths so capture and reference work do not skew steady state.
        for graph in (False,True):
            for _ in model.run(parameters(reference,'凡人，坐下。',25,graph)): pass
        prepared=time.perf_counter()-started
        print('PREPARED',json.dumps(acceleration),flush=True)
        from reply_pipeline import SentenceBuffer
        clauses=[];buffer=SentenceBuffer(clauses.append);buffer.feed(TEXT);buffer.finish()
        results=[]
        for graph,steps,label in ((False,25,'original-25'),(True,25,'graph-25'),
                                  (True,20,'graph-20'),(True,15,'graph-15')):
            for run in range(args.runs):
                started=time.perf_counter();parts=[];arrivals=[];playhead=None;gaps=[]
                for clause in clauses:
                    for rate,data in model.run(parameters(reference,clause,steps,graph)):
                        arrival=time.perf_counter()-started;duration=len(data)/rate
                        if playhead is not None:gaps.append(max(0,arrival-playhead))
                        playhead=max(playhead or 0,arrival)+duration
                        parts.append(data);arrivals.append({'arrival_seconds':arrival,'audio_seconds':duration})
                if not parts:raise RuntimeError('No speech produced')
                audio=args.auditions/(label+'.wav')
                if run==0:
                    with wave.open(str(audio),'wb') as wav:
                        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate)
                        wav.writeframes(b''.join(p.tobytes() for p in parts))
                result={'variant':label,'steps':steps,'cuda_graph':graph,'run':run+1,
                    'first_audio_seconds':arrivals[0]['arrival_seconds'],
                    'total_seconds':time.perf_counter()-started,
                    'audio_seconds':sum(p['audio_seconds'] for p in arrivals),
                    'estimated_inter_segment_wait_seconds':gaps,'fragments':arrivals,
                    'audio':str(audio),'acceleration':dict(acceleration)}
                if graph and not acceleration.get('cuda_graph_enabled'):
                    raise RuntimeError('Graph comparison unexpectedly fell back: '+str(acceleration))
                results.append(result)
                print('RESULT',json.dumps(result),flush=True)
                # Write each completed case for progress and interrupted diagnostics.
                args.output.write_text(json.dumps({'passed':False,'partial':True,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
        report={'passed':True,'test_text':TEXT,'reference':reference['path'],'initialize_seconds':init,
            'prepare_both_paths_seconds':prepared,'results':results,'cloud_requests':0,
            'microphone_started':False,'physical_playback':False}
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'results':[{k:r[k] for k in ('variant','run','first_audio_seconds',
        'total_seconds','audio_seconds','estimated_inter_segment_wait_seconds')} for r in results]}))

if __name__=='__main__':main()
