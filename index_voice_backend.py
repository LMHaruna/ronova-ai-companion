"""Offline IndexTTS-2.5 adapter for the existing appendable voice protocol."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import threading
import time
import native_resources  # Capture the system temp before initialize redirects it.

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / 'runtime/indextts-2.5'
THREADS_CONFIGURED = False
DLL_DIRECTORY = None


def synthesis_options(path=None, environ=None):
    path = Path(path) if path else ROOT/'config/index-voice.json'
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    env = os.environ if environ is None else environ
    graph = saved.get('cuda_graph',True)
    steps = saved.get('diffusion_steps',20)
    if 'RONOVA_INDEX_CUDA_GRAPH' in env:
        if env['RONOVA_INDEX_CUDA_GRAPH'] not in ('0','1'): raise ValueError('Index GPU 加速配置无效')
        graph = env['RONOVA_INDEX_CUDA_GRAPH']=='1'
    if 'RONOVA_INDEX_DIFFUSION_STEPS' in env: steps = int(env['RONOVA_INDEX_DIFFUSION_STEPS'])
    if type(graph) is not bool or type(steps) is not int or steps not in (15,20,25):
        raise ValueError('Index 仅支持布尔 GPU 加速开关和 15、20、25 步扩散')
    return {'cuda_graph':graph,'diffusion_steps':steps}


def initialize(force_cpu=False):
    global THREADS_CONFIGURED, DLL_DIRECTORY
    started = time.perf_counter()
    options = synthesis_options()
    cache = ROOT / 'runtime/index-voice-cache'
    for name in ('tmp', 'matplotlib', 'numba', 'huggingface'):
        (cache / name).mkdir(parents=True, exist_ok=True)
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
        HF_HOME=str(cache/'huggingface'), NLTK_DATA=str(PACKAGE/'nltk_data'),
        TORCH_HOME=str(cache/'torch'), TORCH_EXTENSIONS_DIR=str(cache/'torch-extensions'),
        TORCHINDUCTOR_CACHE_DIR=str(cache/'torchinductor'),
        MPLCONFIGDIR=str(cache/'matplotlib'), NUMBA_CACHE_DIR=str(cache/'numba'),
        TEMP=str(cache/'tmp'), TMP=str(cache/'tmp'), USE_TF='0', USE_FLAX='0',
        TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='6', MKL_NUM_THREADS='6')
    os.environ['PATH'] = str(PACKAGE/'bin') + os.pathsep + os.environ['PATH']
    sys.path.insert(0, str(PACKAGE/'source'))
    if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
    # Extra Index frontend libraries only. The shared GPT environment wins
    # for torch, CUDA, Transformers, NumPy and every dependency it already has.
    sys.path.append(str(PACKAGE/'extras'))
    if os.name=='nt' and DLL_DIRECTORY is None:
        DLL_DIRECTORY = os.add_dll_directory(str(PACKAGE/'extras'))
    os.chdir(PACKAGE/'source')
    import torch
    if not THREADS_CONFIGURED:
        torch.set_num_threads(6)
        torch.set_num_interop_threads(2)
        THREADS_CONFIGURED = True
    from voice_acceleration import inference_device
    acceleration = inference_device(torch, 'cpu' if force_cpu else os.environ.get('RONOVA_VOICE_DEVICE','auto'))
    device = 'cuda:0' if acceleration['device']=='cuda' else 'cpu'
    if device.startswith('cuda'):
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    from indextts.infer_v2_5 import IndexTTS2
    from transformers import StoppingCriteria, StoppingCriteriaList
    use_bf16 = device.startswith('cuda') and torch.cuda.is_bf16_supported()
    model = IndexTTS2(cfg_path=str(PACKAGE/'models/config.yaml'),
        model_dir=str(PACKAGE/'models'), device=device,
        use_bf16=use_bf16, use_cuda_kernel=False,
        use_deepspeed=False, use_accel=False, use_torch_compile=False, use_qwen_emo=False)
    decoder = None
    if device.startswith('cuda') and options['cuda_graph']:
        from index_decode_graph import NativeIndexDecoder
        decoder = NativeIndexDecoder(model.gpt.inference_model).install()
    diffusion_steps = options['diffusion_steps']
    selection = json.loads((ROOT/'config/voice-selection.json').read_text(encoding='utf-8'))
    references = json.loads((ROOT/'voice-reference/prepared/references.json').read_text(encoding='utf-8'))
    reference = next(row for row in references['references'] if row['id']==selection.get('reference_id','C'))
    acceleration.update(precision='bf16' if use_bf16 else 'fp32',
                        is_half=use_bf16, cpu_threads=6,
                        reference_encoder_device=model.reference_device,
                        attention=model.gpt.gpt.config._attn_implementation)
    acceleration.update(diffusion_steps=diffusion_steps,cuda_graph_enabled=False)
    acceleration['inference_thread'] = 'main'

    class Adapter:
        def __init__(self):
            self.cancel = threading.Event()
            self.cancel_check = lambda: False
            self.reference_speaker_cache = SimpleNamespace(hits=0, misses=0)
            self.configs = SimpleNamespace(version='indextts-2.5',
                                          vits_weights_path=str(PACKAGE/'models/s2mel.pth'))
        def stop(self): self.cancel.set()
        def set_cancel_check(self, check): self.cancel_check = check
        def run(self, parameters):
            self.cancel.clear()
            if self.cancel_check(): return
            torch.manual_seed(20261003)
            if device.startswith('cuda'): torch.cuda.manual_seed_all(20261003)
            audio = parameters['ref_audio_path']
            stats = self.reference_speaker_cache
            if model.cache_spk_audio_prompt==audio: stats.hits += 1
            else: stats.misses += 1
            if device.startswith('cuda') and model.cache_spk_audio_prompt==audio:
                torch.set_num_threads(1)  # Avoid CPU pool overhead for small GPU decode steps.
            owner = self
            class Cancel(StoppingCriteria):
                def __call__(self, input_ids, scores, **kwargs):
                    return owner.cancel.is_set() or owner.cancel_check()
            # Same sentence queue as GPT-SoVITS; Index also yields internal
            # segments as soon as available, with no extra interval silence.
            text = parameters['text'].replace('倒是', '<倒|DAO4>是')
            if decoder: decoder.enabled = parameters.get('index_cuda_graph',True)
            steps = parameters.get('index_diffusion_steps',diffusion_steps)
            acceleration['diffusion_steps'] = steps
            generator = model.infer(spk_audio_prompt=audio, text=text,
                output_path=None, lang='ZH', use_emo_text=False, stream_return=True,
                max_text_tokens_per_segment=60, num_beams=1, max_mel_tokens=1500,
                duration_factor=1.0, interval_silence=0, verbose=False,
                diffusion_steps=steps,
                cancel_check=lambda: self.cancel.is_set() or self.cancel_check(),
                stopping_criteria=StoppingCriteriaList([Cancel()]))
            try:
                for samples in generator:
                    if self.cancel.is_set() or self.cancel_check(): return
                    if not torch.is_tensor(samples) or not samples.numel(): continue
                    yield 22050, samples.reshape(-1).numpy().astype('int16')
            finally:
                generator.close()
                if decoder: acceleration.update(decoder.info())

    return Adapter(), reference, time.perf_counter()-started, acceleration
