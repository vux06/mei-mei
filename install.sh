#!/usr/bin/env bash
# Mei Mei installer — idempotent: re-running only does what is missing.
#   ./install.sh                 install everything, verify, run quick tests
#   ./install.sh --autostart     also enable start-with-desktop-session (systemd --user)
#   ./install.sh --yes-system    allow `sudo pacman -S` for missing system packages without asking again
#   ./install.sh --skip-tests
set -uo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
cd "$HERE"
AUTOSTART=0; YES_SYS=0; TESTS=1
for a in "$@"; do case "$a" in
  --autostart) AUTOSTART=1;; --yes-system) YES_SYS=1;; --skip-tests) TESTS=0;;
  -h|--help) sed -n 2,7p "$0"; exit 0;; esac; done

ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✘\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
FAILED=0

step "1/8 System"
[[ -f /etc/os-release ]] && . /etc/os-release && ok "OS: ${PRETTY_NAME:-$NAME}"
[[ "${XDG_SESSION_TYPE:-}" == "wayland" ]] && ok "Wayland session" || warn "not a Wayland session (XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-unset}); the GUI will still run via XWayland"
[[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]] && ok "Hyprland detected ($(hyprctl version 2>/dev/null | head -1 | cut -c1-40))" || warn "Hyprland not detected — window-management tools will be unavailable"

step "2/8 System packages"
declare -A PKG=( [hyprctl]=hyprland [grim]=grim [ydotool]=ydotool [wl-copy]=wl-clipboard [wpctl]=wireplumber
                 [pactl]=libpulse [playerctl]=playerctl [gio]=glib2 [nmcli]=networkmanager [uv]=uv )
MISSING=()
for c in "${!PKG[@]}"; do
  if command -v "$c" >/dev/null; then ok "$c"; else bad "$c missing (package: ${PKG[$c]})"; MISSING+=("${PKG[$c]}"); fi
