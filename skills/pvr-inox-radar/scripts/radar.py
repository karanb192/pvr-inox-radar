#!/usr/bin/env python3
# Adapted from notprashanth/pvr-inox-mcp (MIT), Copyright Prashanth Krishnan.
# https://github.com/notprashanth/pvr-inox-mcp
"""Radar CLI: one movie-night query in, one radar JSON document on stdout.

The show-finding approach (filter a city's venues, walk their day sessions,
verify seats-together on a small shortlist) is adapted from the reference
project's find_shows solver; see the repo README for full credits.

Everything on stdout is machine-parseable JSON: one radar document on
success, one {"error": ...} object on failure. Logs go to stderr only.

Call budget per invocation (SPEC R55): 1 content/cinemas (plus up to 1
content/city and 1 content/nowshowing on cold caches), at most 12
content/csessions, at most --seat-detail (cap 8) ticketing/seatlayout,
absolute ceiling 24 calls, all strictly sequential at >= 0.6s spacing.
Worst case is about 15 seconds of deliberate pacing; that is a feature.

Offline mode: --fixtures DIR reads captured API fixtures from DIR instead
of the network (zero live calls; used by the demo map and by tests). File
naming follows tests/fixtures/: city.json, cinemas_<city>.json,
nowshowing_<city>.json, csessions_<cid>.json, seatlayout_<sessionId>.json.
"""

import argparse
import datetime
import json
import math
import os
import sys
import urllib.request

import pvr_client
from pvr_client import Blocked, BudgetExhausted

MAX_VENUE_CALLS = 12          # csessions cap per run
SEAT_DETAIL_DEFAULT = 3
SEAT_DETAIL_CAP = 8
CALL_CEILING = 24             # absolute ceiling across all endpoints
DEFAULT_LIMIT = 40
RADIUS_LOCAL_KM = 6.0         # caller origin, no format: neighborhood trip
RADIUS_CITY_KM = 60.0         # a premium format is a city-level resource

OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/"
OSRM_TIMEOUT = 4
USER_AGENT = ("pvr-inox-radar/0.1 (+https://github.com/karanb192/pvr-inox-radar; "
              "personal movie showtime lookup)")

CLOSED_MESSAGE = ("date not on sale yet (PVR sells a rolling window of about "
                  "5 days); this is not sold out")
LABELS_LIE_CAVEAT = (
    "Availability labels come from PVR and can lag in both directions "
    "(measured by notprashanth/pvr-inox-mcp); counted seats are exact at "
    "fetch time.")
DRIVE_CAVEAT = "Drive times are estimates."


# ---------------------------------------------------------------------------
# Clients: one live, one fixture-backed. Same five methods, so the solver
# never knows which one it holds (SPEC R17: I/O at the edges).
# ---------------------------------------------------------------------------


class LiveClient:
    """Counts every API call through the pvr_client choke point and refuses
    to exceed the absolute ceiling. Only this class touches the network.

    The counting wrapper raises pvr_client.BudgetExhausted, which the
    pvr_client error handlers re-raise exactly like Blocked, so exhaustion
    always surfaces as itself (meta.partial + CALL_BUDGET_EXHAUSTED), never
    as a fake per-venue transport error. The TRUE original _post is captured
    via a sentinel attribute so a second LiveClient in the same process gets
    a fresh counter instead of chaining onto a spent one.
    """

    source = "live"

    def __init__(self):
        self.calls = 0
        self._real_post = getattr(pvr_client, "_pvr_original_post",
                                  pvr_client._post)
        pvr_client._pvr_original_post = self._real_post
        pvr_client._post = self._counting_post

    def _counting_post(self, path, body, city=pvr_client.DEFAULT_CITY, timeout=30):
        if self.calls >= CALL_CEILING:
            raise BudgetExhausted("call ceiling of %d reached" % CALL_CEILING)
        self.calls += 1
        return self._real_post(path, body, city, timeout)

    def city_known(self, city):
        """True/False from the persisted serviced-city list; None before the
        city list has ever been fetched."""
        names = pvr_client.known_city_names()
        if names is None:
            return None
        return str(city or "").strip().lower() in names

    def city_coords(self, city):
        return pvr_client.city_coords(city)

    def cinemas(self, city, origin, label):
        payload = pvr_client._post(
            "content/cinemas",
            {"city": city, "lat": str(origin[0]), "lng": str(origin[1]), "text": ""},
            city)
        return pvr_client.parse_cinemas(payload.get("output") or {}, origin, label)

    def variants(self, city, lat, lng, date):
        return pvr_client.film_variants(city, lat, lng, date)

    def sessions(self, city, cid, date, lat, lng, variants):
        return pvr_client.day_sessions(city, cid, date, lat, lng, variants)

    def seats(self, show, party_size, tier=None):
        return pvr_client.seat_report(
            show["encrypted"], party_size,
            screen_type=show.get("screenType") or "",
            theatre_id=show.get("theatreId"),
            screen_name=show.get("screenName"),
            tier=tier)


