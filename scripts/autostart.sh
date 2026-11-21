#!/usr/bin/env bash
# Start Mei Mei's always-on listening automatically with your desktop session (systemd --user service).
#   scripts/autostart.sh enable    install + enable (starts at next login; add --now to start immediately)
#   scripts/autostart.sh disable   stop + disable + remove the unit
#   scripts/autostart.sh status
# Nothing in your Hyprland/compositor config is modified.
set -euo pipefail
HERE="$(dirname "$(dirname "$(readlink -f "$0")")")"
UNIT="$HOME/.config/systemd/user/meimei.service"
case "${1:-status}" in
  enable)
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Mei Mei voice assistant (always-on "Hey Mei Mei")
After=pipewire.service pipewire-pulse.service wireplumber.service

[Service]
Type=simple
# wait for the compositor and the audio server before starting
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 90); do ls "\$XDG_RUNTIME_DIR"/wayland-* >/dev/null 2>&1 && pactl info >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
ExecStart=$HERE/run.sh --hidden
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=default.target
UNIT_EOF
    systemctl --user daemon-reload
    systemctl --user enable meimei.service
    [[ "${2:-}" == "--now" ]] && systemctl --user start meimei.service
    echo "Enabled. Mei Mei will start (hidden, listening) at your next login."
    echo "Show the window any time:  $HERE/run.sh   (or scripts/meimei_ctl.py show)"
    ;;
  disable)
    systemctl --user disable --now meimei.service 2>/dev/null || true
    rm -f "$UNIT"; systemctl --user daemon-reload
    echo "Autostart removed." ;;
  status)
    systemctl --user is-enabled meimei.service 2>&1 || true
    systemctl --user is-active meimei.service 2>&1 || true ;;
  *) echo "usage: $0 enable [--now] | disable | status"; exit 1 ;;
esac
