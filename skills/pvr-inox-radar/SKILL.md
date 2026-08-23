---
name: pvr-inox-radar
description: >-
  Find and map PVR INOX movie showtimes anywhere in India. Use when the user
  wants movie showtimes, PVR or INOX shows, IMAX near me, movie tickets on a
  map, seats together for a group, recliners near me, cheapest recliner
  seats, what to watch tonight, or asks things like "Dune in IMAX Saturday
  night, 4 seats together, under 30 minutes from Sector 56 Gurgaon" or "what
  recliners are available near me tonight, 2 seats together, cheapest
  first". Renders
  one self-contained HTML map: cinemas as pins, showtime chips colored by
  availability, exact seats-together counts, travel-time labels, and every
  chip deep-linking into PVR's own seat-selection page. Read-only and
  personal-use: it never books; the user buys on pvrcinemas.com. Covers about
  116 Indian cities (Gurgaon, Delhi, Noida, Mumbai, Bengaluru, and more) and
  formats like IMAX, 4DX, ATMOS, LASER, PLAYHOUSE, DIRECTOR'S CUT, INSIGNIA.
license: MIT
metadata:
  author: karanb192
---

# pvr-inox-radar

Turn a movie-night ask into one HTML map of PVR INOX shows, with verified
seats-together counts for the shortlist and a deep link into PVR's own
booking page on every chip.

## Iron rules first

- **Politeness.** The client enforces a minimum 0.6s gap between API calls,
  strictly sequential. A 403 or 429 means the upstream blocked us: STOP the
  whole run, tell the user, never retry in the same turn (the cooldown is 15
  minutes and persists across processes). Never hit the API in parallel.
  Seat maps are fetched only for the top shortlisted shows, never in bulk.
- **Read-only.** Never book, hold, or poll. The user buys tickets themselves
  on pvrcinemas.com; every show links straight into PVR's seat page.
- **Credit.** Client engineering adapted from notprashanth/pvr-inox-mcp
  (MIT); see LICENSE. Never claim this project discovered the API.

## Parse the query

Extract from the user's ask:

- **Film**: title substring ("Dune"). Optional; omit to list everything.
- **Format**: IMAX, 4DX, ATMOS, LASER, GOLD, INSIGNIA, PLAYHOUSE,
  DIRECTOR'S CUT, ICE. Optional.
- **Date**: run `date` first, then resolve weekday words to YYYY-MM-DD in
  IST ("Saturday" means the coming Saturday in IST, not local time).
