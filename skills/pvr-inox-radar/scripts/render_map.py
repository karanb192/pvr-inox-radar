#!/usr/bin/env python3
"""Render a radar JSON document into ONE self-contained map.html.

Original code (no adapted client logic); the client engineering elsewhere in
this skill is adapted from notprashanth/pvr-inox-mcp (MIT) by Prashanth
Krishnan, credited in the page footer.

Usage:
    python3 scripts/render_map.py --in radar.json --out map.html
    ... radar.py ... | python3 scripts/render_map.py --in - --out map.html

The output embeds the radar JSON inline and performs zero fetches at view
time except: Leaflet 1.9.4 from unpkg (SRI-pinned) and OpenStreetMap raster
tiles. A plain semantic HTML table under the map always carries the full
answer, so the page still works with no JS, no CDN, or no tiles.
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
        return ("%d together: zone full, hall yes (run %d)"
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


def fallback_table(radar):
    """Always-rendered static table: the full answer without JS, CDN, or
    tiles (SPEC R75). One row per ranked show."""
    venues = {v.get("theatreId"): v for v in radar.get("venues") or []}
    party = (radar.get("query") or {}).get("party_size") or 1
    head = ("<tr><th>Venue</th><th>Distance</th><th>Drive est.</th>"
            "<th>Show</th><th>Format</th><th>Status</th><th>Seats</th>"
            "<th>Link</th></tr>")
    rows = [head]
    for show in radar.get("shows") or []:
        venue = venues.get(show.get("theatreId")) or {}
        km = venue.get("distance_km")
        drive = venue.get("drive_min_est")
        tokens = []
        for part in (show.get("screenType"), show.get("movieFormat")):
            for word in str(part or "").split():
                if word not in tokens:
                    tokens.append(word)
        # No format data means an empty cell, not an invented "2D": the API
        # left it blank and this page does not guess.
        fmt = " ".join(tokens)
        link = show.get("deep_link") or ""
        link_cell = ('<a class="book" href="%s" target="_blank" '
                     'rel="noopener">book</a>' % esc(link)) if link else ""
        seats_text = seats_cell(show, party)
        seats_html = ('<strong>%s</strong>' % esc(seats_text)
                      if show.get("seats", {}) and
                      show["seats"].get("meets_party_size")
                      else esc(seats_text))
        rows.append(
            '<tr><td class="c-venue">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td>%s</td><td class="c-fmt">%s</td>'
            '<td><span class="dot %s"></span>%s</td><td>%s</td>'
            '<td class="c-link">%s</td></tr>' % (
                esc(venue.get("name") or show.get("theatreId")),
                esc("%.1f km" % km) if km is not None else "",
                esc("%d min (%s)" % (drive, venue.get("drive_min_source") or
                                     "heuristic")) if drive else "",
                esc("%s %s" % (show.get("showTime") or "?",
                               show.get("film") or "")),
                esc(fmt),
                status_dot(show),
                esc(status_cell(show)),
                seats_html,
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
                '<td colspan="5">%s</td></tr>' % (
                    esc(venue.get("name") or venue.get("theatreId")),
                    esc("%.1f km" % km) if km is not None else "",
                    esc("%d min (%s)" % (drive, venue.get("drive_min_source")
                                         or "heuristic")) if drive else "",
                    esc(outcome)))
        if len(rows) == 1:
            rows.append('<tr><td colspan="8">no shows matched</td></tr>')
    return "<table>%s</table>" % "".join(rows)


def notice_banner(radar):
    meta = radar.get("meta") or {}
    party = (radar.get("query") or {}).get("party_size") or 1
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
    drives = [v.get("drive_min_est") for v in venues
              if v.get("drive_min_est") is not None]
    items = [
        ("%d" % len(venues), "venue" if len(venues) == 1 else "venues"),
        ("%d" % len(shows), "show" if len(shows) == 1 else "shows"),
        ("%d" % verified, "seat-verified"),
    ]
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
.popup-venue { font-weight: 650; margin-bottom: 2px; font-size: 14px; }
.popup-dist { color: var(--sub); font-size: 12px; margin-bottom: 6px; }
.leaflet-popup-content { margin: 12px 14px; max-width: 280px;
                         font-family: var(--sans); }
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
    if (s.seats.hall_meets_party_size) { return "zone full"; }
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
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">'
      + "OpenStreetMap</a> contributors"
  }).addTo(map);

  var points = [];
  var origin = q.origin || {};
  if (origin.lat != null && origin.lng != null) {
    var here = [Number(origin.lat), Number(origin.lng)];
    points.push(here);
    L.circleMarker(here, { radius: 8, color: "%(origin)s", weight: 3,
                           fillColor: "#9db8dd", fillOpacity: 0.9 })
      .addTo(map).bindTooltip(escapeHtml(origin.label || "origin"),
                              { permanent: false });
  }
  var byVenue = {};
  (RADAR.shows || []).forEach(function (s) {
    (byVenue[s.theatreId] = byVenue[s.theatreId] || []).push(s);
  });
  (RADAR.venues || []).forEach(function (v) {
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
    L.circleMarker(at, {
      radius: 9, color: color, weight: hollow ? 2 : 1.5,
      dashArray: hollow ? "3 3" : null,
      fillColor: color, fillOpacity: hollow ? 0 : 0.85
    }).addTo(map)
      .bindTooltip(escapeHtml(v.name))
      .bindPopup(popupHtml(v, shows), { maxWidth: 300 });
  });
  if (points.length > 1) {
    map.fitBounds(points, { padding: [30, 30] });
  } else if (points.length === 1) {
    map.setView(points[0], 12);
  } else {
    map.setView([22.97, 78.65], 5);  // India
  }
})();
""" % INK


def render(radar):
    """Pure: radar document dict to a complete HTML page string (SPEC R78)."""
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
        '<li><span class="chipdemo chip-unverified" '
        'style="background:%s;border:1px dashed #4b5563">9:00 PM '
        "· ?</span>dashed with a ? : PVR's own label, not verified</li>"
        "</ul>"
        "</div>"
        "<div>Note: %s.</div>"
        "</section>" % (INK["ok"], INK["warn"], INK["full"], INK["muted"],
                        INK["ok"], INK["muted"], esc(LEGEND_LINE)))

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
        notice_banner(radar),
        '<div id="map"></div>',
        legend,
        '<section class="table-wrap">%s</section>' % fallback_table(radar),
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
    args = parser.parse_args(argv)

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

    page = render(radar)
    with open(args.outfile, "w") as fh:
        fh.write(page)
    print("wrote %s" % args.outfile, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
