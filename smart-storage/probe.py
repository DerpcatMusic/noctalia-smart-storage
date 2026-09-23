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
    refs, paths, names, errors, building = set(), set(), set(), [], set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal() or int(proc.name) == os.getpid():
            continue
        try:
            if (proc/'stat').read_text().rpartition(')')[2].split()[0] in ('Z','X'):
                continue
            comm = (proc / 'comm').read_text().strip()
            names.add(comm)
            hidden = []
            def reference(p):
                try:
                    s = p.stat()
                    refs.add((s.st_dev, s.st_ino))
                    target = os.readlink(p)
                    if target.startswith('/'):
                        paths.add(target.removesuffix(' (deleted)'))
                except (FileNotFoundError, ProcessLookupError):
                    pass
                except PermissionError:
                    hidden.append(p)
            for kind in ('cwd', 'exe', 'root'):
                reference(proc / kind)
            for fd in (proc / 'fd').iterdir():
                reference(fd)
            if hidden:
                paths.update(mount_paths(proc))
            if comm in BUILDERS and not hidden:
                building.add(os.readlink(proc/'cwd'))
            for line in (proc / 'maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) >= 5 and int(fields[4]):
                    major, minor = (int(n, 16) for n in fields[3].split(':'))
                    refs.add((os.makedev(major, minor), int(fields[4])))
                    if len(fields) == 6 and fields[5].startswith('/'):
                        paths.add(fields[5].removesuffix(' (deleted)'))
            # Only these cache/build path variables are observed; never emit environment contents.
            for raw in (proc/'environ').read_bytes().split(b'\0'):
                name,_,value=raw.partition(b'=')
                if name in (b'CARGO_TARGET_DIR',b'CARGO_HOME',b'npm_config_cache',b'BUN_INSTALL_CACHE_DIR',b'UV_CACHE_DIR',b'SCCACHE_DIR',b'TMPDIR'):
                    path=os.fsdecode(value)
                    if path.startswith('/') and Path(path).exists(): paths.add(str(Path(path).resolve()))
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
                        paths.add(str(p.resolve()))
                        s = p.stat()
                        refs.add((s.st_dev, s.st_ino))
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
            paths.add(fields[7])
    return {'refs': sorted(refs), 'paths': sorted(paths), 'names': sorted(names), 'errors': errors, 'building': sorted(building)}

if __name__ == '__main__':
    print(json.dumps(snapshot()))
