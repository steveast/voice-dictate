#!/bin/bash
# Benchmark the last dictation against both whisper models and prompt variants,
# scoring each transcript against an expected term list. Read-only: nothing here
# touches the running daemon.
#
#   1. dictate the reference phrase (see REFERENCE below) with the normal PTT key
#   2. ./compare-models.sh
#
# large-v3 is optional; install it to ~/.local/share/whisper-large-v3 (model.bin,
# config.json, preprocessor_config.json, tokenizer.json, vocabulary.json) or the
# script just reports it as missing and benchmarks turbo alone.
set -eu
S=${XDG_RUNTIME_DIR:-/tmp}/voice-dictate/ptt.clean.wav
[ -f "$S" ] || { echo "no recording at $S — dictate once first"; exit 1; }
echo "recording from $(date -r "$S" +%H:%M:%S)"
cd "$(dirname "$0")"
exec ./venv/bin/python - "$S" <<'PY'
import os
import re
import sys
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

# The phrase to dictate, and the terms that must survive transcription. Scoring
# only looks at these — filler words don't matter for a vocabulary test.
REFERENCE = ("Открой пул-реквест, сделай ребейз на мастер и мёрдж после ревью. "
             "Коллектор на msx пишет book_ticker и хитмап в дата-лейк, но "
             "материализатор перестал пушить в прод, поэтому свежесть фидов "
             "по BTCUSDT просела.")
TERMS = ["пул-реквест", "ребейз", "мёрдж", "коллектор", "msx", "book_ticker",
         "хитмап", "дата-лейк", "материализатор", "свежесть фидов", "BTCUSDT"]


def found(term, text):
    """Presence test that ignores orthographic noise, which is not a recognition
    error: whisper writes book_ticker as 'book ticker' or 'bookticker', дата-лейк
    as 'дата лейк', and picks е over ё freely. All hits — the word was heard.
    Only an actual substitution ('рибейс', 'свежесть видов') counts as a miss, so
    the separator is optional and ё is folded to е on both sides."""
    norm = lambda s: s.replace("ё", "е").replace("Ё", "Е")
    loose = r"[\s_\-]*".join(re.escape(w) for w in re.split(r"[\s_\-]+", norm(term)))
    return re.search(loose, norm(text), re.IGNORECASE) is not None


def score(text):
    hits = [t for t in TERMS if found(t, text)]
    return hits, [t for t in TERMS if t not in hits]


with wave.open(sys.argv[1]) as w:
    audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0

unit = os.path.expanduser("~/.config/systemd/user/voice-ptt.service")
live = re.search(r'Environment="VD_PROMPT=(.*?)"\s*$', open(unit).read(), re.S | re.M)
PROMPTS = {"live VD_PROMPT": live.group(1).strip() if live else None,
           "no prompt": None}
MODELS = {"turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
          "large-v3": os.path.expanduser("~/.local/share/whisper-large-v3")}

print(f"{audio.size / 16000:.2f}s of audio, scoring {len(TERMS)} terms\n")
for name, mid in MODELS.items():
    if mid.startswith("/") and not os.path.isdir(mid):
        print(f"=== {name}: not installed, skipped\n")
        continue
    model = WhisperModel(mid, device="cpu",
                         compute_type=os.environ.get("VOICE_DICTATE_COMPUTE", "int8"),
                         cpu_threads=os.cpu_count())
    print(f"=== {name}")
    for label, prompt in PROMPTS.items():
        t0 = time.time()
        segments, _ = model.transcribe(audio, language="ru", beam_size=5,
                                       vad_filter=True, initial_prompt=prompt)
        text = "".join(s.text for s in segments).strip()
        hits, missed = score(text)
        print(f"  [{time.time() - t0:5.1f}s] {label}: {len(hits)}/{len(TERMS)} terms")
        print(f"      {text}")
        if missed:
            print(f"      MISSED: {', '.join(missed)}")
    print()
    del model
PY
