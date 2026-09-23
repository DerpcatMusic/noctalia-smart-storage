# Smart Storage

Finds the regenerable junk developer machines collect — package manager caches, Rust `target/` dirs, CI build workspaces, merged git worktrees, stale branches, logs, shader caches, temp files — and lets you delete it from a panel with a per-disk usage chart, per-item reasons and live-use protection. Opt-in categories add app caches and DAW project backups.

| Plugin | |
|---|---|
| id | `derpcat/smart-storage` |
| bar widget | `storage` — free space on `/`, click opens the panel |
| panel | `dashboard` |
| requires | `python3`, `sudo`, `findmnt` |
| license | MIT |

## Usage

Open the panel from the bar widget or:

```sh
noctalia msg panel-toggle derpcat/smart-storage:dashboard
```

1. **Rescan** builds the inventory: your home and every mounted data drive, walked in parallel (a few seconds warm, ~25 s cold for ~3 M files).
2. Click a category's color square to include it in Delete; click the row to see its items. Click a disk bar to see what it holds.
3. **Delete selected** removes every eligible item in the enabled categories, or **Delete** a single row. Rows vanish at once: after the live-use re-check (under a second) each item is renamed aside and deleted in the background. With the *stage* mode in Settings, items move to `.smart-storage-recovery` beside them for seven days instead.
4. **System** runs package-cache, Snapper snapshot, `/tmp` and journal cleanup through `pkexec` (graphical admin prompt, nothing stored).
5. **Settings**: minimum age, schedule (manual / daily / weekly / monthly), stage vs delete.

Every row has a detail line saying what it is and why it is safe to remove (`Cargo target of foo · debug 1.2 G · rebuilt on next build`, `older version · current is grok-1.0.40`, `same content as ~/x.zip (sampled)`).

### Categories

| Category | What it finds | Default |
|---|---|---|
| AUR package downloads | pacman/paru/yay/makepkg clone `src`/`pkg` dirs | on |
| Bun / npm caches | `~/.npm/_cacache`, `_npx`, bun, pnpm store, node-gyp | on |
| Python / pip / uv caches | `~/.cache/pip`, `~/.cache/uv` | on |
| Rust / compiler caches | sccache, go-build, cargo registry cache and extracted sources | on |
| Rust build outputs | every cargo `target/` and shared `CARGO_TARGET_DIR` on any scanned drive, CI build workspaces | on |
| Thumbnails / shaders | thumbnails, mesa shader cache, Steam shader caches (all libraries, named by game) | on |
| Application logs | `*.log` trees under `~/.cache`, `~/.config`, `~/.local`, runner `_diag` | on |
| Git worktrees | worktrees whose branch is merged, tree is clean and not `git worktree lock`ed (removed, then `git worktree prune`) | off |
| Stale git branches | merged local branches (`git branch -d`) | off |
| Temporary files | your own top-level entries in `/tmp`, `/var/tmp`, `~/tmp` | on |
| Trash | XDG trash on every scanned mount | off |
| App caches | Chrome / Zen / Spotify / Thunderbird caches, Chrome on-device AI model, flatpak app caches, Bitwig undo history | off |
| DAW project backups | Bitwig `auto-backups` folders next to a `.bwproject`, and the `Backup` folder inside an Ableton project (next to `Ableton Project Info`). Nothing else: no files, no name guesses, no duplicate or old-version matching (an earlier version matched by name and by sampled content and deleted audio stems). Nothing inside git work trees, never removed by the schedule | off |

Builds, worktrees, branches and DAW backups come from one walk of your home directory plus every mounted data drive: anything mounted outside the system tree (`/usr`, `/var`, `/boot`, …), including removable drives under `/run/media`. Bind mounts of an already-scanned tree are walked once. The walk skips caches, `node_modules`, `.git`, toolchains, Steam libraries, trash and recycle bins. Add directories that are neither in home nor on their own mount in `~/.local/state/smart-storage/config.json`:

```json
{"roots": ["/srv/work"]}
```

## Requirements

- `python3` (3.9+), `sudo`, `findmnt` (util-linux), `du`, `git`.
- Optional: `pacman`/`paru`, `snapper`, `journalctl` for the System view.
- The root helpers from `install.sh` (below). Without them the plugin refuses to scan, because the live-use check needs `/proc` of every user.

### One-time setup

```sh
sh <plugin dir>/install.sh
```

It installs three fixed read-only helpers to `/usr/local/libexec/` (`smart-storage-probe`: process snapshot; `smart-storage-usage`: `du` of one directory; `smart-storage-system preview`: sizes of package cache, snapshots, journal), a sudoers line allowing only those for your user, and a user systemd timer that checks the schedule hourly. Mutating system actions always go through `pkexec` and prompt.

## Safety

Before each removal the cleaner re-checks open descriptors, mapped executables, working directories, file arguments, build/cache environment paths and Unix sockets of every process. Anything in use, on another mount, owned by someone else, changed since the scan, or under a `.keepstorage` / `.keepbuild` marker is protected. Symlinks are unlinked without following them. Hard links are counted once. It never touches project sources, installed tools, recordings, mail or general app data; the opt-in categories are the only ones that look at user files and they only ever *suggest* — each item says why.

Sizes are logical estimates. Btrfs compression, reflinks and snapshots mean freed space can be smaller than the number shown.

## IPC

- `python3 <plugin dir>/cleaner.py scan | status | stage | delete | restore | purge | system --kind <packages|snapshots|temp|journal>` — everything the panel does, as JSON. `reap` is the background remover: deleted items are first renamed to `.smart-storage-deleting-*` beside themselves and journaled in `reap.json`, so an interrupted removal resumes on the next run.
- State and audit trail: `~/.local/state/smart-storage/` (`report.json` is the full inventory).
- Schedule: `systemctl --user disable --now smart-storage.timer` turns automatic cleanup off.

## Notes

- Processes: `sudo -n` for the three helpers, `pkexec` for system actions, `git`, `du`, `findmnt`. No network access.
- Scans use a process pool (one worker per CPU) that splits every walk, including a single huge item like a cargo registry, into pieces. Items are measured while the walk is still discovering more.
- Filesystem writes: only under `~/.local/state/smart-storage`, `.smart-storage-recovery` / `.smart-storage-deleting-*` beside the items you remove, and those items.
