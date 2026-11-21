#!/usr/bin/env bash
# usage: react_seq.sh "<delay_s>:<mood>[:<secs>]" "<delay_s>:q=<giphy query>[:<secs>]" ...
# Runs in the background and returns instantly, so GIFs pop up at timed moments while Mei Mei speaks.
r="$HOME/.local/share/meimei/reactions/react.sh"; export LOCAL=1  # instant: use saved GIFs (falls back to download if none)
for item in "$@"; do
  d="${item%%:*}"; rest="${item#*:}"; secs=3
  case "$rest" in q=*) q="${rest#q=}"; [ "${q##*:}" -eq "${q##*:}" ] 2>/dev/null && { secs="${q##*:}"; q="${q%:*}"; }
       ( sleep "$d"; QUERY="$q" "$r" meme "$secs" >/dev/null 2>&1 ) & ;;
     *) m="${rest%%:*}"; [ "$rest" != "$m" ] && secs="${rest#*:}"
       ( sleep "$d"; "$r" "$m" "$secs" >/dev/null 2>&1 ) & ;;
  esac
done
wait