class FixtureClient:
    """Reads captured API fixture files instead of the network. Zero live
    calls; each fixture read counts as one simulated call so budget logic
    behaves identically in tests and demos."""

    source = "fixtures"

    def __init__(self, root):
        self.root = root
        self.calls = 0

    def _load(self, name):
        path = os.path.join(self.root, name)
        if not os.path.exists(path):
            return None
        self.calls += 1
        with open(path) as fh:
            return json.load(fh)

    def city_coords(self, city):
        payload = self._load("city.json")
        if not payload:
            return None
        for rec in pvr_client.parse_city_records(payload.get("output") or {}):
            if rec["name"].strip().lower() == str(city).strip().lower():
                if rec.get("lat") and rec.get("lng"):
                    return (str(rec["lat"]), str(rec["lng"]))
        return None

    def city_known(self, city):
        # A cache re-read in live mode costs zero calls, so no call is
        # simulated here either: read the fixture directly.
        path = os.path.join(self.root, "city.json")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            payload = json.load(fh)
        wanted = str(city or "").strip().lower()
        return any(rec["name"].strip().lower() == wanted for rec in
                   pvr_client.parse_city_records(payload.get("output") or {}))

    def cinemas(self, city, origin, label):
        payload = self._load("cinemas_%s.json" % str(city).strip().lower())
        if not payload:
            return []
        return pvr_client.parse_cinemas(payload.get("output") or {}, origin, label)

    def variants(self, city, lat, lng, date):
        payload = self._load("nowshowing_%s.json" % str(city).strip().lower())
        if not payload:
            return {}
        return pvr_client.parse_variants(payload.get("output") or {})

    def sessions(self, city, cid, date, lat, lng, variants):
        payload = self._load("csessions_%s.json" % cid)
        if payload is None:
            return {"outcome": "error", "error": "no fixture for cinema %s" % cid}
        if pvr_client.classify_csessions(payload) != "open":
            return {"outcome": "closed"}
        return {"outcome": "open",
                "shows": pvr_client.extract_shows(payload.get("output") or {}, variants)}

    def seats(self, show, party_size, tier=None):
        payload = self._load("seatlayout_%s.json" % show.get("sessionId"))
        if payload is None:
            return {"outcome": "error",
                    "error": "no fixture for session %s" % show.get("sessionId")}
        if payload.get("status") != 200 or not payload.get("output"):
            return {"outcome": "error", "error": "seatmap unavailable"}
        report = pvr_client.seat_report_from_payload(
            payload["output"], party_size,
            screen_type=show.get("screenType") or "", tier=tier)
        return {"outcome": "ok", "report": report}


# ---------------------------------------------------------------------------
# Pure helpers (fixture-testable, no network, no clock)
# ---------------------------------------------------------------------------


def parse_hhmm(text):
    """'18:00' to minutes since midnight; ValueError on anything else."""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError("expected HH:MM, got %r" % text)
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("expected HH:MM, got %r" % text)
    return hour * 60 + minute


