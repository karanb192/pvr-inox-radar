# Solver logic (SPEC R113 to R118) against fixtures and a stubbed client,
# plus the offline CLI invoked via subprocess. Zero network.

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402

import pvr_client  # noqa: E402
import radar  # noqa: E402


def fixture_shows():
    variants = pvr_client.parse_variants(
        context.load_fixture("nowshowing_gurugram.json")["output"])
    return pvr_client.extract_shows(
        context.load_fixture("csessions_470.json")["output"], variants)


def make_query(**overrides):
    query = {
        "city": "Gurugram", "movie": None, "format": None,
        "date": "2026-08-22", "time_from": None, "time_to": None,
        "time_from_min": None, "time_to_min": None, "party_size": 1,
        "max_km": 60.0, "limit": 40, "seat_detail": 0, "no_osrm": True,
        "fixtures": None,
        "origin": {"lat": 28.4595, "lng": 77.0266,
                   "label": "test origin", "source": "caller"},
    }
    query.update(overrides)
    return query


def make_venue(theatre_id, km=2.0, formats=None):
    return {"theatreId": theatre_id, "name": "Stub %s" % theatre_id,
            "lat": 28.46 + km / 100.0, "lng": 77.03, "distance_km": km,
            "distance_from": "caller", "showCount": 10,
            "formats": formats or []}


def make_show(session_id, theatre_id, stamp, show_time, film="DUNE",
              status_txt="Available"):
    return {
        "movieId": "m%s" % session_id, "sessionId": session_id,
        "theatreId": theatre_id, "showDate": "2026-08-22",
        "showTime": show_time, "showTimeStamp": stamp, "endTimeStamp": 0,
        "screenName": "AUDI 1", "screenType": "", "language": "English",
        "language_source": "variant", "language_disputed": False,
        "subtitle": False, "movieFormat": "", "soundFormat": "",
        "statusCode": "76BE43", "status_category": "available",
        "statusTxt": status_txt, "encrypted": "tok%s" % session_id,
        "deep_link": "https://www.pvrcinemas.com/seatlayout/tok%s" % session_id,
        "film": film, "filmId": "f1", "experienceKey": "",
    }


def fake_report(best_run, hall_best_run, party):
    return {
        "free": 20, "sold": 5, "held": 0, "zone_free": best_run,
        "zone_rows_used": ["C"], "best_run": best_run, "best_where": "C1",
        "meets_party_size": best_run >= party, "widened_to": [],
        "hall_free": 20, "hall_best_run": hall_best_run,
        "hall_meets_party_size": hall_best_run >= party, "premium": False,
        "price_tiers": [],
    }


class StubClient:
    """The five client methods radar.solve needs, fully scripted."""

    source = "stub"

    def __init__(self, venues, sessions_by_cid, seats_by_session=None):
        self.calls = 0
        self.venues = venues
        self.sessions_by_cid = sessions_by_cid
        self.seats_by_session = seats_by_session or {}
        self.seat_calls = []

    def city_coords(self, city):
        return ("28.4595", "77.0266")

    def cinemas(self, city, origin, label):
        return self.venues

    def variants(self, city, lat, lng, date):
        return {}

    def sessions(self, city, cid, date, lat, lng, variants):
        self.calls += 1
        outcome = self.sessions_by_cid[cid]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def seats(self, show, party_size):
        self.calls += 1
        self.seat_calls.append(show["sessionId"])
        outcome = self.seats_by_session.get(show["sessionId"])
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return {"outcome": "ok",
                    "report": fake_report(8, 8, party_size)}
        return outcome


