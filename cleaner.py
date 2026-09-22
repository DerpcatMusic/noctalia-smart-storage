#!/usr/bin/python3
"""Smart Storage: explicit disposable roots, live-use checks and reversible staging."""
import argparse, concurrent.futures, fcntl, hashlib, json, math, os, re, shutil, stat, struct, subprocess, sys, time, uuid, zlib
from pathlib import Path

HOME = Path.home()
STATE = HOME / '.local/state/smart-storage'
CONFIG = STATE / 'config.json'
DAY = 86400
LABELS = {'packages':'AUR package downloads', 'javascript':'Bun / npm caches',
          'python':'Python / pip / uv caches', 'rust':'Rust / compiler caches',
          'builds':'Rust build outputs', 'desktop':'Thumbnails / shaders', 'logs':'Application logs',
          'worktrees':'Git worktrees (merged, clean)', 'branches':'Stale git branches', 'temp':'Temporary files', 'trash':'Trash / recycle bins',
          'apps':'App caches (opt-in)'}
TOOLS = {
 'packages': {'pacman','paru','yay','shelly','makepkg','bsdtar'},
 'javascript': {'bun','npm','pnpm','yarn'},
 'python': {'pip','pip3','uv'},
 'rust': {'cargo','rustc','sccache','rustup','clang','gcc','cc','cmake','ninja','make'},
 'builds': {'cargo','rustc','clang','gcc','cc','cmake','ninja','make'},
 'desktop': {'steam','gamescope'}, 'logs': set(), 'worktrees': set(), 'branches': set(), 'temp': set(), 'trash':set(), 'apps':set()}
DEFAULT = {'age_days':0, 'schedule_days':0, 'categories':[x for x in LABELS if x not in ('trash','worktrees','branches','apps')], 'pins':[], 'mode':'stage'}
DEV_ROOTS = [HOME/'projects', HOME/'Projects', HOME/'actions-runners', HOME/'.t3/worktrees', HOME/'src', HOME/'.codex/worktrees',
             Path('/mnt/Windows11/DEV_PROJECTS'), Path('/mnt/Windows11/DEV_WORKSPACE/BuildScratch')]
EXCLUDE = {'.git','node_modules','.venv','venv','vendor','.smart-storage-recovery'}
LOG_BASES = [HOME/'.cache', HOME/'.config', HOME/'.local/share', HOME/'.local/state']
LOG_NAME = re.compile(r'\.log(\.|$)')
STEAM_APPS = [Path('/mnt/Gaming/SteamLibrary/steamapps'), HOME/'.local/share/Steam/steamapps']
COLORS = ['mPrimary','mSecondary','mTertiary','mError']
PREVIOUS = {}  # last report's worktree rows; ineligible worktrees reuse their size while git state is unchanged

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

def git(*args, cwd):
    return subprocess.run(['/usr/bin/git','-c','core.fsmonitor=false',*args],cwd=cwd,capture_output=True,text=True,timeout=60)

def default_ref(repo):
    r = git('symbolic-ref','-q','--short','refs/remotes/origin/HEAD',cwd=repo)
    if r.returncode == 0: return r.stdout.strip()
    for name in ('main','master'):
        if git('rev-parse','-q','--verify','refs/heads/'+name,cwd=repo).returncode == 0: return name
    return ''

