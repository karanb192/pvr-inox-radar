# Adapted from notprashanth/pvr-inox-mcp (MIT), Copyright Prashanth Krishnan.
# https://github.com/notprashanth/pvr-inox-mcp
"""Stdlib-only client for the PVR INOX web booking API.

The API surface, the blank Bearer header trick, the closed-date semantics,
the pacing and backoff discipline, the IST handling, the IPv4-first resolver,
the variant mapping, the zone and seats-together engine, and the horizon
binary search are all engineering from notprashanth/pvr-inox-mcp (MIT) by
Prashanth Krishnan. This module adapts them with credit; none of it is ours.

Key upstream facts (theirs, verified against our 2026-08-21 fixtures):

- Every call needs "Authorization: Bearer " with an EMPTY token. The API
  403s without the header and works with it blank. No login, no key.
- A date not yet open for booking can answer transport HTTP 500. "Has the
  window opened" is a boolean, not a diff of show lists.
- The edge is Akamai-fronted and blocks IPs that hammer it. Calls here are
  strictly sequential with a minimum spacing, and a 403/429 trips a persisted
  cooldown so even a fresh process backs off.
- PVR withholds whole seat rows and releases them later; a withheld seat is
  indistinguishable from a sold one without seat history (their README's
  insight, credited wherever we surface seat counts).

Design per SPEC section 2: pure parsing/counting functions over dicts, with
network and disk I/O only at the edges, and every cross-process fact
(pacing clock, block cooldown, city coords, variants, horizon, screen
geometry) persisted under the cache directory.
"""

import datetime
import json
import math
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# IPv4-first resolver (adapted from the reference; see module docstring).
# Akamai-fronted hosts hang about 75s on a black-holed IPv6 route before
# falling back, so prefer A records. Capture the ORIGINAL resolver via a
# sentinel attribute: a module reload would otherwise wrap the wrapper and
# recurse on the first lookup.
# ---------------------------------------------------------------------------

_getaddrinfo = getattr(socket, "_pvr_original_getaddrinfo", socket.getaddrinfo)
socket._pvr_original_getaddrinfo = _getaddrinfo


def _ipv4_first(*args, **kwargs):
    """Prefer IPv4, but never return an empty list.

    Filtering to AF_INET unconditionally means a resolver that momentarily
    answers with only AAAA records yields nothing, which in a long-lived
    process looks like a permanent outage. Fall back to the full answer.
    """
    results = _getaddrinfo(*args, **kwargs)
    return [r for r in results if r[0] == socket.AF_INET] or results


socket.getaddrinfo = _ipv4_first

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://api3.pvrcinemas.com/api/v1/booking"
SEAT_PAGE = "https://www.pvrcinemas.com/seatlayout/"

# The chain is India-only, so "today" means today in India, never on the
# machine: UTC machines between IST midnight and 05:30 otherwise poll a day
# whose shows have all finished (reference lesson).
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

DEFAULT_CITY = "Chennai"  # for calls with no city context, matching reference
DEFAULT_LATLNG = ("13.0827", "80.2707")  # last-resort body coords only

MIN_INTERVAL_FLOOR = 0.6  # seconds between calls; env may only raise this
BLOCK_COOLDOWN = 900.0  # seconds to back off after an upstream 403/429

STATE_FILE = "state.json"
CITY_COORDS_FILE = "city_coords.json"
CITIES_FILE = "cities.json"
VARIANTS_FILE = "variants.json"
HORIZON_FILE = "horizon.json"
SCREENS_FILE = "screens.json"

# statusCode is a hex chip color the API attaches to every show. Confirmed
# from the 2026-08-21 capture: 76BE43 on all 82 sampled shows, statusTxt
# "Available". Every other hex maps to "unknown" until a busier capture pins
# it; callers always carry the raw hex alongside the category so a renderer
# can paint the API's own color even when the category is unknown.
STATUS_CATEGORY_BY_HEX = {
    "76BE43": "available",
}

# Formats where the centre block can be a distinct priced class, so a
# sold-out zone is a real fact and silent widening would misreport it
# (reference BACKLOG #8; SPEC R41).
PREMIUM_FORMATS = (
    "IMAX",
    "4DX",
    "GOLD",
    "INSIGNIA",
    "DIRECTOR'S CUT",
    "PLAYHOUSE",
    "LUXE",
    "ICE",
)

# Rows 60 to 85 percent of the way toward the BACK are the usual big-screen
# sweet spot. The rows list arrives FRONT-FIRST while letters usually descend
# toward the back (so row A is commonly the BACK row): always index by list
# position, never by letter.
ZONE_BAND = (0.60, 0.85)

WITHHELD_CAVEAT = (
    "A withheld seat is indistinguishable from a sold one without seat "
    "history: PVR opens dates with rows withheld and releases them later "
    "(finding by notprashanth/pvr-inox-mcp, MIT)."
)

LANGUAGES = {
    "english": "English", "hindi": "Hindi", "tamil": "Tamil",
    "telugu": "Telugu", "malayalam": "Malayalam", "kannada": "Kannada",
    "marathi": "Marathi", "bengali": "Bengali", "punjabi": "Punjabi",
    "gujarati": "Gujarati", "odia": "Odia", "assamese": "Assamese",
    "bhojpuri": "Bhojpuri", "urdu": "Urdu", "japanese": "Japanese",
    "korean": "Korean", "spanish": "Spanish", "french": "French",
    "german": "German", "mandarin": "Mandarin", "chinese": "Chinese",
    "nepali": "Nepali", "konkani": "Konkani", "tulu": "Tulu",
}

