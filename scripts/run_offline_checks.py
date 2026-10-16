"""Run existing offline tests against the packaged project, isolated from customer state."""
import argparse,json,os,socket,sys,tempfile,unittest
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('root',type=Path)
parser.add_argument('output',type=Path)
parser.add_argument('--tests',nargs='+',metavar='TEST',help='Optional unittest module, class or method names; otherwise discover all tests.')
args=parser.parse_args()
root=args.root.resolve()
output=args.output
trace_enabled=bool(os.environ.get('RONOVA_TEST_TRACE'))
if trace_enabled:
 import faulthandler
 output.parent.mkdir(parents=True,exist_ok=True)
 trace_file=output.with_suffix('.trace.log').open('w',encoding='utf-8')
 faulthandler.dump_traceback_later(60,repeat=True,file=trace_file)
sys.path[:0]=[str(root),str(root/'scripts')]
os.environ.update(QT_QPA_PLATFORM='offscreen',RONOVA_ASSET_ROOT=str(root),RONOVA_CODE_ROOT=str(root),
 PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',LOCALAPPDATA=str(output.parent/'isolated-user'),RONOVA_PREVIEW_DIR=str(output.parent/'previews'))
for name in list(os.environ):
 if 'api_key' in name.casefold() or 'api_token' in name.casefold(): os.environ.pop(name)
(output.parent/'previews').mkdir(parents=True,exist_ok=True)
network_attempts=[]; loopback_connections=[]
original_connect=socket.socket.connect; original_connection=socket.create_connection
def forbid(*args,**kwargs):
 network_attempts.append(True)
 raise RuntimeError('Forbidden: external network during offline tests.')
def loopback(address):
 return isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost')
def safe_connect(self,address):
 if not loopback(address): return forbid()
 loopback_connections.append(True); return original_connect(self,address)
def safe_connection(address,*args,**kwargs):
 if not loopback(address): return forbid()
 return original_connection(address,*args,**kwargs)
socket.socket.connect=safe_connect; socket.create_connection=safe_connection
import voice_input,pcm_player,ctypes,sounddevice
original_dll=ctypes.WinDLL
class GuardedAudioDll:
 def __init__(self,api):
  self.api=api
  def forbidden_audio(*args): raise RuntimeError('physical audio device forbidden')
  self.waveOutOpen=forbidden_audio; self.waveInOpen=forbidden_audio
 def __getattr__(self,name): return getattr(self.api,name)
def safe_dll(name,*args,**kwargs):
 api=original_dll(name,*args,**kwargs)
 return GuardedAudioDll(api) if str(name).casefold() in ('winmm','winmm.dll') else api
from unittest.mock import patch
suite=(unittest.defaultTestLoader.loadTestsFromNames(args.tests) if args.tests else
       unittest.defaultTestLoader.discover(str(root/'scripts'),pattern='test_*.py'))
with patch.object(sounddevice,'RawInputStream',side_effect=RuntimeError('physical microphone forbidden')), \
     patch.object(ctypes,'WinDLL',side_effect=safe_dll):
 result=unittest.TextTestRunner(verbosity=2 if trace_enabled else 1).run(suite)
report={'passed':result.wasSuccessful(),'python':sys.version.split()[0],'tests':result.testsRun,
 'failures':[(str(t),message) for t,message in result.failures],'errors':[(str(t),message) for t,message in result.errors],
 'network_attempts':len(network_attempts),'local_loopback_connections':len(loopback_connections),'cloud_requests':0,'microphone_started':False,'physical_playback':False}
output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k not in ('errors','failures')},ensure_ascii=False),flush=True)
raise SystemExit(not result.wasSuccessful())
