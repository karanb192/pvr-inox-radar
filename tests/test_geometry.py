# Seat parsing, zones, adjacency, counting, haversine (SPEC R89 to R102).
# Pure functions against the real captured fixtures plus small synthetic
# halls; zero network, zero clock.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402  (inserts the scripts dir on sys.path)

import pvr_client  # noqa: E402


def seats_row(name, statuses, start=1, price_code=""):
    """Raw seatlayout row entry. statuses: seat status ints, None = aisle."""
    cells, number = [], start
    for status in statuses:
        if status is None:
            cells.append({"sn": "", "s": 0})
        else:
            cells.append({"sn": "%s%d" % (name, number), "s": status,
                          "displaynumber": str(number), "c": price_code})
            number += 1
    return {"t": "seats", "n": name, "s": cells}


def area_row(label, net="0.00"):
    return {"t": "area", "n": label, "nn": net}


class TestFixtureCounts(unittest.TestCase):
    """R89, R90: the two captured seat maps parse to their known counts."""

    def test_playhouse_hall_counts(self):
        output = context.load_fixture("seatlayout_53821.json")["output"]
        report = pvr_client.seat_report_from_payload(output, party_size=1)
        self.assertEqual((report["total"], report["free"], report["sold"]),
                         (58, 51, 7))
        self.assertEqual(report["held"], 0)
        self.assertEqual(report["free"] + report["sold"] + report["held"],
                         report["total"])

    def test_audi06_hall_counts(self):
        output = context.load_fixture("seatlayout_53697.json")["output"]
        report = pvr_client.seat_report_from_payload(output, party_size=1)
        self.assertEqual((report["total"], report["free"], report["sold"]),
                         (152, 122, 30))
        self.assertEqual(report["held"], 0)


class TestTierHeaders(unittest.TestCase):
    """R91: area headers apply to following seat rows until the next header,
    and the same tier can repeat (AUDI 06's split-tier layout)."""

    def test_audi06_split_tiers(self):
        output = context.load_fixture("seatlayout_53697.json")["output"]
        rows = pvr_client.parse_seat_rows(output["rows"])
        by_row = {row["name"]: row["tier"] for row in rows}
        self.assertEqual(by_row["A"], "CLASSIC (370.00)")
        self.assertEqual(by_row["D"], "CLASSIC (370.00)")
        self.assertEqual(by_row["E"], "XTRA LEGROOM (580.00)")
        # CLASSIC repeats after the XTRA LEGROOM header
        self.assertEqual(by_row["F"], "CLASSIC (370.00)")
        self.assertEqual(by_row["G"], "CLASSIC (370.00)")
        for name in ("H", "J", "K", "L", "M"):
            self.assertEqual(by_row[name], "SUPERIOR (480.00)")


class TestAdjacency(unittest.TestCase):
    def test_aisle_breaks_runs(self):
        """R92: a cell without sn is an aisle and splits one block in two."""
        row = pvr_client.parse_seat_rows(
            [seats_row("A", [1, 1, None, 1, 1, 1])])[0]
        runs = pvr_client.row_free_runs(row)
        self.assertEqual([len(r) for r in runs], [2, 3])

    def test_descending_display_order(self):
        """R93: runs are counted on seat positions, not array order; the
        span label reads low to high whichever way the payload numbers."""
        cells = [{"sn": "A%d" % n, "s": 1, "displaynumber": str(n)}
                 for n in (5, 4, 3, 2, 1)]
        row = pvr_client.parse_seat_rows([{"t": "seats", "n": "A",
                                           "s": cells}])[0]
        runs = pvr_client.row_free_runs(row)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0]), 5)
        self.assertEqual(pvr_client._span_label(runs[0]), "A1-A5")

    def test_st_field_is_not_availability(self):
        """R99: seat M1 in the capture has st == 2 yet s == 1; it is free."""
        output = context.load_fixture("seatlayout_53697.json")["output"]
        rows = pvr_client.parse_seat_rows(output["rows"])
        row_m = next(r for r in rows if r["name"] == "M")
        m1 = next(c for c in row_m["cells"] if c["sn"] == "M1")
        self.assertEqual(m1["st"], 2)
        self.assertEqual(m1["s"], 1)
        free_labels = [label for run in pvr_client.row_free_runs(row_m)
                       for label in run]
        self.assertIn("M1", free_labels)


class TestZoneDerivation(unittest.TestCase):
    def test_zone_is_toward_the_back(self):
        """R94: rows arrive front-first and letters usually descend toward
        the back; the zone must sit 60 to 85 percent of the way toward the
        BACK by list position. A letter-ordered (inverted) reading picks a
        different row set and fails this test."""
        names = list("NMLKJHGFEDCBA")  # 13 rows, front (N) to back (A)
        raw = [seats_row(name, [1] * 8) for name in names]
        rows = pvr_client.parse_seat_rows(raw)
        zone = pvr_client.derive_zone(rows)
        chosen = {name for name, seats in zone.items() if seats}
        self.assertEqual(chosen, {"F", "E", "D", "C", "B"})
        self.assertNotIn("N", chosen)  # front row never in the zone

    def test_fixture_zone_rows(self):
        """The 12-row AUDI 06 zone lands on the SUPERIOR back rows."""
        output = context.load_fixture("seatlayout_53697.json")["output"]
        report = pvr_client.seat_report_from_payload(output, party_size=1)
        self.assertEqual(report["zone_rows_used"], ["H", "J", "K", "L"])

    def test_explicit_override_never_widens(self):
        """R102: an explicit zone is an instruction; even a hopeless one is
        never auto-widened."""
        raw = [seats_row("D", [2] * 6), seats_row("C", [1] * 6),
               seats_row("B", [1] * 6), seats_row("A", [1] * 6)]
        report = pvr_client.seat_report_from_payload(
            {"rows": raw}, party_size=4, zone_rows=["D"])
        self.assertEqual(report["widened_to"], [])
        self.assertFalse(report["meets_party_size"])
        self.assertEqual(report["zone_rows_used"], ["D"])


