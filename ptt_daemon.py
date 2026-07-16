#!/usr/bin/env python
"""Push-to-talk voice dictation daemon (Wayland/KDE).

Holds a faster-whisper model resident in RAM and listens for a push-to-talk
chord on every keyboard via evdev (raw kernel input, so it works globally
regardless of the Wayland compositor):

    chord DOWN  (e.g. hold Alt, press X)  -> start recording from the mic
    chord UP    (release the main key or the modifier) -> stop, transcribe
                (Russian), and paste into the focused window.

Paste is wl-copy + ydotool Ctrl+V (KWin exposes no virtual-keyboard protocol
and ydotool's own "type" cannot emit Cyrillic). Before pasting we wait for the
physical modifier to be released, otherwise the injected Ctrl+V would combine
with a still-held Alt into Ctrl+Alt+V and not paste.

The keys are NOT grabbed, so they still pass through to apps. With a modifier
configured, the main key alone (plain "x") is left completely untouched.

Config via env:
  VOICE_DICTATE_MODEL    faster-whisper model id/size/path
                         (default: cached large-v3-turbo)
  VOICE_DICTATE_LANG     language code or "auto"           (default: ru)
  VOICE_DICTATE_COMPUTE  ctranslate2 compute type          (default: int8)
  VOICE_DICTATE_THREADS  CPU threads                       (default: all)
  VD_PTT_KEY             evdev main key name                (default: KEY_X)
  VD_PTT_MOD             modifier key name(s), comma/space separated, or empty
                         for a plain single-key hold  (default: KEY_LEFTALT,KEY_RIGHTALT)
  VD_PASTE_KEYS          ydotool key codes for paste
                         (default: Ctrl+V "29:1 47:1 47:0 29:0";
                          terminals: Ctrl+Shift+V "29:1 42:1 47:1 47:0 42:0 29:0")
  VD_MIN_MS             ignore presses shorter than this   (default: 250)
  VD_KEEP_CLIPBOARD     1 = leave dictated text in the clipboard; default 0 =
                        restore the clipboard the user had before pasting
"""
import os
import sys
import time
import wave
import signal
import selectors
import subprocess

import numpy as np
import evdev
from evdev import ecodes

BASE = os.path.dirname(os.path.abspath(__file__))
RUNDIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "voice-dictate")
WAV = os.path.join(RUNDIR, "ptt.wav")

