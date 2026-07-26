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
- **Clipboard-backed paste**: the dictated text is left in the clipboard, so if
  the auto-paste misses (focus moved off the field) you can just Ctrl+V it.
- **Clear feedback**: soft start/stop beeps + a single, self-replacing desktop
  notification.
- **Optional "polish" mode**: a second push-to-talk key rewrites the dictation
  through a Claude model (fixes grammar/structure, an occasional emoji at most)
  before pasting, with its own start chime — the normal key stays verbatim.
- **Mic conditioning** (on by default, needs `ffmpeg`): the recording is
  captured from the card's cleaner 48 kHz altset and run through a gentle
  ffmpeg chain — high-pass, mild denoise, speech leveling (louder & clearer),
  limiter — before transcription. It touches only the dictation buffer, never
  the system mic, so calls and other apps are unaffected.

## ⚙️ How it works

```
hold key ──▶ pw-record (48 kHz stereo WAV)
release  ──▶ ffmpeg (high-pass · denoise · level · limiter → 16 kHz mono)
         ──▶ faster-whisper (Russian, VAD) ──▶ text
         ──▶ wl-copy + ydotool Ctrl+V ──▶ focused window (text kept in clipboard)
```

- The key is captured with **evdev** (raw kernel input) and its real state is
  read via `active_keys()` (`EVIOCGKEY`), so a missed press/release can never
  leave recording stuck.
- Text is inserted via **clipboard + `ydotool` Ctrl+V**. A real clipboard-free
  Unicode "type" isn't possible on KWin (no `virtual_keyboard_manager_v1`, no
  `input-method-v2`), so the text is left in the clipboard as a fallback: if the
  auto-paste lands in the wrong place, just Ctrl+V it yourself.
- Pick a **non-printing** PTT key (a modifier or a spare key). Keys are not
  grabbed, so a letter key would leak into the focused app — `Right Ctrl` is the
  sensible default (like Discord PTT).

There's also a legacy **toggle** variant (`voice-dictate.sh` +
`transcribe_daemon.py`, press once to start / again to stop) bound to a KDE
global shortcut — kept as a fallback.

## ✨ Polish mode (optional)

A second push-to-talk key rewrites the dictation before pasting — fixes grammar
and punctuation, tidies rambling phrasing, and adds an occasional emoji at most —
while the normal key still pastes the raw transcription. Great for chat and notes; keep
the verbatim key for terminals, code, and search boxes.

```
hold polish key ──▶ pw-record ──▶ faster-whisper ──▶ Claude (rewrite) ──▶ paste
```

- Give it a **dedicated key** via `VD_POLISH_KEY` (a spare key held on its own —
  a shared-key chord like Ctrl+Shift is fiddly because of press ordering).
  Multiple keys are allowed (comma-separated), so each keyboard can use a key it
  actually has.
- The rewrite reuses the **Claude Code CLI's** OAuth token from
  `~/.claude/.credentials.json` (so you need Claude Code installed and logged in)
  — no separate API key. Model defaults to `claude-haiku-4-5` (fast, ~2 s).
- If the token is stale it pastes the **raw** text and shows a hint, so a
  dictation is never lost.
- A distinct start chime (`polish_start.wav`) tells you which mode you're in.

> Some keys (a laptop vendor key, the Menu/Compose key) fire an OS action or
> emit a modifier combo. If that gets in the way, remap the physical key to an
> inert one (e.g. `F24`) with a udev `hwdb` rule and point `VD_POLISH_KEY` at it.

## 🎚️ Mic conditioning (optional, on by default)

Cheap USB mics are quiet and noisy. But route dictation through the *system*
default source (an EasyEffects/RNNoise chain) and you'll fight its VAD gate,
which clips word starts and tanks accuracy. So the cleanup is done **in-process
on the recorded buffer** instead — the system mic stays raw and calls/other apps
are untouched.

On key release the recording is passed through one `ffmpeg` filter chain:

```
highpass=f=90        → cut sub-90 Hz rumble & handling noise
afftdn=nr=12:nf=-25  → gentle FFT denoise (≈no-op in a quiet room, helps when noisy)
speechnorm=…         → raise the level (the "louder & clearer" win), capped so
                       silence/pauses are NOT amplified into loud hiss
alimiter=…           → catch peaks so nothing clips
                     → resampled to whisper's 16 kHz mono
```

