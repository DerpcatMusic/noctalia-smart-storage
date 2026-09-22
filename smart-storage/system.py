#!/usr/bin/python3 -I
"""Fixed native maintenance operations; invoked through polkit for mutations."""
import csv, importlib.machinery, importlib.util, io, json, os, subprocess, sys
from pathlib import Path

def command(*args):
    r=subprocess.run(args,capture_output=True,text=True,timeout=600)
    if r.returncode: raise RuntimeError(r.stderr.strip() or r.stdout.strip() or 'Command failed')
    return r.stdout.strip()

def snapshots():
    raw=command('/usr/bin/snapper','--csv','-c','root','list','--columns','number,type,pre-number')
    rows=list(csv.DictReader(io.StringIO(raw)))
    ids=sorted(int(r['number'].strip('*+- ')) for r in rows if r['number'].strip('*+- ')!='0')
    keep=set(ids[-4:])
    for r in rows:
        if int(r['number'].strip('*+- ')) in keep and r['pre-number']:
            keep.add(int(r['pre-number']))
    return ids,[i for i in ids if i not in keep]

def live():
    loader=importlib.machinery.SourceFileLoader('root_probe','/usr/local/libexec/smart-storage-probe')
    spec=importlib.util.spec_from_loader(loader.name,loader);mod=importlib.util.module_from_spec(spec);loader.exec_module(mod)
    result=mod.snapshot()
    if result['errors']: raise RuntimeError('Incomplete process visibility; stopped')
    return result

def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('preview','packages','packages-all','snapshots','temp','journal'):
        raise ValueError('Expected preview, packages, snapshots, temp or journal')
    action=sys.argv[1]
    ids,remove=snapshots()
    if action=='preview':
        size=int(command('/usr/bin/du','-sx','-B1','/var/cache/pacman/pkg').split()[0])
        logs=int(command('/usr/bin/du','-sx','-B1','/var/log').split()[0])
        journal=int(command('/usr/bin/du','-sx','-B1','/var/log/journal').split()[0]) if Path('/var/log/journal').is_dir() else 0
        print(json.dumps({'package_bytes':size,'package_preview':command('/usr/bin/paccache','-d','-k','1'),
              'log_bytes':logs,'journal_bytes':journal,
              'snapshot_count':len(ids),'snapshot_remove':remove,'snapshot_keep':[i for i in ids if i not in remove],
              'note':'Btrfs snapshots share extents; their sizes cannot be summed as reclaimable space.'}))
        return
    p=live()
    if action in ('packages','packages-all'):
        if Path('/var/lib/pacman/db.lck').exists() or set(p['names']) & {'pacman','paru','yay','makepkg','shelly'}:
            raise RuntimeError('Package manager is running; try again later')
        if any(x.startswith('/var/cache/pacman/') for x in p['paths']):
            raise RuntimeError('Package cache is in use; stopped')
        result=command('/usr/bin/paccache','-r','-k','0' if action=='packages-all' else '1')
    elif action=='journal':
        result=command('/usr/bin/journalctl','--vacuum-size=200M','--vacuum-time=14d')
    elif action=='snapshots':
        paths=['/.snapshots/'+str(i)+'/' for i in remove]
        if any(any(x.startswith(base) for base in paths) for x in p['paths']):
            raise RuntimeError('A snapshot is in use; stopped')
        result=command('/usr/bin/snapper','-c','root','delete',*[str(i) for i in remove]) if remove else 'No old snapshots'
    else:
        excludes=set()
        for value in p['paths']:
            for base in ('/tmp/','/var/tmp/'):
                if value.startswith(base):excludes.add(base+value[len(base):].split('/')[0])
        for base in (Path('/tmp'),Path('/var/tmp')):
            for directory,dirs,files in os.walk(base,followlinks=False):
                if '.keepstorage' in files or '.keepbuild' in files:
                    excludes.add(directory);dirs[:]=[]
        result=command('/usr/bin/systemd-tmpfiles','--clean','--prefix=/tmp','--prefix=/var/tmp',
                       *['--exclude-prefix='+x for x in sorted(excludes)])
    print(json.dumps({'ok':True,'action':action,'result':result}))

if __name__=='__main__':
    try:main()
    except Exception as e:
        print(json.dumps({'error':str(e)}));sys.exit(1)
