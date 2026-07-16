#!/usr/bin/env python
"""Voice-dictate transcription daemon.

Server mode (no args): load a faster-whisper model once, listen on a unix
socket, transcribe WAV files whose paths arrive over the socket, and reply
with the recognized text. Exits after an idle period to free RAM.

Client mode (--client PATH): connect to the running daemon, hand it a WAV
path, print the recognized text to stdout. Starts nothing on its own.

Config via env:
  VOICE_DICTATE_MODEL    faster-whisper model id / size / path
                         (default: cached large-v3-turbo)
  VOICE_DICTATE_LANG     language code, or "auto" (default: ru)
  VOICE_DICTATE_COMPUTE  ctranslate2 compute type (default: int8)
  VOICE_DICTATE_THREADS  CPU threads (default: all cores)
  VOICE_DICTATE_IDLE     idle seconds before the server exits (default: 1800)
"""
import os
import sys
import time
import wave
import socket
import signal

import numpy as np

RUNDIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "voice-dictate")
SOCK = os.path.join(RUNDIR, "daemon.sock")

MODEL = os.environ.get(
    "VOICE_DICTATE_MODEL", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
)
LANG = os.environ.get("VOICE_DICTATE_LANG", "ru")
COMPUTE = os.environ.get("VOICE_DICTATE_COMPUTE", "int8")
THREADS = int(os.environ.get("VOICE_DICTATE_THREADS", "0")) or (os.cpu_count() or 4)
IDLE_TIMEOUT = int(os.environ.get("VOICE_DICTATE_IDLE", "1800"))


def log(*a):
    print("[vd-daemon]", *a, file=sys.stderr, flush=True)


def load_wav(path):
    """Load a PCM WAV as a mono float32 array at 16 kHz."""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"expected 16-bit PCM WAV, got sample width {sw} bytes")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != 16000 and a.size:
        idx = np.linspace(0.0, len(a) - 1, int(round(len(a) * 16000 / sr)))
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    return a


def recv_line(conn):
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", "replace").strip()


def run_client(path):
    if not os.path.exists(SOCK):
        print("", end="")
        sys.exit(3)  # daemon not up
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(180)
    s.connect(SOCK)
    s.sendall((os.path.abspath(path) + "\n").encode("utf-8"))
    sys.stdout.write(recv_line(s))
    s.close()


def run_server():
    os.makedirs(RUNDIR, exist_ok=True)
    # single-instance: if a live socket already answers, bail out
    if os.path.exists(SOCK):
        try:
            t = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            t.settimeout(1)
            t.connect(SOCK)
            t.close()
            log("another daemon is already listening; exiting")
            return
        except OSError:
            os.unlink(SOCK)

    from faster_whisper import WhisperModel

    log(f"loading model {MODEL} (compute={COMPUTE}, threads={THREADS}) ...")
    t0 = time.time()
    model = WhisperModel(MODEL, device="cpu", compute_type=COMPUTE, cpu_threads=THREADS)
    log(f"model ready in {time.time() - t0:.1f}s; listening on {SOCK}")

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)
    srv.settimeout(IDLE_TIMEOUT)

    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            log("idle timeout reached; exiting")
            break
        try:
            path = recv_line(conn)
            if not path:
                conn.sendall(b"\n")
                continue
            audio = load_wav(path)
            t1 = time.time()
            lang = None if LANG == "auto" else LANG
            segments, _info = model.transcribe(
                audio, language=lang, beam_size=5, vad_filter=True
            )
            text = "".join(s.text for s in segments).strip()
            log(f"transcribed {audio.size / 16000:.1f}s audio in "
                f"{time.time() - t1:.1f}s -> {len(text)} chars")
            conn.sendall((text + "\n").encode("utf-8"))
        except Exception as e:  # noqa: BLE001 - report to client, keep serving
            log("error:", repr(e))
            try:
                conn.sendall(b"\n")
            except OSError:
                pass
        finally:
            conn.close()

    try:
        os.unlink(SOCK)
    except OSError:
        pass


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--client":
        if len(sys.argv) < 3:
            sys.exit("usage: transcribe_daemon.py --client WAV")
        run_client(sys.argv[2])
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        run_server()


if __name__ == "__main__":
    main()
