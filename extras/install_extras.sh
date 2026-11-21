#!/usr/bin/env bash
# Installs the reaction-GIF overlay and helper scripts into the places Mei Mei expects.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/share/meimei/reactions ~/.local/share/meimei/overlay ~/.local/share/meimei/secrets ~/.config/quickshell/meimei-react
cp "$here"/reactions/*.sh ~/.local/share/meimei/reactions/ && chmod +x ~/.local/share/meimei/reactions/*.sh
cp "$here"/quickshell/meimei-react/shell.qml ~/.config/quickshell/meimei-react/
cp "$here"/overlay/listen_overlay.py ~/.local/share/meimei/overlay/
echo "Done. Optional: put your own Giphy API key in ~/.local/share/meimei/secrets/giphy_key"
echo "Then run: ~/.local/share/meimei/reactions/download_pack.sh   (downloads a starter GIF pack)"
