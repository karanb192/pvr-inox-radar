#!/usr/bin/env python3
"""Render a radar JSON document into ONE self-contained map.html.

Original code (no adapted client logic); the client engineering elsewhere in
this skill is adapted from notprashanth/pvr-inox-mcp (MIT) by Prashanth
Krishnan, credited in the page footer.

Usage:
    python3 scripts/render_map.py --in radar.json --out map.html
    ... radar.py ... | python3 scripts/render_map.py --in - --out map.html

The output embeds the radar JSON inline and performs zero fetches at view
time except: Leaflet 1.9.4 from unpkg (SRI-pinned) and OpenStreetMap
basemap tiles. A plain
semantic HTML table under the map always carries the full answer, so the
page still works with no JS, no CDN, or no tiles.
"""

import argparse
import html
import json
import sys

# SRI provenance: both hashes recomputed 2026-08-21 from the live unpkg
# assets (openssl dgst -sha256 -binary | base64) and matched. A mismatch
# would make browsers drop the asset; the fallback table still carries the
# full answer in that case.
LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_CSS_SRI = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
LEAFLET_JS_SRI = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="

FOOTER_CREDIT = (
    "Data: PVR INOX web API (unofficial, read-only). Client adapted from "
    "pvr-inox-mcp (MIT). Map: Leaflet, (c) OpenStreetMap contributors. "
    "Drive times are estimates. Book on pvrcinemas.com.")

LEGEND_LINE = ("availability labels come from PVR and can lag; counted "
               "seats are exact at fetch time")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# One palette, used by the legend swatches, the chip CSS, and the JS pin
# map below. Change it here and every surface moves together.
INK = {"ok": "#177245", "warn": "#b3540e", "full": "#b3261e",
       "muted": "#8a8375", "none": "#b5afa2", "origin": "#23508f"}


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def date_words(iso_date):
    """'2026-08-22' to 'Sat 22 Aug'; the raw string when unparseable."""
    try:
        year, month, day = (int(p) for p in str(iso_date).split("-"))
        import datetime
        weekday = WEEKDAYS[datetime.date(year, month, day).weekday()]
        return "%s %d %s" % (weekday, day, MONTHS[month - 1])
    except (ValueError, IndexError):
        return str(iso_date)


def query_words(query):
    """Restate the query in plain words for the page header (SPEC R74)."""
    parts = [query.get("movie") or "All films"]
    if query.get("format"):
        parts.append(str(query["format"]))
    if query.get("tier"):
        parts.append("%s seats" % str(query["tier"]).lower())
    if query.get("date"):
        parts.append(date_words(query["date"]))
    time_from, time_to = query.get("time_from"), query.get("time_to")
    if time_from and time_to:
        parts.append("%s to %s IST" % (time_from, time_to))
    elif time_from:
        parts.append("from %s IST" % time_from)
    elif time_to:
        parts.append("before %s IST" % time_to)
    party = query.get("party_size") or 1
    if party > 1:
        parts.append("%d seats together" % party)
    origin = query.get("origin") or {}
    label = origin.get("label") or ""
    max_km = query.get("max_km")
    if label and max_km:
        parts.append("within %.0f km of %s" % (float(max_km), label))
    elif label:
        parts.append("near %s" % label)
    return ", ".join(parts)


def tier_prefix(seats):
    """'recliner (Rs 400): ' when the counted seats sit in an asked-for tier;
    empty otherwise. Whole-hall premium matches carry the house name."""
    tier = (seats or {}).get("tier") or {}
    mode = tier.get("mode")
    if mode == "rows":
        gross = tier.get("gross")
        label = (tier.get("matched") or [tier.get("requested") or ""])[0]
        head = str(label).lower()
        return "%s (Rs %s): " % (head, gross) if gross else "%s: " % head
    if mode == "whole_hall":
        label = (tier.get("matched") or [""])[0]
        return "%s (whole hall): " % str(label).lower() if label else ""
    return ""


def seats_cell(show, party):
    """Fallback-table seats column: verified counts vs an honest 'unverified'."""
    seats = show.get("seats")
    if not seats:
        return "unverified"
    if seats.get("meets_party_size"):
        if party > 1:
            return "%d together: yes (best run %d)" % (party, seats["best_run"])
        return "%d free, best run %d" % (seats["free"], seats["best_run"])
    if seats.get("hall_meets_party_size"):
        return ("%d together: good rows full, fits elsewhere in hall (run %d)"
                % (party, seats["hall_best_run"]))
    return "%d together: no (best run %d)" % (party, seats["best_run"])


def status_cell(show):
    seats = show.get("seats")
    if seats:
        return "verified seat count"
    category = show.get("status_category") or "unknown"
    label = show.get("statusTxt") or ""
    return "%s (%s)" % (category, label) if label else category