def drive_minutes_heuristic(km):
    """ceil(5 + 2.4 * (km * 1.3)): 1.3 road circuity over straight-line,
    about 25 km/h effective urban speed, 5 min overhead. Always labeled an
    estimate (SPEC R62)."""
    if km is None:
        return None
    return int(math.ceil(5 + 2.4 * (km * 1.3)))


def movie_matches(show, movie):
    """Substring match on the resolved film name (case-insensitive), or
    exact match on filmId / movieId for canonical ids."""
    if not movie:
        return True
    needle = str(movie).strip().lower()
    if needle in str(show.get("film") or "").lower():
        return True
    return needle in (str(show.get("filmId") or ""), str(show.get("movieId") or ""))


def format_matches(show, fmt_norm):
    """Normalized substring match across screenType, movieFormat, and
    soundFormat ("4DX" must catch movieFormat "3D 4DX")."""
    if not fmt_norm:
        return True
    haystack = " ".join(
        pvr_client.screen_type_norm(show.get(key))
        for key in ("screenType", "movieFormat", "soundFormat"))
    return fmt_norm in haystack


def in_time_window(show, time_from, time_to):
    """(keep, time_unparsed). Shows with unparseable showTime are KEPT and
    flagged, surviving both window flags symmetrically (SPEC R59)."""
    minutes = pvr_client.show_time_minutes(show.get("showTime"))
    if minutes is None:
        return True, True
    if time_from is not None and minutes < time_from:
        return False, False
    if time_to is not None and minutes > time_to:
        return False, False
    return True, False


def filter_shows(shows, movie, fmt_norm, time_from, time_to, date=None):
    """Pure filter pass; annotates kept shows with time_unparsed.

    Returns (kept, date_mismatches). The date guard is the reference
    project's: this API family can answer for a day you did not ask about,
    so any show whose own showDate disagrees with the queried date is
    dropped and counted rather than silently mixed in with the wrong day's
    time window and deep links.
    """
    kept = []
    date_mismatches = 0
    for show in shows:
        if date and show.get("showDate") and show["showDate"] != date:
            date_mismatches += 1
            continue
        if not movie_matches(show, movie):
            continue
        if not format_matches(show, fmt_norm):
            continue
        keep, unparsed = in_time_window(show, time_from, time_to)
        if not keep:
            continue
        show = dict(show, time_unparsed=unparsed)
        kept.append(show)
    return kept, date_mismatches


def plan_venues(venues, max_km, fmt_norm, cap=MAX_VENUE_CALLS):
    """(selected, skipped_with_reasons). Venues in radius, nearest-first;
    with a format, capability-first (venues whose screens advertise it go
    first, others demoted but never dropped). Beyond the cap or outside the
    radius goes to skipped with a reason (SPEC R55, R56)."""
    selected, skipped = [], []

    def skip(venue, reason):
        skipped.append({"theatreId": venue.get("theatreId"),
                        "name": venue.get("name"), "reason": reason})

    for venue in venues:
        km = venue.get("distance_km")
        if km is None:
            skip(venue, "no published coordinates")
        elif km > max_km:
            skip(venue, "outside radius (%.1f km > %.0f km)" % (km, max_km))
        else:
            selected.append(venue)

    if fmt_norm:
        selected.sort(key=lambda v: (
            0 if any(fmt_norm in f for f in v.get("formats") or []) else 1,
            v.get("distance_km") or 0.0))

    for venue in selected[cap:]:
        skip(venue, "beyond the %d-venue call budget" % cap)
    return selected[:cap], skipped


def relevance_key(show):
    """Default ranking: soonest show, then nearest venue (SPEC R64)."""
    stamp = show.get("showTimeStamp") or float("inf")
    km = show.get("_distance_km")
    return (stamp, km if km is not None else float("inf"))


def verified_meets(show):
    seats = show.get("seats")
    return bool(seats and (seats.get("meets_party_size")
                           or seats.get("hall_meets_party_size")))


