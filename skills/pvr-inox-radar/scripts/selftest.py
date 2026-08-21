#!/usr/bin/env python3
# Adapted from notprashanth/pvr-inox-mcp (MIT), Copyright Prashanth Krishnan.
# https://github.com/notprashanth/pvr-inox-mcp
"""Offline self-check for an installed copy of pvr-inox-radar.

Run by install.sh after every copy, and safe to run by hand:

    python3 scripts/selftest.py

Zero network, stdlib only: exercises the adapted seat-counting engine and
the map renderer against the small fixtures in assets/, asserts a handful
of invariants, prints PASS/FAIL lines, exits 0 on success and 1 on any
failure. Installed copies ship without the repo test suite; this is the
post-install verification (SPEC R10, R79).
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ASSETS = os.path.join(HERE, os.pardir, "assets")

import pvr_client  # noqa: E402
import render_map  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("PASS %s" % name)
    else:
        print("FAIL %s%s" % (name, (" (%s)" % detail) if detail else ""))
        FAILURES.append(name)


def load_asset(name):
    with open(os.path.join(ASSETS, name)) as fh:
        return json.load(fh)


def main():
    # --- seat engine over the trimmed real seat map -----------------------
    layout = load_asset("sample_seatlayout.json")
    output = layout["output"]
    rows = pvr_client.parse_seat_rows(output.get("rows"))
    check("seat rows parsed", len(rows) == 9, "got %d rows" % len(rows))

    report = pvr_client.seat_report_from_payload(
        output, party_size=4, screen_type=output.get("experience") or "")
    check("seat counts (58 total, 51 free, 7 sold)",
          (report["total"], report["free"], report["sold"]) == (58, 51, 7),
          "got %s/%s/%s" % (report["total"], report["free"], report["sold"]))
    check("totals add up",
          report["free"] + report["sold"] + report["held"] == report["total"])
    check("zone derived from hall geometry", bool(report["zone_rows_used"]))
    check("party of 4 seated together", report["meets_party_size"]
          and report["best_run"] >= 4)
    check("price tiers joined", any(t["price"] for t in report["price_tiers"]))
    check("withheld-rows caveat carried (credited)",
          any("notprashanth" in c for c in report["caveats"]))

    # --- renderer over the sample radar document --------------------------
    radar = load_asset("sample_radar.json")
    page = render_map.render(radar)
    check("map html non-empty", len(page) > 5000)
    check("deep links present",
          "https://www.pvrcinemas.com/seatlayout/" in page)
    check("credit in footer", "notprashanth/pvr-inox-mcp" in page)
    check("fallback table present", "<table>" in page)
    check("no em or en dashes in output",
          "\u2014" not in page and "\u2013" not in page)
    check("venue pins in embedded data",
          all(str(v["theatreId"]) in page for v in radar["venues"]))

    if FAILURES:
        print("selftest: %d check(s) failed" % len(FAILURES))
        return 1
    print("selftest: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
