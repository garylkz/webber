---
id: googlemaps
type: module-design
status: draft
title: Google Maps Link Expander
parent: webber-architecture
---

# Google Maps Link Expander: Specification

A short Google Maps link (`maps.app.goo.gl/...`) turned into a full
`google.com/maps/place` link. It started as the owner's global `google-map.sh`
agent script and was moved here in Python.

Implemented in [googlemaps_link.py](googlemaps_link.py).

---

## Usage

```bash
lib/.venv/bin/python lib/googlemaps/googlemaps_link.py "https://maps.app.goo.gl/XXXX"
```

## Output

```json
{"short_url": "https://maps.app.goo.gl/XXXX", "url": "https://www.google.com/maps/place/...", "kind": "place"}
```

`kind` is one of:

| kind | The redirect | url |
| --- | --- | --- |
| `place` | a `/maps/place/` link | the redirect as it is |
| `place_id` | a search for `place_id:...` | `https://www.google.com/maps/place/?q=place_id:...` |
| `coordinates` | a search for `lat, lon` | `https://www.google.com/maps/place/@lat,lon`; the pin resolves to the nearest address, not the exact place |
| `query` | a search for text | `https://www.google.com/maps/place/<text>` |
| `redirect` | anything else | the redirect as it is |

## Design

- **One request, no page.** The script sends a HEAD request (a GET when the
  server refuses HEAD) without following redirects, and reads only the
  `Location` header. A path-only redirect is made absolute on
  `maps.google.com`.
- **Only goo.gl.** A link whose host is not `maps.app.goo.gl` or
  `goo.gl/maps` is refused before any request, so the script fetches no other
  host.
- **The `ftid` in a link is not a place id**, so no `place_id:` link is built
  from it.
- A link that does not redirect is an error: `No redirect found (not a short
  link?)` on stderr and exit 1.

## Testing

`test_googlemaps.py` runs `classify` over each kind of redirect, the host
check, and the path-only redirect with `requests.head` replaced. Nothing in it
touches the network.
