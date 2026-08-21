# Tier (recliner) filtering and agent-supplied ratings, against REAL
# captured fixtures: seatlayout_recliner.json (MGF Screen 5, mixed hall
# with CLASSIC / PRIME / RECLINER ROWS) and seatlayout_53697.json
# (CLASSIC / XTRA LEGROOM / SUPERIOR). Zero network.

import json
import os
import subprocess
import sys
import unittest

import context
import pvr_client
import render_map


def load_fixture(name):
    with open(os.path.join(context.FIXTURES_DIR, name)) as fh:
        return json.load(fh)


class TestTierHelpers(unittest.TestCase):
    def test_tier_word_normalizes_plural_and_case(self):
        self.assertEqual(pvr_client.tier_word("Recliners"), "RECLINER")
        self.assertEqual(pvr_client.tier_word("recliner"), "RECLINER")
        self.assertEqual(pvr_client.tier_word("  loungers "), "LOUNGER")

    def test_split_tier_label(self):
        self.assertEqual(pvr_client.split_tier_label("RECLINER ROWS (400.00)"),
                         ("RECLINER ROWS", "400.00"))
        self.assertEqual(pvr_client.split_tier_label("CLASSIC"),
                         ("CLASSIC", ""))


class TestTierReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recliner = load_fixture("seatlayout_recliner.json")["output"]
        cls.mixed = load_fixture("seatlayout_53697.json")["output"]

    def test_recliner_rows_matched_and_priced(self):
        report = pvr_client.seat_report_from_payload(
            self.recliner, party_size=2, tier="recliners")
        self.assertEqual(report["tier"]["mode"], "rows")
        self.assertIn("RECLINER ROWS", report["tier"]["matched"])
        self.assertEqual(report["tier"]["gross"], "400.00")

    def test_counting_restricted_to_tier_rows(self):
        whole = pvr_client.seat_report_from_payload(self.recliner, party_size=2)
        tiered = pvr_client.seat_report_from_payload(
            self.recliner, party_size=2, tier="recliner")
        self.assertLess(tiered["total"], whole["total"])
        self.assertLessEqual(tiered["free"], whole["free"])
        for row in tiered["zone_rows_used"]:
            self.assertIn(row, [r["name"] for r in pvr_client.parse_seat_rows(
                self.recliner.get("rows"))
                if "RECLINER" in r["tier"].upper()])

    def test_absent_tier_reports_available_names(self):
        report = pvr_client.seat_report_from_payload(
            self.mixed, party_size=2, tier="recliner")
        self.assertEqual(report["tier"]["mode"], "absent")
        names = " ".join(report["tier"]["available"])
        self.assertIn("CLASSIC", names)
        self.assertIn("XTRA LEGROOM", names)

    def test_legroom_matches_xtra_legroom_rows_only(self):
        report = pvr_client.seat_report_from_payload(
            self.mixed, party_size=1, tier="legroom")
        self.assertEqual(report["tier"]["mode"], "rows")
        self.assertEqual(report["tier"]["matched"], ["XTRA LEGROOM"])
        self.assertEqual(report["zone_rows_used"], ["E"])

    def test_single_tier_premium_hall_counts_whole(self):
        rows = [row for row in self.recliner["rows"] if row.get("t") == "seats"]
        payload = {"rows": rows, "priceList": {},
                   "cinemaName": "x", "showDateTime": ""}
        report = pvr_client.seat_report_from_payload(
            payload, party_size=2, tier="recliner",
            screen_type="DIRECTOR'S CUT")
        self.assertEqual(report["tier"]["mode"], "whole_hall")

    def test_single_tier_ordinary_hall_is_absent(self):
        rows = [row for row in self.recliner["rows"] if row.get("t") == "seats"]
        payload = {"rows": rows, "priceList": {},
                   "cinemaName": "x", "showDateTime": ""}
        report = pvr_client.seat_report_from_payload(
            payload, party_size=2, tier="recliner", screen_type="PREMIUM")
        self.assertEqual(report["tier"]["mode"], "absent")

    def test_tiers_available_listed_without_a_tier_ask(self):
        report = pvr_client.seat_report_from_payload(self.recliner, party_size=1)
        names = [t["name"] for t in report["tiers_available"]]
        self.assertIn("RECLINER ROWS", names)
        self.assertIsNone(report["tier"])


class TestTierRendering(unittest.TestCase):
    def test_tier_prefix_rows(self):
        seats = {"tier": {"mode": "rows", "matched": ["RECLINER ROWS"],
                          "gross": "400.00"}}
        self.assertEqual(render_map.tier_prefix(seats),
                         "recliner rows (Rs 400.00): ")

    def test_tier_prefix_whole_hall_and_absent(self):
        self.assertEqual(render_map.tier_prefix(
            {"tier": {"mode": "whole_hall", "matched": ["DIRECTOR'S CUT"]}}),
            "director's cut (whole hall): ")
        self.assertEqual(render_map.tier_prefix(
            {"tier": {"mode": "absent", "available": []}}), "")
        self.assertEqual(render_map.tier_prefix(None), "")

    def test_query_words_name_the_tier(self):
        words = render_map.query_words({"movie": "Dune", "tier": "Recliner"})
        self.assertIn("recliner seats", words)


