#!/usr/bin/env python
"""Regenerate the README banner: a push-to-talk take, waveform to text.

    ./venv/bin/python assets/make-banner.py

Needs numpy (the project venv has it), ImageMagick for the captions and ffmpeg
for the GIF. Pillow is deliberately not required — frames are written as raw PPM
and handed straight to ffmpeg. Captions go through ImageMagick because this
ffmpeg build's drawtext filter refuses every text source it is given.

The signal is synthetic but shaped like speech: a pitch that drifts the way a
voice does, under a syllable envelope with real gaps. A bare sine reads as a test
tone, and overlapping syllables wash into a continuous drone — both look wrong.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = "/usr/share/fonts/TTF/JetBrainsMonoNLNerdFont-SemiBold.ttf"
GIF = os.path.join(HERE, "waveform.gif")

W, H, FPS = 1200, 320, 20
SECONDS = 6.0
N = int(FPS * SECONDS)

BG = np.array([13, 17, 23], dtype=np.float64)        # GitHub dark
C_HOT = np.array([88, 214, 255], dtype=np.float64)   # cyan, the live signal
C_COOL = np.array([167, 139, 250], dtype=np.float64) # violet, the settled one
GRID = np.array([26, 32, 42], dtype=np.float64)

rng = np.random.default_rng(7)
# A syllable train: bursts of energy with gaps, like actual speech.
t = np.linspace(0, 1, 9000)
env = np.zeros_like(t)
for centre, width, gain in ((0.05, .016, .55), (0.12, .020, .95), (0.19, .014, .70),
                            (0.27, .022, 1.00), (0.36, .015, .80), (0.44, .019, .92),
                            (0.53, .013, .60), (0.61, .021, .88), (0.70, .016, .75),
                            (0.78, .014, .95), (0.86, .019, .68), (0.94, .013, .50)):
    env += gain * np.exp(-((t - centre) ** 2) / (2 * width ** 2))
env /= env.max()
# Deepen the valleys: overlapping syllables wash into a continuous tone, which
# reads as a test signal rather than as someone talking.
env = env ** 2.2
env /= env.max()
# Pitch drifts the way a voice does, and the harmonics ride on it — a fixed
# carrier looks like a picket fence, not like speech.
f0 = 105 + 34 * np.sin(2 * np.pi * 1.6 * t) + 15 * np.sin(2 * np.pi * 0.73 * t + 1.0)
phase = 2 * np.pi * np.cumsum(f0) / t.size
carrier = (np.sin(phase) * 0.62
           + np.sin(2 * phase + 1.1) * 0.26
           + np.sin(3 * phase + 2.2) * 0.14
           + np.sin(5 * phase + 0.4) * 0.07
           + rng.normal(0, .06, t.size))
WAVE = carrier * env



def load_pgm(path):
    d = open(path, "rb").read()
    tok, idx = [], 0
    while len(tok) < 4:
        nl = d.index(b"\n", idx)
        line = d[idx:nl]
        idx = nl + 1
        if not line.startswith(b"#"):
            tok += line.split()
    w, h = int(tok[1]), int(tok[2])
    return (np.frombuffer(d[idx:idx + w * h], dtype=np.uint8)
            .reshape(h, w).astype(np.float64) / 255.0)


CAPTIONS = {
    "rec":  (26, ["-gravity", "NorthWest", "-annotate", "+42+40",
                  "REC   hold to speak"]),
    "meta": (22, ["-gravity", "NorthEast", "-annotate", "+42+46",
                  "0.67 s   Arc iGPU"]),
    "out":  (30, ["-gravity", "SouthWest", "-annotate", "+42+44",
                  "Сейчас работает почти идеально."]),
}


def render_caption(name, size, args, into):
    """One caption as a greyscale mask, so the frame renderer can fade it in and
    out on its own clock instead of ffmpeg's."""
    path = os.path.join(into, f"{name}.pgm")
    subprocess.run(["magick", "-size", f"{W}x{H}", "xc:black", "-font", FONT,
                    "-pointsize", str(size), "-fill", "white", *args,
                    "-colorspace", "gray", "-depth", "8", path], check=True)
    return load_pgm(path)
CAP_COL = {"rec": np.array([88., 214., 255.]),
           "meta": np.array([107., 118., 132.]),
           "out": np.array([167., 139., 250.])}


