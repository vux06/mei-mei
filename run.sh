#!/usr/bin/env bash
# Launch Mei Mei (GUI + always-on "Hey Mei Mei" listening). Extra args are passed through, e.g. --hidden.
set -euo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
PY="$HERE/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Mei Mei is not installed yet. Run ./install.sh first." >&2
  exit 1
fi
cd "$HERE"
exec "$PY" -I "$HERE/main.py" "$@" 2> >(grep --line-buffered -v -E "^(ALSA lib|Expression '|Cannot connect to server|jack server|JackShmReadWritePtr)" >&2)