FORMAT_HINTS = ("IMAX", "4DX", "3D", "2D", "ATMOS", "DTSX", "DOLBY",
                "P[XL]", "BIGPIX", "LASER", "ICE")


class Blocked(Exception):
    """Upstream refused us (403/429). Stop the whole run; never retry in a loop."""


class BudgetExhausted(Exception):
    """A caller's self-imposed call ceiling was reached; stop making calls.

    Defined here (not in the caller) so this module's broad error handlers
    can re-raise it the same way they re-raise Blocked: budget exhaustion
    must surface as itself, never masquerade as a per-venue transport error.
    """


# ---------------------------------------------------------------------------
# Time helpers (IST everywhere)
# ---------------------------------------------------------------------------


def now_ist():
    return datetime.datetime.now(IST)


def today_ist():
    return now_ist().date()


def show_time_minutes(text):
    """'06:05 PM' or '19:40' to minutes since midnight; None when unparseable.

    Returns None instead of a zero fallback so callers can keep and flag
    unparseable times symmetrically (SPEC R59) rather than silently dropping
    the show on one side of a time window.
    """
    raw = str(text or "").strip().upper()
    if not raw:
        return None
    try:
        if raw.endswith("AM") or raw.endswith("PM"):
            meridiem = raw[-2:]
            parts = raw[:-2].strip().split(":")
            hour = int(parts[0]) % 12 + (12 if meridiem == "PM" else 0)
            minute = int(parts[1]) if len(parts) > 1 else 0
            return hour * 60 + minute
        parts = raw.split(":")
        return int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Persisted caches (SPEC R13, R14): everything cross-process lives on disk,
# written atomically, under one 0700 directory.
# ---------------------------------------------------------------------------


def cache_dir():
    path = os.environ.get("PVR_RADAR_CACHE_DIR")
    if not path:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
        path = os.path.join(base, "pvr-inox-radar")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def load_cache(name):
    """Read one JSON cache file; missing or corrupt reads as empty."""
    try:
        with open(os.path.join(cache_dir(), name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(name, obj):
    """Atomic JSON write: tempfile in the same directory, then os.replace."""
    directory = cache_dir()
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, os.path.join(directory, name))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Transport and politeness (SPEC R19 to R25)
# ---------------------------------------------------------------------------


def min_interval():
    """Seconds between calls. PVR_MIN_INTERVAL may only RAISE the floor."""
    try:
        value = float(os.environ.get("PVR_MIN_INTERVAL", MIN_INTERVAL_FLOOR))
    except (TypeError, ValueError):
        value = MIN_INTERVAL_FLOOR
    return max(value, MIN_INTERVAL_FLOOR)


def _headers(city):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Authorization": "Bearer ",  # deliberately blank; 403 without the header
        "chain": "PVR",
        "country": "INDIA",
        "appVersion": "1.0",
        "platform": "WEBSITE",
        "flow": "PVRINOX",
        "city": city,
    }


_throttle = threading.Lock()
_last_call_mono = [0.0]


def _pace():
    """Enforce spacing and the persisted block cooldown before every call.

    The pacing clock is persisted (state.json last_call) so consecutive
    short-lived processes honor the interval too, and a 403/429 cooldown
    written by any process stops every later process until it expires.
    """
    with _throttle:
        state = load_cache(STATE_FILE)
        now_wall = time.time()
        blocked_until = float(state.get("blocked_until") or 0.0)
        if now_wall < blocked_until:
            raise Blocked(
                "upstream refused us earlier; cooling off for %d more seconds"
                % int(blocked_until - now_wall))

        interval = min_interval()
        wait = 0.0
        now_mono = time.monotonic()
        if _last_call_mono[0]:
            wait = max(wait, interval - (now_mono - _last_call_mono[0]))
        last_wall = float(state.get("last_call") or 0.0)
        if last_wall:
            wait = max(wait, interval - (now_wall - last_wall))
        if wait > 0:
            time.sleep(wait)

        _last_call_mono[0] = time.monotonic()
        state["last_call"] = time.time()
        save_cache(STATE_FILE, state)


def _mark_blocked():
    state = load_cache(STATE_FILE)
    state["blocked_until"] = time.time() + BLOCK_COOLDOWN
    save_cache(STATE_FILE, state)


