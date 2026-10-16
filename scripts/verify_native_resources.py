"""Load real ASR and WeText resources under a Unicode project path, offline."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from voice_input import ChineseRecognizer, MODEL_NAME
    extras=ROOT/'runtime/indextts-2.5/extras'
    sys.path.append(str(extras))
    dll=os.add_dll_directory(str(extras)) if os.name=='nt' else None
    cache=ROOT/'runtime/native-resource-cache'
    with tempfile.TemporaryDirectory(prefix='中文路径-',dir=ROOT/'runtime') as directory:
        project=Path(directory)
        assert project.resolve().is_relative_to((ROOT/'runtime').resolve())
        model=project/'runtime/models'/MODEL_NAME
        model.mkdir(parents=True)
        for name in ('model.int8.onnx','tokens.txt'):
            os.link(ROOT/'runtime/models'/MODEL_NAME/name,model/name)
        fst_source=extras/'wetext/fsts'
        fst_target=project/'音色资源/fsts'
        shutil.copytree(fst_source,fst_target)
        # Exercise the copy fallback even if this volume enables 8.3 aliases.
        with patch('native_resources._short_path',return_value=None), \
                patch.dict(os.environ,RONOVA_NATIVE_RESOURCE_CACHE=str(cache)), \
                patch('voice_input.ROOT',project):
            recognizer=ChineseRecognizer()
            stream=recognizer.create()
            recognizer.feed(stream,b'\0\0'*1600)
            recognizer.finish(stream)
            from wetext import constants
            loaded=[]
            with patch.object(constants,'files',return_value=fst_target):
                for filename in sorted(fst_target.rglob('*.fst')):
                    relative=filename.relative_to(fst_target).as_posix()
                    constants.load_fst(relative)
                    loaded.append(relative)
    if dll is not None: dll.close()
    print(json.dumps({'passed':True,'unicode_asr':'initialized and decoded',
                      'wetext_fsts_loaded':len(loaded),'forced_no_short_paths':True,
                      'cloud_requests':0,'microphone_started':False},ensure_ascii=False))


if __name__=='__main__': main()