def status_dot(show):
    """A colored dot class for the table's status cell."""
    if show.get("seats"):
        return "ok"
    category = show.get("status_category") or "unknown"
    if category in ("housefull", "soldout"):
        return "full"
    if category in ("available", "filling"):
        return "warn"
    return "muted"


def rating_words(entry):
    """One ratings entry ('8.1', 8.1, or {'rating':..,'source':..}) to a
    cell like '8.1 (IMDb)'. Ratings arrive from the calling agent's own
    lookup; this page only labels what it was handed."""
    if isinstance(entry, dict):
        value = entry.get("rating")
        source = entry.get("source") or ""
        text = str(value) if value is not None else ""
        return ("%s (%s)" % (text, source)) if text and source else text
    return str(entry) if entry is not None else ""


def price_value(seats):
    """Cheapest verified per-ticket gross as a float, or None: the matched
    tier first, else the cheapest hall tier. Feeds price_words and the
    table's numeric sort key."""
    if not seats:
        return None
    tier = seats.get("tier") or {}
    if tier.get("mode") in ("rows", "whole_hall") and tier.get("gross"):
        candidates = [tier["gross"]]
    else:
        candidates = [t.get("price") for t in seats.get("price_tiers") or []]
    values = []
    for text in candidates:
        try:
            values.append(float(text))
        except (TypeError, ValueError):
            continue
    return min(values) if values else None


def price_words(seats):
    """'from Rs 400' for a verified show, from its matched tier or the
    cheapest hall tier; empty when unverified (csessions carries no
    prices, so nothing is guessed)."""
    low = price_value(seats)
    if low is None:
        return ""
    text = ("%d" % low) if low == int(low) else ("%.2f" % low)
    return "from Rs %s" % text


def seats_sort_value(show):
    """Numeric sort key for the Seats column: counted-yes first, then
    good-rows-full-hall-yes, then counted-no, then unverified; bigger free
    runs sort earlier within each band."""
    seats = show.get("seats")
    if not seats:
        return 3000
    if seats.get("meets_party_size"):
        return 0 - min(int(seats.get("best_run") or 0), 999)
    if seats.get("hall_meets_party_size"):
        return 1000 - min(int(seats.get("hall_best_run") or 0), 999)
    return 2000 - min(int(seats.get("best_run") or 0), 999)


def films_strip(radar, ratings=None):
    """A visible strip of rated films under the stats, so ratings never
    live only in the table. One chip per rating key that matched a film
    actually on the page."""
    if not ratings:
        return ""
    films = []
    for show in radar.get("shows") or []:
        name = str(show.get("film") or "").strip().lower()
        if name and name not in films:
            films.append(name)
    chips = []
    seen = set()
    for key, entry in ratings.items():
        low = str(key).strip().lower()
        if low in seen:
            continue
        if any(low == f or low in f for f in films):
            seen.add(low)
            words = rating_words(entry)
            if words:
                chips.append('<span class="film-chip">%s<b>%s</b></span>'
                             % (esc(key), esc(words)))
    if not chips:
        return ""
    return '<section class="films">%s</section>' % "".join(chips)