MODEL = os.environ.get("VOICE_DICTATE_MODEL",
                       "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
LANG = os.environ.get("VOICE_DICTATE_LANG", "ru")
COMPUTE = os.environ.get("VOICE_DICTATE_COMPUTE", "int8")
THREADS = int(os.environ.get("VOICE_DICTATE_THREADS", "0")) or (os.cpu_count() or 4)
PTT_KEY = os.environ.get("VD_PTT_KEY", "KEY_X")
PTT_MOD = os.environ.get("VD_PTT_MOD", "KEY_LEFTALT,KEY_RIGHTALT")
PASTE_KEYS = os.environ.get("VD_PASTE_KEYS", "29:1 47:1 47:0 29:0").split()
MIN_MS = int(os.environ.get("VD_MIN_MS", "250"))
# Safety cap: force-stop a recording that somehow never received a key release,
# so a stuck "recording" notification can never hang indefinitely.
MAX_REC = float(os.environ.get("VD_MAX_SEC", "180"))
# Restore the previous clipboard after pasting (default). Set to 1 to instead
# leave the dictated text sitting in the clipboard.
KEEP_CLIPBOARD = os.environ.get("VD_KEEP_CLIPBOARD", "0") == "1"

START_WAV = os.path.join(BASE, "start.wav")
STOP_WAV = os.path.join(BASE, "stop.wav")


def log(*a):
    print("[vd-ptt]", *a, file=sys.stderr, flush=True)


_notif_id = {"v": None}


def notify(text, timeout=3000):
    # Reuse one notification and replace it in place (-r) so status updates
    # never pile up into a stack of bubbles.
    args = ["notify-send", "-a", "Voice Dictate", "-p", "-t", str(timeout),
            "-h", "string:x-canonical-private-synchronous:voice-dictate"]
    if _notif_id["v"]:
        args += ["-r", str(_notif_id["v"])]
    args += ["Voice Dictate", text]
    try:
        out = subprocess.run(args, capture_output=True, timeout=2)
        nid = out.stdout.decode("utf-8", "replace").strip()
        if nid.isdigit():
            _notif_id["v"] = int(nid)
    except (OSError, subprocess.TimeoutExpired):
        pass


def beep(path):
    if os.path.exists(path):
        try:
            subprocess.Popen(["paplay", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def load_wav(path):
    """Load a PCM WAV as a mono float32 array at 16 kHz."""
    with wave.open(path, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"expected 16-bit PCM WAV, got sample width {sw}")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != 16000 and a.size:
        idx = np.linspace(0.0, len(a) - 1, int(round(len(a) * 16000 / sr)))
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    return a


def find_recorder():
    for cmd in (["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"],
                ["parecord", "--rate=16000", "--channels=1", "--format=s16le",
                 "--file-format=wav"],
                ["arecord", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav"]):
        if any(os.access(os.path.join(p, cmd[0]), os.X_OK)
               for p in os.environ.get("PATH", "").split(":")):
            return cmd
    return None


def pretty_combo(mod_codes, main_code):
    def label(code):
        n = ecodes.KEY[code]
        n = n[0] if isinstance(n, (list, tuple)) else n
        n = n[4:] if n.startswith("KEY_") else n
        for side in ("RIGHT", "LEFT"):
            if n.startswith(side) and len(n) > len(side):
                return side.capitalize() + " " + n[len(side):].capitalize()
        return n.capitalize() if len(n) > 1 else n
    seen, parts = set(), []
    for c in mod_codes:
        lbl = label(c)
        if lbl not in seen:
            seen.add(lbl)
            parts.append(lbl)
    parts.append(label(main_code))
    return "+".join(parts)


class Dictation:
    def __init__(self, model, recorder):
        self.model = model
        self.recorder = recorder
        self.proc = None
        self.t0 = 0.0

    @property
    def active(self):
        return self.proc is not None

    def start(self):
        if self.proc is not None:
            return
        os.makedirs(RUNDIR, exist_ok=True)
        try:
            os.unlink(WAV)
        except OSError:
            pass
        self.t0 = time.time()
        self.proc = subprocess.Popen(self.recorder + [WAV],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        beep(START_WAV)
        notify("🎙️ Запись… (держи клавишу)", 10000)
        log("recording started")

    def stop_and_transcribe(self):
        """Stop the recorder and return the recognized text (or "")."""
        if self.proc is None:
            return ""
        held_ms = (time.time() - self.t0) * 1000
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        beep(STOP_WAV)

        if held_ms < MIN_MS:
            notify("⏱️ Слишком коротко", 1500)
            log(f"press too short ({held_ms:.0f}ms), ignored")
            return ""
        if not os.path.exists(WAV) or os.path.getsize(WAV) < 1000:
            notify("🔇 Пустая запись", 2000)
            return ""

        notify("✍️ Расшифровка…", 3000)
        try:
            audio = load_wav(WAV)
            lang = None if LANG == "auto" else LANG
            t1 = time.time()
            segments, _ = self.model.transcribe(audio, language=lang,
                                                beam_size=5, vad_filter=True)
            text = "".join(s.text for s in segments).strip()
            log(f"transcribed {audio.size / 16000:.1f}s in {time.time() - t1:.1f}s "
                f"-> {len(text)} chars")
            if not text:
                notify("🤷 Ничего не распознано", 2000)
            return text
        except Exception as e:  # noqa: BLE001
            log("transcribe error:", repr(e))
            notify("❌ Ошибка распознавания", 3000)
            return ""

    def paste(self, text):
        env = dict(os.environ)
        env.setdefault("YDOTOOL_SOCKET",
                       os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
                                    ".ydotool_socket"))

        # Save the user's current clipboard so dictation doesn't clobber it.
        # (KWin exposes no virtual-keyboard / input-method-v2 protocol, so a
        # real clipboard-free Unicode type is not possible here; the next best
        # thing is to paste and then put the old clipboard back.)
        old = None
        if not KEEP_CLIPBOARD:
            try:
                r = subprocess.run(["wl-paste", "-n"], capture_output=True, timeout=1)
                old = r.stdout if r.returncode == 0 else b""
            except (OSError, subprocess.TimeoutExpired):
                old = b""

        try:
            subprocess.run(["wl-copy"], input=text.encode("utf-8"), check=False)
        except OSError:
            log("wl-copy missing")
            return
        time.sleep(0.05)
        try:
            subprocess.run(["ydotool", "key", *PASTE_KEYS], env=env, check=False)
        except OSError:
            log("ydotool missing")
            return

        if not KEEP_CLIPBOARD:
            time.sleep(0.4)  # let the target consume the paste before restoring
            try:
                if old:
                    subprocess.run(["wl-copy"], input=old, check=False)
                else:
                    subprocess.run(["wl-copy", "--clear"], check=False)
            except OSError:
                pass


def enumerate_event_paths():
    """List /dev/input/event* robustly (list_devices can come back empty when
    directory enumeration is restricted, e.g. inside a sandbox)."""
    import glob
    paths = list(evdev.list_devices()) or sorted(glob.glob("/dev/input/event*"))
    if not paths:
        paths = [f"/dev/input/event{i}" for i in range(64)]
    return paths


def open_keyboards(main_code):
    """Return InputDevice objects for every keyboard exposing the main key."""
    devs = []
    for path in enumerate_event_paths():
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue
        caps = d.capabilities().get(ecodes.EV_KEY, [])
        if main_code in caps:
            devs.append(d)
            log(f"listening on {d.path} ({d.name})")
    return devs


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    main_code = getattr(ecodes, PTT_KEY, ecodes.KEY_X)
    mod_codes = {getattr(ecodes, m) for m in PTT_MOD.replace(",", " ").split()
                 if hasattr(ecodes, m)}
    require_mod = bool(mod_codes)
    combo = pretty_combo(sorted(mod_codes), main_code)

    recorder = find_recorder()
    if recorder is None:
        log("no recorder (pw-record/parecord/arecord) found")
        sys.exit(1)

    devs = open_keyboards(main_code)
    if not devs:
        log(f"no keyboard exposes {PTT_KEY}; check /dev/input permissions")
        sys.exit(1)

    from faster_whisper import WhisperModel
    log(f"loading model {MODEL} (compute={COMPUTE}, threads={THREADS}) ...")
    t0 = time.time()
    model = WhisperModel(MODEL, device="cpu", compute_type=COMPUTE,
                         cpu_threads=THREADS)
    log(f"model ready in {time.time() - t0:.1f}s; hold {combo} to dictate")
    notify(f"🚀 Готово — держи {combo} для диктовки", 4000)

    dictation = Dictation(model, recorder)
    sel = selectors.DefaultSelector()
    for d in devs:
        sel.register(d, selectors.EVENT_READ)

    def keys_now():
        """Ground-truth key state straight from the kernel (EVIOCGKEY). Immune
        to a missed press/release event, so the chord can never desync."""
        main_now = mod_now = False
        for d in devs:
            try:
                ak = d.active_keys()
            except OSError:
                continue
            if main_code in ak:
                main_now = True
            if mod_codes.intersection(ak):
                mod_now = True
        return main_now, mod_now

    def drain_ready(timeout):
        """Consume queued events so select() doesn't spin. The details don't
        matter — the real chord state is read from keys_now(), not from here."""
        for key, _ in sel.select(timeout):
            try:
                for _ in key.fileobj.read():
                    pass
            except OSError:
                try:
                    sel.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass

    def wait_mod_release(timeout=1.5):
        """Before pasting, wait for the modifier to be physically released, else
        the injected Ctrl+V would combine with a held Alt into Ctrl+Alt+V."""
        if not require_mod:
            return
        end = time.time() + timeout
        while time.time() < end and keys_now()[1]:
            drain_ready(0.05)

    def finish():
        text = dictation.stop_and_transcribe()
        wait_mod_release()
        if text:
            dictation.paste(text)
            notify("✅ " + text, 4000)

    # Event-driven when idle; while recording, wake every 0.5s to re-check the
    # real key state (self-heals a missed release) and enforce the safety cap.
    armed = True
    while True:
        drain_ready(0.5 if dictation.active else None)
        main_now, mod_now = keys_now()
        chord_held = main_now and (mod_now or not require_mod)

        if not chord_held:
            armed = True                      # a fresh press is required to start
        if chord_held and armed and not dictation.active:
            dictation.start()
            armed = False
        elif dictation.active and not chord_held:
            finish()
        elif dictation.active and (time.time() - dictation.t0) > MAX_REC:
            log("max recording duration reached; auto-stopping")
            finish()


if __name__ == "__main__":
    main()
