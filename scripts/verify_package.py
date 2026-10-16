"""Verify original packaged content before personal preferences or source edits change it."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
 path=ROOT/'package-manifest.json'
 if not path.is_file():
  print(json.dumps({'passed':False,'files':0,'errors':['No release snapshot manifest has been generated. Run Verify-Desktop.cmd to check the current runtime.']}))
  return 2
 manifest=json.loads(path.read_text(encoding='utf-8'))
 errors=[]
 for row in manifest['files']:
  relative=Path(row['path']); target=ROOT/relative
  if relative.is_absolute() or '..' in relative.parts or not target.resolve().is_relative_to(ROOT.resolve()) or target.is_symlink():
   errors.append(row['path']+': unexpected path'); continue
  if not target.is_file() or target.stat().st_size!=row['bytes']:
   errors.append(row['path']+': missing or size differs'); continue
  digest=hashlib.sha256()
  with target.open('rb') as stream:
   while chunk:=stream.read(1024*1024): digest.update(chunk)
  if digest.hexdigest()!=row['sha256']: errors.append(row['path']+': hash differs')
 print(json.dumps({'passed':not errors,'files':len(manifest['files']),'errors':errors},ensure_ascii=False,indent=2))
 return bool(errors)
if __name__=='__main__': sys.exit(main())