class TestFiltering(unittest.TestCase):
    """R113 on the 59 captured shows of cid 470."""

    @classmethod
    def setUpClass(cls):
        cls.shows = fixture_shows()

    def test_movie_substring(self):
        kept = radar.filter_shows(self.shows, "spiderman", "", None, None)[0]
        self.assertEqual(len(kept), 9)
        for show in kept:
            self.assertIn("SPIDERMAN", show["film"])

    def test_movie_canonical_id(self):
        by_film_id = radar.filter_shows(self.shows, "35294", "", None, None)[0]
        self.assertEqual(len(by_film_id), 9)

    def test_format_normalized(self):
        imax = radar.filter_shows(self.shows, None, "IMAX", None, None)[0]
        self.assertEqual(len(imax), 5)
        fourdx = radar.filter_shows(
            self.shows, None, pvr_client.screen_type_norm("4dx"), None, None)[0]
        self.assertEqual(len(fourdx), 6)
        for show in fourdx:
            self.assertIn("4DX", show["screenType"] + show["movieFormat"])

    def test_time_window_boundaries_inclusive(self):
        # earliest shows start 09:00 AM (minute 540)
        self.assertEqual(
            len(radar.filter_shows(self.shows, None, "", 540, None)[0]), 59)
        self.assertEqual(
            len(radar.filter_shows(self.shows, None, "", 541, None)[0]), 57)
        # a window ending exactly at 09:00 AM keeps the two 9am shows
        self.assertEqual(
            len(radar.filter_shows(self.shows, None, "", None, 540)[0]), 2)

    def test_combined_filters(self):
        both = radar.filter_shows(self.shows, "spiderman", "IMAX",
                                  None, None)[0]
        self.assertEqual(both, [])


class TestUnparseableTimes(unittest.TestCase):
    """R114 (gap 11 regression): unparseable showTime survives BOTH window
    flags and is flagged, never silently dropped on one side."""

    def test_kept_and_flagged_under_both_flags(self):
        show = make_show(1, "470", 0, "gibberish")
        keep, unparsed = radar.in_time_window(show, 600, 1200)
        self.assertTrue(keep)
        self.assertTrue(unparsed)

    def test_flagged_through_filter_shows(self):
        shows = [make_show(1, "470", 0, "gibberish"),
                 make_show(2, "470", 0, "06:00 PM")]
        kept = radar.filter_shows(shows, None, "", 600, 700)[0]
        self.assertEqual([s["sessionId"] for s in kept], [1])
        self.assertTrue(kept[0]["time_unparsed"])

    def test_parseable_times_carry_false_flag(self):
        shows = [make_show(2, "470", 0, "10:30 AM")]
        kept = radar.filter_shows(shows, None, "", 600, 700)[0]
        self.assertEqual(len(kept), 1)
        self.assertFalse(kept[0]["time_unparsed"])


class TestSeatShortlist(unittest.TestCase):
    """R115: exactly min(seat_detail, matching shows) seat calls, against
    the top-ranked shows only; lapsed shows never consume the budget."""

    def run_solve(self, shows, seat_detail, party=2):
        client = StubClient(
            [make_venue("470")],
            {"470": {"outcome": "open", "shows": shows}})
        document = radar.solve(client, make_query(seat_detail=seat_detail,
                                                  party_size=party))
        return client, document

    def test_budget_caps_seat_calls(self):
        shows = [make_show(i, "470", i * 1000, "0%d:00 PM" % i)
                 for i in range(1, 6)]
        client, _ = self.run_solve(shows, seat_detail=3)
        self.assertEqual(client.seat_calls, [1, 2, 3])

    def test_fewer_shows_than_budget(self):
        shows = [make_show(i, "470", i * 1000, "0%d:00 PM" % i)
                 for i in range(1, 4)]
        client, _ = self.run_solve(shows, seat_detail=8)
        self.assertEqual(client.seat_calls, [1, 2, 3])

    def test_zero_budget_means_zero_seat_calls(self):
        shows = [make_show(1, "470", 1000, "01:00 PM")]
        client, document = self.run_solve(shows, seat_detail=0)
        self.assertEqual(client.seat_calls, [])
        self.assertIsNone(document["shows"][0]["seats"])

    def test_lapsed_shows_skip_without_consuming_budget(self):
        shows = [make_show(1, "470", 1000, "01:00 PM",
                           status_txt="Lapsed"),
                 make_show(2, "470", 2000, "02:00 PM"),
                 make_show(3, "470", 3000, "03:00 PM")]
        client, _ = self.run_solve(shows, seat_detail=2)
        self.assertEqual(client.seat_calls, [2, 3])


