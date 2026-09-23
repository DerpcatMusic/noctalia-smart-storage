#!/usr/bin/python3
"""Smart Storage: explicit disposable roots, live-use checks and reversible staging."""
import argparse, collections, concurrent.futures, fcntl, hashlib, json, multiprocessing, os, re, shutil, stat, struct, subprocess, sys, time, uuid
from pathlib import Path

HOME = Path.home()
STATE = HOME / '.local/state/smart-storage'
CONFIG = STATE / 'config.json'
REAP = STATE / 'reap.json'  # trees move_aside() renamed; reap() deletes them in the background
HELPER = '/usr/local/libexec/smart-storage-'  # root helpers, see install.sh
DAY = 86400
LABELS = {'packages':'AUR package downloads', 'javascript':'Bun / npm caches',
          'python':'Python / pip / uv caches', 'rust':'Rust / compiler caches',
          'builds':'Rust build outputs', 'desktop':'Thumbnails / shaders', 'logs':'Application logs',
          'worktrees':'Git worktrees', 'branches':'Stale git branches', 'temp':'Temporary files', 'trash':'Trash / recycle bins',
          'apps':'App caches', 'redundant':'DAW project backups'}
TOOLS = {
 'packages': {'pacman','paru','yay','shelly','makepkg','bsdtar'},
 'javascript': {'bun','npm','pnpm','yarn'},
 'python': {'pip','pip3','uv'},
 'rust': {'cargo','rustc','rust-analyzer'},  # registry readers; sccache and go-build are keyed in GUARDS
 'builds': {'cargo','rustc','clang','gcc','cc','cmake','ninja','make'},
 'desktop': {'steam','gamescope'}, 'logs': set(), 'worktrees': set(), 'branches': set(), 'temp': set(), 'trash':set(), 'apps':set(), 'redundant':set()}
GUARDS = {'sccache': set(), 'go-build': {'go'}}  # sccache treats a vanished entry as a miss
DEFAULT = {'age_days':0, 'schedule_days':0, 'categories':[x for x in LABELS if x not in ('trash','worktrees','branches','apps','redundant')], 'pins':[], 'mode':'stage',
           'roots':[]}  # roots: extra directories to walk besides home and mounted drives (config.json only)
SYSTEM = ('/boot','/efi','/usr','/var','/tmp','/root','/srv','/opt','/etc','/dev','/proc','/sys','/run','/home','/nix','/snap')
EXCLUDE = {'.git','node_modules','.venv','venv','vendor','.smart-storage-recovery'}
LOG_BASES = [HOME/'.cache', HOME/'.config', HOME/'.local/share', HOME/'.local/state']
LOG_NAME = re.compile(r'\.log(\.|$)')
COLORS = ['mPrimary','mSecondary','mTertiary','mError']
SKIP = {'.git','node_modules','.cache','.rustup','.cargo','flatpak','containers','Steam','steamapps','.var','.smart-storage-recovery',
        '.venv','venv','vendor','__pycache__','.snapshots','externals','_update','registry','toolchains','rustup','cargo','.expo',
        'Trash','$RECYCLE.BIN','$Recycle.Bin','System Volume Information','lost+found'}  # trash has its own category
INSTALLED = re.compile(r'/drive_c/(windows|ProgramData|Program Files( \(x86\))?|users/[^/]+/AppData)$')  # installed software, not user copies
BUDGET, SPLIT = 4000, 4  # entries per pool task; pieces an unfinished walk is split into
MASK = (1<<128)-1
UID = os.getuid()
PREVIOUS = {}  # last report's worktree rows; ineligible worktrees reuse their size while git state is unchanged
REFS, MOUNTS = set(), set()  # per process: in-use inodes and mount points, set by _init

def _init(refs, mounts):
    global REFS, MOUNTS
    REFS, MOUNTS = refs, mounts

