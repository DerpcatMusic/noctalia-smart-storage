#!/bin/sh
# One-time setup: root helpers (read-only, passwordless) + the hourly user timer. Asks for sudo once.
set -e
D=$(cd "$(dirname "$0")" && pwd); u=$(id -un)
for h in probe system usage; do sudo install -m755 "$D/$h.py" "/usr/local/libexec/smart-storage-$h"; done
printf '%s ALL=(root) NOPASSWD: /usr/local/libexec/smart-storage-probe, /usr/local/libexec/smart-storage-system preview, /usr/local/libexec/smart-storage-usage *\n' "$u" \
  | sudo tee /etc/sudoers.d/smart-storage >/dev/null
sudo chmod 440 /etc/sudoers.d/smart-storage && sudo visudo -cf /etc/sudoers.d/smart-storage
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/smart-storage.service <<UNIT
[Unit]
Description=Smart Storage scheduled cleanup with live-use checks
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $D/cleaner.py auto
Nice=15
IOSchedulingClass=idle
StandardOutput=null
TimeoutStartSec=1h
UNIT
cat > ~/.config/systemd/user/smart-storage.timer <<'UNIT'
[Unit]
Description=Check Smart Storage's selected cleanup schedule
[Timer]
OnCalendar=hourly
RandomizedDelaySec=5m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload && systemctl --user enable --now smart-storage.timer
echo "installed. Schedule is Manual until you pick one in Settings."
