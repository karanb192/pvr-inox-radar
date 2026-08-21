# PVR INOX web API notes

Trimmed field reference for the endpoints this skill calls, verified against
live captures from 2026-08-21 (the raw responses are the repo's
tests/fixtures/). The API surface, header requirements, and closed-date
semantics were mapped by notprashanth/pvr-inox-mcp (MIT) by Prashanth
Krishnan (https://github.com/notprashanth/pvr-inox-mcp); this skill adapts
that work with credit and discovered none of it.

## Transport

Base: `https://api3.pvrcinemas.com/api/v1/booking`, all POST JSON.

Required headers on every call: `Content-Type: application/json`,
`Accept: application/json, text/plain, */*`, `Authorization: "Bearer "`
(deliberately blank token; the API 403s without the header), `chain: PVR`,
`country: INDIA`, `appVersion: 1.0`, `platform: WEBSITE`, `flow: PVRINOX`,
`city: <city>` (default Chennai for city-less endpoints).

Politeness is mandatory: at least 0.6s between calls, strictly sequential,
and a 403 or 429 means blocked (the Akamai edge blocks IPs that hammer it):
stop the whole run. pvr_client.py enforces all of this, including a
persisted 15 minute cooldown.

## Endpoints used

- `content/city` body `{"lat","lng"}` (strings): about 116 cities with
  coords. A few (4 in the capture: Gangtok, Jabalpur, Leh, Muzaffarpur)
  publish no coordinates. Metro rollups like Mumbai-All carry subcities.
- `content/cinemas` body `{"city","lat","lng","text":""}`: venues with
  theatreId, name, latitude, longitude, showCount, screens (screenType per
  screen, mixed case: "Premium", "Atmos"; normalize before matching).
  Distances echoed by the API are untrustworthy; compute locally.
- `content/nowshowing` body `{"city","lat","lng"}`: films with per-print
  variants (filmId to name, language, format). A schedule block's title can
  span many filmIds; the per-show movieId is the true identity.
- `content/csessions` body `{"city","cid","lat","lng","dated":"YYYY-MM-DD",
  "qr":"NO","cineType":"","cineTypeQR":""}`: one venue's shows for one date.
  Three-way outcome: payload status 302 with output = open; any other JSON
  body = closed (date not on sale YET, never sold out); transport HTTP 500 =
  also closed (verified live: an unopened date can answer 500); any other
  transport failure = error. Top-level output.showCount is 0 even with 59
  shows inside: count shows yourself.
- `ticketing/seatlayout` body `{"encrypted": "<token from csessions>"}`:
  the per-show seat map. Success is payload status 200 with output.
- NEVER call `content/cinemasessions`: the reference project measured it
  ignoring the date and lagging across midnight, and deleted it.

## Show fields that matter

Per csessions show: sessionId, movieId, showDate, showTime ("06:05 PM"),
showTimeStamp / endTimeStamp (epoch ms, the reliable times), screenName,
screenType (upper case: IMAX, 4DX, PLAYHOUSE, ...), movieFormat,
soundFormat, language, subtitle, statusCode (a hex chip color, see
status-colors.md), statusTxt, encrypted.

Deep link per show: `https://www.pvrcinemas.com/seatlayout/<encrypted>`.

## Seat map fields that matter

- rows[] interleaves `t == "area"` price-tier headers (apply to following
  seat rows until the next header; a tier can repeat) and `t == "seats"`
  rows. Rows arrive FRONT-FIRST; letters usually descend toward the back,
  so row A is commonly the BACK row.
- Cell: `sn` empty = aisle or padding (breaks seats-together runs); `s` 1 =
  free, 2 = sold, anything else = withheld/held; `displaynumber` is the
  in-row seat number, stored right to left; `c`/`pc` joins priceList for
  the per-seat price; `st` is NOT availability; `hc` marks handicap seats.
- Withheld rows (the reference README's key insight, credited): PVR opens
  dates with whole rows withheld and releases them later, so a withheld
  seat is indistinguishable from a sold one without seat history. Free
  counts are a floor, not a ceiling.

## Time traps

- Everything user-facing is IST; "today" means today in IST, never the
  machine's local date.
- seatlayout showTime/endTime are UTC despite carrying no timezone marker
  (verified live: showTime "2026-08-22 12:35:00" for an 18:05 IST show).
  Use showDateTime (an IST display string) or the csessions epoch stamps.