def fan_out(pool, task, stack, *args):
    """Run a walk task inline, or spread each unwalked rest it returns across the pool."""
    if pool is None:
        while stack:
            part, stack = task(stack, *args)
            yield part
        return
    pending = {pool.submit(task, stack, *args)}
    while pending:
        done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
        for f in done:
            part, rest = f.result()
            yield part
            pending |= {pool.submit(task, rest[i::SPLIT], *args) for i in range(min(SPLIT, len(rest)))}

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
    if not isinstance(c['roots'], list) or any(not (isinstance(x,str) and x.startswith('/')) for x in c['roots']):
        raise ValueError('Invalid roots')
    return c

def probe():
    if not os.path.exists(HELPER+'probe'): raise ValueError('Root helpers not installed: run install.sh from the plugin directory')
    result = subprocess.run(['/usr/bin/sudo','-n',HELPER+'probe'],
                            capture_output=True, text=True, timeout=45, check=True)
    p = json.loads(result.stdout)
    def who(e):  # '1234: PermissionError' -> 'podman (1234)', so the panel can say what blocks cleanup
        pid = e.split(':')[0]
        try: return open(f'/proc/{pid}/comm').read().strip()+f' ({pid})'
        except OSError: return e
    p['errors'] = [who(e) for e in p['errors']]
    p['refs'] = {tuple(r) for r in p['refs']}
    p['mounts'] = {m['target'] for m in mounts()}
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
    try:
        with open(path/'.git') as f: locked = os.path.exists(os.path.join(path, f.read().partition('gitdir:')[2].strip(), 'locked'))
    except OSError: locked = True
    detail = f"{branch} · {'merged into '+base if merged else 'not merged'} · {idle}d since last commit · {'dirty '+str(dirty) if dirty else 'clean'} · of {Path(main).name}"
    reason = 'Locked (git worktree lock)' if locked else 'Uncommitted changes' if dirty else '' if merged else 'Not merged into '+(base or 'a default branch')
    return reason, detail, main, [head, dirty, base, locked]

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

def steam_apps():
    libs = [HOME/'.local/share/Steam', HOME/'.steam/steam']
    for f in [l/'steamapps/libraryfolders.vdf' for l in libs]:
        try: libs += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', f.read_text(errors='replace'))]
        except OSError: pass
    return sorted({l.resolve()/'steamapps' for l in libs if l.is_dir()})

_steam = {}
def steam_name(appid):
    if not _steam:
        for base in steam_apps():
            for f in (base.glob('appmanifest_*.acf') if base.is_dir() else ()):
                try: m = re.search(r'"name"\s+"([^"]*)"', f.read_text(errors='replace'))
                except OSError: continue
                if m: _steam[f.stem.split('_',1)[1]] = m.group(1)
    return _steam.get(appid, 'app '+appid)

def cargo_target(p):
    # cargo writes .rustc_info.json at the target root and .fingerprint under each profile; CI targets lack CACHEDIR.TAG
    p = Path(p)
    try:
        if not (p/'.rustc_info.json').is_file(): return False
        if (p/'CACHEDIR.TAG').read_text().startswith('Signature: 8a477f597d28d172789f06886806bc55'): return True
    except (OSError,UnicodeError): pass
    try:
        return any(e.is_dir() and os.path.isdir(os.path.join(e.path,'.fingerprint')) for e in os.scandir(p))
    except OSError: return False

def is_target(d):
    if cargo_target(d): return True
    if not os.path.basename(d).startswith('target'): return False
    with os.scandir(d) as it: return any(e.is_dir(follow_symlinks=False) and cargo_target(e.path) for e in it)

def is_worktree(d):
    g = os.path.join(d,'.git')
    try:
        if os.path.islink(g): return False
        with open(g) as f: gitdir = f.read(4096)
    except (OSError,UnicodeError): return False
    return gitdir.startswith('gitdir: ') and '/.git/worktrees/' in gitdir

