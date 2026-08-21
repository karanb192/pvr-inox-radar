#!/usr/bin/env python3
"""One-shot Nominatim locality lookup for pvr-inox-radar.

Original code (the PVR client engineering elsewhere in this skill is adapted
from notprashanth/pvr-inox-mcp; see the repo README for credits). Used ONLY
when the user names a locality finer than a city ("Sector 56 Gurgaon"):
plain city names use the PVR API's own city coordinates and never touch
Nominatim.

Usage:
    python3 scripts/geocode.py "Sector 56, Gurugram"

Stdout is always ONE JSON object:
    success:  {"lat": <float>, "lng": <float>, "display_name": "<str>",
               "source": "nominatim", "cached": <bool>}      exit 0
    no hit:   {"error": "no_result", "query": "..."}          exit 3
    network:  {"error": "network", "detail": "..."}           exit 4

Politeness (Nominatim usage policy): an identifying User-Agent, at most one
request per second persisted across processes, one query per invocation, a
single attempt with no retries, and a persistent result cache so repeat
queries never touch the network (localities do not move; no TTL).
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pvr_client  # shared cache directory, atomic writes, state.json

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = ("pvr-inox-radar/0.1 (+https://github.com/karanb192/pvr-inox-radar; "
              "personal movie showtime lookup)")
NOMINATIM_MIN_INTERVAL = 1.0  # seconds; Nominatim's hard ceiling is 1 rps
GEOCODE_FILE = "geocode.json"
STATE_KEY = "nominatim_last"


def normalize_query(text):
    """Cache key: lowercased, whitespace-collapsed."""
    return " ".join(str(text or "").split()).lower()


def first_result(results):
    """Pure: the first usable Nominatim hit as {lat, lng, display_name}."""
    for row in results or []:
        if not isinstance(row, dict):
            continue
        try:
            return {
                "lat": float(row.get("lat")),
                "lng": float(row.get("lon")),
                "display_name": str(row.get("display_name") or ""),
            }
        except (TypeError, ValueError):
            continue
    return None


def _honor_rate_limit():
    """Sleep so that calls from ANY process stay at least 1 second apart."""
    state = pvr_client.load_cache(pvr_client.STATE_FILE)
    last = float(state.get(STATE_KEY) or 0.0)
    wait = NOMINATIM_MIN_INTERVAL - (time.time() - last)
    if wait > 0:
        time.sleep(wait)


def _record_call():
    state = pvr_client.load_cache(pvr_client.STATE_FILE)
    state[STATE_KEY] = time.time()
    pvr_client.save_cache(pvr_client.STATE_FILE, state)


def geocode(text):
    """Resolve one free-text locality. Returns (result, error); exactly one
    is set. Cache hits skip the network entirely."""
    key = normalize_query(text)
    cache = pvr_client.load_cache(GEOCODE_FILE)
    hit = cache.get(key)
    if hit:
        return dict(hit, source="nominatim", cached=True), None

    params = urllib.parse.urlencode({
        "q": " ".join(str(text).split()),
        "format": "jsonv2",
        "limit": 3,
        "countrycodes": "in",
        "addressdetails": 0,
    })
    request = urllib.request.Request(
        "%s?%s" % (NOMINATIM_URL, params),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    _honor_rate_limit()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            results = json.load(response)
    except Exception as exc:
        _record_call()
        return None, {"error": "network", "detail": str(exc)}
    _record_call()

    found = first_result(results)
    if not found:
        return None, {"error": "no_result", "query": str(text)}

    cache = pvr_client.load_cache(GEOCODE_FILE)
    cache[key] = found
    pvr_client.save_cache(GEOCODE_FILE, cache)
    return dict(found, source="nominatim", cached=False), None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve one Indian locality to coordinates via Nominatim.")
    parser.add_argument("query", help="free text locality, e.g. 'Sector 56, Gurugram'")
    args = parser.parse_args(argv)

    if not args.query.strip():
        print(json.dumps({"error": "usage", "detail": "empty query"}))
        return 2

    result, error = geocode(args.query)
    if result is not None:
        print(json.dumps(result))
        return 0
    print(json.dumps(error))
    return 3 if error.get("error") == "no_result" else 4


if __name__ == "__main__":
    sys.exit(main())