def idle_days(ts):
    return int((time.time()-ts)//DAY) if ts else 0

def worktree_state(path):
    """Linked worktree: eligible only when clean and HEAD is already in the default branch."""
    main = str(Path(git('rev-parse','--path-format=absolute','--git-common-dir',cwd=path).stdout.strip()).parent)
    branch = git('rev-parse','--abbrev-ref','HEAD',cwd=path).stdout.strip() or 'HEAD'
    dirty = len(git('status','--porcelain','--ignore-submodules',cwd=path).stdout.splitlines())
    base = default_ref(path)
    merged = bool(base) and git('merge-base','--is-ancestor','HEAD',base,cwd=path).returncode == 0
    head, ts = (git('log','-1','--format=%H %ct',cwd=path).stdout.split() or ['',0])[:2]
    idle = idle_days(int(ts))
    detail = f"{branch} · {'merged into '+base if merged else 'not merged'} · {idle}d since last commit · {'dirty '+str(dirty) if dirty else 'clean'} · of {Path(main).name}"
    reason = 'Uncommitted changes' if dirty else '' if merged else 'Not merged into '+(base or 'a default branch')
    return reason, detail, main, [head, dirty, base]

def branch_items(repo):
    """Merged branches are deletable (git branch -d); unmerged ones idle 30+ days are listed only."""
    base = default_ref(repo)
    if not base: return []
    merged = set(git('branch','--merged',base,'--format=%(refname:short)',cwd=repo).stdout.split())
    used = {l.split('/',2)[-1] for l in git('worktree','list','--porcelain',cwd=repo).stdout.splitlines() if l.startswith('branch ')}
    items = []
    for line in git('for-each-ref','--format=%(refname:short) %(objectname) %(committerdate:unix)','refs/heads',cwd=repo).stdout.splitlines():
        name, sha, ts = line.split(); idle = idle_days(int(ts))
        is_merged = name in merged and name != base.split('/')[-1]
        if not is_merged and idle < 30: continue
        reason = 'Checked out in a worktree' if name in used else '' if is_merged else 'Not merged; listed only'
        items.append({'id':key(f'{repo}#{name}'),'path':f'{repo}#{name}','repo':str(repo),'branch':name,'category':'branches',
                      'bytes':0,'reclaimable':0,'files':0,'latest':int(ts),'fingerprint':sha,'reason':reason,'eligible':not reason,
                      'detail':f"{'merged into '+base if is_merged else 'unmerged'} · {idle}d since last commit · {repo.name}"})
    return items

_steam = {}
def steam_name(appid):
    if not _steam:
        for base in STEAM_APPS:
            for f in (base.glob('appmanifest_*.acf') if base.is_dir() else ()):
                try: m = re.search(r'"name"\s+"([^"]*)"', f.read_text(errors='replace'))
                except OSError: continue
                if m: _steam[f.stem.split('_',1)[1]] = m.group(1)
    return _steam.get(appid, 'app '+appid)

def cargo_target(p):
    # cargo writes .rustc_info.json at the target root and .fingerprint under each profile; CI targets lack CACHEDIR.TAG
    try:
        if not (p/'.rustc_info.json').is_file(): return False
        if (p/'CACHEDIR.TAG').read_text().startswith('Signature: 8a477f597d28d172789f06886806bc55'): return True
    except (OSError,UnicodeError): pass
    try:
        return any(e.is_dir() and os.path.isdir(os.path.join(e.path,'.fingerprint')) for e in os.scandir(p))
    except OSError: return False

def describe(path, category, item):
    s, name, parent = str(path), path.name, path.parent.name.lstrip('.')
    age = f"{idle_days(item['latest'])}d since last change"
    if category=='rust':
        if parent=='src': return 'extracted crate sources · cargo re-extracts on build · '+age
        return {'sccache':'sccache compiler cache','go-build':'Go build cache','cache':'crates.io downloads'}.get(path.name,path.name)+' · regenerates on next build · '+age
    if category=='javascript':
        tool = 'npm' if '/.npm/' in s else 'pnpm' if 'pnpm' in s else 'node-gyp' if 'node-gyp' in s else 'bun'
        return tool+' package cache · re-downloaded on install · '+age
    if category=='apps':
        for needle, what in (('OptGuideOnDeviceModel','Chrome on-device AI model · re-downloaded if the feature is used'),
                             ('buffr/capture','BUFFR capture spill buffers'), ('plugin-undo','Bitwig plugin-state undo history · only undo steps are lost'), ('session-artifacts','Prime agent session artifacts'),
                             ('winetricks','winetricks download cache · re-downloaded'), ('.var/app','flatpak app cache')):
            if needle in s: return f"{what} · {item['files']} files · {age}"
        return f"{parent} cache · regenerates · {age}"
    if category=='python': return parent+' cache · re-downloaded on install · '+age
    if category=='desktop':
        if parent=='shadercache': return 'Steam shader cache · '+steam_name(name)+' · rebuilt while playing · '+age
        return ('Mesa shader cache' if 'mesa' in s else 'thumbnail cache')+' · regenerates · '+age
    if category=='builds':
        profiles = sorted(d.name for d in os.scandir(path) if d.is_dir() and not d.name.startswith('.'))
        kind = 'dev build' if 'debug' in profiles else 'release build' if 'release' in profiles else 'build'
        if not (path/'.rustc_info.json').exists() and any(cargo_target(path/d) for d in profiles):
            return f"CI build workspace of {path.parent.name} · {len(profiles)} targets · CI rebuilds · {age}"
        return f"Cargo target of {path.parent.name} · {kind} ({', '.join(profiles[:4])}) · cargo build recreates · {age}"
    if category=='logs':
        if path.is_dir(): return f"log folder of {parent} · {item['files']} files · {age}"
        return ('rotated' if re.search(r'\.log\.|\.old$',name) else 'current')+f' log file of {parent} · {age}'
    if category=='packages': return 'built package archive · reinstall re-downloads · '+age
    if category=='temp':
        if 'actions-runners' in s: return f'superseded GitHub runner version · {age}'
        return ('folder' if path.is_dir() else 'file')+f' in {path.parent} · {age}'
    if category=='trash': return ('trashed folder' if path.is_dir() else 'trashed file')+' · '+age
    return ''

def inventory(path, live, c, category):
    """Any uncertainty protects the entire unit; never follows symlinks/mounts."""
    rootstat = path.lstat()
    total, latest, count = 0, 0, 0
    inodes = {}
    digest = hashlib.sha256()
    uid = os.getuid()
    reason = 'Pinned / keep marker' if kept(path, c) else ''
    if path.resolve() != path:
        reason = 'Symlink ancestor; protected'
    if live['errors']:
        reason = 'Process visibility incomplete; cleanup blocked'
    if any(under(p, str(path)) for p in live['paths']):
        reason = 'In use: open file, binary, working directory or argument'
    # Tool-wide guards cover lazily opened cache files, beyond open descriptors.
    if category not in ('builds','worktrees') and any(n.lower() in TOOLS[category] for n in live['names']):
        reason = 'Protected while related tools are running'
    if category=='builds' and (path.parent/'Cargo.toml').exists() and any(under(p,str(path.parent)) for p in live['paths']):
        reason = 'Project is in use'
    extra = {}
    if category == 'worktrees':
        wreason, extra['detail'], extra['main'], extra['state'] = worktree_state(path)
        reason = reason or wreason
        old = PREVIOUS.get(str(path))
        if reason and old and old.get('state') == extra['state']:
            return old | extra | {'reason':reason,'eligible':False}
    root = str(path)
    def note(p, s):
        nonlocal reason, latest, count
        if s.st_dev != rootstat.st_dev or (stat.S_ISDIR(s.st_mode) and os.path.ismount(p)):
            reason = 'Contains a mount; protected'
            return False
        if s.st_uid != uid:
            reason = 'Not entirely owned by this user'
        if (s.st_dev,s.st_ino) in live['refs']:
            reason = 'In use: open file, mapped binary or working directory'
        name = os.path.basename(p)
        if name in ('.keepbuild','.keepstorage'):
            reason = 'Contains a keep marker'
        link, isdir = stat.S_ISLNK(s.st_mode), stat.S_ISDIR(s.st_mode)
        if not link and not isdir and not stat.S_ISREG(s.st_mode):
            reason = 'Contains sockets or special files'
        entry=inodes.setdefault((s.st_dev,s.st_ino),[s.st_blocks*512,0,s.st_nlink if stat.S_ISREG(s.st_mode) else 1])
        entry[1]+=1
        latest = max(latest, s.st_mtime, s.st_ctime)
        count += 1
        digest.update(os.fsencode(p[len(root)+1:] or '.'))
        digest.update(str((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode)).encode())
        return isdir and not link  # rmtree unlinks symlink entries and never follows their targets.
    stack = [root] if note(root, rootstat) else []
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in sorted(it, key=lambda e: e.name):
                if note(e.path, e.stat(follow_symlinks=False)):
                    stack.append(e.path)
    total=sum(v[0] for v in inodes.values())
    reclaimable=sum(v[0] for v in inodes.values() if v[1]>=v[2])
    item = {'id':key(path),'path':str(path),'category':category,'bytes':total,'reclaimable':reclaimable,'files':count,
            'latest':latest,'fingerprint':digest.hexdigest(),'reason':reason,'eligible':not reason} | extra
    item.setdefault('detail', describe(path, category, item))
    return item

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
      'javascript':[HOME/'.npm/_cacache',HOME/'.bun/install/cache',HOME/'.local/share/pnpm/store',HOME/'.cache/pnpm',HOME/'.npm/_npx',HOME/'.cache/node-gyp'],
      'python':[HOME/'.cache/pip',HOME/'.cache/uv'],
      'rust':[HOME/'.cache/sccache',HOME/'.cache/go-build',HOME/'.cargo/registry/cache',HOME/'.cargo/registry/src'],
      'desktop':[HOME/'.cache/thumbnails',HOME/'.cache/mesa_shader_cache',HOME/'.local/share/Steam/steamapps/shadercache'],
      'apps':[HOME/'.config/google-chrome/OptGuideOnDeviceModel',HOME/'.cache/google-chrome',HOME/'.cache/google-chrome-headless',
              HOME/'.cache/google-chrome-for-testing-headless',HOME/'.cache/zen',HOME/'.cache/spotify',HOME/'.cache/winetricks',
              HOME/'.cache/thunderbird',HOME/'.cache/codex-desktop',HOME/'.prime/agent/session-artifacts',HOME/'.cache/buffr/capture',
              HOME/'.BitwigStudio/plugin-undo',HOME/'.BitwigStudio/cache',
              *HOME.glob('.var/app/*/cache')]}
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
            for clone in base.iterdir():
                for n in ('src','pkg'):
                    if (clone/n).is_dir() and not (clone/n).is_symlink() and not clone.is_symlink(): yield clone/n, 'packages'
            for directory, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if d not in EXCLUDE and d not in ('src','pkg')
                           and not Path(directory,d).is_symlink()]
                for f in files:
                    if ('.pkg.tar.' in f or f.endswith(('.tar.gz','.tar.xz','.tar.zst','.zip','.deb','.rpm','.AppImage'))) and not f.endswith(('.sig','.part','.tmp')):
                        yield Path(directory,f), 'packages'
    for base in LOG_BASES:
        if not base.is_dir(): continue
        for app in base.iterdir():
            if app.is_symlink() or not app.is_dir(): continue
            for name in ('logs','log','Logs'):
                if (app/name).is_dir() and not (app/name).is_symlink(): yield app/name, 'logs'
            for f in app.iterdir():
                if LOG_NAME.search(f.name) and f.is_file() and not f.is_symlink(): yield f, 'logs'
    for d in (HOME/'.npm/_logs', HOME/'.t3/userdata/logs', *HOME.glob('actions-runners/*/_diag')):
        if d.is_dir() and not d.is_symlink(): yield d, 'logs'
    for r in HOME.glob('actions-runners/*'):  # runner self-update leaves the previous bin.X/externals.X behind
        if (r/'bin').is_symlink():
            cur = Path(os.readlink(r/'bin')).name.split('.',1)[-1]
            for d in (*r.glob('bin.*'), *r.glob('externals.*')):
                if d.is_dir() and not d.is_symlink() and d.name.split('.',1)[-1] != cur: yield d, 'temp'
    for p in HOME.glob('.codex/*target*'):
        if p.is_dir() and not p.is_symlink() and cargo_target(p): yield p, 'builds'
    for base in DEV_ROOTS:
        if not base.is_dir():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            p = Path(directory)
            if '.git' in dirs:
                yield p, 'repo'  # pseudo-category: scan() expands it into 'branches' items
            elif '.git' in files and not (p/'.git').is_symlink():
                try: gitdir = (p/'.git').read_text()
                except (OSError,UnicodeError): gitdir = ''
                if gitdir.startswith('gitdir: ') and '/.git/worktrees/' in gitdir:
                    yield p, 'worktrees'  # keep walking: build outputs inside are separate items
            dirs[:] = [d for d in dirs if d not in EXCLUDE and not (p/d).is_symlink()]
            if str(p) not in seen and (cargo_target(p) or (p.name.startswith('target') and any(cargo_target(p/d) for d in dirs))):
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
    PREVIOUS.update({x['path']:x for x in read(STATE/'report.json',{}).get('items',[]) if x['category']=='worktrees'})
    try:
        system=json.loads(subprocess.check_output(['/usr/bin/sudo','-n','/usr/local/libexec/smart-storage-system','preview'],text=True,timeout=30))
        write(STATE/'system.json',system)
    except (subprocess.SubprocessError,ValueError):
        pass
    def one(p, category):
        try:
            return branch_items(p) if category=='repo' else [inventory(p, live, c, category)]
        except (OSError,ValueError,subprocess.SubprocessError) as e:
            return [] if category=='repo' else [{'id':key(p),'path':str(p),'category':category,'bytes':0,'files':0,'latest':0,
                          'detail':'','reason':'Unreadable or changed: '+type(e).__name__,'eligible':False}]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for rows in pool.map(lambda a: one(*a), list(candidates())):
            items += rows
    items.sort(key=lambda x:(x['bytes'],x['latest']), reverse=True)
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