def app_backup(name, siblings):
    """Only backup folders a DAW writes by itself, recognised by the project file next to them. Never files, never
    name guesses: 'Verse 1 Backup R.wav' is a vocal take, and same-size stems are not duplicates."""
    if name == 'auto-backups' and any(n.endswith('.bwproject') for n in siblings): return 'Bitwig auto-backups'
    if name == 'Backup' and 'Ableton Project Info' in siblings: return 'Ableton project backups'
    return ''

def discover(stack):
    """Pool task: walk up to BUDGET entries for repos, worktrees, cargo targets and DAW backup folders; returns the unwalked rest."""
    found, notes, n = [], {}, 0
    while stack and n < BUDGET:
        d = stack.pop()
        try:
            with os.scandir(d) as it: entries = list(it)
        except OSError: continue
        n += len(entries)+1
        names = {e.name for e in entries}
        if ('.rustc_info.json' in names or os.path.basename(d).startswith('target')) and is_target(d):
            found.append((d,'builds'))
            continue
        if '.git' in names:
            if os.path.isdir(os.path.join(d,'.git')) and not os.path.islink(os.path.join(d,'.git')): found.append((d,'repo'))  # scan() expands it into 'branches' items
            elif is_worktree(d): found.append((d,'worktrees'))  # keep walking: build outputs inside are separate items
        for e in entries:
            if e.is_symlink() or e.name.startswith('.smart-storage'): continue  # recovery vaults, trees being reaped
            p, isdir = e.path, e.is_dir(follow_symlinks=False)
            if isdir and (why := app_backup(e.name, names)):
                notes[p] = why
                continue
            # Mount points are walked as their own root (or not at all); flagged folders are still walked for builds and repos.
            if isdir and e.name not in SKIP and not e.name.startswith(('externals','.Trash')) and p not in MOUNTS and not INSTALLED.search(p):
                stack.append(p)
    return {'found':found,'notes':notes}, stack

def nested(p, notes):
    while (q := os.path.dirname(p)) != p:
        if q in notes: return True
        p = q
    return False

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
        return ('folder' if path.is_dir() else 'file')+f' in {path.parent} · {age}'
    if category=='trash': return ('trashed folder' if path.is_dir() else 'trashed file')+' · '+age
    return ''

def note(part, p, name, s, root, dev):
    """Fold one entry into a partial inventory; True for a directory to descend."""
    mode = s.st_mode
    isdir = stat.S_ISDIR(mode)
    if s.st_dev != dev or (isdir and p in MOUNTS):
        part['reason'] = 'Contains a mount; protected'
        return False
    if s.st_uid != UID:
        part['reason'] = 'Not entirely owned by this user'
    if (s.st_dev,s.st_ino) in REFS:
        part['reason'] = 'In use: open file, mapped binary or working directory'
    if name in ('.keepbuild','.keepstorage'):
        part['reason'] = 'Contains a keep marker'
    # A bound socket is in the probe's paths; an unbound one (a dead daemon's) is just an inode.
    if not isdir and not stat.S_ISLNK(mode) and not stat.S_ISREG(mode) and not stat.S_ISSOCK(mode):
        part['reason'] = 'Contains FIFOs or device files'
    if stat.S_ISREG(mode) and s.st_nlink > 1:  # hard links count once; reclaimable only when every link is inside
        part['links'].setdefault((s.st_dev,s.st_ino),[s.st_blocks*512,0,s.st_nlink])[1] += 1
    else:
        part['bytes'] += s.st_blocks*512
    part['latest'] = max(part['latest'], s.st_mtime, s.st_ctime)
    part['files'] += 1
    # Order-free fingerprint (sum of entry hashes), so pieces of one walk can run in parallel. A root folder's own
    # times change when it is moved aside; its entries carry every change inside it.
    rel = p[len(root)+1:]
    times = (s.st_size,s.st_mtime_ns,s.st_ctime_ns) if rel or not isdir else (0,0,0)
    h = hashlib.blake2b(os.fsencode(rel or '.')+struct.pack('<QQqqqI',s.st_dev,s.st_ino,*times,mode),digest_size=16)
    part['fp'] = (part['fp']+int.from_bytes(h.digest(),'little')) & MASK
    return isdir  # lstat: rmtree unlinks symlink entries and never follows their targets.