class TestRatingsColumn(unittest.TestCase):
    RADAR = {
        "query": {"movie": "", "party_size": 1, "origin": {}},
        "venues": [{"theatreId": "1", "name": "Venue 1", "lat": 28.4,
                    "lng": 77.0, "distance_km": 2.0}],
        "shows": [{"theatreId": "1", "film": "AWARAPAN 2 (HINDI)",
                   "showTime": "06:05 PM", "sessionId": "9",
                   "deep_link": "https://www.pvrcinemas.com/seatlayout/x"}],
        "meta": {},
    }

    def test_no_ratings_no_column(self):
        page = render_map.render(self.RADAR)
        self.assertNotIn("<th>Rating</th>", page)

    def test_ratings_column_matches_case_insensitive(self):
        page = render_map.render(self.RADAR, ratings={
            "awarapan 2 (hindi)": {"rating": "7.2", "source": "IMDb"}})
        self.assertIn("<th>Rating</th>", page)
        self.assertIn("7.2 (IMDb)", page)

    def test_plain_value_rating(self):
        self.assertEqual(render_map.rating_words(8.1), "8.1")
        self.assertEqual(render_map.rating_words(
            {"rating": "7.9", "source": "IMDb"}), "7.9 (IMDb)")


class TestTierCli(unittest.TestCase):
    """Offline CLI: fixture seat maps carry no RECLINER tier, so verified
    shows are excluded with the count surfaced, and unverified shows stay."""

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.run(
            [sys.executable,
             os.path.join(context.SCRIPTS_DIR, "radar.py"),
             "--city", "Gurugram", "--date", "2026-08-22",
             "--time-from", "16:00", "--time-to", "18:30",
             "--tier", "recliner", "--party-size", "2", "--seat-detail", "8",
             "--fixtures", context.FIXTURES_DIR, "--no-osrm"],
            capture_output=True, text=True)
        cls.doc = json.loads(cls.proc.stdout or "{}")

    def test_exit_zero_and_excluded_count(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertGreaterEqual(self.doc["meta"]["excluded_by_tier"], 1)

    def test_no_verified_show_survives_without_the_tier(self):
        for show in self.doc["shows"]:
            seats = show.get("seats")
            if seats:
                self.assertNotEqual(
                    (seats.get("tier") or {}).get("mode"), "absent")

    def test_query_echo_carries_tier(self):
        self.assertEqual(self.doc["query"]["tier"], "recliner")




class TestPriceQueries(unittest.TestCase):
    RECLINER_SEATS = {"tier": {"mode": "rows", "matched": ["RECLINER ROWS"],
                               "gross": "400.00"},
                      "price_tiers": [{"price": "250.00"}, {"price": "400.00"}]}
    PLAIN_SEATS = {"tier": None,
                   "price_tiers": [{"price": "510.00"}, {"price": "199.00"}]}

    def test_price_from_prefers_matched_tier(self):
        import radar
        self.assertEqual(radar.price_from(self.RECLINER_SEATS), 400.0)

    def test_price_from_cheapest_hall_tier_otherwise(self):
        import radar
        self.assertEqual(radar.price_from(self.PLAIN_SEATS), 199.0)
        self.assertIsNone(radar.price_from(None))
        self.assertIsNone(radar.price_from({"price_tiers": []}))

    def test_price_words(self):
        self.assertEqual(render_map.price_words(self.RECLINER_SEATS),
                         "from Rs 400")
        self.assertEqual(render_map.price_words(self.PLAIN_SEATS),
                         "from Rs 199")
        self.assertEqual(render_map.price_words(None), "")

    def test_price_column_conditional(self):
        radar_doc = {"query": {"party_size": 1, "origin": {}},
                     "venues": [{"theatreId": "1", "name": "V", "lat": 1,
                                 "lng": 1}],
                     "shows": [{"theatreId": "1", "film": "X",
                                "showTime": "06:05 PM", "sessionId": "9",
                                "seats": dict(self.PLAIN_SEATS, free=5,
                                              sold=1, held=0, zone_free=5,
                                              zone_rows_used=[], best_run=5,
                                              best_where="", widened_to=[],
                                              hall_free=5, hall_best_run=5,
                                              meets_party_size=True,
                                              hall_meets_party_size=True,
                                              premium=False, verified=True)}],
                     "meta": {}}
        page = render_map.render(radar_doc)
        self.assertIn("<th>Price</th>", page)
        self.assertIn("from Rs 199", page)
        radar_doc["shows"][0].pop("seats")
        self.assertNotIn("<th>Price</th>", render_map.render(radar_doc))


class TestPriceCli(unittest.TestCase):
    """Offline: --max-price 1 excludes every verified show (real fixture
    prices start above Rs 1) and surfaces the count; --sort cheapest is
    accepted and echoes."""

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.run(
            [sys.executable,
             os.path.join(context.SCRIPTS_DIR, "radar.py"),
             "--city", "Gurugram", "--date", "2026-08-22",
             "--time-from", "16:00", "--time-to", "18:30",
             "--max-price", "1", "--sort", "cheapest", "--seat-detail", "8",
             "--fixtures", context.FIXTURES_DIR, "--no-osrm"],
            capture_output=True, text=True)
        cls.doc = json.loads(cls.proc.stdout or "{}")

    def test_priced_shows_excluded_and_counted(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertGreaterEqual(self.doc["meta"]["excluded_by_price"], 1)
        for show in self.doc["shows"]:
            self.assertIsNone(__import__("radar").price_from(show.get("seats")))

    def test_query_echo_carries_price_and_sort(self):
        self.assertEqual(self.doc["query"]["max_price"], 1)
        self.assertEqual(self.doc["query"]["sort"], "cheapest")


if __name__ == "__main__":
    unittest.main()
