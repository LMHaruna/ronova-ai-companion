"""One synthetic text reply through the configured model; no user history or audio."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
CODE=Path(os.environ.get('RONOVA_CODE_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(CODE),str(CODE/'scripts'),str(ROOT),str(ROOT/'scripts')]
from client_models import ClientModels
from client_store import Preferences
from client_controller import DesktopController
from client_fixtures import FakeCall,FakeVoice,FakeAnalyzer
from voice_backends import BackendRouter
from qwen_realtime import QwenRealtime
from chat_client import ChatClient

engine=Preferences.load(ROOT/'config/client-preferences.json').engine
config=ClientModels(ROOT).config(engine)
chat=QwenRealtime(config=config) if engine=='qwen' else ChatClient(config=config)
voice=FakeVoice()
router=BackendRouter(chat,voice,qwen_factory=lambda:(chat,voice),gpt_factory=lambda:(chat,voice))
router.analysis_factory=lambda *args:FakeAnalyzer()
question='刚才是你主动打电话给我的吗？你想聊什么？'
report={'engine':engine,'synthetic_test':True,'cloud_requests':0,'physical_microphone_started':False,
        'audio_requested':False,'private_history_loaded':False,'passed':False}
with tempfile.TemporaryDirectory() as directory, \
        patch('voice_input.LiveMicrophone.start',side_effect=AssertionError('No physical microphone allowed')):
    controller=DesktopController(directory,router=router,call_factory=FakeCall)
    try:
        identifier=controller.incoming_calls.ring({'reason':'按昨天的约定问问答辩准备',
                    'opening':'昨天你说要准备答辩，今天准备得怎么样了？'})
        if controller.answer_call(identifier).result(5) is not True: raise RuntimeError('Synthetic answer did not complete')
        if not controller.pipeline.wait_finished(5): raise RuntimeError('Synthetic opening did not finish')
        reply=chat.reply(question,audio_enabled=False) if engine=='qwen' else chat.reply(question,max_tokens=160)
        report.update(cloud_requests=1,question=question,reply=reply,call_fact_in_request='【客户端来电事实】' in chat.context_provider(question),
                      passed=True)
        print(json.dumps({'engine':engine,'cloud_requests':1,'microphone':False,'reply':reply},ensure_ascii=False))
    except Exception as exc:
        report.update(error_type=type(exc).__name__)
        print(json.dumps({'engine':engine,'passed':False,'error_type':type(exc).__name__},ensure_ascii=False))
    finally: controller.close().result(10)
directory=Path(os.environ.get('RONOVA_PREVIEW_DIR',CODE/'verification')); directory.mkdir(parents=True,exist_ok=True)
(directory/'live-call-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
sys.exit(not report['passed'])
