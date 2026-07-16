#!/usr/bin/env bash
# Voice dictation toggle for Wayland/KDE.
#
#   First run  -> start recording from the default microphone.
#   Next run   -> stop, transcribe with faster-whisper, and paste the text
#                 into the focused window (clipboard + Ctrl+V via ydotool,
#                 because KWin does not expose the virtual-keyboard protocol
#                 and ydotool's own "type" cannot emit Cyrillic).
#
# Bind this script to a global shortcut and press it to start/stop.
set -euo pipefail

BASE="$HOME/.local/share/voice-dictate"
VENV_PY="$BASE/venv/bin/python"
DAEMON_PY="$BASE/transcribe_daemon.py"

RUNDIR="${XDG_RUNTIME_DIR:-/tmp}/voice-dictate"
SOCK="$RUNDIR/daemon.sock"
PIDFILE="$RUNDIR/rec.pid"
WAV="$RUNDIR/rec.wav"
LOG="$RUNDIR/daemon.log"
mkdir -p "$RUNDIR"

export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/.ydotool_socket}"

# Ctrl+V by default; override for terminals, e.g. VD_PASTE_KEYS="29:1 42:1 47:1 47:0 42:0 29:0" (Ctrl+Shift+V)
PASTE_KEYS="${VD_PASTE_KEYS:-29:1 47:1 47:0 29:0}"

notify() {
    command -v notify-send >/dev/null 2>&1 &&
        notify-send -a "Voice Dictate" -t "${2:-3000}" "Voice Dictate" "$1" || true
}

start_daemon() {
    [ -S "$SOCK" ] && return 0
    setsid "$VENV_PY" "$DAEMON_PY" >>"$LOG" 2>&1 < /dev/null &
    disown 2>/dev/null || true
}

wait_daemon() {  # up to ~60s (first launch loads the model)
    for _ in $(seq 1 120); do
        [ -S "$SOCK" ] && return 0
        sleep 0.5
    done
    return 1
}

pick_recorder() {
    if command -v pw-record >/dev/null 2>&1; then
        echo "pw-record --rate 16000 --channels 1 --format s16"
    elif command -v parecord >/dev/null 2>&1; then
        echo "parecord --rate=16000 --channels=1 --format=s16le --file-format=wav"
    elif command -v arecord >/dev/null 2>&1; then
        echo "arecord -f S16_LE -r 16000 -c 1 -t wav"
    else
        echo ""
    fi
}

recording() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
}

if recording; then
    # ---- STOP + transcribe + paste ----
    rpid="$(cat "$PIDFILE")"
    kill -INT "$rpid" 2>/dev/null || kill -TERM "$rpid" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$rpid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL "$rpid" 2>/dev/null || true
    rm -f "$PIDFILE"

    if [ ! -s "$WAV" ]; then
        notify "Пустая запись — микрофон не отдал звук"
        exit 0
    fi

    notify "Расшифровка…" 1500
    start_daemon
    if ! wait_daemon; then
        notify "Демон распознавания не поднялся (см. $LOG)"
        exit 1
    fi

    text="$("$VENV_PY" "$DAEMON_PY" --client "$WAV" || true)"
    if [ -z "$text" ]; then
        notify "Ничего не распознано"
        exit 0
    fi

    printf '%s' "$text" | wl-copy
    # shellcheck disable=SC2086
    ydotool key $PASTE_KEYS
    notify "▸ $text" 4000
else
    # ---- START recording ----
    rec="$(pick_recorder)"
    if [ -z "$rec" ]; then
        notify "Не найден инструмент записи (pw-record/parecord/arecord)"
        exit 1
    fi
    start_daemon          # warm the model while the user is speaking
    rm -f "$WAV"
    # shellcheck disable=SC2086
    $rec "$WAV" >/dev/null 2>&1 &
    echo $! > "$PIDFILE"
    notify "● Запись… нажми хоткей ещё раз, чтобы остановить" 2000
fi
