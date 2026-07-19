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
  VD_PROMPT             initial_prompt to bias recognition toward your own
                        vocabulary (names, jargon, tickers)      (default: none)
  VD_BEAM               beam size; 1 = greedy (faster, slightly
                        less accurate)                           (default: 5)
  VD_TRAILING           text appended after each dictation so back-to-back
                        phrases don't glue together; \n / \t honoured
                        (default: one space; set empty to disable)

A second "polish" chord rewrites the dictation through a Claude model (fixes
grammar/structure, adds a little emoji) before pasting; the normal chord stays
verbatim. It reuses the Claude Code CLI's OAuth token (VD_POLISH_CREDS), so no
separate API key is needed; if the token is stale the raw text is pasted and a
hint is shown. Config via env:
  VD_POLISH_KEY         dedicated key(s) for the polish hold — its own push-to-
                        talk key, independent of VD_PTT_KEY (avoids the press-
                        order races of a shared-key chord). Accepts several keys,
                        comma/space separated; holding ANY of them starts polish
                        (handy when two keyboards expose different keys). Empty =
                        ride on VD_PTT_KEY + VD_POLISH_MOD instead.
                        (default: empty)
  VD_POLISH_MOD         extra modifier groups required for polish, on top of the
                        polish key: space-separated groups, comma-separated
                        alternatives within a group; every group must have a key
                        held. Polish is off only if VD_POLISH_KEY and this are
                        both empty.
                        (default: KEY_LEFTCTRL,KEY_RIGHTCTRL KEY_LEFTSHIFT,KEY_RIGHTSHIFT)
  VD_POLISH_MODEL       Claude model id            (default: claude-haiku-4-5)
  VD_POLISH_PROMPT      system prompt for the rewrite (has a sensible default)
  VD_POLISH_MAX_TOKENS  response cap               (default: 1024)
  VD_POLISH_TIMEOUT     seconds to wait on the API (default: 20)
  VD_POLISH_CREDS       Claude CLI credentials json
                        (default: ~/.claude/.credentials.json)
  VD_POLISH_START_WAV   distinct start beep for the polish chord, so you can
                        hear which mode you're in (default: polish_start.wav;
                        falls back to start.wav if missing)

