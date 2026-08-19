#!/usr/bin/env bash
# Make the Intel Arc iGPU and the NPU usable for inference, so whisper can be
# measured off the CPU. Arch / EndeavourOS (pacman); everything here is in the
# official repos.
#
#   sudo ./install-openvino.sh              # GPU + NPU runtime, python side, model
#   sudo ./install-openvino.sh --no-npu     # GPU only
#   sudo ./install-openvino.sh --no-model   # skip the ~1 GB model download
#   sudo ./install-openvino.sh --yes        # don't let pacman ask
#
# WHY: ctranslate2, which faster-whisper runs on, is CPU-or-CUDA only and this
# machine has neither an NVIDIA card nor spare CPU. Measurements kept landing on
# the same wall — 8 threads already saturate the cores, so beam size, batching
# and prompt tuning all came out as noise. Different silicon is the one lever
# left, and OpenVINO is how these two devices are reached.
#
# WHAT THIS DOES NOT DO: it does not change the dictation daemon. faster-whisper
# cannot drive OpenVINO, so using the GPU means a second backend behind the same
# interface — a code change worth making only if a benchmark says it is faster.
# This script just makes that benchmark possible. Nothing here touches the
# running service, and dictation keeps working exactly as it does now.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT/venv"
MODEL_ID="OpenVINO/whisper-medium-int8-ov"
MODEL_DIR_REL=".local/share/voice-dictate/models/whisper-medium-int8-ov"

WITH_NPU=1
WITH_MODEL=1
PACMAN_ARGS=(-S --needed)

for arg in "$@"; do
  case "$arg" in
    --no-npu)   WITH_NPU=0 ;;
    --no-model) WITH_MODEL=0 ;;
    --yes|-y)   PACMAN_ARGS+=(--noconfirm) ;;
    -h|--help)  sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo — this installs system packages" >&2; exit 1; }
command -v pacman >/dev/null || { echo "pacman not found; this script is for Arch/EndeavourOS" >&2; exit 1; }

# The venv belongs to a normal user and must stay that way: pip as root would
# leave root-owned files in it and the daemon could no longer update them.
RUN_USER="${SUDO_USER:-}"
[ -n "$RUN_USER" ] || RUN_USER="$(stat -c %U "$VENV" 2>/dev/null || true)"
[ -n "$RUN_USER" ] && [ "$RUN_USER" != "root" ] || {
  echo "cannot tell which user owns $VENV — run this via sudo from your own account" >&2
  exit 1
}
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
MODEL_DIR="$RUN_HOME/$MODEL_DIR_REL"
as_user() { sudo -u "$RUN_USER" -H "$@"; }

say "System runtime (Level Zero + OpenCL for the Arc iGPU)"
# intel-compute-runtime is the actual GPU backend; level-zero-loader is the API
# OpenVINO talks to; intel-graphics-compiler builds kernels at load time.
PKGS=(intel-compute-runtime level-zero-loader level-zero-headers
      intel-graphics-compiler opencl-headers ocl-icd)
[ "$WITH_NPU" -eq 1 ] && PKGS+=(intel-npu-driver)
printf '  installing: %s\n' "${PKGS[*]}"
pacman "${PACMAN_ARGS[@]}" "${PKGS[@]}"

say "Device nodes"
# The GPU node is world-accessible out of the box, but installing the NPU driver
# lays down a udev rule that tightens /dev/accel/accel0 to root:render 0660. So
# the group only becomes necessary *after* this script has run — which is why
# the check lives here rather than up front.
NEED_RELOGIN=0
for dev in /dev/dri/renderD128 /dev/accel/accel0; do
  if [ -e "$dev" ]; then
    printf '  %-24s %s\n' "$dev" "$(stat -c '%A %U:%G' "$dev")"
  else
    printf '  %-24s MISSING\n' "$dev"
  fi
done

if [ -e /dev/accel/accel0 ] && ! sudo -u "$RUN_USER" test -r /dev/accel/accel0; then
  GRP="$(stat -c %G /dev/accel/accel0)"
  if id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$GRP"; then
    echo "  $RUN_USER is already in '$GRP' but the session predates it"
  else
    echo "  adding $RUN_USER to '$GRP' so the NPU is reachable"
    usermod -aG "$GRP" "$RUN_USER"
  fi
  NEED_RELOGIN=1
fi

say "Python side, into $VENV (as $RUN_USER, not root)"
[ -x "$VENV/bin/pip" ] || { echo "no venv at $VENV — create it first" >&2; exit 1; }
as_user "$VENV/bin/pip" install --upgrade openvino openvino-genai

if [ "$WITH_MODEL" -eq 1 ]; then
  say "Whisper model, pre-converted to OpenVINO ($MODEL_ID)"
  # Pre-converted on purpose: converting locally would drag in optimum-intel,
  # transformers and torch — some 3 GB — to produce the same files.
  as_user mkdir -p "$MODEL_DIR"
  as_user "$VENV/bin/python" - "$MODEL_ID" "$MODEL_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download
path = snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])
print(f"  model at {path}")
PY
fi

say "What OpenVINO can see now"
as_user "$VENV/bin/python" - <<'PY'
import openvino as ov
core = ov.Core()
devs = core.available_devices
print(f"  available: {devs}")
for d in devs:
    try:
        print(f"    {d:5} {core.get_property(d, 'FULL_DEVICE_NAME')}")
    except Exception as e:
        print(f"    {d:5} (no name: {e!r})")
if not any(d.startswith("GPU") for d in devs):
    print("\n  No GPU. A reboot usually settles this — the compute runtime was")
    print("  not loaded when the session started.")
if not any(d.startswith("NPU") for d in devs):
    print("\n  No NPU yet: group membership only takes effect on a new login,")
    print("  so log out and back in (or reboot) and check again.")
PY

say "Done"
cat <<EOF
  Nothing about dictation changed yet: the daemon still runs faster-whisper on
  the CPU, and it was not restarted.
EOF
[ "$NEED_RELOGIN" -eq 1 ] && cat <<EOF
  Log out and back in before expecting the NPU: group changes only apply to new
  sessions, and this one still carries the old set.
EOF
cat <<EOF

  Next is a benchmark, as your own user, comparing this against the current
  setup on takes you actually dictated:

    ./compare-takes.py --limit 40        # today's CPU baseline, for reference

  If GPU or NPU shows up above, say so and the OpenVINO backend can be measured
  against that baseline before any of it goes near the daemon.
EOF
