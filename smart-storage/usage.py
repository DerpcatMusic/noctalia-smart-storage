#!/usr/bin/python3 -I
"""Read-only directory usage. No deletion, file contents, or shell execution."""
import json, os, subprocess, sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv)==2 else '/')
if len(sys.argv)>2 or not p.is_absolute() or not p.is_dir():
    raise SystemExit('Expected one absolute directory')
p = p.resolve()
if any(p==Path(x) or Path(x) in p.parents for x in ('/proc','/sys')) or (Path('/dev') in p.parents and not (p==Path('/dev/shm') or Path('/dev/shm') in p.parents)) or p==Path('/dev'):
    raise SystemExit('Kernel pseudo-filesystems are not storage directories')
r = subprocess.run(['/usr/bin/du','-x','-B1','--max-depth=1','--null','--',str(p)],capture_output=True)
rows=[]
for line in r.stdout.split(b'\0'):
    if b'\t' in line:
        size,name=line.split(b'\t',1)
        rows.append({'path':os.fsdecode(name),'bytes':int(size)})
print(json.dumps({'path':str(p),'rows':sorted(rows,key=lambda r:r['bytes'],reverse=True),
                  'partial':r.returncode!=0,'note':r.stderr.decode(errors='replace')[:2000]}))
