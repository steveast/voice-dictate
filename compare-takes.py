#!/usr/bin/env python
"""Replay archived dictations through several models and show where they differ.

compare-models.sh scores ONE freshly dictated reference phrase. This replays
everything VD_KEEP_DIR has collected, which is what you actually say — the only
way to tell whether a model or backend change helps in practice rather than on a
lucky sample. Choosing medium over small came out of exactly this, and so did
rejecting three plausible ideas that measured as noise.

    VD_KEEP_DIR=~/.local/share/voice-dictate/takes   # in the unit, then dictate
    ./compare-takes.py                               # a few days later
    ./compare-takes.py --models small,medium --limit 40

There is no ground truth here, so the output is the DISAGREEMENTS: takes where
the models produced different text. Where they agree the audio was clear and the
choice did not matter, and reading those tells you nothing.

The prompt and language come from the running unit and each take's filename, so
the replay matches what the daemon actually does.
"""
import os
import sys

# Dependencies live in the venv, exactly as compare-models.sh assumes. Re-exec
# into it rather than making the caller remember, and keep it relative so the
# checkout stays movable.
# (sys.prefix, not realpath(sys.executable): a venv's python symlinks straight
# to the system one, so realpath matches even from outside and the exec is skipped.)
_VENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv")
if os.path.exists(_VENV) and os.path.abspath(sys.prefix) != _VENV:
    _PY = os.path.join(_VENV, "bin", "python")
    os.execv(_PY, [_PY] + sys.argv)

import argparse
import difflib
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptt_daemon as vd  # noqa: E402

KNOWN = {"tiny": "Systran/faster-whisper-tiny",
         "base": "Systran/faster-whisper-base",
         "small": "Systran/faster-whisper-small",
         "medium": "Systran/faster-whisper-medium",
         "large-v3": "Systran/faster-whisper-large-v3",
         "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}


def unit_prompt():
    """VD_PROMPT as the running daemon has it — a replay under a different
    prompt would not be comparable with what was actually pasted."""
    try:
        env = subprocess.run(
            ["systemctl", "--user", "show", "voice-ptt", "-p", "Environment",
             "--value"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"VD_PROMPT=(.*?)(?= VD_[A-Z_]+=| VOICE_[A-Z_]+=|$)", env, re.S)
    return m.group(1).strip() if m else None


def norm(s):
    """Ignore the differences that are not recognition errors: case, commas,
    е/ё. Otherwise every take 'disagrees' and the real ones are lost in noise."""
    s = s.lower().replace("ё", "е")
    return " ".join(re.sub(r"[.,!?;:—–-]", " ", s).split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("takes", nargs="?",
                    default=os.path.expanduser("~/.local/share/voice-dictate/takes"))
    ap.add_argument("--models", default="small,medium,turbo",
                    help=f"comma-separated; known names {sorted(KNOWN)} or any HF id")
    ap.add_argument("--limit", type=int, default=0, help="only the newest N takes")
    ap.add_argument("--since", default="", help="only takes named on/after this (YYYYMMDD)")
    args = ap.parse_args()

    wavs = sorted(f for f in os.listdir(args.takes) if f.endswith(".wav"))
    if args.since:
        wavs = [f for f in wavs if f >= args.since]
    if args.limit:
        wavs = wavs[-args.limit:]
    if not wavs:
        sys.exit(f"no takes in {args.takes} — set VD_KEEP_DIR and dictate for a while")

    prompt = unit_prompt()
    names = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"{len(wavs)} takes from {args.takes}")
    print(f"prompt: {len(prompt or '')} chars from the running unit\n")

    from faster_whisper import WhisperModel
    out, secs = {}, {}
    for name in names:
        model = WhisperModel(KNOWN.get(name, name), device="cpu",
                             compute_type=vd.COMPUTE, cpu_threads=vd.THREADS)
        texts, ts = {}, []
        for w in wavs:
            lang = w.rsplit("-", 1)[-1][:-4]          # ...-0042-ru.wav
            audio = vd.load_wav(os.path.join(args.takes, w))
            t = time.time()
            segs, _ = model.transcribe(
                audio, language=None if lang == "auto" else lang,
                beam_size=vd.BEAM, vad_filter=True, initial_prompt=prompt)
            texts[w] = "".join(s.text for s in segs).strip()
            ts.append(time.time() - t)
        out[name], secs[name] = texts, ts
        print(f"{name:10} median {statistics.median(ts):5.2f}s/take", flush=True)
        del model

    print("\n=== takes where the models disagree ===\n")
    differ = 0
    for w in wavs:
        variants = {n: out[n][w] for n in names}
        if len({norm(t) for t in variants.values()}) == 1:
            continue
        differ += 1
        live_path = os.path.join(args.takes, w[:-4] + ".txt")
        live = (open(live_path).read().strip()
                if os.path.exists(live_path) else "")
        print(f"--- {w}")
        if live:
            print(f"    pasted at the time: {live}")
        for n in names:
            flag = " " if live and norm(variants[n]) == norm(live) else "*"
            print(f"  {flag} {n:10}: {variants[n]}")
        if len(names) >= 2:
            a, b = norm(variants[names[0]]).split(), norm(variants[names[-1]]).split()
            d = [x for x in difflib.ndiff(a, b) if x[0] in "+-"]
            if d:
                print(f"    {names[0]} vs {names[-1]}: {' '.join(d)}")
        print()

    print(f"{differ}/{len(wavs)} takes differ; {len(wavs) - differ} identical "
          f"(model choice did not matter there)")
    print("\nA '*' marks a model whose replay differs from what was pasted live.")


if __name__ == "__main__":
    main()
