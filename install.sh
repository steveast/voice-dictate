#!/usr/bin/env bash
# Installer for voice-dictate. Run it from the cloned repo directory.
# It sets up the Python venv and the systemd user service, then prints the
# one-time privileged steps it can't perform itself.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/.local/share/voice-dictate"

echo "==> voice-dictate installer"
echo "    repo:   $REPO"
echo "    target: $TARGET"

if [ "$REPO" != "$TARGET" ]; then
  echo "!!  The systemd unit expects the code at $TARGET."
  echo "    Clone/move it there, or edit systemd/voice-ptt.service accordingly."
fi

# 1) Python venv + dependencies
if [ ! -x "$TARGET/venv/bin/python" ]; then
  echo "==> creating venv at $TARGET/venv"
  python3 -m venv "$TARGET/venv"
fi
echo "==> installing Python dependencies"
"$TARGET/venv/bin/pip" install --upgrade pip >/dev/null
"$TARGET/venv/bin/pip" install -r "$REPO/requirements.txt"

# 2) systemd user service
echo "==> installing systemd user unit"
mkdir -p "$HOME/.config/systemd/user"
install -m 644 "$REPO/systemd/voice-ptt.service" \
        "$HOME/.config/systemd/user/voice-ptt.service"
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
