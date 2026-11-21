#!/usr/bin/env bash
# Downloads a starter pack. Never deletes anything. usage: download_pack.sh [amount]
amt="${1:-5}"; base="$HOME/.local/share/meimei/reactions"; kf="$HOME/.local/share/meimei/secrets/giphy_key"
dl_anime() {
  ep="$1"; folder="$ep"; [ "$ep" = cry ] && folder=sad; d="$base/$folder"; mkdir -p "$d"
  curl -s --max-time 15 "https://nekos.best/api/v2/$ep?amount=$amt" | python3 -c "import sys,json;[print(r['url']) for r in json.load(sys.stdin)['results']]" 2>/dev/null | while read -r u; do
    f="$d/${u##*/}"; [ -s "$f" ] || curl -s --max-time 30 -o "$f" "$u"; [ -s "$f" ] || rm -f "$f"
  done
  echo "anime $folder: $(ls "$d" | wc -l)"
}
dl_giphy() {
  q="$1"; slug=$(printf '%s' "${q,,}" | tr -cs 'a-z0-9' '_' | sed 's/^_//;s/_$//'); d="$base/meme/$slug"; mkdir -p "$d"
  curl -s --max-time 15 -G "https://api.giphy.com/v1/gifs/search" --data-urlencode "q=$q" --data-urlencode "api_key=$(cat "$kf")" --data-urlencode "limit=15" --data-urlencode "rating=pg-13" | python3 -c "
import sys,json,re
B=re.compile(r'brain.?rot|skibidi|sigma|rizz|gyat|fanum|ohio|mewing|looksmax|italian|tung.?tung|sahur|bombardiro|tralalero|ballerina|capuccina|slop',re.I)
n=0
for x in json.load(sys.stdin)['data']:
    if B.search(x.get('title','')+' '+x.get('slug','')+' '+x.get('username','')): continue
    print(x['id']+' '+x['images']['downsized']['url']); n+=1
    if n>=$amt: break" 2>/dev/null | while read -r id u; do
    f="$d/$id.gif"; [ -s "$f" ] || curl -s --max-time 30 -o "$f" "$u"; [ -s "$f" ] || rm -f "$f"
  done
  echo "giphy $slug: $(ls "$d" | wc -l)"
}
export -f dl_anime dl_giphy; export amt base kf
printf '%s\n' happy cry angry smug shocked laugh blush facepalm pout think thumbsup wave sleep bored yawn stare shrug confused smile wink clap highfive teehee bleh baka bonk nope nod dance salute tableflip yeet shake poke run | xargs -P5 -I{} bash -c 'dl_anime "{}"'
printf '%s\n' "an iq too high" "anime headdesk" "anime confused" "anime sweating nervous" "here we go again cj" "anime slam table" "anime rage" "anime crying" "anime shocked" "anime smug" "anime facepalm" "anime laugh" "anime excited" "anime disappointed" "anime sarcastic clap" "anime what" | xargs -P4 -I{} bash -c 'dl_giphy "{}"'
echo ALL-DONE