def measure(stack, root, dev):
    """Pool task: fold up to BUDGET entries of one item; returns the partial and the unwalked rest."""
    part, n = {'reason':'','bytes':0,'links':{},'latest':0,'files':0,'fp':0}, 0
    while stack and n < BUDGET:
        with os.scandir(stack.pop()) as it:
            for e in it:
                n += 1
                if note(part, e.path, e.name, e.stat(follow_symlinks=False), root, dev):
                    stack.append(e.path)
    return part, stack

def inventory(path, live, c, category, pool=None):
    """Any uncertainty protects the entire unit; never follows symlinks/mounts. A pool walks big trees in parallel."""
    rootstat = path.lstat()
    reason = 'Pinned / keep marker' if kept(path, c) else ''
    if path.resolve() != path:
        reason = 'Symlink ancestor; protected'
    if live['errors']:
        reason = 'Blocked: cannot see inside '+', '.join(live['errors'][:2])+'; runs again when it exits'
    if any(under(p, str(path)) for p in live['paths']):
        reason = 'In use: open file, binary, working directory or argument'
    # Tool-wide guards cover lazily opened cache files, beyond open descriptors.
    if category not in ('builds','worktrees') and any(n.lower() in GUARDS.get(path.parent.name, TOOLS[category]) for n in live['names']):
        reason = 'Protected while related tools are running'
    # A shell or editor sitting in the project isn't building; only a cargo/rustc working there is.
    if category=='builds' and (path.parent/'Cargo.toml').exists() and any(under(p,str(path.parent)) for p in live.get('building',live['paths'])):
        reason = 'Project is in use'
    extra = {}
    if category == 'worktrees':
        wreason, extra['detail'], extra['main'], extra['state'] = worktree_state(path)
        reason = reason or wreason
        old = PREVIOUS.get(str(path))
        if reason and old and old.get('state') == extra['state']:
            return old | extra | {'reason':reason,'eligible':False}
    _init(live['refs'], live['mounts'])
    root, dev = str(path), rootstat.st_dev
    acc = {'reason':'','bytes':0,'links':{},'latest':0,'files':0,'fp':0}
    for part in (fan_out(pool, measure, [root], root, dev) if note(acc, root, path.name, rootstat, root, dev) else ()):
        acc['reason'] = part['reason'] or acc['reason']
        acc['bytes'] += part['bytes']
        acc['files'] += part['files']
        acc['latest'] = max(acc['latest'], part['latest'])
        acc['fp'] = (acc['fp']+part['fp']) & MASK
        for k, v in part['links'].items():
            if k in acc['links']: acc['links'][k][1] += v[1]
            else: acc['links'][k] = v
    reason = acc['reason'] or reason
    links = acc['links'].values()
    item = {'id':key(path),'path':root,'category':category,'bytes':acc['bytes']+sum(v[0] for v in links),
            'reclaimable':acc['bytes']+sum(v[0] for v in links if v[1]>=v[2]),'files':acc['files'],
            'latest':acc['latest'],'fingerprint':f"{acc['fp']:032x}",'reason':reason,'eligible':not reason} | extra
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
      'desktop':[HOME/'.cache/thumbnails',HOME/'.cache/mesa_shader_cache',*[a/'shadercache' for a in steam_apps()]],
      'apps':[HOME/'.config/google-chrome/OptGuideOnDeviceModel',HOME/'.cache/google-chrome',HOME/'.cache/google-chrome-headless',
              HOME/'.cache/google-chrome-for-testing-headless',HOME/'.cache/zen',HOME/'.cache/spotify',HOME/'.cache/winetricks',
              HOME/'.cache/thunderbird',HOME/'.cache/codex-desktop',HOME/'.prime/agent/session-artifacts',HOME/'.cache/buffr/capture',
              HOME/'.BitwigStudio/plugin-undo',HOME/'.BitwigStudio/cache',
              *HOME.glob('.var/app/*/cache')]}
    def children(p, cat):
        p = p.resolve()
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if not child.name.startswith('.smart-storage') and not child.is_symlink():
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
                dirs[:] = [d for d in dirs if d not in EXCLUDE and d not in ('src','pkg') and not d.startswith('.smart-storage')
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
        source=m['source'].split('[')[0]
        identity=(source,m['fsroot'])
        # A bind of part of another mount (Steam compatdata into a library) is traversed there, never twice.
        bind=any(n['source'].split('[')[0]==source and n['fsroot']!=m['fsroot'] and under(m['fsroot'],n['fsroot']) for n in rows)
        m['scan']=m['fstype'] in ('btrfs','ext4','xfs','ntfs','ntfs3','fuseblk','vfat','exfat','tmpfs') and identity not in seen and not bind
        # /run holds live services and credentials; report its capacity, don't traverse. /run/media is removable drives.
        if under(m['target'],'/run') and not under(m['target'],'/run/media'): m['scan']=False
        seen.add(identity)
    return rows

def walk_roots(c):
    """Home, every mounted data drive and config roots; system trees are the System view's."""
    home, rows = str(HOME), mounts()
    targets = {m['target'] for m in rows}
    roots = {home, *c['roots']} | {m['target'] for m in rows if m['scan'] and m['fstype']!='tmpfs' and m['target']!='/'
             and (under(m['target'],home) or under(m['target'],'/run/media') or not any(under(m['target'],s) for s in SYSTEM))}
    # Walks skip mount points, so mounted roots stay separate; other nested roots are already covered.
    return sorted(r for r in roots if r in targets or not any(r!=x and under(r,x) for x in roots))

def walked(p, category, roots):
    """Stage-time check for walk-found items: still inside a scan root and still the same kind of thing."""
    if not any(under(p,r) and p!=r for r in roots): return False
    return is_target(p) if category=='builds' else is_worktree(p) if category=='worktrees' else category=='redundant' and os.path.isdir(p) and bool(app_backup(os.path.basename(p), os.listdir(os.path.dirname(p))))

def disks():
    """Each filesystem once (btrfs subvolumes share a device), plus /tmp when it lives in RAM. 'mounts' lists
    every mount point of the filesystem, so summary() can put each item on the disk that actually holds it."""
    result = {}
    for m in json.loads(subprocess.check_output(['/usr/bin/findmnt','--json','--list','-o','TARGET,SOURCE,FSTYPE'],text=True))['filesystems']:
        ram = m['fstype']=='tmpfs' and m['target']=='/tmp'
        if m['fstype'] not in ('btrfs','ext4','xfs','ntfs','ntfs3','fuseblk','exfat') and not ram:
            continue
        source = m['source'].split('[')[0]
        if source not in result:
            s = shutil.disk_usage(m['target'])
            result[source] = {'path':m['target'],'source':source,'total':s.total,'used':s.used,'free':s.free,'ram':ram,'mounts':[]}
        result[source]['mounts'].append(m['target'])
    return list(result.values())

def recovery():
    return read(STATE/'recovery.json',[])

def walkers(live):
    """Process pool for walks (started on first use); forkserver workers get the probe snapshot through _init."""
    return concurrent.futures.ProcessPoolExecutor(mp_context=multiprocessing.get_context('forkserver'),
                                                  initializer=_init,initargs=(live['refs'],live['mounts']))

def journal(change):
    """Read-modify-write the reap journal under its own lock: move_aside() appends, reap() removes."""
    with (STATE/'reap.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        paths = change(read(REAP,[]))
        write(REAP,paths)
        return paths

def move_aside(p):
    """Instant removal: rename beside itself for the reaper. Renamed under the journal lock, so the reaper never
    sees an entry before its move, and a crash after the move still leaves it journaled."""
    dest = str(p.parent/f'.smart-storage-deleting-{uuid.uuid4().hex}')
    def add(paths):
        p.rename(dest)
        return paths+[dest]
    journal(add)
    reap_later()  # space comes back while the rest is still being checked

def remove_tree(p):
    quiet = {'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
    if subprocess.run(['/usr/bin/rm','-rf','--one-file-system','--',p],**quiet,check=False).returncode:
        # Read-only folders (Go's module cache, some tool installs) block unlinking their entries: make ours writable, retry.
        subprocess.run(['/usr/bin/chmod','-R','u+w','--',p],**quiet,check=False)
        subprocess.run(['/usr/bin/rm','-rf','--one-file-system','--',p],**quiet,check=False)

def reap():
    """Background: delete what move_aside() renamed. One reaper at a time; a later spawn waits, then re-checks."""
    with (STATE/'reaper.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        while paths := read(REAP,[]):
            with concurrent.futures.ThreadPoolExecutor(8) as threads:
                threads.map(remove_tree,[p for p in paths if os.path.basename(p).startswith('.smart-storage-deleting-')])  # only ever what move_aside() made
            left = journal(lambda j: [p for p in j if os.path.lexists(p)])
            if set(paths) <= set(left): return  # nothing removable this pass; the next spawn retries

def reap_later():
    """Start the background remover unless one is running. Detached, so closing the panel never stops it."""
    with (STATE/'reaper.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return  # a running reaper re-reads the journal until it is empty
    subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'reap'],start_new_session=True,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def forget(report, result):
    """Drop what was just removed, and anything inside it, from the report instead of rescanning."""
    ids = set(result['removed'])
    gone = {x['path'] for x in report['items'] if x['id'] in ids}
    report['items'] = [x for x in report['items'] if x['path'] not in gone and not nested(x['path'],gone)]
    write(STATE/'report.json',report)
    return report

def scan():
    c, live, items = config(), probe(), []
    PREVIOUS.update({x['path']:x for x in read(STATE/'report.json',{}).get('items',[]) if x['category']=='worktrees'})
    def one(p, category, why=''):
        try:
            if category=='repo': return branch_items(Path(p))
            item = inventory(Path(p), live, c, category, procs)
            if why:  # a name heuristic: only worth suggesting when it frees real space (skips 2FA "backup codes", lock files)
                if item['bytes'] < 1<<20: return []
                item['detail'] = f"{why} · {idle_days(item['latest'])}d since last change"
            return [item]
        except (OSError,ValueError,subprocess.SubprocessError) as e:
            return [] if category in ('repo','redundant') else [{'id':key(p),'path':str(p),'category':category,'bytes':0,'files':0,'latest':0,
                          'detail':'','reason':'Unreadable or changed: '+type(e).__name__,'eligible':False}]
    # Processes do the walking (no GIL); threads wait on them and on git. Items start measuring while the walk still runs.
    with walkers(live) as procs, concurrent.futures.ThreadPoolExecutor(32) as threads:
        preview = threads.submit(subprocess.check_output,['/usr/bin/sudo','-n',HELPER+'system','preview'],text=True,timeout=30)
        jobs, notes, repos = [threads.submit(one,p,cat) for p,cat in candidates()], {}, set()
        for part in fan_out(procs, discover, walk_roots(c)):
            jobs += [threads.submit(one,p,cat) for p,cat in part['found']]
            repos |= {p for p,cat in part['found'] if cat in ('repo','worktrees')}
            notes |= part['notes']
        # Inside a work tree, backups are git's business, not ours.
        jobs += [threads.submit(one,p,'redundant',why) for p,why in notes.items() if not nested(p,notes) and not nested(p,repos)]
        for job in jobs: items += job.result()
        try: write(STATE/'system.json',json.loads(preview.result()))
        except (subprocess.SubprocessError,ValueError): pass
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
    c, records, result = (rules or config()), recovery(), {'staged':0,'deleted':0,'bytes':0,'skipped':0,'errors':[],'removed':[]}
    allowed, roots = {str(p):cat for p,cat in candidates()}, walk_roots(c)
    live, todo = probe(), []
    for old in report['items']:
        if ids is not None:
            if old['id'] not in ids: continue
        elif not old['eligible'] or old['category'] not in c['categories']:
            continue
        if c['age_days'] and time.time()-old.get('latest',0) < c['age_days']*DAY:
            result['skipped'] += 1
            continue
        todo.append(old)
    def verify(old):
        if old['category']=='branches':
            return next((b for b in branch_items(Path(old['repo'])) if b['branch']==old['branch']),None)
        p = Path(old['path'])
        if not p.exists() or (allowed.get(str(p)) != old['category'] and not walked(str(p),old['category'],roots)):
            return None
        # Small items are quicker inline than through the pool.
        return inventory(p,live,c,old['category'],procs if old.get('files',0) > 4*BUDGET else None)
    # Every item is re-checked in parallel against one probe snapshot; removals are renames, so instant.
    with walkers(live) as procs, concurrent.futures.ThreadPoolExecutor(16) as threads:
        for old, check in [(old, threads.submit(verify,old)) for old in todo]:
            p = Path(old['path'])
            try:
                now = check.result()
                if not now or not now['eligible'] or now['fingerprint'] != old['fingerprint']:
                    result['skipped'] += 1
                    continue
                if old['category']=='branches':
                    r = git('branch','-d',old['branch'],cwd=old['repo'])
                    if r.returncode: raise ValueError((r.stderr.strip().splitlines() or ['git branch -d failed'])[0])  # drop git's multi-line -D hint
                    result['deleted'] += 1
                elif old['category']=='worktrees':
                    # Always permanent. worktree_state() required clean, merged and unlocked; prune drops git's record.
                    move_aside(p)
                    git('worktree','prune',cwd=now['main'])
                    result['deleted'] += 1
                elif permanent:
                    logdir = stat.S_IMODE(p.lstat().st_mode) if old['category']=='logs' and p.is_dir() else None
                    move_aside(p)
                    if logdir is not None: p.mkdir(mode=logdir)
                    if old['category']=='trash' and p.parent.name=='files':
                        info=p.parent.parent/'info'/(p.name+'.trashinfo')
                        if info.is_file() and not info.is_symlink(): info.unlink()
                    result['deleted'] += 1
                else:
                    logdir = stat.S_IMODE(p.lstat().st_mode) if old['category']=='logs' and p.is_dir() else None
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
                    # A folder's fingerprint survives the move (root times are left out); a file's ctime does not.
                    record['fingerprint'] = now['fingerprint'] if dest.is_dir() else inventory(dest,live,c,old['category'])['fingerprint']
                    write(STATE/'recovery.json',records)
                    result['staged'] += 1
                result['bytes'] += now['bytes']
                result['removed'].append(old['id'])
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
    live = probe() if action == 'purge' and records else None
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
                check = inventory(stored,live,c | {'age_days':1},r['category'])
                if check['fingerprint'] != r.get('fingerprint'):
                    raise ValueError('Recovery content changed; restore or inspect it: '+str(p))
                if check['reason'] and check['reason'] != 'Recently changed':
                    raise ValueError(check['reason']+': '+str(p))
                # ponytail: snapshots cannot prevent a process starting just after the probe.
                # Keep staging + grace; use .keepstorage for workloads that open files later.
                move_aside(stored)
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
    palette = [shade(theme.get(name,'#8899aa'),k) for k in (0,-0.35,0.4,-0.6) for name in COLORS]
    items = aged(report.get('items',[]), c)
    drives = disks()
    where = sorted(((m,d) for d in drives for m in d['mounts']), key=lambda t: -len(t[0]))
    for d in drives: d['cats'] = {}
    groups, shown, per, why = [], [], {}, collections.defaultdict(collections.Counter)
    for x in items:
        d = next((d for m,d in where if under(x['path'],m)), None)
        ready = x.get('reclaimable',x['bytes']) if x['eligible'] else 0
        if d:  # per disk and category: [bytes, ready to free]
            x['disk'] = d['path']
            b, e = d['cats'].get(x['category'],(0,0))
            d['cats'][x['category']] = (b+x['bytes'], e+ready)
        if not x['eligible']: why[x['category']][x['reason']] += x['bytes'] or 1
        n = per.get(x['category'],0)
        if n < 60: shown.append(x); per[x['category']] = n+1  # ponytail: 60 rows per category; paginate when someone actually has more
    for i,(cat,label) in enumerate(LABELS.items()):
        rows = [x for x in items if x['category']==cat]
        groups.append({'id':cat,'label':label,'bytes':sum(x['bytes'] for x in rows),'color':palette[i],
                      'eligible':sum(x.get('reclaimable',x['bytes']) for x in rows if x['eligible']),
                      'count':len(rows),'enabled':cat in c['categories'],
                      'why':why[cat].most_common(1)[0][0] if why[cat] else ''})  # what holds most of the rest back
    return {'at':report.get('at',0),'config':c,'disks':drives,'groups':groups,
            'items':shown,'item_count':len(items),
            'eligible':sum(g['eligible'] for g in groups if g['enabled']),
            'recovery_bytes':sum(r['bytes'] for r in recovery()),'recovery_count':len(recovery()),
            'history':report.get('history',[]),'last_action':read(STATE/'last-action.json',{}),
            'process_errors':report.get('process_errors',[]),'mounts':mounts(),
            'explorer':read(STATE/'explorer.json',{}),
            'all_mounts':read(STATE/'all-mounts.json',{}),'system':read(STATE/'system.json',{})}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['scan','status','stage','purge','restore','configure','auto','start','delete','explore','all-mounts','clean-temp','system','reap'])
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
        out['pending']=len(read(REAP,[]))
        if out['pending']: reap_later()  # resumes a removal that was interrupted (killed, crash, reboot)
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
    if args.action == 'reap':  # never under the job lock: scans and deletes go on while it works
        reap()
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
            result=subprocess.run(['/usr/bin/pkexec',HELPER+'system',args.kind],
                                  capture_output=True,text=True,timeout=1200)
            write(STATE/'system-result.json',{'returncode':result.returncode,'output':result.stdout,'error':result.stderr})
            if result.returncode: raise ValueError(result.stdout or result.stderr or 'Administrator authorization cancelled')
            preview=subprocess.check_output(['/usr/bin/sudo','-n',HELPER+'system','preview'],text=True)
            write(STATE/'system.json',json.loads(preview))
            report=scan()
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
                result=subprocess.run(['/usr/bin/sudo','-n',HELPER+'usage',path],
                                      capture_output=True,text=True,timeout=1200,check=True)
                results.append(json.loads(result.stdout))
            write(STATE/('explorer.json' if args.action=='explore' else 'all-mounts.json'),
                  {'at':time.time(),'results':results})
        elif args.action in ('stage','delete'):
            if not report: raise ValueError('Scan before cleaning')
            report=forget(report,stage(report,permanent=args.action=='delete',ids=set(args.ids.split(',')) if args.ids else None))
        elif args.action in ('restore','purge'):
            recover_or_purge(args.action)
            if args.action=='restore': report=scan()
        elif args.action=='auto':
            c=config()
            last=read(STATE/'last-auto.json',{'at':0})
            if c['schedule_days'] and time.time()-last['at'] >= c['schedule_days']*DAY:
                report=scan()
                if report['process_errors']: raise ValueError('Incomplete process visibility')
                recover_or_purge('purge',aged=True)
                # Duplicates / old versions / backups are user files on every drive: suggested in the panel, never removed unattended.
                stage(report,permanent=c['mode']=='delete',rules=c|{'categories':[x for x in c['categories'] if x!='redundant']})
                reap()  # unattended: finish here, systemd ends the unit's processes when it exits
                write(STATE/'last-auto.json',{'at':time.time()})
                report=scan()
        if read(REAP,[]): reap_later()  # also resumes a removal interrupted by a crash or reboot
        print(json.dumps(summary(report),separators=(',',':')))

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'error':str(e)}))
        sys.exit(1)
