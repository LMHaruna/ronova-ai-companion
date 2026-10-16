"""Build the titled local knowledge index from the supplied character profile."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client_lore import build_index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=ROOT)
    args = parser.parse_args()
    source = args.root/'docs/02-character-profile.md'
    index = build_index(source.read_bytes())
    target = args.root/'knowledge/ronova-lore.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'chunks':len(index['chunks']),'path':str(target),'titles':[row['title'] for row in index['chunks']]},ensure_ascii=False))


if __name__=='__main__':
    main()