def fallback_table(radar, ratings=None):
    """Always-rendered static table: the full answer without JS, CDN, or
    tiles (SPEC R75). One row per ranked show."""
    venues = {v.get("theatreId"): v for v in radar.get("venues") or []}
    party = (radar.get("query") or {}).get("party_size") or 1
    ratings = {str(k).strip().lower(): v for k, v in (ratings or {}).items()}
    rating_col = ('<th data-key="rating" data-num="1">Rating</th>'
                  if ratings else "")
    has_price = any(price_words(s.get("seats"))
                    for s in radar.get("shows") or [])
    price_col = ('<th data-key="price" data-num="1">Price</th>'
                 if has_price else "")
    head = ('<tr><th data-key="venue">Venue</th>'
            '<th data-key="km" data-num="1">Distance</th>'
            '<th data-key="drive" data-num="1">Drive est.</th>'
            '<th data-key="t" data-num="1">Show</th>%s'
            '<th data-key="fmt">Format</th>'
            '<th data-key="status">Status</th>'
            '<th data-key="seats" data-num="1">Seats</th>'
            "%s<th>Link</th></tr>" % (rating_col, price_col))
    rows = [head]
    for index, show in enumerate(radar.get("shows") or []):
        entry = None
        venue = venues.get(show.get("theatreId")) or {}
        km = venue.get("distance_km")
        drive = venue.get("drive_min_est")
        tokens = []
        seen = set()
        for part in (show.get("screenType"), show.get("movieFormat")):
            for word in str(part or "").split():
                # case-insensitive: "Atmos" + "ATMOS" is one format, not two
                if word.casefold() not in seen:
                    seen.add(word.casefold())
                    tokens.append(word)
        # No format data means an empty cell, not an invented "2D": the API
        # left it blank and this page does not guess.
        fmt = " ".join(tokens)
        link = show.get("deep_link") or ""
        link_cell = ('<a class="book" href="%s" target="_blank" '
                     'rel="noopener">book</a>' % esc(link)) if link else ""
        seats_text = tier_prefix(show.get("seats")) + seats_cell(show, party)
        seats_html = ('<strong>%s</strong>' % esc(seats_text)
                      if show.get("seats", {}) and
                      show["seats"].get("meets_party_size")
                      else esc(seats_text))
        rating_cell = ""
        if ratings:
            film = str(show.get("film") or "").strip().lower()
            entry = ratings.get(film)
            if entry is None:
                # Prints vary ("X (HINDI)", "X (HINDI ATMOS)"): a rating
                # keyed on the base title covers them all.
                for key, value in ratings.items():
                    if key and key in film:
                        entry = value
                        break
            rating_cell = '<td class="num">%s</td>' % esc(rating_words(entry))
        price_cell = ('<td class="num">%s</td>'
                      % esc(price_words(show.get("seats")))
                      if has_price else "")
        # Row-level sort keys for the client-side header sorter; every
        # value is derived and escaped, nothing user-controlled leaks in
        # unescaped.
        attrs = [
            'data-i="%d"' % index,
            'data-venue="%s"' % esc(
                str(venue.get("name") or "").casefold()),
            'data-km="%s"' % (("%.3f" % km) if km is not None else "999999"),
            'data-drive="%s"' % (drive if drive is not None else "999999"),
            'data-t="%s"' % (show.get("showTimeStamp") or "999999999999"),
            'data-fmt="%s"' % esc(fmt.casefold()),
            'data-status="%s"' % ("0 verified" if show.get("seats")
                                  else esc("1 %s" % (
                                      show.get("status_category")
                                      or "unknown"))),
            'data-seats="%d"' % seats_sort_value(show),
        ]
        if has_price:
            value = price_value(show.get("seats"))
            attrs.append('data-price="%s"'
                         % (("%.2f" % value) if value is not None
                            else "999999"))
        if ratings:
            try:
                rating_num = float((entry or {}).get("rating") or "")
            except ValueError:
                rating_num = -1.0
            attrs.append('data-rating="%s"' % rating_num)
        rows.append(
            '<tr %s>' % " ".join(attrs) +
            '<td class="c-venue">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td>%s</td>%s<td class="c-fmt">%s</td>'
            '<td><span class="dot %s"></span>%s</td><td>%s</td>%s'
            '<td class="c-link">%s</td></tr>' % (
                esc(venue.get("name") or show.get("theatreId")),
                esc("%.1f km" % km) if km is not None else "",
                esc("%d min (%s)" % (drive, venue.get("drive_min_source") or
                                     "heuristic")) if drive else "",
                esc("%s %s" % (show.get("showTime") or "?",
                               show.get("film") or "")),
                rating_cell,
                esc(fmt),
                status_dot(show),
                esc(status_cell(show)),
                seats_html,
                price_cell,
                link_cell))
    if len(rows) == 1:
        # Empty state keeps the per-venue open/closed distinction the pins
        # carry, so the no-JS table never flattens "not on sale yet" into
        # "no shows matched" (they are different answers).
        for venue in radar.get("venues") or []:
            km = venue.get("distance_km")
            drive = venue.get("drive_min_est")
            outcome = ("booking not open yet for this date (not sold out)"
                       if venue.get("booking_open") is False
                       else "no shows matched the query")
            rows.append(
                '<tr><td class="c-venue">%s</td><td class="num">%s</td>'
                '<td class="num">%s</td>'
                '<td colspan="%d">%s</td></tr>' % (
                    esc(venue.get("name") or venue.get("theatreId")),
                    esc("%.1f km" % km) if km is not None else "",
                    esc("%d min (%s)" % (drive, venue.get("drive_min_source")
                                         or "heuristic")) if drive else "",
                    5 + (1 if ratings else 0),
                    esc(outcome)))
        if len(rows) == 1:
            rows.append('<tr><td colspan="%d">no shows matched</td></tr>'
                        % (8 + (1 if ratings else 0)))
    note = ""
    if radar.get("shows"):
        order = ("cheapest verified first, unpriced after in time order"
                 if (radar.get("query") or {}).get("sort") == "cheapest"
                 else "soonest show first, then nearest venue")
        note = ('<p class="table-note">Order: %s. Click a column header to '
                're-sort; a third click restores this order.</p>'
                % esc(order))
    return note + "<table>%s</table>" % "".join(rows)


