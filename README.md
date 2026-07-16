# 🎙️ voice-dictate

Push-to-talk voice dictation for **Wayland / KDE Plasma**, powered by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper). Hold a key, speak,
release — the recognized text is typed into whatever window is focused. Tuned
for **Russian** out of the box, but works with any Whisper language.

Built because on KDE Wayland the usual tricks don't work: `wtype` needs a
virtual-keyboard protocol KWin doesn't expose, and `ydotool type` can't emit
Cyrillic. This project takes a different route (see [How it works](#-how-it-works)).

## ✨ Features

- **Push-to-talk**: hold a key while speaking, release to transcribe & insert.
- **Always warm**: the Whisper model stays resident in RAM (systemd user
  service), so transcription starts instantly (~3–4 s for a phrase on CPU).
- **Global hotkey on Wayland**: the key is read straight from the kernel via
  `evdev`, so it works regardless of the compositor and in any app.
- **Non-clobbering paste**: your clipboard is saved before pasting and restored
  after.
- **Clear feedback**: soft start/stop beeps + a single, self-replacing desktop
  notification.

## ⚙️ How it works

```
hold key ──▶ pw-record (16 kHz mono WAV)
release  ──▶ faster-whisper (Russian, VAD) ──▶ text
         ──▶ wl-copy + ydotool Ctrl+V ──▶ focused window (clipboard restored)
```

- The key is captured with **evdev** (raw kernel input) and its real state is
  read via `active_keys()` (`EVIOCGKEY`), so a missed press/release can never
  leave recording stuck.
- Text is inserted via **clipboard + `ydotool` Ctrl+V**. A real clipboard-free
  Unicode "type" isn't possible on KWin (no `virtual_keyboard_manager_v1`, no
  `input-method-v2`), so the clipboard is saved and restored around the paste.
- Pick a **non-printing** PTT key (a modifier or a spare key). Keys are not
  grabbed, so a letter key would leak into the focused app — `Right Ctrl` is the
  sensible default (like Discord PTT).

There's also a legacy **toggle** variant (`voice-dictate.sh` +
`transcribe_daemon.py`, press once to start / again to stop) bound to a KDE
global shortcut — kept as a fallback.

## 📦 Requirements

- Linux, Wayland (developed on KDE Plasma 6)
- Python 3.10+
- `pipewire`/`pulseaudio` (`pw-record`, `parecord`, or `arecord`)
- [`ydotool`](https://github.com/ReimuNotMoe/ydotool) + `ydotoold` running
- `wl-clipboard` (`wl-copy`)
- `libnotify` (`notify-send`), `pulseaudio-utils`/`pipewire` (`paplay`)
- Read access to `/dev/input/event*` (be in the `input` group)

## 🚀 Install

```bash
git clone https://github.com/steveast/voice-dictate ~/.local/share/voice-dictate
cd ~/.local/share/voice-dictate
./install.sh
```

`install.sh` creates the venv, installs deps, and installs the systemd user
service. It will print the **privileged one-time steps** it can't do itself:

```bash
# 1) allow reading the keyboard (evdev) — relogin makes it permanent,
#    setfacl makes it work right now without a relogin
sudo usermod -aG input "$USER"
sudo setfacl -m 'u:'"$USER"':r' /dev/input/event*

# 2) allow ydotool to inject keys (uinput)
sudo install -m 644 udev/99-uinput.rules /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger
systemctl --user enable --now ydotool.service
```

Then enable the dictation service:

```bash
systemctl --user enable --now voice-ptt.service
```

> ⚠️ zsh eats the `:r` in `u:$USER:r` (it's a modifier) — quote it as shown.

## 🎧 Usage

Hold **Right Ctrl**, speak, release. The text appears in the focused window.

The first line of the model log confirms it's ready:

```bash
journalctl --user -u voice-ptt -f
```

## 🔧 Configuration

Set these in `systemd/voice-ptt.service` (`Environment=…`) or the shell env:

| Variable | Default | Meaning |
|---|---|---|
| `VD_PTT_KEY` | `KEY_RIGHTCTRL` | evdev key to hold ([key names](https://github.com/torvalds/linux/blob/master/include/uapi/linux/input-event-codes.h)) |
| `VD_PTT_MOD` | *(empty)* | optional modifier(s), comma-separated; empty = single-key hold |
| `VOICE_DICTATE_MODEL` | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | model id / size / path |
| `VOICE_DICTATE_LANG` | `ru` | language code, or `auto` |
| `VOICE_DICTATE_COMPUTE` | `int8` | ctranslate2 compute type |
| `VOICE_DICTATE_THREADS` | all cores | CPU threads |
| `VD_PASTE_KEYS` | `29:1 47:1 47:0 29:0` | ydotool codes for paste (Ctrl+V); for terminals use Ctrl+Shift+V: `29:1 42:1 47:1 47:0 42:0 29:0` |
| `VD_KEEP_CLIPBOARD` | `0` | `1` = leave dictated text in the clipboard instead of restoring |
| `VD_MIN_MS` | `250` | ignore presses shorter than this |
| `VD_MAX_SEC` | `180` | safety cap on a single recording |

## 🩺 Troubleshooting

- **`no keyboard exposes …`** → you lack read access to `/dev/input` (see install step 1).
- **Nothing pastes** → `ydotoold` isn't running or lacks `/dev/uinput` access (install step 2).
- **The PTT key types a character** → you picked a printing key; use a modifier/spare key.
- **Cold start ~35 s once** → first model load; it stays warm afterwards.

## 📄 License

MIT — see [LICENSE](LICENSE).