def _post(path, body, city=DEFAULT_CITY, timeout=30):
    """The single choke point for every API call. Paced; maps 403/429 to Blocked."""
    _pace()
    req = urllib.request.Request(
        "%s/%s" % (API_BASE, path),
        json.dumps(body).encode("utf-8"),
        _headers(city),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            _mark_blocked()
            raise Blocked(
                "upstream returned %d; backing off %d seconds"
                % (exc.code, int(BLOCK_COOLDOWN))) from exc
        raise


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def haversine_km(lat1, lng1, lat2, lng2):
    """Straight-line km, rounded to 0.1. Computed locally so distance never
    depends on what the upstream assumed our origin was. None on bad input."""
    try:
        lat1, lng1, lat2, lng2 = (float(x) for x in (lat1, lng1, lat2, lng2))
    except (TypeError, ValueError):
        return None
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return round(2 * radius * math.asin(math.sqrt(a)), 1)


def status_category(hex_code):
    """Map a show's statusCode hex color to a category (SPEC R31).

    Only 76BE43 = available is confirmed from the 2026-08-21 capture; every
    other hex is "unknown". Callers must pass the raw hex through alongside
    the category so renderers can still paint the API's own color. The
    reference repo measured that statusTxt lies in both directions, so these
    are cheap first-pass colors only; verified seat counts beat them.
    """
    key = str(hex_code or "").strip().lstrip("#").upper()
    return STATUS_CATEGORY_BY_HEX.get(key, "unknown")


def screen_type_norm(value):
    """Uppercase and strip for format matching: the venue payload mixes case
    ("Premium", "Atmos") while csessions uses upper case (PREMIUM, ATMOS)."""
    return str(value or "").strip().upper()


def is_premium_format(value):
    """True when a screenType / experience names a premium house (SPEC R41)."""
    norm = screen_type_norm(value)
    if not norm:
        return False
    return any(fmt in norm for fmt in PREMIUM_FORMATS)


def deep_link(encrypted):
    """Per-show deep link into PVR's own purchase flow (SPEC R33)."""
    return SEAT_PAGE + encrypted if encrypted else None


def is_lapsed(show):
    """True when no seat map exists upstream for this show (SPEC R45).

    Sold-out shows are NOT lapsed: their maps exist and restocks appear there.
    """
    if not show.get("encrypted"):
        return True
    return (show.get("statusTxt") or "").strip().lower() == "lapsed"


def parse_title(raw):
    """Split 'NAME (TAMIL WITH ENGLISH SUBTITLE)' into recognizable parts.

    A parenthetical is NOT reliably a language, so only recognized language
    words and format hints are reported; callers prefer structured fields.
    Adapted from the reference title parser.
    """
    text = str(raw or "")
    out = {"raw": text, "title": text, "language": None,
           "subtitle_language": None, "formats": []}

    body = text
    groups = []
    while body.endswith(")") and "(" in body:
        start = body.rfind("(")
        groups.append(body[start + 1:-1])
        body = body[:start].strip()
    out["title"] = body or text

    for group in groups:
        upper = group.upper()
        for hint in FORMAT_HINTS:
            if hint in upper and hint not in out["formats"]:
                out["formats"].append(hint)
        parts = upper.split(" WITH ")
        for word in parts[0].replace("[", " ").replace("]", " ").split():
            name = LANGUAGES.get(word.lower())
            if name and not out["language"]:
                out["language"] = name
        if len(parts) > 1:
            for word in parts[1].split():
                for lang_key, name in LANGUAGES.items():
                    # Truncated data ("ENGLI") means prefix matching.
                    if lang_key.startswith(word.lower()) or word.lower().startswith(lang_key[:5]):
                        out["subtitle_language"] = out["subtitle_language"] or name
                        break
    return out


def resolve_language(variant_language, field_language, film_title):
    """Per-show language, resolution order per SPEC R28:
    variant map first, then the show's own language field, then the title
    parenthetical. Returns (language, source, disputed); disputed is True
    when any two present values disagree. Never judged from block titles or
    release-language lists.
    """
    title_language = parse_title(film_title)["language"]
    candidates = (
        ("variant", variant_language),
        ("api_field", field_language),
        ("title", title_language),
    )
    language, source = None, None
    for src, value in candidates:
        if value:
            language, source = str(value).strip(), src
            break
    present = {str(v).strip().lower() for _, v in candidates if v}
    return language, source, len(present) > 1


# ---------------------------------------------------------------------------
# Cities (SPEC R26)
# ---------------------------------------------------------------------------


def parse_city_records(output):
    """Pure: city records from a content/city payload output.

    Each record is typed city vs metro_rollup (subcities present, or name
    ends "-All"); rollup constituents are inferred by name stem when absent.
    Records are deduped by name here, and a coordinate-less duplicate (the
    subcity stubs under rollups carry no coords) is backfilled from the
    coordinated top-level entry whichever the walk meets first. Anyone
    AGGREGATING venues across a rollup must dedup by cinema theatreId, never
    by city name, or the same cinema counts several times. Cities without any
    published coordinates claim none (no fabricated origin).
    """
    found = {}

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            if name and (node.get("lat") or node.get("id")):
                key = str(name).strip()
                rec = found.setdefault(key, {
                    "name": key, "id": None, "state": "", "cinemas": None,
                    "lat": None, "lng": None, "subcities": [],
                })
                if rec["id"] is None:
                    rec["id"] = node.get("id")
                if not rec["state"]:
                    rec["state"] = node.get("state") or ""
                if rec["cinemas"] is None:
                    rec["cinemas"] = node.get("cinemaCount")
                if not (rec["lat"] and rec["lng"]) and node.get("lat") and node.get("lng"):
                    rec["lat"], rec["lng"] = node.get("lat"), node.get("lng")
                for sub in node.get("subcities") or []:
                    if isinstance(sub, dict) and sub.get("name") \
                            and sub["name"] not in rec["subcities"]:
                        rec["subcities"].append(sub["name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(output or {})

    for rec in found.values():
        rollup = bool(rec["subcities"]) or rec["name"].lower().endswith("-all")
        rec["type"] = "metro_rollup" if rollup else "city"
        if rollup and not rec["subcities"]:
            stem = rec["name"].rsplit("-", 1)[0].strip().lower()
            rec["subcities"] = sorted(
                other["name"] for other in found.values()
                if other["name"] != rec["name"]
                and other["name"].strip().lower().startswith(stem))
    return sorted(found.values(), key=lambda r: r["name"])


def list_cities():
    """content/city: every city record, coords cached into city_coords.json
    and the full serviced-city name list into cities.json."""
    payload = _post("content/city",
                    {"lat": DEFAULT_LATLNG[0], "lng": DEFAULT_LATLNG[1]})
    records = parse_city_records(payload.get("output") or {})
    coords = load_cache(CITY_COORDS_FILE)
    for rec in records:
        if rec.get("lat") and rec.get("lng"):
            coords[rec["name"].strip().lower()] = [str(rec["lat"]), str(rec["lng"])]
    save_cache(CITY_COORDS_FILE, coords)
    if records:
        save_cache(CITIES_FILE,
                   {"names": sorted(r["name"].strip().lower() for r in records)})
    return records


def known_city_names():
    """The serviced-city name set (lowercased) from the persisted cache, or
    None when the city list has never been fetched. Lets callers distinguish
    'PVR does not serve this city' from 'city has no published coords'."""
    names = load_cache(CITIES_FILE).get("names")
    return set(names) if names else None


def city_coords(city):
    """(lat, lng) strings for a city from the persisted cache, fetching the
    city list once on a cold cache. None when the city publishes no coords."""
    name = str(city or "").strip().lower()
    if not name:
        return None
    coords = load_cache(CITY_COORDS_FILE)
    if name in coords:
        return tuple(coords[name])
    try:
        list_cities()
    except (Blocked, BudgetExhausted):
        raise
    except Exception:
        return None
    coords = load_cache(CITY_COORDS_FILE)
    value = coords.get(name)
    return tuple(value) if value else None


# ---------------------------------------------------------------------------
# Cinemas (SPEC R27)
# ---------------------------------------------------------------------------


def parse_cinemas(output, origin, origin_label):
    """Pure: venue records from a content/cinemas payload output.

    Distance is always computed locally with haversine from a stated origin
    (the API distance-filters and its echoed distances are untrustworthy when
    the origin is wrong); with no origin, no distance is claimed.
    """
    found = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("theatreId") and node.get("name"):
                screens = node.get("screens") or {}
                screen_values = screens.values() if isinstance(screens, dict) else screens
                found[node["theatreId"]] = {
                    "theatreId": node["theatreId"],
                    "name": node["name"],
                    "address": node.get("address1", ""),
                    "lat": node.get("latitude"),
                    "lng": node.get("longitude"),
                    "showCount": node.get("showCount", 0),
                    "formats": sorted({
                        screen_type_norm(s.get("screenType"))
                        for s in screen_values
                        if isinstance(s, dict) and s.get("screenType")
                    }),
                }
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(output or {})
    rows = list(found.values())
    for row in rows:
        km = (haversine_km(origin[0], origin[1], row["lat"], row["lng"])
              if origin else None)
        row["distance_km"] = km
        row["distance_from"] = origin_label if km is not None else "none"
    if origin:
        rows.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0.0))
    else:
        rows.sort(key=lambda r: -(r["showCount"] or 0))
    return rows


def list_cinemas(city, lat=None, lng=None):
    """content/cinemas for a city. Caller origin beats city centre; the body
    lat/lng are strings; venues carry distance_from: caller | city_centre | none."""
    centre = city_coords(city)
    if lat and lng:
        origin, label = (lat, lng), "caller"
    elif centre:
        origin, label = centre, "city_centre"
    else:
        origin, label = None, "none"
    body_lat, body_lng = origin or DEFAULT_LATLNG
    payload = _post(
        "content/cinemas",
        {"city": city, "lat": str(body_lat), "lng": str(body_lng), "text": ""},
        city,
    )
    return parse_cinemas(payload.get("output") or {}, origin, label)


# ---------------------------------------------------------------------------
# Now showing and film variants (SPEC R28)
# ---------------------------------------------------------------------------


def parse_films(output):
    """Pure: film summaries from a content/nowshowing payload output."""
    films = []
    for movie in (output or {}).get("mv") or []:
        films.append({
            "name": movie.get("n") or movie.get("filmName"),
            "languages": movie.get("mfs") or [],
            "genres": movie.get("grs") or [],
            "showCount": movie.get("showCount") or 0,
            "formats": [e.get("expName") for e in (movie.get("experiences") or [])
                        if isinstance(e, dict) and e.get("expName")],
            "releaseDate": movie.get("releaseDate") or "",
        })
    return sorted(films, key=lambda f: -f["showCount"])


def parse_variants(output):
    """Pure: per-print variant map {filmId: {name, language, format, subtitle}}.

    A schedule block carries ONE variant's title while its shows span many
    filmIds (reference finding), so the per-show movieId is the true identity
    and this map resolves it to its actual print.
    """
    found = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("filmId") and node.get("filmName"):
                found[str(node["filmId"])] = {
                    "name": node["filmName"],
                    "language": node.get("language"),
                    "format": node.get("format") or "",
                    "subtitle": node.get("subtitle") or "",
                }
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(output or {})
    return found


def now_showing(city, lat=None, lng=None):
    """content/nowshowing: films currently playing in a city."""
    centre = city_coords(city)
    body_lat, body_lng = (lat, lng) if (lat and lng) else (centre or DEFAULT_LATLNG)
    payload = _post(
        "content/nowshowing",
        {"city": city, "lat": str(body_lat), "lng": str(body_lng)},
        city,
    )
    return parse_films(payload.get("output") or {})


def film_variants(city, lat=None, lng=None, date=None):
    """Variant map for a city, cached per city per IST day in variants.json.

    One live call per city per day at most. Blocked propagates; any other
    failure returns {} so a showtimes lookup degrades instead of breaking.
    """
    day = date or today_ist().isoformat()
    key = "%s|%s" % (str(city or "").strip().lower(), day)
    cache = load_cache(VARIANTS_FILE)
    if key in cache:
        return cache[key]
    try:
        centre = city_coords(city)
        body_lat, body_lng = (lat, lng) if (lat and lng) else (centre or DEFAULT_LATLNG)
        payload = _post(
            "content/nowshowing",
            {"city": city, "lat": str(body_lat), "lng": str(body_lng)},
            city,
        )
    except (Blocked, BudgetExhausted):
        raise
    except Exception:
        return {}
    found = parse_variants(payload.get("output") or {})
    cache = load_cache(VARIANTS_FILE)
    cache[key] = found
    save_cache(VARIANTS_FILE, cache)
    return found


# ---------------------------------------------------------------------------
# Day sessions (SPEC R29, R30) and the booking horizon (R34)
# ---------------------------------------------------------------------------


def classify_csessions(payload):
    """Pure three-way JSON-body mapping: open when status == 302 with
    non-empty output; any other JSON body is closed (not on sale yet)."""
    if payload.get("status") == 302 and payload.get("output"):
        return "open"
    return "closed"


def _parse_show(node, movie_re, experience_key, variants):
    variant = (variants or {}).get(str(node.get("movieId"))) or {}
    block = movie_re or {}
    film = variant.get("name") or block.get("filmName") or ""
    language, source, disputed = resolve_language(
        variant.get("language"), node.get("language"), film)
    token = node.get("encrypted") or ""
    hex_code = node.get("statusCode") or ""
    return {
        "movieId": node.get("movieId"),
        "sessionId": node.get("sessionId"),
        "theatreId": node.get("theatreId"),
        "showDate": node.get("showDate"),
        "showTime": node.get("showTime"),
        "showTimeStamp": node.get("showTimeStamp") or 0,
        "endTimeStamp": node.get("endTimeStamp") or 0,
        "screenName": node.get("screenName") or "",
        "screenType": node.get("screenType") or "",
        "language": language,
        "language_source": source,
        "language_disputed": disputed,
        "subtitle": bool(node.get("subtitle")),
        "movieFormat": node.get("movieFormat") or "",
        "soundFormat": node.get("soundFormat") or "",
        "statusCode": hex_code,
        "status_category": status_category(hex_code),
        "statusTxt": node.get("statusTxt") or "",
        "encrypted": token,
        "deep_link": deep_link(token),
        "film": film,
        "filmId": block.get("id"),
        "experienceKey": experience_key or "",
    }


def extract_shows(output, variants=None):
    """Pure: every show in a csessions payload output, sorted by start stamp.

    The walk is recursive and keyed on what a show LOOKS like (sessionId plus
    encrypted) rather than hardcoded nesting, carrying the nearest enclosing
    movieRe block and experienceKey as context, so an upstream reshuffle
    degrades gracefully. The top-level output.showCount is 0 even with 59
    shows inside: never trust it, count the shows directly.

    Payload-internal duplicates are dropped on the reference project's dedup
    key (cinema, date, time, screen): the same physical show listed twice
    would otherwise appear twice and could burn two seat calls.
    """
    shows = []
    seen = set()

    def walk(node, film_ctx, exp_key):
        if isinstance(node, dict):
            movie_re = node.get("movieRe")
            if isinstance(movie_re, dict):
                film_ctx = movie_re
            if isinstance(node.get("experienceKey"), str):
                exp_key = node["experienceKey"]
            if node.get("sessionId") is not None and "encrypted" in node:
                show = _parse_show(node, film_ctx, exp_key, variants)
                key = (show["theatreId"], show["showDate"],
                       show["showTime"], show["screenName"])
                if key not in seen:
                    seen.add(key)
                    shows.append(show)
                return
            for value in node.values():
                walk(value, film_ctx, exp_key)
        elif isinstance(node, list):
            for value in node:
                walk(value, film_ctx, exp_key)

    walk(output or {}, {}, "")
    shows.sort(key=lambda s: s.get("showTimeStamp") or 0)
    return shows


def day_sessions(city, cid, date, lat=None, lng=None, variants=None):
    """content/csessions: one cinema, one date. Three-way outcome dict, never
    conflated (SPEC R29):

      {"outcome": "open", "shows": [...]}
      {"outcome": "closed"}                 date not on sale YET, never "sold out"
      {"outcome": "error", "error": "..."}  transport trouble, never closed

    Mapping: payload status 302 with non-empty output is open; a JSON body
    with any other status is closed; transport HTTP 500 is closed (live
    capture proved an unopened date can answer transport-level 500); any
    other transport failure is an error. Blocked raises.
    """
    if not (lat and lng):
        centre = city_coords(city)
        lat, lng = centre or DEFAULT_LATLNG
    body = {
        "city": city,
        "cid": str(cid),
        "lat": str(lat),
        "lng": str(lng),
        "dated": date,
        "qr": "NO",
        "cineType": "",
        "cineTypeQR": "",
    }
    try:
        payload = _post("content/csessions", body, city)
    except (Blocked, BudgetExhausted):
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 500:
            return {"outcome": "closed"}
        return {"outcome": "error", "error": "http %s" % exc.code}
    except Exception as exc:
        return {"outcome": "error", "error": "error %s" % exc}

    if classify_csessions(payload) != "open":
        return {"outcome": "closed"}

    if variants is None:
        variants = film_variants(city, lat, lng)
    return {"outcome": "open",
            "shows": extract_shows(payload.get("output") or {}, variants)}


def booking_horizon(city, cid, lat=None, lng=None, max_days=21):
    """Last date currently on sale at one cinema: binary search over offsets
    0..max_days from IST today (lo known-open, hi assumed-closed, about 5
    calls), cached per (city, cinema, IST day) in horizon.json.

    Returns {"last_open_date": iso or None, "days_ahead": int or None}.
    Blocked aborts and returns unknown (both None, never cached) so a horizon
    probe never deepens a block. With transport 500 mapped to closed in
    day_sessions, the horizon cannot overstate. "Closed" always means "not on
    sale YET", never "sold out".
    """
    today = today_ist()
    key = "%s|%s|%s" % (str(city or "").strip().lower(), cid, today.isoformat())
    cache = load_cache(HORIZON_FILE)
    if key in cache:
        stored = cache[key]
        return {"last_open_date": stored[0], "days_ahead": stored[1]}

    def open_on(offset):
        day = (today + datetime.timedelta(days=offset)).isoformat()
        return day_sessions(city, cid, day, lat, lng)["outcome"] != "closed"

    result = (None, None)
    try:
        if open_on(0):
            lo, hi = 0, max_days
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if open_on(mid):
                    lo = mid
                else:
                    hi = mid
            result = ((today + datetime.timedelta(days=lo)).isoformat(), lo)
    except Blocked:
        return {"last_open_date": None, "days_ahead": None}

    cache = load_cache(HORIZON_FILE)
    cache[key] = list(result)
    save_cache(HORIZON_FILE, cache)
    return {"last_open_date": result[0], "days_ahead": result[1]}


# ---------------------------------------------------------------------------
# Seat layout parsing and the seats-together engine (SPEC R35 to R45).
# Zone derivation, aisle-aware adjacency, auto-widening, and the geometry
# memo are all adapted from the reference (see module docstring).
# ---------------------------------------------------------------------------


def parse_seat_rows(raw_rows):
    """Pure: parsed seat rows from a seatlayout output rows[] array.

    rows[] interleaves t == "area" price-tier headers (applying to the seat
    rows that FOLLOW until the next header; the same tier can repeat) and
    t == "seats" rows. Rows arrive FRONT-FIRST. Cells: an empty sn is an
    aisle or grid padding (breaks adjacency); displaynumber is the within-row
    seat number, stored in DESCENDING order (right to left); c/pc joins
    priceList; st is NOT availability (nonzero st observed on free seats).
    """
    rows, tier, tier_net = [], "", ""
    for entry in raw_rows or []:
        kind = entry.get("t")
        if kind == "area":
            tier = entry.get("n") or ""
            tier_net = entry.get("nn") or ""
            continue
        if kind != "seats":
            continue
        cells = []
        for cell in entry.get("s") or []:
            sn = cell.get("sn") or ""
            number = None
            if sn:
                try:
                    number = int(cell.get("displaynumber") or "")
                except (TypeError, ValueError):
                    number = None
            cells.append({
                "sn": sn,
                "aisle": not sn,
                "s": cell.get("s"),
                "number": number,
                "price_code": cell.get("c") or cell.get("pc") or "",
                "hc": bool(cell.get("hc")),
                "st": cell.get("st"),
            })
        rows.append({"name": entry.get("n") or "", "tier": tier,
                     "tier_net": tier_net, "cells": cells})
    return rows


def row_blocks(row):
    """Aisle-delimited blocks of real seats, in row order."""
    blocks, current = [], []
    for cell in row["cells"]:
        if cell["aisle"]:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(cell)
    if current:
        blocks.append(current)
    return blocks


def centre_block(row):
    """The block the row's midpoint falls in: the middle section between aisles."""
    blocks = row_blocks(row)
    if not blocks:
        return []
    total = sum(len(b) for b in blocks)
    midpoint, seen = total // 2, 0
    for block in blocks:
        seen += len(block)
        if seen > midpoint:
            return block
    return blocks[-1]


def derive_zone(rows, zone_rows=None, zone_seats=None):
    """The good-seats zone: {row_name: set of seat numbers}.

    Derived per hall from its own geometry (hardcoded row letters never
    transfer): rows 60 to 85 percent of the way toward the BACK by list
    position (the list is front-first; letters commonly descend toward the
    back, so row A is often the BACK row and letters must never be read as
    front-to-back), and within each row the aisle-delimited block containing
    the row's midpoint. Explicit zone_rows / zone_seats override the
    derivation and are treated as instructions.
    """
    if zone_rows:
        wanted = set(zone_rows)
        chosen = [r for r in rows if r["name"] in wanted]
    else:
        count = len(rows)
        lo, hi = int(count * ZONE_BAND[0]), int(count * ZONE_BAND[1])
        chosen = rows[lo:hi + 1] or list(rows)

    zone = {}
    for row in chosen:
        if zone_seats:
            lo_n, hi_n = zone_seats
            numbers = {c["number"] for c in row["cells"]
                       if c["sn"] and c["number"] is not None
                       and lo_n <= c["number"] <= hi_n}
        else:
            numbers = {c["number"] for c in centre_block(row)
                       if c["number"] is not None}
        zone[row["name"]] = numbers
    return zone


def _seat_number(label):
    digits = "".join(ch for ch in str(label) if ch.isdigit())
    return int(digits) if digits else 0


def _span_label(labels):
    """'B1-B11' regardless of which way the row is numbered in the payload:
    runs are labeled by seat position, never by array order."""
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    lo = min(labels, key=_seat_number)
    hi = max(labels, key=_seat_number)
    return "%s-%s" % (lo, hi)


def row_free_runs(row):
    """Contiguous free runs in a whole row, as lists of seat labels.

    Aisles and non-free seats break runs. Adjacency is physical: consecutive
    non-aisle cells are neighbors whichever way the row is numbered.
    """
    runs, current = [], []
    for cell in row["cells"]:
        if cell["aisle"]:
            if current:
                runs.append(current)
                current = []
            continue
        if cell["s"] == 1:
            current.append(cell["sn"])
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def score_zone(rows, zone):
    """Pure arithmetic over one hall against one zone definition; no requests.

    Split out so a hall can be scored more than once from the SAME payload,
    which is what lets widening re-judge a bigger zone for free. Seat status:
    s == 1 free, s == 2 sold, anything else is the house withholding the seat
    (counted as held, never free, never sold). Runs break on aisles,
    out-of-zone seats, and non-free seats.
    """
    out = {"total": 0, "free": 0, "sold": 0, "held": 0,
           "zone_total": 0, "zone_free": 0, "zone_held": 0,
           "best_run": 0, "best_where": ""}
    for row in rows:
        allowed = zone.get(row["name"], set())
        run = []
        for cell in row["cells"]:
            if cell["aisle"]:
                run = []
                continue
            out["total"] += 1
            code = cell["s"]
            is_free = code == 1
            if is_free:
                out["free"] += 1
            elif code == 2:
                out["sold"] += 1
            else:
                out["held"] += 1
            if cell["number"] not in allowed:
                run = []  # outside the zone; a run cannot straddle the edge
                continue
            out["zone_total"] += 1
            if is_free:
                out["zone_free"] += 1
                run.append(cell["sn"])
                if len(run) > out["best_run"]:
                    out["best_run"] = len(run)
                    out["best_where"] = _span_label(run)
            else:
                if code != 2:
                    out["zone_held"] += 1
                run = []
    return out


def hall_best_run(rows):
    """(length, span) of the longest free run anywhere in the hall."""
    best, where = 0, ""
    for row in rows:
        for run in row_free_runs(row):
            if len(run) > best:
                best, where = len(run), _span_label(run)
    return best, where


def alternative_rows(rows, zone, party_size):
    """Rows OUTSIDE the zone that could seat the party, best first.

    Rows are listed front-first, so deeper (further back) is generally
    better; ranked by depth capped at the band's back edge, then run length.
    """
    need = max(1, party_size)
    candidates = []
    for index, row in enumerate(rows):
        runs = [r for r in row_free_runs(row) if len(r) >= need]
        if not runs:
            continue
        best = max(runs, key=len)
        candidates.append({
            "row": row["name"],
            "in_zone": bool(zone.get(row["name"])),
            "best_run": len(best),
            "best_where": _span_label(best),
            "depth": index / max(1, len(rows) - 1),
        })
    return sorted(
        [c for c in candidates if not c["in_zone"]],
        key=lambda c: (-min(c["depth"], ZONE_BAND[1]), -c["best_run"]))


def widen_zone(rows, zone, party_size, alternatives):
    """Widen into up to 3 best alternative rows taken WHOLE, from the same
    payload at zero extra requests. Re-restricting added rows to their centre
    block was measured (by the reference) to add zero free seats: the run
    being widened to is off-centre by construction. Returns (zone, added)."""
    need = max(1, party_size)
    extra = [a["row"] for a in alternatives if a["best_run"] >= need][:3]
    if not extra:
        return zone, []
    widened = dict(zone)
    wanted = set(extra)
    for row in rows:
        if row["name"] in wanted:
            widened[row["name"]] = {c["number"] for c in row["cells"]
                                    if c["sn"] and c["number"] is not None}
    return widened, extra


def _price_value(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def price_tiers(price_list):
    """Pure: per-tier price summary from a seatlayout priceList."""
    tiers = []
    for code, info in (price_list or {}).items():
        if not isinstance(info, dict):
            continue
        tiers.append({
            "code": code,
            "description": info.get("description") or "",
            "price": info.get("price") or "",
            "net": info.get("netPerTkt") or "",
        })
    tiers.sort(key=lambda t: -_price_value(t["price"]))
    return tiers


def seat_price(cell, price_list):
    """Per-seat gross price via the c/pc join to priceList, or None."""
    info = (price_list or {}).get(cell.get("price_code") or "")
    return info.get("price") if isinstance(info, dict) else None


def geometry_from_rows(rows):
    """Pure: hall shape memo from parsed rows (shape never changes)."""
    seats_per_row = [sum(1 for c in row["cells"] if c["sn"]) for row in rows]
    return {
        "rows": len(rows),
        "max_row_seats": max(seats_per_row) if seats_per_row else 0,
        "seats": sum(seats_per_row),
    }


def seat_report_from_payload(output, party_size=1, zone_rows=None,
                             zone_seats=None, screen_type=""):
    """Pure: full seats-together report from one seatlayout payload output.

    best_run is the longest contiguous run of free seats inside the zone;
    aisles and out-of-zone seats break runs; meets_party_size compares it to
    the party. Auto-widen applies only when the zone was DERIVED (an explicit
    zone is an instruction) and the hall is not a premium format: in premium
    houses the centre block can be a distinct priced class, so the zone
    verdict and the whole-hall verdict are reported separately instead of
    silently reframing a sold-out premium block (SPEC R41).

    Display context uses showDateTime (already an IST display string). The
    payload's showTime/endTime are UTC despite carrying no timezone marker
    and are never read (SPEC R43).
    """
    rows = parse_seat_rows((output or {}).get("rows"))
    need = max(1, int(party_size or 1))
    premium = is_premium_format(screen_type) or is_premium_format(
        (output or {}).get("experience"))
    derived = not zone_rows and not zone_seats

    zone = derive_zone(rows, zone_rows, zone_seats)
    scored = score_zone(rows, zone)
    widened_to = []
    if derived and not premium and scored["best_run"] < need:
        alternatives = alternative_rows(rows, zone, need)
        widened, widened_to = widen_zone(rows, zone, need, alternatives)
        if widened_to:
            zone = widened
            scored = score_zone(rows, zone)

    hall_run, hall_where = hall_best_run(rows)
    return {
        "cinemaName": (output or {}).get("cinemaName", ""),
        "showDateTime": (output or {}).get("showDateTime", ""),
        "experience": (output or {}).get("experience", ""),
        "premium": premium,
        "party_size": need,
        "total": scored["total"],
        "free": scored["free"],
        "sold": scored["sold"],
        "held": scored["held"],
        "hall_free": scored["free"],
        "zone_rows_used": [r["name"] for r in rows if zone.get(r["name"])],
        "zone_total": scored["zone_total"],
        "zone_free": scored["zone_free"],
        "zone_held": scored["zone_held"],
        "best_run": scored["best_run"],
        "best_where": scored["best_where"],
        "meets_party_size": scored["best_run"] >= need,
        "widened_to": widened_to,
        "hall_best_run": hall_run,
        "hall_best_where": hall_where,
        "hall_meets_party_size": hall_run >= need,
        "price_tiers": price_tiers((output or {}).get("priceList")),
        "verified": True,
        "caveats": [WITHHELD_CAVEAT],
    }


def remember_geometry(theatre_id, screen_name, rows):
    """Memo a hall's shape into screens.json keyed theatreId|screenName, so
    later hall-size questions cost zero calls. First write wins."""
    geometry = geometry_from_rows(rows)
    if not geometry["seats"]:
        return
    key = "%s|%s" % (theatre_id, screen_name)
    store = load_cache(SCREENS_FILE)
    if key in store:
        return
    store[key] = dict(geometry, theatreId=str(theatre_id), screenName=screen_name)
    save_cache(SCREENS_FILE, store)


def seat_layout(encrypted, city=DEFAULT_CITY):
    """ticketing/seatlayout for one show. Success is payload status 200 with
    output present; callers check. The city header value need not match the
    venue's city (reference behavior)."""
    return _post("ticketing/seatlayout", {"encrypted": encrypted}, city)


def seat_report(encrypted, party_size=1, zone_rows=None, zone_seats=None,
                screen_type="", theatre_id=None, screen_name=None):
    """One live call: fetch a show's seat map and score it.

    Returns {"outcome": "ok", "report": {...}} or
    {"outcome": "error", "error": "..."}; the shapes never mix. Blocked
    raises (stop the whole run). When theatre_id and screen_name are given
    the hall geometry is memoized as a side effect of a call already paid for.
    """
    try:
        payload = seat_layout(encrypted)
    except (Blocked, BudgetExhausted):
        raise
    except urllib.error.HTTPError as exc:
        return {"outcome": "error", "error": "http %s" % exc.code}
    except Exception as exc:
        return {"outcome": "error", "error": "error %s" % exc}

    if payload.get("status") != 200 or not payload.get("output"):
        return {"outcome": "error", "error": "seatmap unavailable"}

    output = payload["output"]
    report = seat_report_from_payload(
        output, party_size, zone_rows, zone_seats, screen_type)
    if theatre_id and screen_name:
        remember_geometry(theatre_id, screen_name,
                          parse_seat_rows(output.get("rows")))
    return {"outcome": "ok", "report": report}