def stage(report, permanent=False, rules=None, ids=None):
    if time.time()-report['at'] > 1800:
        raise ValueError('Preview expired. Scan again before cleaning.')
    c, records, result = (rules or config()), recovery(), {'staged':0,'deleted':0,'bytes':0,'skipped':0,'errors':[]}
    allowed = {str(p):cat for p,cat in candidates()}
    live = probe()
    for old in report['items']:
        if ids is not None:
            if old['id'] not in ids: continue
        elif not old['eligible'] or old['category'] not in c['categories']:
            continue
        if c['age_days'] and time.time()-old.get('latest',0) < c['age_days']*DAY:
            result['skipped'] += 1
            continue
        p = Path(old['path'])
        try:
            if old['category']=='branches':
                now = next((b for b in branch_items(Path(old['repo'])) if b['branch']==old['branch']),None)
                if not now or not now['eligible'] or now['fingerprint'] != old['fingerprint']:
                    result['skipped'] += 1
                    continue
                r = git('branch','-d',old['branch'],cwd=old['repo'])
                if r.returncode: raise ValueError(r.stderr.strip() or 'git branch -d failed')
                result['deleted'] += 1
                continue
            if allowed.get(str(p)) != old['category'] or not p.exists():
                result['skipped'] += 1
                continue
            now = inventory(p,live,c,old['category'])
            if not now['eligible'] or now['fingerprint'] != old['fingerprint']:
                result['skipped'] += 1
                continue
            if old['category']=='worktrees':
                # Always permanent: git refuses unclean trees itself, a second guard after inventory().
                r = git('worktree','remove',str(p),cwd=now['main'])
                if r.returncode: raise ValueError(r.stderr.strip() or 'git worktree remove failed')
                git('worktree','prune',cwd=now['main'])
                result['deleted'] += 1
                result['bytes'] += now['bytes']
                continue
            logdir = stat.S_IMODE(p.lstat().st_mode) if old['category']=='logs' and p.is_dir() else None
            if permanent:
                if p.is_dir(): shutil.rmtree(p)
                else: p.unlink()
                if logdir is not None: p.mkdir(mode=logdir)
                if old['category']=='trash' and p.parent.name=='files':
                    info=p.parent.parent/'info'/(p.name+'.trashinfo')
                    if info.is_file() and not info.is_symlink(): info.unlink()
                result['deleted'] += 1
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
            if logdir is not None: p.mkdir(mode=logdir)
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