def price_from(seats):
    """Cheapest verified per-ticket gross for a show, or None.

    Tier-matched shows price at their tier; otherwise the cheapest tier in
    the hall. Prices exist only inside seat layouts (csessions carries
    none), so unverified shows have no price and never pretend to."""
    if not seats:
        return None
    tier = seats.get("tier") or {}
    candidates = []
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


def seats_from_report(report):
    """The seats block a show carries in the radar document (SPEC R60)."""
    return {
        "free": report["free"],
        "sold": report["sold"],
        "held": report["held"],
        "zone_free": report["zone_free"],
        "zone_rows_used": report["zone_rows_used"],
        "best_run": report["best_run"],
        "best_where": report["best_where"],
        "meets_party_size": report["meets_party_size"],
        "widened_to": report["widened_to"],
        "hall_free": report["hall_free"],
        "hall_best_run": report["hall_best_run"],
        "hall_meets_party_size": report["hall_meets_party_size"],
        "premium": report["premium"],
        "price_tiers": report["price_tiers"],
        "tier": report.get("tier"),
        "tiers_available": report.get("tiers_available") or [],
        "verified": True,
    }


def apply_party_filter(shows, party_size):
    """SPEC R65 with the R72 consistency rule: a VERIFIED show is excluded
    only when neither the zone nor the whole hall can seat the party (a
    premium hall whose zone is full but whose hall can seat the party stays,
    rendered amber). Unverified shows pass through with seats = null and are
    never promoted to bookable."""
    if party_size <= 1:
        return shows, 0
    kept, excluded = [], 0
    for show in shows:
        seats = show.get("seats")
        if seats and not seats["meets_party_size"] \
                and not seats["hall_meets_party_size"]:
            excluded += 1
        else:
            kept.append(show)
    return kept, excluded


def venue_best_status(venue_shows, booking_open):
    """Pin-color rollup (SPEC R66)."""
    if any(verified_meets(s) for s in venue_shows):
        return "verified_ok"
    categories = {s.get("status_category") for s in venue_shows}
    for category in ("available", "filling"):
        if category in categories:
            return category
    if "unknown" in categories:
        return "unknown"
    if booking_open is False:
        return "closed"
    return "none"


def show_output(show):
    """The public show dict (SPEC R60); internal underscore keys dropped."""
    return {
        "theatreId": show.get("theatreId"),
        "sessionId": show.get("sessionId"),
        "film": show.get("film"),
        "filmId": show.get("filmId"),
        "movieId": show.get("movieId"),
        "language": show.get("language"),
        "language_disputed": show.get("language_disputed"),
        "showTime": show.get("showTime"),
        "showTimeStamp": show.get("showTimeStamp"),
        "screenName": show.get("screenName"),
        "screenType": show.get("screenType"),
        "movieFormat": show.get("movieFormat"),
        "soundFormat": show.get("soundFormat"),
        "statusCode": show.get("statusCode"),
        "status_category": show.get("status_category"),
        "statusTxt": show.get("statusTxt"),
        "deep_link": show.get("deep_link"),
        "time_unparsed": show.get("time_unparsed", False),
        "seats": show.get("seats"),
        "seat_error": show.get("seat_error"),
        "rank": show.get("rank"),
    }


# ---------------------------------------------------------------------------
# OSRM refinement (SPEC R63): exactly one call per run against the shared
# demo server, graceful silent fallback to the heuristic on ANY failure.
# ---------------------------------------------------------------------------


