#!/usr/bin/python3
"""Smart Storage: explicit disposable roots, live-use checks and reversible staging."""
import argparse, fcntl, hashlib, json, os, shutil, stat, subprocess, sys, time, uuid
from pathlib import Path

HOME = Path.home()
STATE = HOME / '.local/state/smart-storage'
CONFIG = STATE / 'config.json'
DAY = 86400
LABELS = {'packages':'AUR package downloads', 'javascript':'Bun / npm caches',
          'python':'Python / pip / uv caches', 'rust':'Rust / compiler caches',
          'builds':'Rust build outputs', 'desktop':'Thumbnails / shaders', 'temp':'Temporary files', 'trash':'Trash / recycle bins'}
TOOLS = {
 'packages': {'pacman','paru','yay','shelly','makepkg','bsdtar'},
 'javascript': {'bun','npm','pnpm','yarn'},
 'python': {'pip','pip3','uv'},
 'rust': {'cargo','rustc','sccache','rustup','clang','gcc','cc','cmake','ninja','make'},
 'builds': {'cargo','rustc','clang','gcc','cc','cmake','ninja','make'},
 'desktop': {'steam','gamescope'}, 'temp': set(), 'trash':set()}
DEFAULT = {'age_days':0, 'schedule_days':0, 'categories':[x for x in LABELS if x!='trash'], 'pins':[], 'mode':'stage'}
DEV_ROOTS = [HOME/'projects', HOME/'Projects', HOME/'actions-runners', HOME/'.t3/worktrees', HOME/'src',
             Path('/mnt/Windows11/DEV_PROJECTS'), Path('/mnt/Windows11/DEV_WORKSPACE/BuildScratch')]
EXCLUDE = {'.git','node_modules','.venv','venv','vendor','.smart-storage-recovery'}

