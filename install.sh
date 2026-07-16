#!/usr/bin/env bash
# Installer for voice-dictate. Run it from wherever you cloned the repo — the
# app runs in place, and the systemd unit is generated to point here. Sets up
# the Python venv and the service, then prints the one-time privileged steps.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> voice-dictate installer"
echo "    location: $REPO"

# 1) Python venv + dependencies (in place)
if [ ! -x "$REPO/venv/bin/python" ]; then
  echo "==> creating venv at $REPO/venv"
  python3 -m venv "$REPO/venv"
fi
echo "==> installing Python dependencies"
"$REPO/venv/bin/pip" install --upgrade pip >/dev/null
"$REPO/venv/bin/pip" install -r "$REPO/requirements.txt"

# 2) systemd user service, wired to this location
echo "==> installing systemd user unit"
mkdir -p "$HOME/.config/systemd/user"
sed "s#^ExecStart=.*#ExecStart=$REPO/venv/bin/python $REPO/ptt_daemon.py#" \
    "$REPO/systemd/voice-ptt.service" \
    > "$HOME/.config/systemd/user/voice-ptt.service"
systemctl --user daemon-reload

cat <<'EOF'

==> Almost done. Run these one-time privileged steps yourself (need root):

    # allow reading the keyboard (evdev); relogin makes group membership stick,
    # setfacl grants access to the current session without a relogin
    sudo usermod -aG input "$USER"
    sudo setfacl -m 'u:'"$USER"':r' /dev/input/event*

    # allow ydotool to inject keystrokes (uinput)
    sudo install -m 644 udev/99-uinput.rules /etc/udev/rules.d/99-uinput.rules
    sudo udevadm control --reload && sudo udevadm trigger
    systemctl --user enable --now ydotool.service

==> Then start dictation (first run downloads the model, ~1.6 GB):

    systemctl --user enable --now voice-ptt.service

    Hold Right Ctrl, speak, release.  Logs: journalctl --user -u voice-ptt -f
EOF