def notice_banner(radar):
    meta = radar.get("meta") or {}
    query = radar.get("query") or {}
    party = query.get("party_size") or 1
    notes = []
    if meta.get("partial"):
        notes.append("Partial results: the run stopped early (%s)."
                     % (meta.get("error") or "unknown reason"))
    if meta.get("booking_open") is False:
        notes.append("Booking not open: %s." % (meta.get("message") or
                                                "date not on sale yet"))
    excluded = meta.get("excluded_by_party") or 0
    if excluded:
        notes.append("%d show(s) dropped: the live seat map could not seat "
                     "%d together." % (excluded, party))
    tier_excluded = meta.get("excluded_by_tier") or 0
    if tier_excluded:
        notes.append("%d verified show(s) dropped: no %s tier in that "
                     "hall." % (tier_excluded,
                                query.get("tier") or "matching"))
    price_excluded = meta.get("excluded_by_price") or 0
    if price_excluded:
        cap = query.get("max_price")
        cap_text = ("%g" % cap) if isinstance(cap, (int, float)) else "the"
        notes.append("%d verified show(s) dropped: over the Rs %s cap."
                     % (price_excluded, cap_text))
    # An Indiranagar run once spent its whole seat budget, dropped every
    # verified show, and left a page of "unverified" with no explanation:
    # when that happens, the page says so instead of looking broken.
    shows = radar.get("shows") or []
    if (shows and (excluded or tier_excluded or price_excluded)
            and not any(s.get("seats") for s in shows)):
        notes.append("Every seat map this run opened was dropped by the "
                     "filters above, so each remaining show is honestly "
                     "unverified. A higher seat-detail or a wider time "
                     "window verifies more.")
    if not notes:
        return ""
    return '<p class="notice">%s</p>' % esc(" ".join(notes))


def stats_strip(radar):
    """A small row of computed numbers under the header. Purely derived
    from the document; omitted when there is nothing to count."""
    venues = radar.get("venues") or []
    shows = radar.get("shows") or []
    if not venues:
        return ""
    verified = sum(1 for s in shows if s.get("seats"))
    films = {str(s.get("film") or "").strip().casefold()
             for s in shows if str(s.get("film") or "").strip()}
    drives = [v.get("drive_min_est") for v in venues
              if v.get("drive_min_est") is not None]
    items = [
        ("%d" % len(venues), "venue" if len(venues) == 1 else "venues"),
        ("%d" % len(shows), "show" if len(shows) == 1 else "shows"),
    ]
    # An advance-sale date can be 30 shows of ONE film; the films count is
    # what makes that legible from the header instead of a table scroll.
    if films:
        items.append(("%d" % len(films),
                      "film" if len(films) == 1 else "films"))
    items.append(("%d" % verified, "seat-verified"))
    if drives:
        items.append(("%d min" % min(drives), "closest drive"))
    cells = "".join(
        '<div class="stat"><div class="stat-n">%s</div>'
        '<div class="stat-l">%s</div></div>' % (esc(n), esc(label))
        for n, label in items)
    return '<section class="stats">%s</section>' % cells


