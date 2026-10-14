#!/usr/bin/env bash
# Installs everything the dashboard needs on a Raspberry Pi (or any Debian/Ubuntu Linux).
# Run once from this folder:  ./install.sh
# Safe to run again: it only adds what is missing and updates the Python packages.
#
# 1. System packages with apt (audio library, Python venv, Chromium, curl).
# 2. A Python virtual environment in .venv (Raspberry Pi OS blocks plain pip).
# 3. The Python packages from requirements.txt, group by group, so one that
#    fails (usually mediapipe) does not stop the rest.
# 4. A check that every module imports, with a summary at the end.

cd "$(dirname "$(readlink -f "$0")")" || exit 1

echo "== Checking the system"
ARCH=$(uname -m)
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null)
echo "   CPU: $ARCH, Python: ${PYVER:-not found}"
if [ "$ARCH" = "armv7l" ] || [ "$ARCH" = "armv6l" ]; then
    echo "   WARNING: this is 32-bit Raspberry Pi OS. Hand gestures (mediapipe) need the 64-bit version."
fi

# MediaPipe 1.x is built for CPUs with AES instructions (Pi 5); on a Pi 4 it dies with
# "Illegal instruction". 0.10.18 runs there, but only has packages up to Python 3.12,
# and Raspberry Pi OS 13 (Trixie) has 3.13. So on a Pi 4 the .venv gets Python 3.12 from uv.
MEDIAPIPE="mediapipe"
VENV_PY=python3
if [ "$ARCH" = "aarch64" ] && ! grep -qw aes /proc/cpuinfo; then
    MEDIAPIPE="mediapipe==0.10.18"
    echo "   No AES in this CPU (Pi 4): hand gestures use $MEDIAPIPE, which needs Python 3.12 or older."
fi

echo "== Installing system packages (asks for your password)"
PKGS="python3-venv python3-dev libportaudio2 libgl1 libegl1 libgles2 curl"   # libegl1/libgles2: mediapipe needs them
if ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
    if apt-cache show chromium >/dev/null 2>&1; then PKGS="$PKGS chromium"; else PKGS="$PKGS chromium-browser"; fi
fi
sudo apt-get update && sudo apt-get install -y $PKGS || echo "   WARNING: apt had a problem; continuing."

if [ "$MEDIAPIPE" != "mediapipe" ] && python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
    echo "== Getting Python 3.12 for the hand gestures (uv, into ~/.local)"
    UV=$(command -v uv || echo "$HOME/.local/bin/uv")
    [ -x "$UV" ] || curl -LsSf https://astral.sh/uv/install.sh | sh
    if "$UV" python install 3.12 && PY312=$("$UV" python find 3.12); then
        VENV_PY=$PY312
    else
        echo "   WARNING: could not get Python 3.12. Everything else works; gestures stay off."
    fi
fi

echo "== Making the Python environment in .venv"
WANT=$("$VENV_PY" -c 'import sys; print(sys.version_info[:2])')
if [ -x .venv/bin/python ] && [ "$(.venv/bin/python -c 'import sys; print(sys.version_info[:2])')" != "$WANT" ]; then
    echo "   .venv has a different Python version; making it again"
    rm -rf .venv
fi
[ -x .venv/bin/python ] || "$VENV_PY" -m venv .venv || { echo "Could not make .venv. Is python3-venv installed?"; exit 1; }
PIP=".venv/bin/pip"
"$PIP" install --upgrade pip wheel

FAILED=()
group() {   # group <name> <packages...>
    local name=$1; shift
    echo "== Installing $name: $*"
    "$PIP" install --upgrade "$@" || FAILED+=("$name ($*)")
}
group "screen, Spotify, weather, map" flask requests spotipy cryptography
group "Claude assistant" anthropic
group "voice" vosk sounddevice piper-tts
group "hand gestures" numpy "$MEDIAPIPE"  # last: 0.10.18 needs numpy 1.x. Brings its own OpenCV (opencv-contrib-python)

echo "== Checking that everything imports"
.venv/bin/python - <<'EOF'
import importlib
modules = {"flask": "screen", "requests": "network", "spotipy": "Spotify", "cryptography": "phone https",
           "anthropic": "Claude", "vosk": "voice", "sounddevice": "voice", "piper": "voice",
           "numpy": "gestures", "cv2": "gestures", "mediapipe": "gestures"}
missing = []
for name, part in modules.items():
    try:
        importlib.import_module(name)
        print(f"   ok       {name}")
    except Exception as err:
        print(f"   MISSING  {name} ({part}): {err}")
        missing.append(name)
open(".install_missing", "w").write(" ".join(missing))
EOF

MISSING=$(cat .install_missing 2>/dev/null); rm -f .install_missing
echo
if [ -z "$MISSING" ] && [ ${#FAILED[@]} -eq 0 ]; then
    echo "All installed."
else
    [ ${#FAILED[@]} -gt 0 ] && printf '   pip failed for: %s\n' "${FAILED[@]}"
    [ -n "$MISSING" ] && echo "   Not importable: $MISSING"
    echo "The rest works; the dashboard turns those parts off and says so on screen."
    case " $MISSING " in *" mediapipe "*)
        echo "   mediapipe only supports some Python versions (.venv has $WANT) and needs 64-bit."
    esac
fi
echo
echo "Next: .venv/bin/python dashboard.py --demo     (see the screen with fake songs)"
echo "Then: .venv/bin/python dashboard.py            (fill in config.json, log in to Spotify)"
echo "Then: ./install_autostart.sh                   (start by itself when the Pi turns on)"
