#!/usr/bin/python3 -I
"""Privileged read-only /proc probe. Never accepts paths, commands or arguments."""
import json, os
from pathlib import Path

def mount_paths(proc):
    """Host paths behind a process's mounts. Rootless containers hide their cwd and open files even from root,
    so a hidden process protects everything it has mounted from the host instead of blocking every cleanup."""
    host = [l.split() for l in Path('/proc/self/mountinfo').read_text().splitlines()]
    out = set()
    for f in (l.split() for l in (proc/'mountinfo').read_text().splitlines()):
        for h in host:  # same device, and the host mount's root contains the process mount's root
            if h[2] == f[2] and (f[3] == h[3] or f[3].startswith(h[3].rstrip('/')+'/')):
                out.add('/'+os.path.normpath((h[4]+'/'+f[3][len(h[3]):]).replace('\\040',' ')).lstrip('/'))
    return out

BUILDERS = {'cargo', 'rustc', 'clippy-driver', 'build-script-bu'}  # comm is cut to 15 chars

def snapshot():
    refs, paths, names, errors, building = {}, {}, set(), [], {}  # path/inode -> 'comm (pid)' of a holder
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal() or int(proc.name) == os.getpid():
            continue
        try:
            if (proc/'stat').read_text().rpartition(')')[2].split()[0] in ('Z','X'):
                continue
            comm = (proc / 'comm').read_text().strip()
            names.add(comm)
            who = f'{comm} ({proc.name})'
            hidden = []
            def reference(p):
                try:
                    s = p.stat()
                    refs.setdefault((s.st_dev, s.st_ino), who)
                    target = os.readlink(p)
                    if target.startswith('/'):
                        paths.setdefault(target.removesuffix(' (deleted)'), who)
                except (FileNotFoundError, ProcessLookupError):
                    pass
                except PermissionError:
                    hidden.append(p)
            # sccache's daemon keeps the cwd of whichever cargo first started it and never uses it.
            for kind in ('exe', 'root') if comm == 'sccache' else ('cwd', 'exe', 'root'):
                reference(proc / kind)
            for fd in (proc / 'fd').iterdir():
                reference(fd)
            if hidden:
                for m in mount_paths(proc): paths.setdefault(m, who)
            if comm in BUILDERS and not hidden:
                building.setdefault(os.readlink(proc/'cwd'), who)
            for line in (proc / 'maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) >= 5 and int(fields[4]):
                    major, minor = (int(n, 16) for n in fields[3].split(':'))
                    refs.setdefault((os.makedev(major, minor), int(fields[4])), who)
                    if len(fields) == 6 and fields[5].startswith('/'):
                        paths.setdefault(fields[5].removesuffix(' (deleted)'), who)
            # Only these cache/build path variables are observed; never emit environment contents.
            for raw in (proc/'environ').read_bytes().split(b'\0'):
                name,_,value=raw.partition(b'=')
                if name in (b'CARGO_TARGET_DIR',b'CARGO_HOME',b'npm_config_cache',b'BUN_INSTALL_CACHE_DIR',b'UV_CACHE_DIR',b'SCCACHE_DIR',b'TMPDIR'):
                    path=os.fsdecode(value)
                    if path.startswith('/') and Path(path).exists(): paths.setdefault(str(Path(path).resolve()), who)
            # Resolve only existing file arguments; never emit command lines or environments.
            args = (proc / 'cmdline').read_bytes().split(b'\0')[1:]
            for arg in args:
                value = os.fsdecode(arg)
                manager = Path(value).name
                if manager in ('npm-cli.js','pnpm.cjs','yarn.js','pip','pip3'):
                    names.add({'npm-cli.js':'npm','pnpm.cjs':'pnpm','yarn.js':'yarn'}.get(manager,manager))
                if not value or value.startswith('-'):
                    continue
                p = Path(value) if value.startswith('/') else proc / 'cwd' / value
                try:
                    if p.exists() and p.is_absolute():
                        paths.setdefault(str(p.resolve()), who)
                        s = p.stat()
                        refs.setdefault((s.st_dev, s.st_ino), who)
                except (OSError, ValueError):
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass  # Process exited during the snapshot.
        except (PermissionError, OSError) as e:
            if proc.exists():
                errors.append(proc.name + ': ' + type(e).__name__)
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields=line.split(None,7)
        if len(fields)==8 and fields[7].startswith('/'):
            paths.setdefault(fields[7], 'a socket')
    return {'refs': [[*k, w] for k, w in sorted(refs.items())], 'paths': paths, 'names': sorted(names), 'errors': errors,
            'building': building}

if __name__ == '__main__':
    print(json.dumps(snapshot()))