def blend(img, name, alpha):
    if alpha <= 0.001:
        return
    a = (MASK[name] * alpha)[..., None]
    img += (CAP_COL[name] - img) * a


def frame(i):
    p = i / (N - 1)
    img = np.tile(BG, (H, W, 1))

    # centre line + faint ticks, so it reads as an instrument
    img[H // 2 - 34, :] = GRID
    img[H // 2 + 34, :] = GRID
    for x in range(0, W, 24):
        img[H // 2 - 2:H // 2 + 2, x] = GRID

    # 0.00-0.62 record and fill; 0.62-0.78 settle; then hold with the caption
    grow = min(p / 0.62, 1.0)
    settle = max(0.0, min((p - 0.62) / 0.16, 1.0))
    shown = int(len(WAVE) * grow)
    if shown < 8:
        shown = 8
    seg = WAVE[:shown]

    xs = np.linspace(0, int(W * grow) - 1, shown).astype(int)
    amp = (H * 0.30) * (1 - 0.55 * settle)
    # a live wobble while recording, gone once it settles
    jitter = 1 + 0.05 * np.sin(np.linspace(0, 9, shown) + i * 0.5) * (1 - settle)
    ys = (H // 2 - seg * amp * jitter).astype(int)

    col = C_HOT * (1 - settle) + C_COOL * settle
    for x, y in zip(xs, ys):
        lo, hi = sorted((H // 2, y))
        lo, hi = max(lo, 2), min(hi, H - 3)
        # the bar, plus a dimmer halo so it glows instead of looking like a fence
        img[lo:hi + 1, x] = col
        for dx, k in ((-1, .30), (1, .30), (-2, .12), (2, .12)):
            if 0 <= x + dx < W:
                img[lo:hi + 1, x + dx] += (col - img[lo:hi + 1, x + dx]) * k

    # the recording head: a bright edge that runs ahead of the drawn signal
    if grow < 1.0:
        head = int(W * grow)
        for dx, k in ((0, 1.0), (-1, .55), (-2, .28), (1, .55), (2, .28)):
            x = head + dx
            if 0 <= x < W:
                img[H // 2 - 96:H // 2 + 96, x] += (np.array([255., 255., 255.])
                                                    - img[H // 2 - 96:H // 2 + 96, x]) * k * .8

    # captions, faded in and out on the same clock as the waveform
    tt = p * SECONDS
    blend(img, "rec", 0.95 if tt < 3.9 else max(0.0, 0.95 - (tt - 3.9) * 4))
    blend(img, "out", 0.0 if tt < 4.2 else min(1.0, (tt - 4.2) * 3))
    blend(img, "meta", 0.0 if tt < 4.4 else min(1.0, (tt - 4.4) * 3))

    # the REC dot, pulsing while the take is live
    if tt < 4.0:
        puls = 0.55 + 0.45 * (0.5 + 0.5 * np.sin(i * 0.42))
        fade = 1.0 if tt < 3.9 else max(0.0, 1 - (tt - 3.9) * 4)
        cy, cx, r = 52, 26, 7
        yy, xx = np.ogrid[-r - 2:r + 3, -r - 2:r + 3]
        d = np.sqrt(yy * yy + xx * xx)
        a = np.clip((r - d) / 2.0, 0, 1) * puls * fade
        reg = img[cy - r - 2:cy + r + 3, cx - r - 2:cx + r + 3]
        reg += (np.array([255., 95., 86.]) - reg) * a[..., None]

    np.clip(img, 0, 255, out=img)
    return img.astype(np.uint8)


def main():
    tmp = tempfile.mkdtemp(prefix="vd-banner-")
    try:
        global MASK
        MASK = {n: render_caption(n, size, args, tmp)
                for n, (size, args) in CAPTIONS.items()}
        for i in range(N):
            with open(os.path.join(tmp, f"f{i:04d}.ppm"), "wb") as f:
                f.write(b"P6\n%d %d\n255\n" % (W, H))
                f.write(frame(i).tobytes())
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-framerate", str(FPS),
            "-i", os.path.join(tmp, "f%04d.ppm"),
            "-vf", "split[a][b];[a]palettegen=max_colors=48:stats_mode=diff[p];"
                   "[b][p]paletteuse=dither=none",
            "-loop", "0", GIF], check=True)
        print(f"{GIF}  {os.path.getsize(GIF) / 1024:.0f} KB, {N} frames")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