# Design notes: warm paper ground, one serif voice for the question, one
# sans voice for data, hairlines instead of boxes, and the palette from INK
# shared with the pins and chips. No webfonts: the allowed-hosts test keeps
# this page at zero requests beyond Leaflet and tiles.
CSS = """
* { box-sizing: border-box; }
:root {
  --paper: #f7f5f0; --card: #fffdf9; --ink: #16130d; --sub: #7a7264;
  --line: #e6e1d6; --ok: %(ok)s; --warn: %(warn)s; --full: %(full)s;
  --muted: %(muted)s; --origin: %(origin)s;
  --serif: "Iowan Old Style", "Palatino Nova", Palatino, Georgia,
           "Times New Roman", serif;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
}
body { margin: 0; background: var(--paper); color: var(--ink);
       font: 15px/1.55 var(--sans);
       -webkit-font-smoothing: antialiased; }
header { padding: 26px 26px 6px; }
header h1 { margin: 0 0 10px; font-size: 11px; font-weight: 600;
            letter-spacing: 0.22em; text-transform: uppercase;
            color: var(--sub); }
header .ask { font-family: var(--serif); font-size: 30px; line-height: 1.22;
              font-weight: 500; letter-spacing: -0.01em; max-width: 46em; }
header .stamp { color: var(--sub); font-size: 12.5px; margin-top: 8px; }
.stats { display: flex; flex-wrap: wrap; gap: 0; padding: 10px 26px 2px; }
.stat { padding: 6px 22px 6px 0; margin-right: 22px;
        border-right: 1px solid var(--line); }
.stat:last-child { border-right: 0; }
.stat-n { font-family: var(--serif); font-size: 22px;
          font-variant-numeric: tabular-nums; }
.stat-l { font-size: 10.5px; letter-spacing: 0.14em; text-transform:
          uppercase; color: var(--sub); margin-top: 1px; }
.films { padding: 10px 26px 0; }
.film-chip { display: inline-block; background: var(--card);
             border: 1px solid var(--line); border-radius: 999px;
             padding: 4px 14px; margin: 2px 8px 2px 0; font-size: 13px; }
.film-chip b { font-weight: 650; color: var(--ok); margin-left: 8px;
               font-variant-numeric: tabular-nums; }
.notice { background: #fbf3dd; border: 1px solid #eaddb4; color: #6d5716;
          padding: 10px 14px; margin: 14px 26px 0; border-radius: 10px;
          font-size: 13.5px; }
#map { height: 60vh; min-height: 340px; margin: 16px 26px 0;
       border-radius: 14px; border: 1px solid var(--line);
       box-shadow: 0 1px 2px rgba(22, 19, 13, 0.05),
                   0 10px 30px rgba(22, 19, 13, 0.06); }
section, footer { padding: 12px 26px; }
.legend { font-size: 12.5px; color: var(--sub); padding-top: 14px; }
.legend strong { color: var(--ink); font-size: 10.5px; font-weight: 600;
                 letter-spacing: 0.14em; text-transform: uppercase; }
.legend ul { display: inline; margin: 0 0 0 10px; padding: 0;
             list-style: none; }
.legend li { display: inline-block; margin: 2px 16px 2px 0; }
.legend > div { margin-top: 6px; }
.swatch { display: inline-block; width: 10px; height: 10px;
          border-radius: 50%%; margin-right: 6px; vertical-align: -1px; }
.swatch.hollow { background: transparent;
                 border: 2px dashed var(--muted);
                 width: 7px; height: 7px; }
.chipdemo { display: inline-block; padding: 1px 9px; border-radius: 999px;
            font-size: 11.5px; margin-right: 6px; color: #ffffff;
            font-variant-numeric: tabular-nums; }
.table-wrap { padding-top: 6px; }
.table-note { font-size: 12px; color: var(--sub); margin: 0 0 6px; }
.table-wrap th[data-key] { cursor: pointer; user-select: none; }
.table-wrap th[data-key]:hover { color: var(--ink); }
.table-wrap th[aria-sort="ascending"]::after { content: " \\2191"; }
.table-wrap th[aria-sort="descending"]::after { content: " \\2193"; }
.table-wrap table { border-collapse: collapse; width: 100%%;
        background: var(--card); font-size: 13.5px;
        border: 1px solid var(--line); border-radius: 14px;
        overflow: hidden; box-shadow: 0 1px 2px rgba(22, 19, 13, 0.04); }
.table-wrap { overflow-x: auto; }
th, td { padding: 10px 14px; text-align: left; white-space: nowrap;
         border-bottom: 1px solid var(--line); }
tr:last-child td { border-bottom: 0; }
th { font-size: 10.5px; font-weight: 600; letter-spacing: 0.14em;
     text-transform: uppercase; color: var(--sub);
     border-bottom-width: 2px; }
td.num, td:nth-child(4) { font-variant-numeric: tabular-nums; }
td.c-venue { font-weight: 600; }
td.c-fmt { color: var(--sub); font-size: 12.5px; }
td strong { font-weight: 650; color: var(--ok); }
.dot { display: inline-block; width: 8px; height: 8px;
       border-radius: 50%%; margin-right: 7px; vertical-align: 0;
       background: var(--muted); }
.dot.ok { background: var(--ok); }
.dot.warn { background: var(--warn); }
.dot.full { background: var(--full); }
a.book { display: inline-block; padding: 2px 12px; border-radius: 999px;
         border: 1px solid var(--line); color: var(--ink);
         text-decoration: none; font-size: 12.5px; font-weight: 600;
         background: var(--paper); }
a.book:hover { border-color: var(--ink); }
td a { color: var(--origin); }
footer { color: var(--sub); font-size: 11.5px; line-height: 1.6;
         padding-bottom: 26px; max-width: 62em; }
.chip { display: inline-block; margin: 3px 5px 3px 0; padding: 2px 10px;
        border-radius: 999px; font-size: 12px; text-decoration: none;
        color: #ffffff; border: 1px solid transparent;
        font-variant-numeric: tabular-nums; }
.chip-verified.ok { background: var(--ok); }
.chip-verified.warn { background: var(--warn); }
.chip-verified.full { background: var(--full); }
.chip-unverified { border: 1px dashed #4b5563; color: #ffffff;
                   text-shadow: 0 1px 1px rgba(0,0,0,0.35); }
.venue-label { background: var(--card); border: 1px solid var(--line);
               border-radius: 6px; padding: 2px 7px; white-space: nowrap;
               font: 600 11px/1.4 var(--sans); color: var(--ink);
               box-shadow: 0 1px 3px rgba(22, 19, 13, 0.14); opacity: 0.96; }
.origin-label { background: var(--origin); border: 0; border-radius: 6px;
                padding: 2px 8px; white-space: nowrap; color: #ffffff;
                font: 600 11px/1.4 var(--sans);
                box-shadow: 0 1px 3px rgba(22, 19, 13, 0.2); }
.popup-venue { font-weight: 650; margin-bottom: 2px; font-size: 14px; }
.popup-dist { color: var(--sub); font-size: 12px; margin-bottom: 6px; }
.leaflet-popup-content { margin: 12px 14px; max-width: 280px;
                         font-family: var(--sans); }
/* Leaflet's own ".leaflet-container a" outranks ".chip", turning chip
   text link-blue on green. Win specificity explicitly. */
.leaflet-popup-content a.chip,
.leaflet-popup-content a.chip:visited { color: #ffffff; }
.leaflet-popup-content-wrapper { border-radius: 12px; }
.leaflet-container { font-family: var(--sans); }
img { max-width: 100%%; }
@media (max-width: 640px) {
  header, .stats, section, footer { padding-left: 14px;
                                    padding-right: 14px; }
  #map { margin-left: 14px; margin-right: 14px; }
  header .ask { font-size: 23px; }
}
""" % INK