def osrm_refine(origin, venue_records, urlopen=urllib.request.urlopen):
    """One GET covering origin plus every pinned venue. Mutates
    drive_min_est / drive_min_source in place on success; returns True/False.
    Never raises: offline or flaky routing degrades to the heuristic."""
    routable = [v for v in venue_records
                if v.get("lat") is not None and v.get("lng") is not None]
    if not routable:
        return False
    coords = ["%s,%s" % (origin["lng"], origin["lat"])]
    coords += ["%s,%s" % (v["lng"], v["lat"]) for v in routable]
    url = (OSRM_TABLE_URL + ";".join(coords)
           + "?sources=0&annotations=duration")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=OSRM_TIMEOUT) as response:
            data = json.load(response)
        durations = data["durations"][0]
        if len(durations) != len(routable) + 1:
            return False
        for venue, seconds in zip(routable, durations[1:]):
            if isinstance(seconds, (int, float)):
                venue["drive_min_est"] = int(math.ceil(seconds / 60.0))
                venue["drive_min_source"] = "osrm"
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The solver
# ---------------------------------------------------------------------------


def solve(client, query, use_osrm=False, osrm_urlopen=urllib.request.urlopen,
          log=lambda message: None):
    """Query dict in, radar document dict out. Blocked/BudgetExhausted from
    setup calls propagate to the caller; once venue outcomes exist they are
    absorbed into a partial document instead (SPEC R57: partial results are
    never thrown away)."""
    city = query["city"]
    origin = query["origin"]
    origin_pair = (origin["lat"], origin["lng"])
    # Venue distance_from carries the origin source honestly:
    # caller | geocode | city_centre.
    distance_label = origin["source"]
    fmt_norm = pvr_client.screen_type_norm(query["format"]) if query["format"] else ""

    log("radar: listing cinemas in %s (one content/cinemas call)" % city)
    all_venues = client.cinemas(city, origin_pair, distance_label)
    before_variants = client.calls
    variants = client.variants(city, origin_pair[0], origin_pair[1], query["date"])
    if client.calls > before_variants:
        log("radar: film variants fetched (one content/nowshowing call, "
            "cached for the rest of the day)")
    else:
        log("radar: film variants from cache (no call)")

    selected, skipped = plan_venues(all_venues, query["max_km"], fmt_norm)

    venue_records = []
    queried = []
    matched_by_venue = {}
    partial = False
    error = None
    date_mismatch = 0

    for index, venue in enumerate(selected):
        log("radar: sessions %s (%d/%d) %s"
            % (venue["theatreId"], index + 1, len(selected), venue["name"]))
        try:
            outcome = client.sessions(city, venue["theatreId"], query["date"],
                                      origin_pair[0], origin_pair[1], variants)
        except Blocked:
            partial, error = True, "UPSTREAM_BLOCKED"
            log("radar: upstream blocked us; stopping with partial results")
            skipped.extend(
                {"theatreId": v.get("theatreId"), "name": v.get("name"),
                 "reason": "not reached (run stopped early)"}
                for v in selected[index:])
            break
        except BudgetExhausted:
            partial, error = True, "CALL_BUDGET_EXHAUSTED"
            skipped.extend(
                {"theatreId": v.get("theatreId"), "name": v.get("name"),
                 "reason": "not reached (run stopped early)"}
                for v in selected[index:])
            break

        booking_open = {"open": True, "closed": False}.get(outcome["outcome"])
        matched = []
        if outcome["outcome"] == "open":
            matched, mismatched = filter_shows(
                outcome["shows"], query["movie"], fmt_norm,
                query["time_from_min"], query["time_to_min"],
                date=query["date"])
            date_mismatch += mismatched
            for show in matched:
                show["_distance_km"] = venue.get("distance_km")
        matched_by_venue[venue["theatreId"]] = matched

        queried.append({
            "theatreId": venue["theatreId"],
            "name": venue["name"],
            "outcome": outcome["outcome"],
            "error": outcome.get("error"),
            "shows_matched": len(matched),
        })
        venue_records.append({
            "theatreId": venue["theatreId"],
            "name": venue["name"],
            "lat": venue.get("lat"),
            "lng": venue.get("lng"),
            "distance_km": venue.get("distance_km"),
            "distance_from": venue.get("distance_from"),
            "drive_min_est": drive_minutes_heuristic(venue.get("distance_km")),
            "drive_min_source": "heuristic",
            "booking_open": booking_open,
            "best_status": None,  # filled after seat verification
            "formats": venue.get("formats") or [],
        })

    shows = [show for matched in matched_by_venue.values() for show in matched]
    shows.sort(key=relevance_key)

    # Seat verification for the top of the shortlist only (SPEC R52, R45:
    # lapsed shows carry no seat map upstream and are skipped without
    # consuming budget; sold-out shows are allowed, restocks appear there).
    seat_budget = query["seat_detail"]
    fetched = 0
    excluded_by_tier = 0
    if not partial:
        for show in shows:
            if fetched >= seat_budget:
                break
            if pvr_client.is_lapsed(show):
                continue
            log("radar: seat map for session %s" % show.get("sessionId"))
            try:
                outcome = client.seats(show, query["party_size"],
                                       tier=query.get("tier") or None)
            except Blocked:
                partial, error = True, "UPSTREAM_BLOCKED"
                break
            except BudgetExhausted:
                partial, error = True, "CALL_BUDGET_EXHAUSTED"
                break
            fetched += 1
            if outcome["outcome"] == "ok":
                report = outcome["report"]
                # A tier ask excludes verified halls that simply have no such
                # tier; unverified shows stay, honestly unverified (a mixed
                # hall we did not open may still hold recliners).
                if query.get("tier") and \
                        (report.get("tier") or {}).get("mode") == "absent":
                    show["tier_absent"] = True
                    excluded_by_tier += 1
                else:
                    show["seats"] = seats_from_report(report)
            else:
                show["seat_error"] = outcome["error"]

    if query.get("tier"):
        shows = [s for s in shows if not s.get("tier_absent")]

    excluded_by_price = 0
    if query.get("max_price") is not None:
        kept = []
        for show in shows:
            price = price_from(show.get("seats"))
            if price is not None and price > query["max_price"]:
                excluded_by_price += 1
            else:
                kept.append(show)
        shows = kept

    shows, excluded_by_party = apply_party_filter(shows, query["party_size"])

    # Verified-beats-unverified tiebreak: at equal relevance a show with
    # verified seats for the party never ranks below an unverified one.
    if query.get("sort") == "cheapest":
        # Priced (verified) shows first, ascending; unverified keep their
        # relevance order after them, honestly unpriced.
        shows.sort(key=lambda s: (
            (0, price_from(s.get("seats"))) if price_from(s.get("seats"))
            is not None else (1,) + tuple(relevance_key(s))))
    else:
        shows.sort(key=lambda s: relevance_key(s)
                   + ((0 if verified_meets(s) else 1),))
    shows = shows[:query["limit"]]
    for position, show in enumerate(shows):
        show["rank"] = position + 1

    for record in venue_records:
        kept_here = [s for s in shows if s.get("theatreId") == record["theatreId"]]
        rollup_shows = kept_here or matched_by_venue.get(record["theatreId"], [])
        record["best_status"] = venue_best_status(rollup_shows,
                                                  record["booking_open"])

    if use_osrm and venue_records:
        osrm_refine(origin, venue_records, urlopen=osrm_urlopen)

    open_flags = [r["booking_open"] for r in venue_records]
    booking_open = None
    message = None
    if open_flags:
        if any(flag is True for flag in open_flags):
            booking_open = True
        elif all(flag is False for flag in open_flags):
            booking_open = False
            message = CLOSED_MESSAGE
    if not all_venues:
        message = ("no venues returned for city %r; it may not be serviced "
                   "by PVR INOX (check the spelling)" % city)

    return {
        "query": query_echo(query),
        "venues": venue_records,
        "shows": [show_output(s) for s in shows],
        "meta": {
            "calls_made": client.calls,
            "source": client.source,
            "cinemas_queried": queried,
            "cinemas_skipped": skipped,
            "partial": partial,
            "booking_open": booking_open,
            "message": message,
            "error": error,
            "excluded_by_party": excluded_by_party,
            "excluded_by_tier": excluded_by_tier,
            "excluded_by_price": excluded_by_price,
            "date_mismatch": date_mismatch,
            "relaxations_applied": [],
            "caveats": [pvr_client.WITHHELD_CAVEAT, LABELS_LIE_CAVEAT,
                        DRIVE_CAVEAT],
        },
    }