class TestBestRun(unittest.TestCase):
    """R95: best_run and meets_party_size for party sizes 1, 2, 4, 7."""

    def test_playhouse_parties(self):
        output = context.load_fixture("seatlayout_53821.json")["output"]
        for party in (1, 2, 4, 7):
            report = pvr_client.seat_report_from_payload(
                output, party_size=party,
                screen_type=output.get("experience") or "")
            self.assertEqual(report["best_run"], 9, "party %d" % party)
            self.assertTrue(report["meets_party_size"])

    def test_audi06_parties(self):
        output = context.load_fixture("seatlayout_53697.json")["output"]
        for party in (1, 2, 4, 7):
            report = pvr_client.seat_report_from_payload(output,
                                                         party_size=party)
            self.assertEqual(report["best_run"], 13, "party %d" % party)
            self.assertTrue(report["meets_party_size"])


class TestWithheldSeats(unittest.TestCase):
    def test_withheld_counts_as_held(self):
        """R96: any status other than 1 (free) or 2 (sold) is withheld."""
        raw = [seats_row("A", [1, 3, 1, 2, 3])]
        report = pvr_client.seat_report_from_payload({"rows": raw},
                                                     party_size=1)
        self.assertEqual(report["total"], 5)
        self.assertEqual(report["free"], 2)
        self.assertEqual(report["sold"], 1)
        self.assertEqual(report["held"], 2)

    def test_withheld_caveat_credits_the_source(self):
        report = pvr_client.seat_report_from_payload(
            {"rows": [seats_row("A", [1] * 4)]}, party_size=1)
        self.assertTrue(any("notprashanth" in c for c in report["caveats"]))


def full_zone_sold_hall():
    """10 rows front-first; the derived zone rows (positions 6 to 8) are
    entirely sold, every other row entirely free."""
    names = list("KJHGFEDCBA")
    raw = []
    for index, name in enumerate(names):
        status = 2 if index in (6, 7, 8) else 1
        raw.append(seats_row(name, [status] * 8))
    return raw


class TestWidening(unittest.TestCase):
    def test_auto_widen_non_premium(self):
        """R97: a full derived zone widens into up to 3 whole alternative
        rows from the same payload and then seats the party."""
        report = pvr_client.seat_report_from_payload(
            {"rows": full_zone_sold_hall()}, party_size=4, screen_type="")
        self.assertTrue(report["widened_to"])
        self.assertLessEqual(len(report["widened_to"]), 3)
        self.assertTrue(report["meets_party_size"])
        # widened rows are new rows, never the (sold) original zone rows,
        # which for this 10-row hall are list positions 6 to 8: D, C, B
        self.assertTrue(set(report["widened_to"]).isdisjoint({"D", "C", "B"}))

    def test_premium_reports_separately_and_never_widens(self):
        """R98: a premium hall keeps the zone verdict and the whole-hall
        verdict separate instead of silently reframing a sold-out block."""
        report = pvr_client.seat_report_from_payload(
            {"rows": full_zone_sold_hall()}, party_size=4,
            screen_type="IMAX")
        self.assertTrue(report["premium"])
        self.assertEqual(report["widened_to"], [])
        self.assertFalse(report["meets_party_size"])
        self.assertTrue(report["hall_meets_party_size"])
        self.assertGreaterEqual(report["hall_best_run"], 4)


class TestPriceJoin(unittest.TestCase):
    def test_classic_seat_price(self):
        """R100: seat c code joins priceList; CLASSIC is 400.00 gross."""
        output = context.load_fixture("seatlayout_53821.json")["output"]
        rows = pvr_client.parse_seat_rows(output["rows"])
        row_a = next(r for r in rows if r["name"] == "A")
        cell = next(c for c in row_a["cells"]
                    if c["price_code"] == "CC-CLASSIC")
        self.assertEqual(pvr_client.seat_price(cell, output["priceList"]),
                         "400.00")

    def test_price_tiers_summary(self):
        output = context.load_fixture("seatlayout_53821.json")["output"]
        tiers = pvr_client.price_tiers(output["priceList"])
        by_code = {t["code"]: t["price"] for t in tiers}
        self.assertEqual(by_code["CC-CLASSIC"], "400.00")
        self.assertEqual(by_code["PE-PRIME"], "510.00")


class TestHaversine(unittest.TestCase):
    def test_known_pair(self):
        """R101: Gurugram city centre to cid 470; the expected value is the
        spherical law of cosines computed independently (8.5393 km)."""
        km = pvr_client.haversine_km(28.459469, 77.026207,
                                     28.50407229, 77.09733009)
        self.assertAlmostEqual(km, 8.5393, delta=0.06)

    def test_zero_and_symmetry(self):
        self.assertEqual(pvr_client.haversine_km(28.5, 77.1, 28.5, 77.1), 0.0)
        self.assertEqual(
            pvr_client.haversine_km(28.4595, 77.0266, 12.9716, 77.5946),
            pvr_client.haversine_km(12.9716, 77.5946, 28.4595, 77.0266))

    def test_bad_input_is_none(self):
        self.assertIsNone(pvr_client.haversine_km("x", 77.0, 28.5, 77.1))
        self.assertIsNone(pvr_client.haversine_km(None, None, None, None))


if __name__ == "__main__":
    unittest.main()