class TestBlockedMidSearch(unittest.TestCase):
    """R116 (gap 3 regression): partial results survive a mid-run block."""

    def test_partial_results_kept(self):
        shows = [make_show(1, "470", 1000, "01:00 PM"),
                 make_show(2, "470", 2000, "02:00 PM")]
        client = StubClient(
            [make_venue("470", km=1.0), make_venue("241", km=2.0),
             make_venue("999", km=3.0)],
            {"470": {"outcome": "open", "shows": shows},
             "241": pvr_client.Blocked("upstream said 403")})
        document = radar.solve(client, make_query())
        self.assertTrue(document["meta"]["partial"])
        self.assertEqual(document["meta"]["error"], "UPSTREAM_BLOCKED")
        self.assertEqual(len(document["shows"]), 2)
        queried = [q["theatreId"] for q in document["meta"]["cinemas_queried"]]
        self.assertEqual(queried, ["470"])
        # R56 through the block path: everything from the break point on is
        # reported as NOT searched, so coverage never overstates.
        skipped = {s["theatreId"]: s["reason"]
                   for s in document["meta"]["cinemas_skipped"]}
        self.assertIn("not reached", skipped["241"])
        self.assertIn("not reached", skipped["999"])

    def test_blocked_during_seat_fetch_keeps_shows(self):
        shows = [make_show(1, "470", 1000, "01:00 PM")]
        client = StubClient(
            [make_venue("470")],
            {"470": {"outcome": "open", "shows": shows}},
            {1: pvr_client.Blocked("upstream said 429")})
        document = radar.solve(client, make_query(seat_detail=2))
        self.assertTrue(document["meta"]["partial"])
        self.assertEqual(document["meta"]["error"], "UPSTREAM_BLOCKED")
        self.assertEqual(len(document["shows"]), 1)
        self.assertIsNone(document["shows"][0]["seats"])


class TestDateGuard(unittest.TestCase):
    """The reference project's per-show date guard: this API family can
    answer for a day you did not ask about, and such shows are dropped and
    counted, never mixed into the wrong day's map."""

    def test_wrong_day_shows_dropped_and_counted(self):
        shows = [make_show(1, "470", 1000, "01:00 PM"),
                 dict(make_show(2, "470", 2000, "02:00 PM"),
                      showDate="2026-08-23")]
        kept, mismatched = radar.filter_shows(shows, None, "", None, None,
                                              date="2026-08-22")
        self.assertEqual([s["sessionId"] for s in kept], [1])
        self.assertEqual(mismatched, 1)

    def test_missing_show_date_passes_through(self):
        shows = [dict(make_show(1, "470", 1000, "01:00 PM"), showDate=None)]
        kept, mismatched = radar.filter_shows(shows, None, "", None, None,
                                              date="2026-08-22")
        self.assertEqual(len(kept), 1)
        self.assertEqual(mismatched, 0)

    def test_solve_surfaces_date_mismatch_in_meta(self):
        shows = [make_show(1, "470", 1000, "01:00 PM"),
                 dict(make_show(2, "470", 2000, "02:00 PM"),
                      showDate="2026-08-23")]
        client = StubClient([make_venue("470")],
                            {"470": {"outcome": "open", "shows": shows}})
        document = radar.solve(client, make_query())
        self.assertEqual(document["meta"]["date_mismatch"], 1)
        self.assertEqual([s["sessionId"] for s in document["shows"]], [1])