SCRIPT = """
// Table sorter first and standalone: a Leaflet failure later in this file
// must never take the no-map fallback's sorting down with it.
(function () {
  var table = document.querySelector(".table-wrap table");
  if (!table) { return; }
  var ths = table.getElementsByTagName("th");
  var current = { key: null, dir: 0, num: false };
  function apply() {
    var rows = [];
    var all = table.getElementsByTagName("tr");
    for (var i = 0; i < all.length; i++) {
      if (all[i].hasAttribute("data-i")) { rows.push(all[i]); }
    }
    if (!rows.length) { return; }
    rows.sort(function (a, b) {
      if (current.dir === 0) {
        return a.getAttribute("data-i") - b.getAttribute("data-i");
      }
      var ka = a.getAttribute("data-" + current.key) || "";
      var kb = b.getAttribute("data-" + current.key) || "";
      if (current.num) {
        ka = parseFloat(ka);
        kb = parseFloat(kb);
        // A missing value (unpriced, unrated, no timestamp) sorts last in
        // BOTH directions instead of topping the descending order.
        var missA = ka >= 999999 || (current.key === "rating" && ka < 0);
        var missB = kb >= 999999 || (current.key === "rating" && kb < 0);
        if (missA !== missB) { return missA ? 1 : -1; }
      }
      if (ka < kb) { return -current.dir; }
      if (ka > kb) { return current.dir; }
      return a.getAttribute("data-i") - b.getAttribute("data-i");
    });
    var parent = rows[0].parentNode;
    for (var j = 0; j < rows.length; j++) { parent.appendChild(rows[j]); }
  }
  for (var i = 0; i < ths.length; i++) {
    (function (th) {
      var key = th.getAttribute("data-key");
      if (!key) { return; }
      th.addEventListener("click", function () {
        if (current.key === key) {
          current.dir = current.dir === 1 ? -1 :
                        current.dir === -1 ? 0 : 1;
        } else {
          current.key = key;
          current.dir = 1;
        }
        current.num = th.hasAttribute("data-num");
        for (var k = 0; k < ths.length; k++) {
          ths[k].removeAttribute("aria-sort");
        }
        if (current.dir === 1) { th.setAttribute("aria-sort", "ascending"); }
        if (current.dir === -1) {
          th.setAttribute("aria-sort", "descending");
        }
        apply();
      });
    })(ths[i]);
  }
})();
(function () {
  if (typeof L === "undefined") { return; }  // CDN blocked: table has it all
  var q = RADAR.query || {};
  var party = q.party_size || 1;
  var PIN = { verified_ok: "%(ok)s", available: "%(warn)s",
              filling: "%(warn)s", housefull: "%(full)s", soldout: "%(full)s",
              unknown: "%(muted)s", none: "%(none)s", closed: "%(muted)s" };
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function seatsBadge(s) {
    if (!s.seats) { return "?"; }
    if (s.seats.meets_party_size) {
      return party > 1 ? party + " together" : s.seats.best_run + " in a row";
    }
    if (s.seats.hall_meets_party_size) { return "good rows full · fits elsewhere"; }
    return "no " + party + " together";
  }
  function chipClass(s) {
    if (!s.seats) { return "chip chip-unverified"; }
    if (s.seats.meets_party_size) { return "chip chip-verified ok"; }
    if (s.seats.hall_meets_party_size) { return "chip chip-verified warn"; }
    return "chip chip-verified full";
  }
  function chipStyle(s) {
    if (s.seats) { return ""; }
    var hex = /^[0-9A-Fa-f]{6}$/.test(s.statusCode || "")
      ? "#" + s.statusCode : "%(muted)s";
    return ' style="background:' + hex + '"';
  }
  function chipHtml(s) {
    var bits = [escapeHtml(s.showTime || "?")];
    // Without a movie filter, chips at one venue span films: name each.
    if (!q.movie) {
      var title = String(s.film || "").split(" (")[0].trim();
      if (title) {
        bits.unshift(escapeHtml(
          title.length > 18 ? title.slice(0, 16) + "\\u2026" : title));
      }
    }
    var fmt = s.screenType || s.movieFormat;
    if (fmt) { bits.push(escapeHtml(fmt)); }
    bits.push(escapeHtml(seatsBadge(s)));
    var body = bits.join(" \\u00b7 ");
    if (s.deep_link) {
      return '<a class="' + chipClass(s) + '"' + chipStyle(s)
        + ' href="' + escapeHtml(s.deep_link)
        + '" target="_blank" rel="noopener">' + body + "</a>";
    }
    return '<span class="' + chipClass(s) + '"' + chipStyle(s) + ">"
      + body + "</span>";
  }
  function popupHtml(v, shows) {
    var out = '<div class="popup-venue">' + escapeHtml(v.name) + "</div>";
    var dist = [];
    if (v.distance_km != null) { dist.push(v.distance_km + " km"); }
    if (v.drive_min_est != null) {
      dist.push("about " + v.drive_min_est + " min drive (est., "
                + (v.drive_min_source || "heuristic") + ")");
    }
    if (dist.length) {
      out += '<div class="popup-dist">' + escapeHtml(dist.join(", ")) + "</div>";
    }
    if (!shows.length) {
      out += '<div class="popup-dist">'
        + (v.booking_open === false
           ? "booking not open yet for this date (not sold out)"
           : "no shows matched the query") + "</div>";
    }
    shows.forEach(function (s) { out += chipHtml(s); });
    return out;
  }

  var map = L.map("map", { scrollWheelZoom: false });
  // OSM standard tiles: keyless. CARTO's anonymous basemaps serve
  // "API KEY REQUIRED" watermark tiles since 2026-08-28, so no CARTO here.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">'
      + "OpenStreetMap</a> contributors"
  }).addTo(map);

  // Short on-map name: drop the city suffix and any "City: " prefix so the
  // label is readable at a glance; the popup keeps the full name.
  function shortName(name) {
    var head = String(name || "").split(",")[0].trim();
    var colon = head.indexOf(": ");
    if (colon > -1 && head.length - colon > 7) {
      head = head.slice(colon + 2);
    }
    return head.length > 26 ? head.slice(0, 24) + "\\u2026" : head;
  }

  var points = [];
  var origin = q.origin || {};
  if (origin.lat != null && origin.lng != null) {
    var here = [Number(origin.lat), Number(origin.lng)];
    points.push(here);
    L.circleMarker(here, { radius: 8, color: "#ffffff", weight: 2.5,
                           fillColor: "%(origin)s", fillOpacity: 1 })
      .addTo(map)
      .bindTooltip("You: " + escapeHtml(origin.label || "your location"),
                   { permanent: true, direction: "bottom", offset: [0, 8],
                     className: "origin-label" });
  }
  var byVenue = {};
  (RADAR.shows || []).forEach(function (s) {
    (byVenue[s.theatreId] = byVenue[s.theatreId] || []).push(s);
  });
  (RADAR.venues || []).forEach(function (v, index) {
    if (v.lat == null || v.lng == null) { return; }
    var at = [Number(v.lat), Number(v.lng)];
    points.push(at);
    var status = v.best_status || "unknown";
    var color = PIN[status] || PIN.unknown;
    var hollow = status === "closed";
    var shows = (byVenue[v.theatreId] || []).slice()
      .sort(function (a, b) {
        return (a.showTimeStamp || 0) - (b.showTimeStamp || 0);
      });
    var label = shortName(v.name);
    if (v.drive_min_est != null) {
      label += " \\u00b7 " + v.drive_min_est + " min";
    }
    // Cycle four label directions so dense metro clusters (12+ venues in
    // Mumbai) collide far less than the old left/right alternation did.
    var SIDES = [["right", [10, 0]], ["left", [-10, 0]],
                 ["top", [0, -12]], ["bottom", [0, 12]]];
    var pick = SIDES[index %% 4];
    L.circleMarker(at, {
      radius: 9, color: hollow ? color : "#ffffff",
      weight: hollow ? 2 : 2.5,
      dashArray: hollow ? "3 3" : null,
      fillColor: color, fillOpacity: hollow ? 0 : 1
    }).addTo(map)
      .bindTooltip(escapeHtml(label),
                   { permanent: true, direction: pick[0],
                     offset: pick[1],
                     className: "venue-label" })
      .bindPopup(popupHtml(v, shows), { maxWidth: 300 });
  });

  // Every pin AND the origin in the first frame: fitBounds with generous
  // padding (labels need room), re-run after full load because Leaflet can
  // measure a container mid-layout and clip the fit.
  function fitAll() {
    if (points.length > 1) {
      map.fitBounds(points, { padding: [56, 56], maxZoom: 14 });
    } else if (points.length === 1) {
      map.setView(points[0], 12);
    } else {
      map.setView([22.97, 78.65], 5);  // India
    }
  }
  fitAll();
  window.addEventListener("load", function () {
    map.invalidateSize();
    fitAll();
  });
})();
""" % INK