def query_echo(query):
    """Every CLI flag echoed, plus resolved origin, date, and IST timestamp."""
    return {
        "city": query["city"],
        "movie": query["movie"],
        "format": query["format"],
        "tier": query.get("tier") or "",
        "max_price": query.get("max_price"),
        "sort": query.get("sort") or "relevance",
        "date": query["date"],
        "time_from": query["time_from"],
        "time_to": query["time_to"],
        "party_size": query["party_size"],
        "max_km": query["max_km"],
        "limit": query["limit"],
        "seat_detail": query["seat_detail"],
        "no_osrm": query["no_osrm"],
        "fixtures": query["fixtures"],
        "origin": {
            "lat": query["origin"]["lat"],
            "lng": query["origin"]["lng"],
            "label": query["origin"]["label"],
            "source": query["origin"]["source"],
        },
        "generated_at": pvr_client.now_ist().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# CLI shell
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="radar.py",
        description="Find PVR INOX shows matching a query; radar JSON to stdout.")
    parser.add_argument("--city", required=True, help="PVR city name, e.g. Gurugram")
    parser.add_argument("--lat", type=float, help="origin latitude (optional)")
    parser.add_argument("--lng", type=float, help="origin longitude (optional)")
    parser.add_argument("--origin-label", default="",
                        help="human label for the origin, e.g. 'Sector 56 Gurgaon'")
    parser.add_argument("--origin-source", choices=["caller", "geocode"],
                        default="caller",
                        help="where --lat/--lng came from (for honest labeling)")
    parser.add_argument("--movie", default="",
                        help="film name substring or canonical film id")
    parser.add_argument("--tier", default="",
                        help="seat tier word, e.g. recliner: count seats only "
                             "in matching priced rows (RECLINER ROWS), or "
                             "whole premium halls (Director's Cut, INSIGNIA)")
    parser.add_argument("--max-price", type=float, default=None,
                        help="drop verified shows whose cheapest matching "
                             "ticket (gross Rs) exceeds this; prices exist "
                             "only in seat maps, so unverified shows stay")
    parser.add_argument("--sort", choices=["relevance", "cheapest"],
                        default="relevance",
                        help="cheapest: priced verified shows first, "
                             "ascending by their per-ticket gross")
    parser.add_argument("--format", default="",
                        help="IMAX, 4DX, ATMOS, LASER, PLAYHOUSE, ...")
    parser.add_argument("--date", default="",
                        help="YYYY-MM-DD (default: today in IST)")
    parser.add_argument("--time-from", default="", help="HH:MM 24h IST, inclusive")
    parser.add_argument("--time-to", default="", help="HH:MM 24h IST, inclusive")
    parser.add_argument("--party-size", type=int, default=1)
    parser.add_argument("--max-km", type=float, default=None,
                        help="radius; default 6 with caller coords and no "
                             "--format, else 60")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="max shows in output (default %d)" % DEFAULT_LIMIT)
    parser.add_argument("--seat-detail", type=int, default=SEAT_DETAIL_DEFAULT,
                        help="seat maps fetched for the top N shows only "
                             "(default %d, cap %d, 0 disables)"
                             % (SEAT_DETAIL_DEFAULT, SEAT_DETAIL_CAP))
    parser.add_argument("--no-osrm", action="store_true",
                        help="skip the single OSRM drive-time refinement call")
    parser.add_argument("--fixtures", default="",
                        help="offline mode: read captured fixture JSON from "
                             "this directory instead of the network")
    return parser