def png(size, rows):
    def chunk(t, d): return struct.pack('>I',len(d))+t+d+struct.pack('>I',zlib.crc32(t+d)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,6,0,0,0))
            +chunk(b'IDAT',zlib.compress(b''.join(b'\x00'+r for r in rows),6))+chunk(b'IEND',b''))

def pie_png(groups):
    """Donut of bytes per category as a cached PNG for ui.image (no chart primitive in the plugin UI)."""
    slices = [(g['bytes'],g['color']) for g in groups if g['bytes']>0]
    if not slices: return ''
    out = STATE/('pie-'+hashlib.sha256(json.dumps(slices).encode()).hexdigest()[:12]+'.png')
    if out.exists(): return str(out)
    N, SS = 200, 2
    size = N*SS; cx = size/2; R = cx-SS; r0 = R*0.6
    total = sum(b for b,_ in slices); acc = 0.0; bounds = []
    for b,col in slices:
        acc += b/total; bounds.append((acc, bytes.fromhex(col.lstrip('#'))))
    rows = []
    for y in range(N):
        row = bytearray()
        for x in range(N):
            r=g=b=h=0
            for sy in range(SS):
                for sx in range(SS):
                    px, py = x*SS+sx+0.5-cx, y*SS+sy+0.5-cx
                    d = math.hypot(px,py)
                    if r0 <= d <= R:
                        frac = (math.atan2(py,px)/(2*math.pi)+0.25) % 1.0
                        col = next((col for bound,col in bounds if frac <= bound), bounds[-1][1])
                        r+=col[0]; g+=col[1]; b+=col[2]; h+=1
            row += bytes((r//h, g//h, b//h, 255*h//(SS*SS))) if h else b'\0\0\0\0'
        rows.append(bytes(row))
    for old in STATE.glob('pie-*.png'): old.unlink()
    out.write_bytes(png(N, rows))
    return str(out)

def aged(items, c):
    if not c['age_days']: return items
    cut = time.time()-c['age_days']*DAY
    return [x | {'reason':'Recently changed','eligible':False} if x['eligible'] and x.get('latest',0) > cut else x for x in items]

def shade(hexcolor, k):
    """Mix toward white (k>0) or black (k<0) so four theme colors cover every category."""
    rgb = bytes.fromhex(hexcolor.lstrip('#'))
    return '#'+''.join(f'{int(v+(255-v)*k) if k>0 else int(v*(1+k)):02x}' for v in rgb)

def summary(report):
    c = config()
    theme = read(HOME/'.config/noctalia/colors.json',{})
    palette = [shade(theme.get(name,'#8899aa'),k) for k in (0,-0.35,0.4) for name in COLORS]
    items = aged(report.get('items',[]), c)
    groups, shown, per = [], [], {}
    for i,(cat,label) in enumerate(LABELS.items()):
        rows = [x for x in items if x['category']==cat]
        groups.append({'id':cat,'label':label,'bytes':sum(x['bytes'] for x in rows),'color':palette[i],
                      'eligible':sum(x.get('reclaimable',x['bytes']) for x in rows if x['eligible']),
                      'count':len(rows),'enabled':cat in c['categories']})
    for x in items:  # ponytail: 60 rows per category; paginate when someone actually has more
        n = per.get(x['category'],0)
        if n < 60: shown.append(x); per[x['category']] = n+1
    return {'at':report.get('at',0),'config':c,'disks':disks(),'groups':groups,'pie':pie_png(groups),
            'items':shown,'item_count':len(items),
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
    parser.add_argument('--kind',choices=['packages','packages-all','snapshots','temp','journal'])
    parser.add_argument('--mode',choices=['stage','delete'])
    parser.add_argument('--age',type=int,choices=[0,1,7,30,90])
    parser.add_argument('--schedule',type=int,choices=[0,1,7,30])
    parser.add_argument('--categories')
    parser.add_argument('--ids')
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
            for name in ('age','schedule','categories','path','mode','kind','ids'):
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
            stage(report,permanent=args.action=='delete',ids=set(args.ids.split(',')) if args.ids else None)
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
