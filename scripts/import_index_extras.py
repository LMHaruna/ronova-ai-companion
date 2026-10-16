"""Copy only missing Index frontend dependencies; never replace GPT libraries."""
from pathlib import Path
import importlib.util
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('omegaconf', 'antlr4', 'sentencepiece', 'tiktoken', 'tiktoken_ext', 'whisper',
           'wetext', 'kaldifst', '_kaldifst', 'munch', 'json5')

def main():
    source = Path(sys.argv[1]).resolve()
    target = ROOT/'runtime/indextts-2.5/extras'
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in MODULES:
        if importlib.util.find_spec(name): continue
        item = source/name
        if not item.exists(): item = source/(name+'.py')
        if not item.exists():
            binaries = list(source.glob(name+'*.pyd'))
            if not binaries: continue
            item = binaries[0]
        if item.is_dir():
            shutil.copytree(item,target/item.name,dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
        else: shutil.copy2(item,target/item.name)
        copied.append(name)
    # Preserve version and license metadata for copied distributions.
    for info in source.glob('*.dist-info'):
        top = info/'top_level.txt'
        names = top.read_text().split() if top.exists() else [info.name.split('-')[0]]
        if set(names)&set(copied): shutil.copytree(info,target/info.name,dirs_exist_ok=True)
    for library in source.glob('kaldifst_*.dll'):
        shutil.copy2(library,target/library.name)
    (target/'import-manifest.json').write_text(json.dumps({'source':str(source),
        'copied_modules':copied,'shared_python':str(Path(sys.executable).resolve())},
        ensure_ascii=False,indent=2),encoding='utf-8')
    print('Copied missing modules:',copied)

if __name__=='__main__': main()