def render(radar, ratings=None):
    """Pure: radar document dict to a complete HTML page string (SPEC R78).

    ratings: optional {film name: rating entry} handed in by the calling
    agent (which looked them up itself); rendered as an extra table column
    with the source labeled. This page never fetches ratings."""
    query = radar.get("query") or {}
    asked = query_words(query)
    generated = query.get("generated_at") or ""
    # \\u003c-escape the embedded JSON so no </script> or <tag can occur
    # inside it; "<" is only ever inside JSON strings, so this is lossless.
    embedded = json.dumps(radar).replace("<", "\\u003c")

    legend = (
        '<section class="legend">'
        "<strong>Pins</strong>"
        "<ul>"
        '<li><span class="swatch" style="background:%s"></span>'
        "verified seats for your party</li>"
        '<li><span class="swatch" style="background:%s"></span>'
        "available or filling (unverified)</li>"
        '<li><span class="swatch" style="background:%s"></span>'
        "housefull or sold out (reserved: only PVR's green status color is "
        "confirmed so far, so this pin state cannot appear yet)</li>"
        '<li><span class="swatch" style="background:%s"></span>'
        "unknown or no matching shows</li>"
        '<li><span class="swatch hollow"></span>'
        "booking not open yet (not sold out)</li>"
        "</ul>"
        "<div>"
        "<strong>Showtime chips</strong>"
        "<ul>"
        '<li><span class="chipdemo chip-verified ok" '
        'style="background:%s">06:05 PM</span>'
        "solid: seats counted from the live seat map</li>"
        '<li><span class="chipdemo chip-verified warn" '
        'style="background:%s">06:00 PM</span>'
        "solid brown: counted too; the good rows can't seat your party "
        "together, but other rows in the hall can</li>"
        '<li><span class="chipdemo chip-verified full" '
        'style="background:%s">10:15 PM</span>'
        "solid red: counted; this hall cannot seat your party together</li>"
        '<li><span class="chipdemo chip-unverified" '
        'style="background:%s;border:1px dashed #4b5563">9:00 PM '
        "· ?</span>dashed with a ? : PVR's own label, not verified</li>"
        "</ul>"
        "</div>"
        "<div>Note: %s.</div>"
        "</section>" % (INK["ok"], INK["warn"], INK["full"], INK["muted"],
                        INK["ok"], INK["warn"], INK["full"], INK["muted"],
                        esc(LEGEND_LINE)))

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        # An inline empty favicon keeps the page at zero extra requests.
        '<link rel="icon" href="data:,">',
        "<title>PVR INOX Radar: %s</title>" % esc(asked),
        '<link rel="stylesheet" href="%s" integrity="%s" '
        'crossorigin="anonymous">' % (LEAFLET_CSS, LEAFLET_CSS_SRI),
        "<style>%s</style>" % CSS,
        "</head>",
        "<body>",
        "<header>",
        "<h1>PVR INOX Radar</h1>",
        '<div class="ask">%s</div>' % esc(asked),
        '<div class="stamp">generated %s IST</div>' % esc(generated),
        "</header>",
        stats_strip(radar),
        films_strip(radar, ratings),
        notice_banner(radar),
        '<div id="map"></div>',
        legend,
        '<section class="table-wrap">%s</section>'
        % fallback_table(radar, ratings),
        "<footer>%s</footer>" % esc(FOOTER_CREDIT),
        '<script src="%s" integrity="%s" crossorigin="anonymous"></script>'
        % (LEAFLET_JS, LEAFLET_JS_SRI),
        "<script>const RADAR = %s;</script>" % embedded,
        "<script>%s</script>" % SCRIPT,
        "</body>",
        "</html>",
    ]
    return "\n".join(p for p in parts if p)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="render_map.py",
        description="Render radar JSON into one self-contained map.html.")
    parser.add_argument("--in", dest="infile", required=True,
                        help="radar JSON path, or - for stdin")
    parser.add_argument("--out", dest="outfile", default="map.html")
    parser.add_argument("--ratings", default="",
                        help="optional film ratings: a JSON file path or an "
                             "inline JSON object {film name: rating or "
                             "{rating, source}}, looked up by the caller")
    args = parser.parse_args(argv)

    ratings = None
    if args.ratings:
        try:
            if args.ratings.strip().startswith("{"):
                ratings = json.loads(args.ratings)
            else:
                with open(args.ratings) as fh:
                    ratings = json.load(fh)
            if not isinstance(ratings, dict):
                raise ValueError("ratings must be a JSON object")
        except (OSError, ValueError) as exc:
            print("render_map: bad --ratings: %s" % exc, file=sys.stderr)
            return 2

    try:
        if args.infile == "-":
            radar = json.load(sys.stdin)
        else:
            with open(args.infile) as fh:
                radar = json.load(fh)
        if not isinstance(radar, dict) or "error" in radar:
            raise ValueError("input is not a radar document")
    except (OSError, ValueError) as exc:
        print("render_map: bad input JSON: %s" % exc, file=sys.stderr)
        return 2

    page = render(radar, ratings)
    with open(args.outfile, "w") as fh:
        fh.write(page)
    print("wrote %s" % args.outfile, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
