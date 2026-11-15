#!/bin/sh
#Fetch a Hacker News comment thread and print it as one JSON object.
#Usage: hacker_news_comments.sh <item url | item id>
#The JSON holds the id, title, points, url, comments_url, the number of
#top-level comments, how many comments are shown, the depth limit, and the
#comment tree: each comment has its text and its replies. When the only
#top-level comment says "Comments moved to item?id=X", it fetches X instead and
#sets moved_from to the original id (null otherwise).
#Uses the Algolia HN API, which returns the whole comment tree as JSON. Needs curl and jq.
set -u

API=https://hn.algolia.com/api/v1/items
MAX_COMMENTS=80
MAX_DEPTH=4
MAX_CHARS=700

item=
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,8s/^#//p' "$0"; exit 0 ;;
    -?*) echo "hacker_news_comments: unknown option: $1" >&2; exit 2 ;;
    *) item=$1; shift ;;
  esac
done
[ -n "$item" ] || { echo "Usage: hacker_news_comments.sh <item url | item id>" >&2; exit 2; }

#Accept an item url or a bare item id.
id=$(printf '%s\n' "$item" | sed -n 's/.*id=\([0-9][0-9]*\).*/\1/p' | sed -n 1p)
if [ -z "$id" ]; then
  case "$item" in
    *[!0-9]*) ;;
    *) id=$item ;;
  esac
fi
[ -n "$id" ] || { echo "Could not resolve an item id from the argument." >&2; exit 1; }

#Comment HTML to plain text, shared by every jq program below.
JQ_LIB='
def hex: ascii_downcase | explode | reduce .[] as $c (0; . * 16 + (if $c >= 97 then $c - 87 else $c - 48 end));
def entity: if startswith("#x") or startswith("#X") then [.[2:] | hex] | implode
  elif startswith("#") then [.[1:] | tonumber] | implode
  else {"amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'"'"'", "nbsp": " "}[.] // "&\(.);" end;
def clean: (. // "")
  | gsub("<p>"; "\n")
  | gsub("<a [^>]*href=\"(?<h>[^\"]+)\"[^>]*>.*?</a>"; .h)
  | gsub("<[^>]+>"; "")
  | gsub("&(?<e>#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);"; .e | entity)
  | sub("^\\s+"; "") | sub("\\s+$"; "");
'

fetch() {
  body=$(curl -fsS --connect-timeout 10 --max-time 60 "$API/$1" 2>&1) || { echo "Error fetching thread: $body" >&2; exit 1; }
  printf '%s\n' "$body" | jq -e 'type == "object"' >/dev/null 2>&1 || { echo "Error fetching thread: the API did not return a JSON object" >&2; exit 1; }
  printf '%s\n' "$body"
}

data=$(fetch "$id") || exit 1

#If the only top-level comment says the comments moved, fetch the new thread.
target=$(printf '%s\n' "$data" | jq -r "$JQ_LIB"'
  [.children[]? | select((.text // "") != "")]
  | if length == 1 then (.[0].text | clean | capture("(?i)comments moved to [\\s\\S]*?item\\?id=(?<id>[0-9]+)").id) // empty else empty end')
moved_from=null
if [ -n "$target" ] && [ "$target" != "$id" ]; then
  moved_from=$id id=$target
  data=$(fetch "$id") || exit 1
fi

printf '%s\n' "$data" | jq --argjson id "$id" --argjson moved "$moved_from" \
  --argjson max "$MAX_COMMENTS" --argjson depth "$MAX_DEPTH" --argjson chars "$MAX_CHARS" "$JQ_LIB"'
def trim_body: if length > $chars then (.[:$chars] | sub("\\s+$"; "")) + " [...]" else . end;
# Input [node, count]; output [comments, count]. Walks in reading order and stops at $max comments.
def tree($d): .[0] as $node
  | reduce ($node.children // [] | .[] | select((.text // "") != "")) as $c ([[], .[1]];
      if .[1] >= $max then . else
        (.[1] + 1) as $n
        | (if $d + 1 < $depth then [$c, $n] | tree($d + 1) else [[], $n] end) as $r
        | [.[0] + [{text: ($c.text | clean | trim_body), replies: $r[0]}], $r[1]]
      end);
([., 0] | tree(0)) as [$comments, $shown]
| (.children // [] | length) as $top
| {id: $id, title, points, url,
   comments_url: "https://news.ycombinator.com/item?id=\($id)",
   moved_from: $moved, top_level_comments: $top, shown: $shown,
   max_depth: $depth, comments: $comments}
  + (if $comments == [] then {note: "No comments yet."} else {} end)'
