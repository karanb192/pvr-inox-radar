# pvr-inox-radar

**Ask your coding agent "Dune, IMAX, Saturday night, 4 seats together, under 30 min from Sector 56 Gurgaon" and get one HTML map back: every PVR INOX venue as a pin, showtime chips colored by availability, exact seats-together counts for the shortlist, drive-time estimates, and every chip deep-linking into PVR's own seat page.**

A Claude Code / Codex / Gemini skill. Read-only and personal-use: it never books anything. You pick a chip, land on pvrcinemas.com, and buy there. Scope: the merged PVR INOX chain only; other chains and BookMyShow listings are out of scope (see Roadmap).

## Good queries

- "Dune in IMAX Saturday night, 4 seats together, under 30 minutes from Sector 56 Gurgaon"
- "What's playing in 4DX in Bengaluru this weekend?"
- "Hindi shows after 8pm tonight near Indiranagar, 2 seats together"
- "Is the Friday IMAX at Ambience Mall filling up? We are 6 people"

The skill parses the ask, geocodes the locality if there is one (Nominatim, cached), sweeps the city's venues for that date, verifies seats-together on the top few shows from the live seat map, and renders the map.

## What the map shows

- **Pins**: green = verified seats for your party, amber = available or filling (PVR's label, unverified), gray = unknown or no match, hollow = date not on sale yet at that venue (which is not the same as sold out).
- **Chips**: one per show. Solid chips carry a counted seats-together verdict from the real seat map ("4 together"). Dashed chips with a "?" carry only PVR's own status color, honestly unverified. Every chip links to that exact show's seat page.
- **A plain table under the map** with the same answer, so the page works even with no JS or no map tiles.
- Distance and a drive-time estimate per venue, always labeled as an estimate.

## Install

```
# Claude Code plugin
/plugin marketplace add karanb192/pvr-inox-radar
/plugin install pvr-inox-radar@pvr-inox-radar

# Same thing from a plain terminal
claude plugin marketplace add karanb192/pvr-inox-radar
claude plugin install pvr-inox-radar@pvr-inox-radar

# skills.sh one-liner (Claude Code, Codex, Cursor, Gemini and other agents)
npx skills add karanb192/pvr-inox-radar

# Review-first: clone, read it, install the bytes you just read
git clone https://github.com/karanb192/pvr-inox-radar.git
cd pvr-inox-radar && ./install.sh          # also: codex, gemini, all
```

Run from a checkout, `install.sh` never touches the network: it copies the files you just read into your agent's skills directory (`~/.claude/skills/`, `~/.agents/skills/`, `~/.codex/skills/`, `~/.gemini/skills/`), or project-local with `--here` / `--project DIR`, then runs an offline self-test on each installed copy.

Requirements: `python3` 3.9+ (stdlib only, zero pip installs).

Then open your agent and ask: "Dune, IMAX, Saturday night, 4 seats together, under 30 min from Sector 56 Gurgaon".

## How it works

The data source is the JSON API behind the PVR INOX web app (unofficial, no login: the API accepts a deliberately blank bearer token). One radar run is a fixed, paced call budget:

1. One call lists the city's cinemas, plus one city-coordinates call and one now-showing (film variants) call on cold caches, cached thereafter; distance is computed locally from your origin, never trusted from the API.
2. Up to 12 calls fetch each nearby venue's shows for the date (capability-first when you asked for a format like IMAX). A date that is not on sale yet answers differently from a sold-out one, and the tool keeps those separate everywhere.
3. Seat maps are fetched for the top few shortlisted shows only (default 3, hard cap 8). From each map it counts the longest run of free adjacent seats in the good-seats zone of that hall, aisle-aware, so "4 together" is a counted fact, not a status label.
4. `render_map.py` writes one self-contained map.html (Leaflet + OpenStreetMap tiles are the only external references, SRI-pinned).

Status labels get one honest caveat baked into every output: PVR's own availability text can lag in both directions, so labels are a first pass and counted seats beat them.

Everything cross-process (pacing clock, block cooldown, city coordinates, hall geometry) persists in `~/.cache/pvr-inox-radar/`, so repeat questions cost fewer calls.

## Politeness and personal use

This tool is built to be a polite guest on someone else's infrastructure:

- Minimum 0.6 seconds between any two API calls, strictly sequential, never parallel.
- A 403 or 429 stops the whole run and trips a 15-minute cooldown that survives across processes. No retry loops, ever.
- Hard per-run ceiling of 24 calls. Seat maps only for the shortlist. No polling, no watch daemons, no bulk sweeps.
- Read-only by design: no booking automation exists in this codebase. The deep links hand you to PVR's own purchase flow, where you pay them for a ticket like anyone else.

Use it for your own movie nights. Do not wrap it in a hosted service or a scraper farm; PVR's edge is Akamai-fronted and will block you, and you will have earned it.

## Tests

The default suite is fully offline (captured fixtures and mocked transport, zero live calls):

```
python3 -m unittest discover -s tests
```

One deliberate, three-call live smoke test exists, excluded from discovery and off unless you opt in:

```
PVR_LIVE=1 python3 -m unittest tests.live_smoke
```

## Credits

- API client engineering adapted from [notprashanth/pvr-inox-mcp](https://github.com/notprashanth/pvr-inox-mcp) (MIT) by Prashanth Krishnan, including the withheld-rows seat insight; see LICENSE.
- [anthropics/skills](https://github.com/anthropics/skills): the skill-structure conventions this follows.
- Map rendering: [Leaflet](https://leafletjs.com), tiles (c) [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors. Geocoding: [Nominatim](https://nominatim.org), used within its 1 request/second policy.

## Roadmap

- **Drop watcher**: an opt-in, still-polite way to catch a withheld row releasing or a housefull show restocking (the row-release behavior above is what makes this worth building). Not built yet, and only ships if it can stay within the politeness rules; the current tool deliberately contains no polling.
- **BMS cross-chain sweep**: the same ask answered across BookMyShow listings too, so non-PVR cinemas land on the same map.

## License

MIT. Portions adapted from the MIT-licensed [pvr-inox-mcp](https://github.com/notprashanth/pvr-inox-mcp); see LICENSE for the full notice.
