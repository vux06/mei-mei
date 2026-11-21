#!/usr/bin/env bash
# usage: react.sh <mood> [seconds]      (QUERY="meme name" react.sh meme [seconds] for Giphy named search)
# - Mostly downloads a FRESH GIF; ~25% of the time replays one already saved (NOLOCAL=1 to skip).
# - Nothing is ever deleted. Anime moods come from nekos.best (SFW); Giphy (PG-13) for named memes.
mood="${1:-happy}"; secs="${2:-6}"
base="$HOME/.local/share/meimei/reactions"
kf="$HOME/.local/share/meimei/secrets/giphy_key"
case "${QUERY,,}" in *"apology bow"*|*"apologetic bow"*) echo "banned query: $QUERY"; exit 3;; esac
case "$mood" in sad) ep=cry;; *) ep="$mood";; esac
dir="$base/$mood"
if [ -n "$QUERY" ]; then
  slug=$(printf '%s' "${QUERY,,}" | tr -cs 'a-z0-9' '_' | sed 's/^_//;s/_$//')
  dir="$base/meme/$slug"
fi
mkdir -p "$dir"
pgrep -f "qs.*-c meimei-react" >/dev/null || (setsid qs -d -c meimei-react >/dev/null 2>&1; sleep 1.5)
play() { qs -c meimei-react ipc call react play "$1" "$secs"; }

# occasionally replay a saved one
if [ "$LOCAL" = 1 ] || { [ "$NOLOCAL" != 1 ] && [ $((RANDOM % 4)) -eq 0 ]; }; then
  f=$(find "$dir" -iname '*.gif' | shuf -n1)
  [ -n "$f" ] && { play "$f"; echo "local: $f"; exit 0; }
fi

PICK='import sys,json,random,re
B=re.compile(r"brain.?rot|skibidi|sigma|rizz|gyat|fanum|ohio|mewing|looksmax|italian|tung.?tung|sahur|bombardiro|tralalero|ballerina|capuccina|slop",re.I)
d=[x for x in json.load(sys.stdin)["data"] if not B.search(x.get("title","")+" "+x.get("slug","")+" "+x.get("username",""))]
print(random.choice(d[:8])["images"]["downsized"]["url"] if d else "")'
giphy_search() {
  curl -s --max-time 6 -G "https://api.giphy.com/v1/gifs/search" --data-urlencode "q=$1" --data-urlencode "api_key=$(cat "$kf")" --data-urlencode "limit=12" --data-urlencode "rating=pg-13" | python3 -c "$PICK" 2>/dev/null
}
url=""
if [ -n "$QUERY" ] && [ -f "$kf" ]; then
  read -ra w <<< "$QUERY"; n=${#w[@]}
  for try in "$QUERY" "${w[*]:0:3}" "${w[*]:0:2}" "${w[0]} ${w[$((n-1))]}" "${w[$((n-1))]}" "anime ${w[$((n-1))]}" "anime reaction"; do
    url=$(giphy_search "$try"); [ -n "$url" ] && { echo "matched: $try"; break; }
  done
  [ -z "$url" ] && { echo "no giphy results for '$QUERY'"; exit 2; }
fi
# ~1% of the time: a trending Giphy meme
if [ -z "$url" ] && [ -f "$kf" ] && [ "$MEME" != 0 ] && { [ "$MEME" = 1 ] || [ $((RANDOM % 100)) -eq 0 ]; }; then
  dir="$base/meme/trending"; mkdir -p "$dir"
  url=$(curl -s --max-time 6 "https://api.giphy.com/v1/gifs/trending?api_key=$(cat "$kf")&limit=50&rating=pg-13" | python3 -c "$PICK" 2>/dev/null)
fi
[ -z "$url" ] && url=$(curl -s --max-time 6 "https://nekos.best/api/v2/$ep" | python3 -c "import sys,json;print(json.load(sys.stdin)['results'][0]['url'])" 2>/dev/null)
f=""
if [ -n "$url" ]; then
  f="$dir/$(date +%s)_$RANDOM.gif"
  curl -s --max-time 20 -o "$f" "$url"; [ -s "$f" ] || { rm -f "$f"; f=""; }
fi
[ -z "$f" ] && f=$(find "$dir" -iname '*.gif' | shuf -n1)
[ -z "$f" ] && { echo "no gifs for $mood"; exit 1; }
play "$f"