def usage_error(detail):
    print(json.dumps({"error": "usage", "detail": detail}))
    return 2


def build_query(args, client):
    """Validate flags and resolve origin and date. Returns (query, None) or
    (None, (exit_code, error_dict))."""
    if args.date:
        try:
            date = datetime.date.fromisoformat(args.date).isoformat()
        except ValueError:
            return None, (2, {"error": "usage",
                              "detail": "bad --date %r, expected YYYY-MM-DD" % args.date})
    else:
        date = pvr_client.today_ist().isoformat()

    time_from_min = time_to_min = None
    try:
        if args.time_from:
            time_from_min = parse_hhmm(args.time_from)
        if args.time_to:
            time_to_min = parse_hhmm(args.time_to)
    except ValueError as exc:
        return None, (2, {"error": "usage", "detail": str(exc)})

    if args.party_size < 1:
        return None, (2, {"error": "usage", "detail": "--party-size must be >= 1"})
    if (args.lat is None) != (args.lng is None):
        return None, (2, {"error": "usage",
                          "detail": "--lat and --lng must be given together"})

    seat_detail = max(0, min(args.seat_detail, SEAT_DETAIL_CAP))

    if args.lat is not None:
        origin = {"lat": args.lat, "lng": args.lng,
                  "label": args.origin_label or "your location",
                  "source": args.origin_source}
    else:
        print("radar: resolving city coordinates (one content/city call on "
              "a cold cache)", file=sys.stderr)
        coords = client.city_coords(args.city)
        if not coords:
            # Distinguish a city PVR does not serve from one that publishes
            # no coordinates (there are 4 such cities); passing --lat/--lng
            # only helps in the second case. Both are usage conditions.
            if client.city_known(args.city) is False:
                return None, (2, {
                    "error": "city_not_serviced",
                    "detail": "PVR INOX does not list city %r; check the "
                              "spelling against the serviced-city list "
                              "(about 116 cities)" % args.city})
            return None, (2, {"error": "city_coords_unavailable",
                              "detail": "no coordinates for city %r; pass "
                                        "--lat and --lng" % args.city})
        origin = {"lat": float(coords[0]), "lng": float(coords[1]),
                  "label": args.origin_label or "%s city centre" % args.city,
                  "source": "city_centre"}

    if args.max_km is not None:
        max_km = args.max_km
    elif args.lat is not None and not args.format:
        max_km = RADIUS_LOCAL_KM
    else:
        max_km = RADIUS_CITY_KM

    return {
        "city": args.city,
        "movie": args.movie or None,
        "format": args.format or None,
        "date": date,
        "time_from": args.time_from or None,
        "time_to": args.time_to or None,
        "time_from_min": time_from_min,
        "time_to_min": time_to_min,
        "party_size": args.party_size,
        "max_km": max_km,
        "limit": max(1, args.limit),
        "seat_detail": seat_detail,
        "tier": (args.tier or "").strip(),
        "max_price": args.max_price,
        "sort": args.sort,
        "no_osrm": bool(args.no_osrm),
        "fixtures": args.fixtures or None,
        "origin": origin,
    }, None


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.fixtures:
        if not os.path.isdir(args.fixtures):
            return usage_error("--fixtures %r is not a directory" % args.fixtures)
        client = FixtureClient(args.fixtures)
    else:
        client = LiveClient()

    def log(message):
        print(message, file=sys.stderr)

    try:
        query, failure = build_query(args, client)
        if failure:
            code, payload = failure
            print(json.dumps(payload))
            return code
        # OSRM is live-only and opt-out; fixture runs never touch any network.
        use_osrm = not query["no_osrm"] and client.source == "live"
        radar = solve(client, query, use_osrm=use_osrm, log=log)
    except Blocked as exc:
        # Blocked before any venue outcome existed: no partial to salvage.
        print(json.dumps({"error": "UPSTREAM_BLOCKED", "detail": str(exc)}))
        return 3
    except BudgetExhausted as exc:
        print(json.dumps({"error": "CALL_BUDGET_EXHAUSTED", "detail": str(exc)}))
        return 4
    except Exception as exc:  # noqa: BLE001 (structured error shape, R16)
        print(json.dumps({"error": "unexpected", "detail": "%s: %s"
                          % (type(exc).__name__, exc)}))
        return 4

    print(json.dumps(radar, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