class TestDriveTimes(unittest.TestCase):
    """R117: the pinned heuristic, OSRM success, OSRM silent fallback."""

    def test_heuristic_pinned(self):
        # ceil(5 + 2.4 * (5.0 * 1.3)) = ceil(20.6) = 21
        self.assertEqual(radar.drive_minutes_heuristic(5.0), 21)
        self.assertEqual(radar.drive_minutes_heuristic(0.0), 5)
        self.assertIsNone(radar.drive_minutes_heuristic(None))

    @staticmethod
    def osrm_ok(request, timeout=None):
        class Resp:
            def read(self, *a):
                return json.dumps(
                    {"durations": [[0, 600.0]]}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp()

    def solve_with_osrm(self, urlopen):
        shows = [make_show(1, "470", 1000, "01:00 PM")]
        client = StubClient(
            [make_venue("470", km=5.0)],
            {"470": {"outcome": "open", "shows": shows}})
        return radar.solve(client, make_query(no_osrm=False),
                           use_osrm=True, osrm_urlopen=urlopen)

    def test_osrm_success_relabels_source(self):
        document = self.solve_with_osrm(self.osrm_ok)
        venue = document["venues"][0]
        self.assertEqual(venue["drive_min_source"], "osrm")
        self.assertEqual(venue["drive_min_est"], 10)  # 600s
        self.assertIsNone(document["meta"]["error"])

    def test_osrm_failure_falls_back_silently(self):
        def osrm_down(request, timeout=None):
            raise OSError("timed out")
        document = self.solve_with_osrm(osrm_down)
        venue = document["venues"][0]
        self.assertEqual(venue["drive_min_source"], "heuristic")
        self.assertEqual(venue["drive_min_est"], 21)
        self.assertIsNone(document["meta"]["error"])

    def test_osrm_garbage_falls_back_silently(self):
        def osrm_garbage(request, timeout=None):
            class Resp:
                def read(self, *a):
                    return b'{"durations": [[0]]}'  # wrong length

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return Resp()
        document = self.solve_with_osrm(osrm_garbage)
        self.assertEqual(document["venues"][0]["drive_min_source"],
                         "heuristic")


class TestCoverageAndParty(unittest.TestCase):
    """R118 (gaps 6 and 7): coverage from queries, skip reasons, and the
    party filter's verified/unverified split."""

    def test_plan_venues_reasons(self):
        venues = ([make_venue(str(i), km=float(i)) for i in range(1, 15)]
                  + [make_venue("far", km=70.0),
                     dict(make_venue("nocoords"), distance_km=None)])
        selected, skipped = radar.plan_venues(venues, 60.0, "")
        self.assertEqual(len(selected), 12)
        reasons = {s["theatreId"]: s["reason"] for s in skipped}
        self.assertIn("outside radius", reasons["far"])
        self.assertIn("no published coordinates", reasons["nocoords"])
        self.assertIn("call budget", reasons["13"])
        self.assertIn("call budget", reasons["14"])

    def test_format_capability_first_but_never_dropped(self):
        venues = [make_venue("plain", km=1.0),
                  make_venue("imaxhouse", km=5.0, formats=["IMAX"])]
        selected, skipped = radar.plan_venues(venues, 60.0, "IMAX")
        self.assertEqual([v["theatreId"] for v in selected],
                         ["imaxhouse", "plain"])
        self.assertEqual(skipped, [])

    def test_zero_match_venue_still_counted_as_queried(self):
        client = StubClient(
            [make_venue("470", km=1.0), make_venue("241", km=2.0)],
            {"470": {"outcome": "open",
                     "shows": [make_show(1, "470", 1000, "01:00 PM",
                                         film="DUNE")]},
             "241": {"outcome": "open",
                     "shows": [make_show(2, "241", 2000, "02:00 PM",
                                         film="OTHER")]}})
        document = radar.solve(client, make_query(movie="dune"))
        queried = {q["theatreId"]: q for q in
                   document["meta"]["cinemas_queried"]}
        self.assertEqual(queried["241"]["shows_matched"], 0)
        self.assertEqual(queried["241"]["outcome"], "open")
        self.assertEqual(queried["470"]["shows_matched"], 1)

    def test_party_filter_split(self):
        """A verified too-short show is excluded and counted; unverified
        shows pass with seats null, never promoted to bookable."""
        shows = [make_show(1, "470", 1000, "01:00 PM"),
                 make_show(2, "470", 2000, "02:00 PM"),
                 make_show(3, "470", 3000, "03:00 PM")]
        client = StubClient(
            [make_venue("470")],
            {"470": {"outcome": "open", "shows": shows}},
            {1: {"outcome": "ok", "report": fake_report(1, 2, 4)}})
        document = radar.solve(client, make_query(party_size=4,
                                                  seat_detail=1))
        self.assertEqual(document["meta"]["excluded_by_party"], 1)
        kept = {s["sessionId"]: s for s in document["shows"]}
        self.assertEqual(set(kept), {2, 3})
        for show in kept.values():
            self.assertIsNone(show["seats"])

    def test_verified_never_ranks_below_unverified_at_equal_relevance(self):
        """R64: same stamp, same venue; the verified show outranks the
        unverified one even though it arrived later in stable order."""
        shows = [make_show(1, "470", 1000, "01:00 PM"),
                 make_show(2, "470", 1000, "01:00 PM")]
        client = StubClient(
            [make_venue("470")],
            {"470": {"outcome": "open", "shows": shows}},
            {1: {"outcome": "error", "error": "seatmap unavailable"},
             2: {"outcome": "ok", "report": fake_report(6, 6, 2)}})
        document = radar.solve(client, make_query(party_size=2,
                                                  seat_detail=2))
        self.assertEqual(document["shows"][0]["sessionId"], 2)
        self.assertEqual(document["shows"][0]["rank"], 1)
        self.assertTrue(document["shows"][0]["seats"]["verified"])
        self.assertEqual(document["shows"][1]["seat_error"],
                         "seatmap unavailable")

    def test_all_venues_closed_reports_window_not_soldout(self):
        """R58: a closed date is an answer, phrased as not-on-sale-yet."""
        client = StubClient(
            [make_venue("470", km=1.0), make_venue("241", km=2.0)],
            {"470": {"outcome": "closed"}, "241": {"outcome": "closed"}})
        document = radar.solve(client, make_query(date="2026-09-15"))
        self.assertIs(document["meta"]["booking_open"], False)
        self.assertIn("not sold out", document["meta"]["message"])
        self.assertEqual(document["shows"], [])
        for venue in document["venues"]:
            self.assertIs(venue["booking_open"], False)
            self.assertEqual(venue["best_status"], "closed")


class TestCityResolution(unittest.TestCase):
    """An unserviced city and a coords-less city are different usage errors
    with different advice; neither reads as an unexpected crash."""

    def build(self, city):
        args = radar.build_parser().parse_args(
            ["--city", city, "--fixtures", context.FIXTURES_DIR])
        client = radar.FixtureClient(context.FIXTURES_DIR)
        return radar.build_query(args, client)

    def test_unserviced_city_says_so(self):
        query, failure = self.build("Atlantis")
        self.assertIsNone(query)
        code, payload = failure
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "city_not_serviced")
        self.assertIn("Atlantis", payload["detail"])

    def test_known_city_without_coords_asks_for_latlng(self):
        # Gangtok is served but publishes no coordinates in the capture.
        query, failure = self.build("Gangtok")
        self.assertIsNone(query)
        code, payload = failure
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "city_coords_unavailable")
        self.assertIn("--lat", payload["detail"])