def read(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as f:
        json.dump(data, f, separators=(',',':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def config():
    c = DEFAULT | read(CONFIG, {})
    if c['mode'] not in ('stage','delete'): raise ValueError('Invalid cleanup mode')
    if c['age_days'] not in (0,1,7,30,90) or c['schedule_days'] not in (0,1,7,30):
        raise ValueError('Unsupported age or schedule')
    if not isinstance(c['categories'], list) or any(x not in LABELS for x in c['categories']):
        raise ValueError('Unknown cleanup category')
    if not isinstance(c['pins'], list) or any(not isinstance(x,str) for x in c['pins']):
        raise ValueError('Invalid pins')
    return c

def probe():
    result = subprocess.run(['/usr/bin/sudo','-n','/usr/local/libexec/smart-storage-probe'],
                            capture_output=True, text=True, timeout=45, check=True)
    p = json.loads(result.stdout)
    p['refs'] = {tuple(r) for r in p['refs']}
    return p

def key(path):
    return hashlib.sha256(os.fsencode(path)).hexdigest()[:20]

def under(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')

def kept(path, c):
    if any(under(str(path), p) for p in c['pins']):
        return True
    return any((p / marker).exists() for p in (path, *path.parents)
               for marker in ('.keepbuild','.keepstorage'))

def inventory(path, live, c, category):
    """Any uncertainty protects the entire unit; never follows symlinks/mounts."""
    rootstat = path.lstat()
    total, latest, count = 0, 0, 0
    inodes = {}
    digest = hashlib.sha256()
    reason = 'Pinned / keep marker' if kept(path, c) else ''
    if path.resolve() != path:
        reason = 'Symlink ancestor; protected'
    if live['errors']:
        reason = 'Process visibility incomplete; cleanup blocked'
    if any(under(p, str(path)) for p in live['paths']):
        reason = 'In use: open file, binary, working directory or argument'
    # Tool-wide guards cover lazily opened cache files, beyond open descriptors.
    if category != 'builds' and any(n.lower() in TOOLS[category] for n in live['names']):
        reason = 'Protected while related tools are running'
    if category=='builds' and (path.parent/'Cargo.toml').exists() and any(under(p,str(path.parent)) for p in live['paths']):
        reason = 'Project is in use'
    stack = [path]
    while stack:
        p = stack.pop()
        s = p.lstat()
        if s.st_dev != rootstat.st_dev or (stat.S_ISDIR(s.st_mode) and os.path.ismount(p)):
            reason = 'Contains a mount; protected'
            continue
        if s.st_uid != os.getuid():
            reason = 'Not entirely owned by this user'
        if (s.st_dev,s.st_ino) in live['refs']:
            reason = 'In use: open file, mapped binary or working directory'
        if p.name in ('.keepbuild','.keepstorage'):
            reason = 'Contains a keep marker'
        if stat.S_ISLNK(s.st_mode):
            pass  # rmtree unlinks directory entries and never follows these targets.
        elif stat.S_ISDIR(s.st_mode):
            stack.extend(sorted(p.iterdir(), reverse=True))
        elif not stat.S_ISREG(s.st_mode):
            reason = 'Contains sockets or special files'
        inode=(s.st_dev,s.st_ino)
        entry=inodes.setdefault(inode,[s.st_blocks*512,0,s.st_nlink if stat.S_ISREG(s.st_mode) else 1])
        entry[1]+=1
        latest = max(latest, s.st_mtime, s.st_ctime)
        count += 1
        digest.update(os.fsencode(str(p.relative_to(path))))
        digest.update(str((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode)).encode())
    total=sum(v[0] for v in inodes.values())
    reclaimable=sum(v[0] for v in inodes.values() if v[1]>=v[2])
    if not reason and time.time()-latest < c['age_days']*DAY:
        reason = 'Recently changed'
    return {'id':key(path),'path':str(path),'category':category,'bytes':total,'reclaimable':reclaimable,'files':count,
            'latest':latest,'fingerprint':digest.hexdigest(),'reason':reason,'eligible':not reason}

def trash_roots():
    roots=[HOME/'.local/share/Trash/files']
    for m in mounts():
        if not m['scan']: continue
        base=Path(m['target'])
        roots += [base/('.Trash-'+str(os.getuid()))/'files',base/'.Trash'/str(os.getuid())/'files']
        for name in ('$RECYCLE.BIN','$Recycle.Bin'):
            recycle=base/name
            if recycle.is_dir():
                # Never erase another user's recycle bin; ownership is rechecked per file.
                for sid in recycle.iterdir():
                    if sid.is_dir() and sid.name.startswith('S-1-'):
                        roots.append(sid)
    return roots

def candidates():
    # Only known regenerable caches. Never general .cache, user data or environments.
    roots = {
      'javascript':[HOME/'.npm/_cacache',HOME/'.bun/install/cache'],
      'python':[HOME/'.cache/pip',HOME/'.cache/uv'],
      'rust':[HOME/'.cache/sccache',HOME/'.cache/go-build',HOME/'.cargo/registry/cache'],
      'desktop':[HOME/'.cache/thumbnails',HOME/'.cache/mesa_shader_cache',HOME/'.local/share/Steam/steamapps/shadercache']}
    seen = set()
    def children(p, cat):
        p = p.resolve()
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.name != '.smart-storage-recovery' and not child.is_symlink():
                    yield child, cat
    for root in trash_roots():
        if root.name.startswith('S-1-'):
            yield root,'trash'
        else:
            yield from children(root,'trash')
    for cat, paths in roots.items():
        for p in paths:
            yield from children(p, cat)
    for base in (HOME/'.cache/paru/clone',HOME/'.cache/Shelly'):
        if base.is_dir():
            for directory, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if d not in EXCLUDE and d not in ('src','pkg')
                           and not Path(directory,d).is_symlink()]
                for f in files:
                    if ('.pkg.tar.' in f or f.endswith(('.tar.gz','.tar.xz','.tar.zst','.zip','.deb','.rpm','.AppImage'))) and not f.endswith(('.sig','.part','.tmp')):
                        yield Path(directory,f), 'packages'
    for base in DEV_ROOTS:
        if not base.is_dir():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            p = Path(directory)
            dirs[:] = [d for d in dirs if d not in EXCLUDE and not (p/d).is_symlink()]
            if '.rustc_info.json' in files and 'CACHEDIR.TAG' in files:
                try:
                    tagged = (p/'CACHEDIR.TAG').read_text().startswith('Signature: 8a477f597d28d172789f06886806bc55')
                except (OSError,UnicodeError):
                    tagged = False
                if tagged and str(p) not in seen:
                    seen.add(str(p))
                    yield p, 'builds'
                    dirs[:] = []
            # Bound traversal to the explicitly identified development trees.
    for base in (Path('/tmp'),Path('/var/tmp'),HOME/'tmp'):
        if base.is_dir():
            for p in base.iterdir():
                if (not p.name.startswith('.') and p.name != '.smart-storage-recovery'
                        and p.lstat().st_uid == os.getuid() and not p.is_symlink()):
                    yield p, 'temp'

def mounts():
    rows=json.loads(subprocess.check_output(['/usr/bin/findmnt','--json','--list',
                   '-o','TARGET,SOURCE,FSTYPE,FSROOT'],text=True))['filesystems']
    seen=set()
    for m in rows:
        identity=(m['source'].split('[')[0],m['fsroot'])
        m['scan']=m['fstype'] in ('btrfs','ext4','xfs','ntfs','ntfs3','fuseblk','vfat','exfat','tmpfs') and identity not in seen
        # /run holds live services and credentials; report its capacity, don't traverse.
        if under(m['target'],'/run'): m['scan']=False
        seen.add(identity)
    return rows

def disks():
    mounts = json.loads(subprocess.check_output(['/usr/bin/findmnt','--json','--list',
                        '-o','TARGET,SOURCE,FSTYPE'],text=True))['filesystems']
    result, seen = [], set()
    for m in mounts:
        if m['fstype'] not in ('btrfs','ext4','xfs','ntfs','ntfs3','fuseblk'):
            continue
        source = m['source'].split('[')[0]
        if source in seen:
            continue
        seen.add(source)
        s = shutil.disk_usage(m['target'])
        result.append({'path':m['target'],'source':source,'total':s.total,'used':s.used,'free':s.free})
    return result

def recovery():
    return read(STATE/'recovery.json',[])

def scan():
    c, live, items = config(), probe(), []
    try:
        system=json.loads(subprocess.check_output(['/usr/bin/sudo','-n','/usr/local/libexec/smart-storage-system','preview'],text=True,timeout=30))
        write(STATE/'system.json',system)
    except (subprocess.SubprocessError,ValueError):
        pass
    for p, category in candidates():
        try:
            items.append(inventory(p, live, c, category))
        except OSError as e:
            items.append({'id':key(p),'path':str(p),'category':category,'bytes':0,
                          'reason':'Unreadable or changed: '+type(e).__name__,'eligible':False})
    items.sort(key=lambda x:x['bytes'], reverse=True)
    report = {'at':time.time(),'config':c,'disks':disks(),'items':items,
              'process_errors':live['errors'],'recovery':recovery(),
              'last_action':read(STATE/'last-action.json',{})}
    history = read(STATE/'history.json',[])
    history.append({'at':report['at'],'bytes':sum(i['bytes'] for i in items),
                    'eligible':sum(i['bytes'] for i in items if i['eligible'])})
    history = history[-90:]
    write(STATE/'history.json',history)
    report['history'] = history
    write(STATE/'report.json',report)
    return report

def stage(report, permanent=False, rules=None):
    if time.time()-report['at'] > 1800:
        raise ValueError('Preview expired. Scan again before cleaning.')
    c, records, result = (rules or config()), recovery(), {'staged':0,'bytes':0,'skipped':0,'errors':[]}
    allowed = {str(p):cat for p,cat in candidates()}
    for old in report['items']:
        if not old['eligible'] or old['category'] not in c['categories']:
            continue
        p = Path(old['path'])
        try:
            if allowed.get(str(p)) != old['category'] or not p.exists():
                result['skipped'] += 1
                continue
            live = probe()
            now = inventory(p,live,c,old['category'])
            if not now['eligible'] or now['fingerprint'] != old['fingerprint']:
                result['skipped'] += 1
                continue
            if permanent:
                if p.is_dir(): shutil.rmtree(p)
                else: p.unlink()
                if old['category']=='trash' and p.parent.name=='files':
                    info=p.parent.parent/'info'/(p.name+'.trashinfo')
                    if info.is_file() and not info.is_symlink(): info.unlink()
                result['deleted']=result.get('deleted',0)+1
                result['bytes']+=now['bytes']
                continue
            vault = p.parent/'.smart-storage-recovery'
            if vault.is_symlink():
                raise ValueError('Recovery directory is a symlink')
            vault.mkdir(mode=0o700,exist_ok=True)
            if vault.stat().st_uid != os.getuid() or vault.stat().st_mode & 0o077:
                raise ValueError('Recovery directory must be private and user-owned')
            dest = vault/uuid.uuid4().hex
            record = {'id':dest.name,'path':str(p),'stored':str(dest),'at':time.time(),
                      'bytes':now['bytes'],'category':old['category'],'state':'pending'}
            records.append(record)
            write(STATE/'recovery.json',records)  # Journal before the atomic move.
            p.rename(dest)
            record['state'] = 'staged'
            record['fingerprint'] = inventory(dest,live,c,old['category'])['fingerprint']
            write(STATE/'recovery.json',records)
            result['staged'] += 1
            result['bytes'] += now['bytes']
        except (OSError,ValueError,subprocess.SubprocessError) as e:
            result['errors'].append(str(e))
    result['at'] = time.time()
    write(STATE/'last-action.json',result)
    return result

def valid_record(r):
    p, stored = Path(r['path']), Path(r['stored'])
    if stored.parent != p.parent/'.smart-storage-recovery' or stored.name != r['id']:
        raise ValueError('Invalid recovery record')
    if stored.parent.is_symlink() or stored.is_symlink():
        raise ValueError('Recovery path changed')
    if stored.parent.resolve() != stored.parent:
        raise ValueError('Recovery ancestor changed')
    return p, stored

def recover_or_purge(action, aged=False):
    records, remaining, result = recovery(), [], {'restored':0,'purged':0,'bytes':0,'errors':[]}
    c = config()
    for r in records:
        try:
            p, stored = valid_record(r)
            if not stored.exists():
                # A pending journal with its original intact means rename never happened.
                if r['state'] == 'pending' and p.exists():
                    continue
                raise ValueError('Missing recovery item: '+str(stored))
            if action == 'restore':
                if p.exists() or p.is_symlink():
                    raise ValueError('Original exists; kept recovery copy: '+str(p))
                stored.rename(p)
                result['restored'] += 1
            else:
                if aged and (time.time()-r['at'] < 7*DAY or r['category'] not in c['categories']):
                    remaining.append(r)
                    continue
                if kept(p,c):
                    raise ValueError('Pinned original; kept recovery copy: '+str(p))
                check = inventory(stored,probe(),c | {'age_days':1},r['category'])
                if check['fingerprint'] != r.get('fingerprint'):
                    raise ValueError('Recovery content changed; restore or inspect it: '+str(p))
                if check['reason'] and check['reason'] != 'Recently changed':
                    raise ValueError(check['reason']+': '+str(p))
                # ponytail: snapshots cannot prevent a process starting just after the probe.
                # Keep staging + grace; use .keepstorage for workloads that open files later.
                if stored.is_dir():
                    shutil.rmtree(stored)
                else:
                    stored.unlink()
                result['purged'] += 1
                result['bytes'] += r['bytes']
            write(STATE/'recovery.json',remaining + records[records.index(r)+1:])
        except (OSError,ValueError,subprocess.SubprocessError) as e:
            result['errors'].append(str(e))
            remaining.append(r)
    write(STATE/'recovery.json',remaining)
    result['at'] = time.time()
    write(STATE/'last-action.json',result)
    return result

def summary(report):
    c = config()
    groups = []
    for cat,label in LABELS.items():
        rows = [x for x in report.get('items',[]) if x['category']==cat]
        groups.append({'id':cat,'label':label,'bytes':sum(x['bytes'] for x in rows),
                      'eligible':sum(x.get('reclaimable',x['bytes']) for x in rows if x['eligible']),
                      'count':len(rows),'enabled':cat in c['categories']})
    return {'at':report.get('at',0),'config':c,'disks':disks(),'groups':groups,
            'items':report.get('items',[])[:150],'item_count':len(report.get('items',[])),
            'eligible':sum(g['eligible'] for g in groups if g['enabled']),
            'recovery_bytes':sum(r['bytes'] for r in recovery()),'recovery_count':len(recovery()),
            'history':report.get('history',[]),'last_action':read(STATE/'last-action.json',{}),
            'process_errors':report.get('process_errors',[]),'mounts':mounts(),
            'explorer':read(STATE/'explorer.json',{}),
            'all_mounts':read(STATE/'all-mounts.json',{}),'system':read(STATE/'system.json',{}),'vm':read(STATE/'vm-usage.json',{})}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['scan','status','stage','purge','restore','configure','auto','start','delete','explore','all-mounts','clean-temp','system','vm-clean'])
    parser.add_argument('--operation',choices=['scan','stage','purge','restore','configure','delete','explore','all-mounts','system'])
    parser.add_argument('--path',default='/')
    parser.add_argument('--kind',choices=['packages','packages-all','snapshots','temp'])
    parser.add_argument('--mode',choices=['stage','delete'])
    parser.add_argument('--age',type=int,choices=[0,1,7,30,90])
    parser.add_argument('--schedule',type=int,choices=[0,1,7,30])
    parser.add_argument('--categories')
    args=parser.parse_args()
    if os.getuid()==0:
        raise ValueError('The cleaner must run as your normal user')
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.action == 'status':
        out=summary(read(STATE/'report.json',{}))
        job=read(STATE/'job.json',{})
        if job.get('pid'):
            try:
                os.kill(job['pid'],0)
                job['running']=True
            except ProcessLookupError:
                job['running']=False
        if job and not job.get('running'):
            try:
                finished=read(STATE/'job-output.json',{})
                job['error']=finished.get('error','')
            except (ValueError,OSError):
                job['error']='Task ended without a complete result; scan again.'
        out['job']=job
        print(json.dumps(out,separators=(',',':')))
        return
    if args.action == 'start':
        if not args.operation: raise ValueError('Missing operation')
        with (STATE/'launch.lock').open('w') as launch_lock:
            fcntl.flock(launch_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            job=read(STATE/'job.json',{})
            if job.get('pid'):
                try:
                    os.kill(job['pid'],0)
                    raise ValueError('A storage task is already running')
                except ProcessLookupError:
                    pass
            command=[sys.executable,str(Path(__file__).resolve()),args.operation]
            for name in ('age','schedule','categories','path','mode','kind'):
                value=getattr(args,name)
                if value is not None: command += ['--'+name,str(value)]
            with (STATE/'job-output.json').open('w') as output:
                child=subprocess.Popen(command,stdout=output,stderr=output,start_new_session=True)
            write(STATE/'job.json',{'pid':child.pid,'operation':args.operation,'at':time.time()})
            print(json.dumps({'started':True}))
        return
    with (STATE/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        report = read(STATE/'report.json',{})
        if args.action=='configure':
            c=config()
            if args.mode is not None: c['mode']=args.mode
            if args.age is not None: c['age_days']=args.age
            if args.schedule is not None: c['schedule_days']=args.schedule
            if args.categories is not None:
                cats=args.categories.split(',') if args.categories else []
                if any(x not in LABELS for x in cats): raise ValueError('Unknown category')
                c['categories']=cats
            write(CONFIG,c)
            if args.age is not None: report=scan()
        elif args.action=='scan': report=scan()
        elif args.action=='system':
            if not args.kind: raise ValueError('Missing system action')
            result=subprocess.run(['/usr/bin/pkexec','/usr/local/libexec/smart-storage-system',args.kind],
                                  capture_output=True,text=True,timeout=1200)
            write(STATE/'system-result.json',{'returncode':result.returncode,'output':result.stdout,'error':result.stderr})
            if result.returncode: raise ValueError(result.stdout or result.stderr or 'Administrator authorization cancelled')
            preview=subprocess.check_output(['/usr/bin/sudo','-n','/usr/local/libexec/smart-storage-system','preview'],text=True)
            write(STATE/'system.json',json.loads(preview))
            report=scan()
        elif args.action=='vm-clean':
            policy=read(STATE/'vm-policy.json',{})
            base=Path(policy['root'])
            live=probe()
            if any(n.startswith('qemu') or n.startswith('reims') for n in live['names']):
                raise ValueError('VM processes are running; stopped')
            # Only this session's explicitly approved extra artifacts; never guest disks.
            items=[]
            for value in policy['approved_extras']:
                path=Path(value)
                if not under(str(path),str(base)) or path==base: raise ValueError('Invalid VM policy')
                if path.exists(): items.append(inventory(path,live,config()|{'age_days':0},'temp'))
            removed=[]
            for old in items:
                p=Path(old['path'])
                now=inventory(p,probe(),config()|{'age_days':0},'temp')
                if not now['eligible'] or now['fingerprint']!=old['fingerprint']: continue
                if p.is_dir(): shutil.rmtree(p)
                else:p.unlink()
                removed.append(str(p))
                write(STATE/'vm-cleanup.json',{'removed':removed,'at':time.time()})
            total=int(subprocess.check_output(['/usr/bin/du','-sx','-B1',str(base)],text=True).split()[0])
            write(STATE/'vm-usage.json',{'bytes':total,'cap_bytes':policy['cap_bytes'],'within_budget':total<=policy['cap_bytes'],'retained':policy['retained_vms']})
        elif args.action=='clean-temp':
            rules=config() | {'age_days':1,'categories':['temp']}
            live=probe()
            items=[]
            for path,cat in candidates():
                if cat=='temp' and (under(str(path),'/tmp') or under(str(path),'/var/tmp')):
                    try: items.append(inventory(path,live,rules,cat))
                    except OSError: pass
            result=stage({'at':time.time(),'items':items},permanent=True,rules=rules)
            write(STATE/'temp-cleanup.json',result)
            report=scan()
        elif args.action in ('explore','all-mounts'):
            paths=[args.path] if args.action=='explore' else [m['target'] for m in mounts() if m['scan']]
            results=[]
            for path in paths:
                result=subprocess.run(['/usr/bin/sudo','-n','/usr/local/libexec/smart-storage-usage',path],
                                      capture_output=True,text=True,timeout=1200,check=True)
                results.append(json.loads(result.stdout))
            write(STATE/('explorer.json' if args.action=='explore' else 'all-mounts.json'),
                  {'at':time.time(),'results':results})
        elif args.action in ('stage','delete'):
            if not report: raise ValueError('Scan before cleaning')
            stage(report,permanent=args.action=='delete')
            report=scan()
        elif args.action in ('restore','purge'):
            recover_or_purge(args.action)
            report=scan()
        elif args.action=='auto':
            c=config()
            last=read(STATE/'last-auto.json',{'at':0})
            if c['schedule_days'] and time.time()-last['at'] >= c['schedule_days']*DAY:
                report=scan()
                if report['process_errors']: raise ValueError('Incomplete process visibility')
                recover_or_purge('purge',aged=True)
                stage(report,permanent=c['mode']=='delete')
                write(STATE/'last-auto.json',{'at':time.time()})
                report=scan()
        print(json.dumps(summary(report),separators=(',',':')))

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'error':str(e)}))
        sys.exit(1)
