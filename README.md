# Smart Storage

Installed locally for Noctalia v5. No repository checkout is involved.

- **Clean**: choose cache categories, review eligible bytes, then delete permanently or move to recovery.
- **All disks**: scan every storage mount and drill into directories. Kernel pseudo-filesystems are listed by the backend but are not recursively scanned. Native `du` stays on each filesystem; Btrfs snapshots are handled in System.
- **Files**: inspect the largest candidates and why an item is protected. The full inventory is in `~/.local/state/smart-storage/report.json`.
- **System**: graphical `pkexec` authorization for package cache removal, old Snapper snapshots, or system temporary files. No password is stored. Package-cache removal never uninstalls packages. Snapshot removal keeps the newest two pre/post pairs.
- **Schedule & recovery**: choose age, daily/weekly/monthly operation, permanent deletion or seven-day recovery. Restore never overwrites existing files.

The existing daily permanent-deletion selection is active. A user systemd timer checks the selected schedule hourly; its first due run is one day after installation. Closing Noctalia does not disable the schedule. Disable automatic cleanup with **Manual**, or `systemctl --user disable --now smart-storage.timer`.

Root-owned helpers in `/usr/local/libexec/` only provide fixed read-only process/directory inspection without a password. Mutating system maintenance requires polkit authorization. No arbitrary root deletion command is exposed.

## Safety and accounting

Before each removal, the cleaner rechecks open descriptors, executable mappings, working directories, existing file arguments, selected build/cache environment paths and active Unix sockets. Changed preview items, inaccessible process information, mount boundaries, special files, keep markers and ownership mismatches are protected. `.keepstorage` and `.keepbuild` protect an entire subtree. Symlinks inside a directory are unlinked without following their targets. Hard links are deduplicated and external links are excluded from reclaim estimates.

A process snapshot cannot guarantee that a new process will not start immediately afterward, or predict a file opened later. Use keep markers for ongoing work whose files are currently closed. Direct deletion is irreversible. Regenerable categories exclude project source, installed tools, personal recordings, mail and general application data; trash is opt-in.

Displayed file sizes are estimates, not guaranteed free space. Btrfs reflinks, compression and snapshots can retain blocks after unlinking. Use filesystem free-space readings to measure actual reclamation. Snapshot sizes must not be added together.

## VM policy

Only Reims macOS and the hardware-accelerated Win11 guest remain, plus their required QEMU/runtime, firmware and driver files. Current VM-directory allocated usage is about 108.54 GB, below the 120 GB budget. The redundant guest snapshot and other test VM were removed. The active Reims firmware path and the Windows CD image target remain intact.

`vm-policy.json` and `vm-usage.json` under the state directory record the approved removals and budget. This is a cleanup budget, not a filesystem quota: required live guest disks are never truncated to enforce a limit.

## Administration

- Native CLI: `python3 ~/.local/share/noctalia/plugins/smart-storage/cleaner.py status`
- Read-only rescan: replace `status` with `scan`.
- Temporary cleanup requested for this session used an age of one day; native system cleanup also ran with active-path exclusions.
- Audit/state: `~/.local/state/smart-storage/`
- Schedule: `~/.config/systemd/user/smart-storage.{service,timer}`

The previous `storage-sweep`, `build-reaper`, `targone-sweep`, `tmp-reap` user timers and `derpcat-clean-storage` system timer were disabled to avoid overlapping automatic deletion. Their original scripts/units were preserved.

Validation: Python compilation, native plugin lint, Lua syntax check, and temporary-fixture checks for permanent deletion, link-target survival, active executable protection, stale-preview rejection and recovery restoration passed. The panel uses bounded content dimensions and a single scrolling body; it is not opened automatically.

## 2.0 — categories, worktrees, branches, pie

- New categories: application logs, git worktrees (removed only when clean and merged, via `git worktree remove`), stale branches (`git branch -d`, merged only).
- Every item carries a `detail` line (cache type, Steam game, Cargo profile, branch state).
- Per-item removal from the panel; settings changes never trigger a rescan.
- The system journal action needs the updated root helper (run once, asks for your password):

```bash
sudo install -m755 ~/.local/share/noctalia/plugins/smart-storage/system.py /usr/local/libexec/smart-storage-system
```