- **Time window**: evening = 18:00 to 23:59, night = 20:00 onward,
  matinee = before 17:00. Pass as --time-from / --time-to (24h IST).
  Relative asks ("starting in the next 30 to 90 minutes", "something we
  can leave for now") are computed from `date` in IST: now+30min to
  now+90min, rounded to minutes.
- **Party size**: "4 seats together" means --party-size 4.
- **Seat tier**: "recliners", "recliner seats", "loungers" means
  --tier recliner (or the asked word). This counts seats only inside
  matching priced rows (mixed halls name them, e.g. RECLINER ROWS) and
  counts whole halls only for recliner-native houses (Director's Cut,
  INSIGNIA, LUXE, Gold). For tier asks also pass --seat-detail 6 or more:
  a mixed hall's recliners are invisible until its seat map is opened, and
  the map honestly drops verified halls that have no such tier
  (meta.excluded_by_tier says how many).
- **Price**: "under Rs 500" means --max-price 500; "cheapest" means
  --sort cheapest. Prices live ONLY inside seat maps (show listings carry
  none), so pass --seat-detail 8 for price asks and say that unverified
  shows carry no price rather than a guessed one. The map's Price column
  shows "from Rs N": the matched tier's per-ticket gross, or the hall's
  cheapest tier (convenience fee and GST are extra at checkout).
- **Origin**: a locality ("Sector 56 Gurgaon") or a bare city ("Gurugram").
  After resolving it (geocode or city centre), STATE the resolved place and
  coordinates in one line before running ("Using Sector 56, Gurugram at
  28.44, 77.06; say so if that is wrong") so the user can correct the
  location. The map labels this origin as "You: <label>"; if the user
  corrects it, geocode the new place and re-run.
- **City**: usually implied by the locality. If genuinely ambiguous, restate
  the task in one line and ask ONE question instead of guessing.

## When to geocode

Only when the user names a locality finer than a city. Run once and reuse:

    python3 scripts/geocode.py "Sector 56, Gurugram"

It prints one JSON object: `{"lat": ..., "lng": ..., "display_name": ...,
"source": "nominatim", "cached": true|false}` (cached; at most 1 Nominatim
request per second). A bare city name skips geocoding entirely: radar.py
resolves city-centre coordinates itself. Never geocode in a loop.

## Run the radar

    python3 scripts/radar.py --city Gurugram --lat 28.42 --lng 77.09 \
      --origin-label "Sector 56 Gurgaon" --origin-source geocode \
      --movie "Dune" --format IMAX --date 2026-08-22 \
      --time-from 18:00 --party-size 4 --seat-detail 4 > radar.json

Flag guidance:

- --lat/--lng plus --origin-label/--origin-source: pass the geocoded
  coordinates and label; omit all four for a bare city (city centre is used).
  --origin-source takes exactly two values: "geocode" when the coordinates
  came from geocode.py, "caller" (the default) when the user supplied
  coordinates directly.
- --max-km: defaults to 6 km with caller coordinates and no format, else
  60 km (a premium format is a city-level resource). Widen it when the user
  says "anywhere in the city" or results are thin.
- --seat-detail N: seat maps are fetched for the top N shows only (default
  3, cap 8, 0 disables). Raise it when the user cares a lot about exact
  seats; every increment is one more paced API call.
- Expect the run to take about 15 to 25 seconds on cold caches: calls are
  deliberately spaced 0.6s apart and real network latency adds on top. That
  is the politeness budget, not a bug.
- Exit codes: 0 = answer (including partial and closed-date answers),
  2 = usage (bad flags, a city PVR does not serve, or a city with no
  published coordinates: pass --lat and --lng), 3 = blocked before any
  results, 4 = unexpected (including call-ceiling exhaustion before any
  results existed). Stdout is always one JSON document; logs go to stderr.

## Render and open

    python3 scripts/render_map.py --in radar.json --out map.html

Open map.html in the user's browser. Also give a compact ranked summary in
chat (top 3 to 5 shows: venue, time, format, seats-together verdict, drive
estimate, deep link) so the answer survives without the map.

Name the output per query so maps do not clobber each other:
map-<movie-or-all>-<date>.html (e.g. map-awarapan-2026-08-21.html). One
HTML answers one QUERY: an all-films ask is one map holding every film;
a movie ask is one map for that film.

**Ratings (fetch by DEFAULT for any ask that names a film, and for the
top 3 to 5 films of an all-films ask; skip only if the user says skip):**
the scripts never fetch ratings and need no API key; YOU look them up.
After the radar run, take the distinct film titles from radar.json, look
up each rating with your own web search (IMDb preferred; say the source
you actually found; third-party mirrors misreport, prefer imdb.com
itself), then render with them; rated films appear as a strip under the
stats and as a table column:

    python3 scripts/render_map.py --in radar.json --out map.html \
      --ratings '{"AWARAPAN 2 (HINDI)": {"rating": "7.2", "source": "IMDb"}}'

Keys match film names case-insensitively. Tell the user ratings were
looked up by you at ask time and are approximate; never invent one, and
skip the flag entirely for films you could not verify.

## Present honestly

- Verified seat counts and unverified status labels are different things;
  label them differently, as the map does. Never present an unverified show
  as bookable-for-N.
- "Closed" means the date is not on sale yet (PVR sells a rolling window of
  about 5 days). It never means sold out; say so.
- Drive times are estimates (heuristic, or one OSRM table call); say "est.".
- Availability labels can lag in both directions; counted seats are exact
  at fetch time.
- A withheld seat is indistinguishable from a sold one without seat
  history. Free counts are a floor, not a ceiling.

## Handling trouble

- **Blocked (exit 3 or meta.error UPSTREAM_BLOCKED)**: stop, tell the user
  PVR's edge is rate-limiting and to retry after about 15 minutes. Present
  any partial results, labeled partial. Never retry in the same turn.
- **Closed date (meta.booking_open false)**: explain the rolling ~5 day
  window and offer to run an open date instead.
- **Mixed open and closed venues**: when results are empty or thin but
  meta.booking_open is still true, check venues[].booking_open and the
  meta.cinemas_queried outcomes before concluding "no shows": some venues
  may simply not be on sale yet for that date (outcome "closed", which is
  never sold out). Say which venues are open with no match and which are
  not selling yet; that distinction is the point of this tool.
- **Empty results**: relax in this order: set --seat-detail to 0 (see more,
  verify less), widen --max-km, widen the time window. Never silently drop
  the film or the format; ask before changing what the user asked for.

## Do not

- Book tickets, hold seats, or automate any purchase step.
- Poll, watch, or loop on availability.
- Hit the API in parallel or below the 0.6s spacing.
- Call content/cinemasessions (known-broken endpoint; the client never does).
- Guess what an unmapped status color means; say "unknown".
- Claim we discovered the API or the row-release behavior.