class TestCliOffline(unittest.TestCase):
    """The radar CLI end to end in --fixtures mode via subprocess: one JSON
    document on stdout, logs on stderr, deterministic fixture results."""

    @classmethod
    def setUpClass(cls):
        cls.cache = tempfile.mkdtemp(prefix="pvr-test-cli-")
        env = dict(os.environ, PVR_RADAR_CACHE_DIR=cls.cache)
        cls.proc = subprocess.run(
            [sys.executable,
             os.path.join(context.SCRIPTS_DIR, "radar.py"),
             "--city", "Gurugram", "--lat", "28.4276", "--lng", "77.106",
             "--date", "2026-08-22", "--time-from", "16:00",
             "--time-to", "20:00", "--party-size", "4",
             "--seat-detail", "4", "--max-km", "12",
             "--fixtures", context.FIXTURES_DIR, "--no-osrm"],
            capture_output=True, text=True, env=env, timeout=60)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.cache, ignore_errors=True)

    def document(self):
        return json.loads(self.proc.stdout)

    def test_exit_zero_and_parseable_stdout(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        document = self.document()
        self.assertEqual(document["meta"]["source"], "fixtures")

    def test_fixture_run_is_deterministic(self):
        document = self.document()
        self.assertEqual(len(document["venues"]), 9)
        self.assertEqual(len(document["shows"]), 22)
        self.assertEqual(document["meta"]["calls_made"], 5)

    def test_seats_verified_for_the_covered_session(self):
        document = self.document()
        verified = [s for s in document["shows"] if s["seats"]]
        self.assertTrue(verified)
        self.assertEqual(verified[0]["sessionId"], 53697)
        self.assertTrue(verified[0]["seats"]["meets_party_size"])

    def test_every_show_deep_links_into_pvr(self):
        for show in self.document()["shows"]:
            self.assertTrue(str(show["deep_link"]).startswith(
                "https://www.pvrcinemas.com/seatlayout/"))

    def test_usage_error_exits_2(self):
        proc = subprocess.run(
            [sys.executable,
             os.path.join(context.SCRIPTS_DIR, "radar.py"),
             "--city", "Gurugram", "--date", "not-a-date",
             "--fixtures", context.FIXTURES_DIR],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"], "usage")


if __name__ == "__main__":
    unittest.main()