It's all best-effort: if `ffmpeg` is missing, the chain errors, or you set
`VD_CLEAN=0`, dictation falls back to the plain raw 16 kHz capture — nothing is
ever lost. Tune or replace the chain with `VD_CLEAN_FILTER` (e.g. swap `afftdn`
for `arnndn=m=/path/to/model.rnnn` to use the RNNoise **AI** denoiser once you
have a `.rnnn` model).

## 📦 Requirements

- Linux, Wayland (developed on KDE Plasma 6)
- Python 3.10+
- `pipewire`/`pulseaudio` (`pw-record`, `parecord`, or `arecord`)
- `ffmpeg` — for mic conditioning (optional; `VD_CLEAN=0` runs without it)
- [`ydotool`](https://github.com/ReimuNotMoe/ydotool) + `ydotoold` running
- `wl-clipboard` (`wl-copy`)
- `libnotify` (`notify-send`), `pulseaudio-utils`/`pipewire` (`paplay`)
- Read access to `/dev/input/event*` (be in the `input` group)

## 🚀 Install

```bash
git clone https://github.com/steveast/voice-dictate
cd voice-dictate
./install.sh   # runs in place — clone wherever you like
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
| `VD_MIN_MS` | `250` | ignore presses shorter than this |
| `VD_MAX_SEC` | `180` | safety cap on a single recording |
| `VD_PROMPT` | *(empty)* | `initial_prompt` to bias recognition toward your vocabulary (names, jargon, tickers). **Max 223 tokens** — see below |
| `VD_BEAM` | `5` | decoding beam size; `1` = greedy (faster, slightly less accurate) |
| `VD_TRAILING` | *(one space)* | text appended after each dictation so phrases don't glue together; `\n`/`\t` honoured, empty to disable |

#### The 223-token prompt budget

Whisper reserves `max_length // 2 - 1` = **223 tokens** for the prompt, and
faster-whisper keeps the **tail** of anything longer — the vocabulary at the
front is dropped with no error and no log line. The symptom is indistinguishable
from the model simply mishearing those words.

The daemon logs the count on startup (`VD_PROMPT: 153/223 tokens`) and warns
loudly on overflow, naming the terms it had to drop.

Because the budget is tight, spend it on words that actually get misheard.
Common anglicisms whisper already knows (`докер`, `лог`, `кэш`, `React`,
`PostgreSQL`) are wasted tokens; project-specific identifiers and proper nouns
(`book_ticker`, `BTCUSDT`, `msx`, `материализатор`) are what the bias is for.
Note that the prompt only helps words that are *in* it, so every wasted token is
a term you dropped.

### Mic conditioning

| Variable | Default | Meaning |
|---|---|---|
| `VD_CLEAN` | `1` | master switch; `0` (or no `ffmpeg`) = raw 16 kHz capture, no post-processing |
| `VD_REC_RATE` | `48000` | capture sample rate for the conditioned path (the card's cleaner altset) |
| `VD_REC_CH` | `2` | capture channels for the conditioned path |
| `VD_CLEAN_FILTER` | *(tuned chain)* | ffmpeg `-af` chain: high-pass · denoise · speech-level · limiter. Empty = resample only. Swap `afftdn` → `arnndn=m=<model.rnnn>` for RNNoise AI denoise |

### Polish mode

| Variable | Default | Meaning |
|---|---|---|
| `VD_POLISH_KEY` | *(empty)* | dedicated key(s) for the polish hold; comma-separated for multiple keyboards; empty disables polish |
| `VD_POLISH_MOD` | `Ctrl+Shift` | extra modifier groups (used only when no dedicated key is set) |
| `VD_POLISH_MODEL` | `claude-haiku-4-5` | Claude model for the rewrite |
| `VD_POLISH_PROMPT` | *(built-in)* | system prompt steering the rewrite |
| `VD_POLISH_START_WAV` | `polish_start.wav` | distinct start beep for polish mode |
| `VD_POLISH_CREDS` | `~/.claude/.credentials.json` | Claude Code OAuth token source |
| `VD_POLISH_TIMEOUT` | `20` | seconds to wait on the API before falling back to raw |

## 🩺 Troubleshooting

- **`no keyboard exposes …`** → you lack read access to `/dev/input` (see install step 1).
- **Nothing pastes** → `ydotoold` isn't running or lacks `/dev/uinput` access (install step 2).
- **The PTT key types a character** → you picked a printing key; use a modifier/spare key.
- **Cold start ~35 s once** → first model load; it stays warm afterwards.

## 📄 License

MIT — see [LICENSE](LICENSE).