The dictated text is left sitting in the clipboard after pasting (not restored),
so if the auto-paste misses — e.g. focus moved away from the field — you can just
Ctrl+V it in yourself.
"""
import os
import sys
import time
import wave
import json
import signal
import selectors
import subprocess
import urllib.request
import urllib.error

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
# Bias recognition toward a custom vocabulary (names, jargon, tickers).
PROMPT = os.environ.get("VD_PROMPT", "").strip() or None
# Decoding beam width; 1 = greedy decode (faster, marginally less accurate).
BEAM = int(os.environ.get("VD_BEAM", "5"))
# Appended after each dictation so consecutive phrases don't run together.
# Backslash escapes (\n, \t) are honoured; default is a single space.
TRAILING = os.environ.get("VD_TRAILING", " ").replace("\\n", "\n").replace("\\t", "\t")

# --- Polish mode: a second chord that rewrites the dictation through a Claude
# model before pasting (the normal chord stays verbatim). Off if POLISH_MOD is
# empty. Reuses the Claude Code CLI's OAuth token so no separate key is needed.
POLISH_KEY = os.environ.get("VD_POLISH_KEY", "").strip()
POLISH_MOD = os.environ.get(
    "VD_POLISH_MOD", "KEY_LEFTCTRL,KEY_RIGHTCTRL KEY_LEFTSHIFT,KEY_RIGHTSHIFT")
POLISH_MODEL = os.environ.get("VD_POLISH_MODEL", "claude-haiku-4-5")
POLISH_MAX_TOKENS = int(os.environ.get("VD_POLISH_MAX_TOKENS", "1024"))
POLISH_TIMEOUT = float(os.environ.get("VD_POLISH_TIMEOUT", "20"))
POLISH_CREDS = os.path.expanduser(
    os.environ.get("VD_POLISH_CREDS", "~/.claude/.credentials.json"))
POLISH_PROMPT = os.environ.get("VD_POLISH_PROMPT", (
    "Ты — редактор устной речи. Тебе дают фрагмент, надиктованный голосом: он "
    "сумбурный, часто без пунктуации, мысли скомканы. Перепиши его грамотно, "
    "связно и красиво: исправь грамматику и пунктуацию, выстрой предложения, "
    "сохрани смысл, факты и тон говорящего. Добавь немного уместных эмодзи, не "
    "переусердствуй. Верни ТОЛЬКО переписанный текст — без кавычек, без "
    "пояснений, без преамбулы вроде «вот переписанный вариант»."))

START_WAV = os.path.join(BASE, "start.wav")
STOP_WAV = os.path.join(BASE, "stop.wav")
# Distinct start cue for polish mode, so you can hear which mode you're in.
# Falls back to START_WAV if this file is missing.
POLISH_START_WAV = (os.environ.get("VD_POLISH_START_WAV")
                    or os.path.join(BASE, "polish_start.wav"))


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


def polish_text(text):
    """Rewrite dictated text through a Claude model, reusing the Claude Code
    CLI's stored OAuth token. Returns (polished, err): `polished` is the cleaned
    string, or None on any failure — in which case `err` is a short reason for
    the notification and the caller should fall back to the raw text."""
    try:
        with open(POLISH_CREDS) as f:
            token = json.load(f).get("claudeAiOauth", {}).get("accessToken")
        if not token:
            return None, "нет токена Claude"
    except (OSError, ValueError) as e:
        log("polish: creds unreadable:", repr(e))
        return None, "нет доступа к токену"

    body = json.dumps({
        "model": POLISH_MODEL,
        "max_tokens": POLISH_MAX_TOKENS,
        "system": POLISH_PROMPT,
        "messages": [{"role": "user",
                      "content": "Вот надиктованный фрагмент, перепиши его:\n\n" + text}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"authorization": "Bearer " + token,
                 "anthropic-version": "2023-06-01",
                 "anthropic-beta": "oauth-2025-04-20",
                 "content-type": "application/json"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=POLISH_TIMEOUT) as r:
            data = json.load(r)
        out = "".join(b.get("text", "") for b in data.get("content", [])
                      if b.get("type") == "text").strip()
        log(f"polish {len(text)}->{len(out)} chars in {time.time() - t0:.1f}s")
        return (out, None) if out else (None, "пустой ответ")
    except urllib.error.HTTPError as e:
        log("polish HTTP", e.code)
        # 401/403: the OAuth token is stale — Claude Code refreshes it on use.
        if e.code in (401, 403):
            return None, "токен протух — запусти claude разок"
        return None, f"API {e.code}"
    except Exception as e:  # noqa: BLE001
        log("polish error:", repr(e))
        return None, "сеть/таймаут"


class Dictation:
    def __init__(self, model, recorder):
        self.model = model
        self.recorder = recorder
        self.proc = None
        self.t0 = 0.0
        self.mode = "verbatim"          # or "polish"; set per press in start()

    @property
    def active(self):
        return self.proc is not None

    def start(self, mode="verbatim"):
        if self.proc is not None:
            return
        self.mode = mode
        os.makedirs(RUNDIR, exist_ok=True)
        try:
            os.unlink(WAV)
        except OSError:
            pass
        self.t0 = time.time()
        self.proc = subprocess.Popen(self.recorder + [WAV],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        beep(POLISH_START_WAV if mode == "polish" and os.path.exists(POLISH_START_WAV)
             else START_WAV)
        notify("✨🎙️ Запись… (причешу)" if mode == "polish"
               else "🎙️ Запись… (держи клавишу)", 10000)
        log(f"recording started (mode={mode})")

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
                                                beam_size=BEAM, vad_filter=True,
                                                initial_prompt=PROMPT)
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

        # Put the dictated text on the clipboard and leave it there. The auto
        # Ctrl+V below is best-effort — if focus has moved off the target field
        # the paste misses, but the text stays in the clipboard so it can be
        # pasted by hand. (KWin exposes no virtual-keyboard / input-method-v2
        # protocol, so a real clipboard-free Unicode type is not possible here.)
        # TRAILING is appended so back-to-back dictations don't glue together.
        try:
            subprocess.run(["wl-copy"], input=(text + TRAILING).encode("utf-8"),
                           check=False)
        except OSError:
            log("wl-copy missing")
            return
        time.sleep(0.05)
        try:
            subprocess.run(["ydotool", "key", *PASTE_KEYS], env=env, check=False)
        except OSError:
            log("ydotool missing")
            return


def enumerate_event_paths():
    """List /dev/input/event* robustly (list_devices can come back empty when
    directory enumeration is restricted, e.g. inside a sandbox)."""
    import glob
    paths = list(evdev.list_devices()) or sorted(glob.glob("/dev/input/event*"))
    if not paths:
        paths = [f"/dev/input/event{i}" for i in range(64)]
    return paths


def open_keyboards(key_codes):
    """Return InputDevice objects for every keyboard exposing any trigger key
    (the verbatim key or the polish key — they can live on different devices)."""
    devs = []
    for path in enumerate_event_paths():
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue
        caps = d.capabilities().get(ecodes.EV_KEY, [])
        if any(k in caps for k in key_codes):
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

    # Polish trigger: an optional dedicated key (VD_POLISH_KEY) plus optional
    # modifier groups (VD_POLISH_MOD). With a dedicated key it's a plain hold,
    # independent of the verbatim key — no chord ordering to get wrong. Each
    # space-separated group needs one of its (comma-separated) alternatives held.
    polish_mains = {getattr(ecodes, k) for k in POLISH_KEY.replace(",", " ").split()
                    if hasattr(ecodes, k)} or {main_code}
    polish_groups = []
    for grp in POLISH_MOD.split():
        codes = {getattr(ecodes, m) for m in grp.split(",") if hasattr(ecodes, m)}
        if codes:
            polish_groups.append(codes)
    polish_enabled = bool(POLISH_KEY) or bool(polish_groups)
    polish_combo = ("/".join(pretty_combo([min(g) for g in polish_groups], m)
                             for m in sorted(polish_mains)) if polish_enabled else "")

    recorder = find_recorder()
    if recorder is None:
        log("no recorder (pw-record/parecord/arecord) found")
        sys.exit(1)

    devs = open_keyboards({main_code} | polish_mains)
    if not devs:
        log(f"no keyboard exposes {PTT_KEY}"
            + (f"/{POLISH_KEY}" if POLISH_KEY else "")
            + "; check /dev/input permissions")
        sys.exit(1)

    from faster_whisper import WhisperModel
    log(f"loading model {MODEL} (compute={COMPUTE}, threads={THREADS}) ...")
    t0 = time.time()
    model = WhisperModel(MODEL, device="cpu", compute_type=COMPUTE,
                         cpu_threads=THREADS)
    log(f"model ready in {time.time() - t0:.1f}s; hold {combo} to dictate"
        + (f", {polish_combo} to polish" if polish_enabled else ""))
    notify(f"🚀 Готово: {combo} — диктовка" +
           (f", {polish_combo} — причесать ✨" if polish_enabled else ""), 4000)

    dictation = Dictation(model, recorder)
    sel = selectors.DefaultSelector()
    for d in devs:
        sel.register(d, selectors.EVENT_READ)

    def triggers_now():
        """Ground-truth key state straight from the kernel (EVIOCGKEY). Immune
        to a missed press/release event, so the chord can never desync.
        Returns (verbatim held, polish held)."""
        active = set()
        for d in devs:
            try:
                active.update(d.active_keys())
            except OSError:
                continue
        verb_held = (main_code in active
                     and (bool(mod_codes & active) or not require_mod))
        polish_held = (polish_enabled and bool(polish_mains & active)
                       and all(g & active for g in polish_groups))
        return verb_held, polish_held

    def drain_ready(timeout):
        """Consume queued events so select() doesn't spin. The details don't
        matter — the real chord state is read from triggers_now(), not here."""
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
        """Before pasting, wait for the trigger keys to be physically released,
        else the injected Ctrl+V combines with a still-held key/modifier
        (Ctrl+Alt+V, Ctrl+Shift+V) and doesn't paste."""
        end = time.time() + timeout
        while time.time() < end:
            verb_held, polish_held = triggers_now()
            if not (verb_held or polish_held):
                break
            drain_ready(0.05)

    def finish():
        mode = dictation.mode
        text = dictation.stop_and_transcribe()
        # Polish first: the ~2s API call overlaps the user releasing the chord.
        if text and mode == "polish":
            notify("✨ Причёсываю…", 8000)
            polished, err = polish_text(text)
            if polished:
                text = polished
            else:
                notify("⚠️ Без причёсывания: " + (err or "ошибка"), 3000)
        wait_mod_release()
        if text:
            dictation.paste(text)
            notify("📋 " + text, 4000)

    # Event-driven when idle; while recording, wake every 0.5s to re-check the
    # real key state (self-heals a missed release) and enforce the safety cap.
    armed = True
    while True:
        drain_ready(0.5 if dictation.active else None)
        verb_held, polish_held = triggers_now()
        # Polish wins when its trigger is held (the verbatim and polish keys are
        # distinct, so normally only one is held at a time).
        if polish_held:
            want = "polish"
        elif verb_held:
            want = "verbatim"
        else:
            want = None
        chord_held = want is not None

        if not chord_held:
            armed = True                      # a fresh press is required to start
        if chord_held and armed and not dictation.active:
            dictation.start(want)
            armed = False
        elif dictation.active and not chord_held:
            finish()
        elif dictation.active and (time.time() - dictation.t0) > MAX_REC:
            log("max recording duration reached; auto-stopping")
            finish()


if __name__ == "__main__":
    main()
