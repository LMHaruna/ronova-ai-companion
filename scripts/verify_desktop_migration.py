"""Check the relocated package offline; optional local synthesis never plays sound."""
import argparse
import ctypes
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]


def forbid(*args,**kwargs):
    raise RuntimeError('Network access is forbidden during this offline verification.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick',action='store_true')
    parser.add_argument('--voice',action='store_true',help='Warm up GPT-SoVITS locally; no playback or microphone.')
    parser.add_argument('--tts-engine',choices=('gpt-sovitsv2proplus','indextts-2.5'),default='gpt-sovitsv2proplus')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    os.environ.update(QT_QPA_PLATFORM='offscreen',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                      NLTK_DATA=str(ROOT/'runtime/gpt-nltk-data'),PYTHONDONTWRITEBYTECODE='1')
    socket.create_connection=forbid
    socket.socket.connect=forbid
    modules=('PySide6.QtWidgets','numpy','scipy','torch','torchaudio','onnxruntime','sherpa_onnx',
             'webrtcvad','websocket','soundfile','av','ctranslate2','faster_whisper')
    versions={}
    for name in modules:
        module=importlib.import_module(name)
        versions[name]=getattr(module,'__version__','imported')
    from local_voice import validate_resources
    from client_lore import LoreStore
    from client_scene import SceneAssets
    from client_store import Preferences
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    validate_resources(args.tts_engine)
    lore=LoreStore(ROOT); assets=SceneAssets(ROOT)
    if not lore.enabled or len(lore.chunks)!=57: raise RuntimeError('Knowledge index is unavailable or incomplete.')
    prefs=Preferences.load(ROOT/'config/client-preferences.json').validate()
    ctypes.WinDLL(str(ROOT/'runtime/echo-tools/libspeexdsp-1.dll'))
    import nltk
    for name in ('corpora/cmudict.zip','taggers/averaged_perceptron_tagger.zip','taggers/averaged_perceptron_tagger_eng'):
        nltk.data.find(name)
    report={'passed':True,'python':sys.version.split()[0],'versions':versions,'knowledge_chunks':len(lore.chunks),
            'knowledge_stage':lore.stage,'portrait_states':list(assets.portraits),'assets':len(assets.manifest['assets']),
            'cloud_requests':0,'microphone_started':False,'physical_playback':False,'voice_warmup':None}
    if not args.quick:
        result=subprocess.run([sys.executable,str(ROOT/'client_app.py'),'--check'],cwd=ROOT,
                              check=True,capture_output=True,text=True,encoding='utf-8')
        report['client_check']=json.loads(result.stdout.strip().splitlines()[-1])
        from voice_input import ChineseRecognizer
        recognizer=ChineseRecognizer()
        report['streaming_asr']='initialized'
        if args.voice:
            from local_voice import LocalVoice
            voice=LocalVoice(tts_engine=args.tts_engine)
            try:
                ready=voice.prepare(); deadline=time.monotonic()+300
                while not ready.wait(.1):
                    if voice.prepare_error: raise RuntimeError(voice.prepare_error+'; '+' '.join(voice._stderr))
                    if time.monotonic()>deadline: raise TimeoutError('Local voice warmup timed out.')
                report['voice_warmup']=voice.prepare_metrics
            finally: voice.close()
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0


if __name__=='__main__': raise SystemExit(main())