done
command -v brightnessctl >/dev/null && ok brightnessctl || warn "brightnessctl missing (optional: brightness control)"
if ((${#MISSING[@]})); then
  if command -v pacman >/dev/null; then
    echo "  Missing: ${MISSING[*]}"
    if ((YES_SYS)); then REPLY=y; else read -r -p "  Install with 'sudo pacman -S --needed ${MISSING[*]}'? [y/N] " REPLY; fi
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then sudo pacman -S --needed "${MISSING[@]}" || FAILED=1; else warn "skipped — some features will be unavailable"; fi
  else warn "install these with your package manager: ${MISSING[*]}"; fi
fi
if command -v ydotool >/dev/null; then
  if systemctl --user is-active --quiet ydotool.service 2>/dev/null || [[ -S "/run/user/$UID/.ydotool_socket" ]]; then ok "ydotoold running"
  else
    warn "ydotoold is not running (needed for mouse/keyboard automation)"
    echo "     fix:  systemctl --user enable --now ydotool.service    (your user must be in the 'input' group)"
  fi
  id -nG | grep -qw input && ok "user is in the 'input' group" || warn "user not in 'input' group — ydotool cannot create virtual devices"
fi

step "3/8 Python environment"
command -v uv >/dev/null || { bad "uv is required (pacman -S uv)"; exit 1; }
if [[ ! -x .venv/bin/python ]]; then
  # Python 3.14 (system default here) lacks wheels for ctranslate2/onnxruntime; use 3.12 which uv manages.
  uv venv --python 3.12 .venv >/dev/null && ok "created .venv (Python 3.12)" || { bad "could not create venv"; exit 1; }
else ok ".venv exists ($(.venv/bin/python --version))"; fi
uv pip install --python .venv/bin/python -q -r requirements.txt && ok "Python dependencies installed/verified" || { bad "pip install failed"; exit 1; }
.venv/bin/python - <<'PY' && ok "imports OK (faster-whisper, piper, PySide6, claude-agent-sdk)" || { bad "import check failed"; exit 1; }
import faster_whisper, piper, PySide6, claude_agent_sdk, sounddevice, onnxruntime
PY

step "4/8 GPU / CUDA"
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, ".")
from voice import cuda_setup
import ctranslate2
print("  GPU devices visible to CTranslate2:", cuda_setup.cuda_available() and ctranslate2.get_cuda_device_count())
PY
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | sed 's/^/  /'

step "5/8 Directories + models (only what is missing)"
.venv/bin/python scripts/download_models.py 2>&1 | grep -v -i "unauthenticated" | sed 's/^/  /'
[[ ${PIPESTATUS[0]} -eq 0 ]] && ok "models ready" || { bad "model download failed"; FAILED=1; }
ok "data dir: ~/.local/share/meimei   (models, SQLite memory, session, logs)"
ok "settings:  config/settings.json    personality: config/personality.md   (never overwritten by the installer)"

step "6/8 Claude authentication"
.venv/bin/python - <<'PY'
import asyncio, sys
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
async def main():
    opts = ClaudeAgentOptions(tools=[], setting_sources=[], system_prompt="Reply with the single word OK.", thinking={"type": "disabled"})
    async with ClaudeSDKClient(opts) as c:
        await c.query("ping")
        async for m in c.receive_response():
            if isinstance(m, ResultMessage):
                return not m.is_error
try:
    ok = asyncio.run(asyncio.wait_for(main(), 60))
except Exception as e:
    print("  error:", e); ok = False
print("  Claude reachable and authenticated" if ok else "  Claude NOT reachable — run `claude` once to log in")
sys.exit(0 if ok else 1)
PY
[[ $? -eq 0 ]] && ok "Claude authentication verified" || { bad "Claude authentication failed (run: claude  → /login)"; FAILED=1; }

step "7/8 Audio check"
.venv/bin/python - <<'PY'
import sys, time; sys.path.insert(0, ".")
import numpy as np
from core.config import Settings
from voice.audio import MicStream, list_input_sources
srcs = list_input_sources()
print("  capture devices:", "; ".join(s["description"] + (" [default]" if s["default"] else "") for s in srcs) or "NONE")
m = MicStream(Settings()); m.start(); time.sleep(1.5); m.drain(); fr = []
t0 = time.time()
while time.time() - t0 < 2.0:
    try: fr.append(m.frames.get(timeout=0.3))
    except Exception: pass
m.stop()
if not fr: print("  ✘ no audio frames from the microphone"); sys.exit(1)
x = np.concatenate(fr); rms = float(np.sqrt(np.mean(x ** 2)))
print(f"  microphone OK: status was '{m.status}' — noise floor rms={rms:.4f} (after 120 Hz high-pass)")
if rms > 0.05: print("  ! very loud/noisy input; lower the mic volume (wpctl) or pick another mic in Settings")
PY
[[ $? -eq 0 ]] && ok "microphone capture works" || { bad "microphone check failed"; FAILED=1; }
.venv/bin/python - <<'PY' 2>/dev/null
import sys, time; sys.path.insert(0, ".")
from core.config import Settings; from core.events import EventBus
from voice.tts import TTSEngine
t = TTSEngine(Settings(), EventBus()); t.load(); t.chime("wake"); time.sleep(0.4)
t.speak("Mei Mei audio check. If you can hear this, speech output works.", raw=True); t.wait_idle(15)
PY
[[ $? -eq 0 ]] && ok "TTS played through your default output (you should have heard it)" || warn "TTS playback check failed"

step "8/8 Quick tests"
if ((TESTS)); then
  .venv/bin/python -m pytest -q tests/test_core.py tests/test_wake_word.py tests/test_permission_flow.py 2>&1 | tail -2 | sed 's/^/  /'
  [[ ${PIPESTATUS[0]} -eq 0 ]] && ok "unit tests pass" || { bad "unit tests failed"; FAILED=1; }
fi
chmod +x run.sh install.sh clear-context.sh scripts/*.sh scripts/*.py main.py 2>/dev/null
if ((AUTOSTART)); then scripts/autostart.sh enable && ok "autostart enabled"; fi

echo
if ((FAILED)); then bad "Finished with problems (see above)."; exit 1; fi
ok "Installed.  Launch:  ./run.sh     Clear context:  ./clear-context.sh     Autostart:  scripts/autostart.sh enable"
