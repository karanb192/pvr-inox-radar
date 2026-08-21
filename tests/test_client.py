# Mocked-urllib client behavior (SPEC R103 to R112): headers, pacing,
# Blocked backoff persistence, the csessions three-way outcome, the booking
# horizon, city parsing, variants, and the banned endpoint. No test in this
# file touches the network: urllib.request.urlopen is patched at the seam.

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402

import pvr_client  # noqa: E402
import radar  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self, *args):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://api3.pvrcinemas.com/x", code,
                                  "boom", {}, io.BytesIO(b""))


OK_ENVELOPE = {"status": 302, "output": {"anything": True}}


class ClientCase(unittest.TestCase):
    """Isolated cache dir per test; module pacing state reset; sleeps muted
    except where a test patches its own recorder."""

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="pvr-test-cache-")
        self._old_cache = os.environ.get("PVR_RADAR_CACHE_DIR")
        os.environ["PVR_RADAR_CACHE_DIR"] = self.cache
        pvr_client._last_call_mono[0] = 0.0
        patcher = mock.patch("time.sleep", mock.Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("PVR_RADAR_CACHE_DIR", None)
        else:
            os.environ["PVR_RADAR_CACHE_DIR"] = self._old_cache
        shutil.rmtree(self.cache, ignore_errors=True)

    def read_state(self):
        with open(os.path.join(self.cache, "state.json")) as fh:
            return json.load(fh)


class TestHeaders(ClientCase):
    """R103: every request carries the full header set, including the
    deliberately blank Bearer token, and the right city header."""

    def captured_request(self, call, *args, **kwargs):
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request)
            return FakeResponse(OK_ENVELOPE)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            call(*args, **kwargs)
        self.assertEqual(len(seen), 1)
        return seen[0]

    def assert_common_headers(self, request, city):
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.assertEqual(headers["authorization"], "Bearer ")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["accept"], "application/json, text/plain, */*")
        self.assertEqual(headers["chain"], "PVR")
        self.assertEqual(headers["country"], "INDIA")
        self.assertEqual(headers["appversion"], "1.0")
        self.assertEqual(headers["platform"], "WEBSITE")
        self.assertEqual(headers["flow"], "PVRINOX")
        self.assertEqual(headers["city"], city)

    def test_city_endpoint_uses_chennai_default(self):
        request = self.captured_request(pvr_client.list_cities)
        self.assertTrue(request.full_url.endswith("/content/city"))
        self.assert_common_headers(request, "Chennai")

    def test_seatlayout_uses_chennai_default(self):
        request = self.captured_request(pvr_client.seat_layout, "tok")
        self.assertTrue(request.full_url.endswith("/ticketing/seatlayout"))
        self.assert_common_headers(request, "Chennai")
        self.assertEqual(json.loads(request.data), {"encrypted": "tok"})

    def test_csessions_carries_the_query_city(self):
        request = self.captured_request(
            pvr_client.day_sessions, "Gurugram", 470, "2026-08-22",
            "28.4595", "77.0266", {})
        self.assertTrue(request.full_url.endswith("/content/csessions"))
        self.assert_common_headers(request, "Gurugram")
        body = json.loads(request.data)
        self.assertEqual(body["cid"], "470")
        self.assertEqual(body["dated"], "2026-08-22")
        self.assertEqual(body["qr"], "NO")
        self.assertEqual(body["cineType"], "")
        self.assertEqual(body["cineTypeQR"], "")


class TestPacing(ClientCase):
    def test_back_to_back_calls_sleep_the_gap(self):
        """R104: with 0.2s elapsed between calls the client sleeps the
        remaining 0.4s of the 0.6s floor (asserting the sleep math on
        mocked clocks)."""
        sleeps = []
        mono = mock.Mock(side_effect=[100.00, 100.05, 100.25, 100.30])
        wall = mock.Mock(side_effect=[1000.00, 1000.05, 1000.25, 1000.30])
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(OK_ENVELOPE)), \
                mock.patch("time.monotonic", mono), \
                mock.patch("time.time", wall), \
                mock.patch("time.sleep", sleeps.append):
            pvr_client._post("content/city", {})
            pvr_client._post("content/city", {})
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.4, places=6)

    def test_persisted_last_call_paces_a_new_process(self):
        """R104: a last_call written by a previous process delays the first
        call of a fresh one (module pacing state is empty here)."""
        import time as time_module
        pvr_client.save_cache("state.json",
                              {"last_call": time_module.time()})
        sleeps = []
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(OK_ENVELOPE)), \
                mock.patch("time.sleep", sleeps.append):
            pvr_client._post("content/city", {})
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0.5)
        self.assertLessEqual(sleeps[0], 0.6)

    def test_min_interval_env_can_only_raise(self):
        with mock.patch.dict(os.environ, {"PVR_MIN_INTERVAL": "0.1"}):
            self.assertEqual(pvr_client.min_interval(), 0.6)
        with mock.patch.dict(os.environ, {"PVR_MIN_INTERVAL": "2.5"}):
            self.assertEqual(pvr_client.min_interval(), 2.5)
        with mock.patch.dict(os.environ, {"PVR_MIN_INTERVAL": "junk"}):
            self.assertEqual(pvr_client.min_interval(), 0.6)


class TestBlocked(ClientCase):
    def assert_block_cycle(self, code):
        import time as time_module
        with mock.patch("urllib.request.urlopen", side_effect=http_error(code)):
            with self.assertRaises(pvr_client.Blocked):
                pvr_client._post("content/city", {})
        state = self.read_state()
        self.assertGreater(state["blocked_until"],
                           time_module.time() + 800)

        # R105: a NEW process over the same cache dir never touches urlopen
        pvr_client._last_call_mono[0] = 0.0
        untouched = mock.Mock(side_effect=AssertionError("network touched"))
        with mock.patch("urllib.request.urlopen", untouched):
            with self.assertRaises(pvr_client.Blocked):
                pvr_client._post("content/city", {})
        self.assertEqual(untouched.call_count, 0)

        # after the cooldown expires, calls flow again
        state["blocked_until"] = time_module.time() - 1
        pvr_client.save_cache("state.json", state)
        flowing = mock.Mock(return_value=FakeResponse(OK_ENVELOPE))
        with mock.patch("urllib.request.urlopen", flowing):
            payload = pvr_client._post("content/city", {})
        self.assertEqual(flowing.call_count, 1)
        self.assertEqual(payload["status"], 302)

    def test_403_trips_persistent_block(self):
        self.assert_block_cycle(403)

    def test_429_trips_persistent_block(self):
        self.assert_block_cycle(429)


class TestDaySessionsOutcomes(ClientCase):
    """R106: the three-way csessions mapping; errors never equal closed."""

    def outcome(self, urlopen_effect):
        if callable(urlopen_effect) or isinstance(urlopen_effect, Exception):
            patcher = mock.patch("urllib.request.urlopen",
                                 side_effect=urlopen_effect)
        else:
            patcher = mock.patch("urllib.request.urlopen",
                                 return_value=urlopen_effect)
        with patcher:
            return pvr_client.day_sessions("Gurugram", 470, "2026-08-22",
                                           "28.4595", "77.0266", {})

    def test_status_302_with_output_is_open(self):
        payload = {"status": 302, "output": {"cinemaMovieSessions": [{
            "movieRe": {"filmName": "DUNE (ENGLISH)", "id": "1"},
            "experienceSessions": [{"experienceKey": "IMAX", "shows": [{
                "sessionId": 9, "encrypted": "tok", "showTime": "06:00 PM",
                "statusCode": "76BE43", "theatreId": "470"}]}]}]}}
        result = self.outcome(FakeResponse(payload))
        self.assertEqual(result["outcome"], "open")
        self.assertEqual(len(result["shows"]), 1)
        show = result["shows"][0]
        self.assertEqual(show["deep_link"],
                         "https://www.pvrcinemas.com/seatlayout/tok")
        self.assertEqual(show["status_category"], "available")

    def test_json_body_other_status_is_closed(self):
        result = self.outcome(FakeResponse({"status": 500,
                                            "msg": "no shows"}))
        self.assertEqual(result, {"outcome": "closed"})

    def test_transport_500_is_closed(self):
        """Gap 2 regression: an unopened date can answer transport-level
        HTTP 500; that is a closed booking window, not an error."""
        result = self.outcome(http_error(500))
        self.assertEqual(result, {"outcome": "closed"})

    def test_transport_404_is_an_error(self):
        result = self.outcome(http_error(404))
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["error"], "http 404")

    def test_urlerror_is_an_error_never_closed(self):
        result = self.outcome(urllib.error.URLError("dns down"))
        self.assertEqual(result["outcome"], "error")
        self.assertNotEqual(result["outcome"], "closed")
        self.assertTrue(result["error"].startswith("error"))

    def test_blocked_raises_through(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(403)):
            with self.assertRaises(pvr_client.Blocked):
                pvr_client.day_sessions("Gurugram", 470, "2026-08-22",
                                        "28.4595", "77.0266", {})


class TestBookingHorizon(ClientCase):
    """R107: binary search on stubbed day_sessions outcomes."""

    def stub(self, last_open, calls):
        import datetime
        today = pvr_client.today_ist()

        def fake(city, cid, day, lat=None, lng=None, variants=None):
            offset = (datetime.date.fromisoformat(day) - today).days
            calls.append(offset)
            return ({"outcome": "open", "shows": []} if offset <= last_open
                    else {"outcome": "closed"})
        return fake

    def test_boundary_found_and_cached(self):
        calls = []
        with mock.patch.object(pvr_client, "day_sessions",
                               self.stub(4, calls)):
            result = pvr_client.booking_horizon("Gurugram", 470)
        self.assertEqual(result["days_ahead"], 4)
        expected = (pvr_client.today_ist()
                    + __import__("datetime").timedelta(days=4)).isoformat()
        self.assertEqual(result["last_open_date"], expected)
        self.assertLessEqual(len(calls), 6)

        # cached per (city, cinema, IST day): the second ask makes no probes
        with mock.patch.object(pvr_client, "day_sessions",
                               self.stub(4, calls)):
            again = pvr_client.booking_horizon("Gurugram", 470)
        self.assertEqual(again, result)
        self.assertLessEqual(len(calls), 6)

    def test_transport_500_day_counts_as_closed(self):
        """A 500-answering date reads closed, so the horizon never
        overstates (gap 2 through the horizon path)."""
        calls = []
        with mock.patch.object(pvr_client, "day_sessions",
                               self.stub(2, calls)):
            result = pvr_client.booking_horizon("Gurugram", 471)
        self.assertEqual(result["days_ahead"], 2)

    def test_blocked_returns_unknown_and_never_caches(self):
        def blocked(*args, **kwargs):
            raise pvr_client.Blocked("stop")
        with mock.patch.object(pvr_client, "day_sessions", blocked):
            result = pvr_client.booking_horizon("Gurugram", 472)
        self.assertEqual(result,
                         {"last_open_date": None, "days_ahead": None})
        # not cached: a later healthy ask probes again
        calls = []
        with mock.patch.object(pvr_client, "day_sessions",
                               self.stub(3, calls)):
            healthy = pvr_client.booking_horizon("Gurugram", 472)
        self.assertEqual(healthy["days_ahead"], 3)
        self.assertGreater(len(calls), 0)


class TestCityRecords(unittest.TestCase):
    """R108 on the captured city.json."""

    @classmethod
    def setUpClass(cls):
        output = context.load_fixture("city.json")["output"]
        cls.records = pvr_client.parse_city_records(output)
        cls.by_name = {r["name"]: r for r in cls.records}

    def test_rollups_typed_with_constituents(self):
        rollups = {r["name"] for r in self.records
                   if r["type"] == "metro_rollup"}
        self.assertEqual(rollups, {"Delhi-NCR", "Mumbai-All"})
        self.assertIn("Gurugram", self.by_name["Delhi-NCR"]["subcities"])
        self.assertIn("Thane", self.by_name["Mumbai-All"]["subcities"])

    def test_each_city_appears_once(self):
        names = [r["name"] for r in self.records]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(self.by_name["Gurugram"]["type"], "city")

    def test_gurugram_carries_top_level_coords(self):
        gurugram = self.by_name["Gurugram"]
        self.assertEqual(gurugram["lat"], "28.459469")
        self.assertEqual(gurugram["lng"], "77.026207")

    def test_coordinate_less_cities_claim_no_coords(self):
        missing = sorted(r["name"] for r in self.records
                         if not (r["lat"] and r["lng"]))
        self.assertEqual(missing,
                         ["Gangtok", "Jabalpur", "Leh", "Muzaffarpur"])


class TestVariantsAndLanguage(unittest.TestCase):
    """R109 on the captured nowshowing + csessions pair."""

    @classmethod
    def setUpClass(cls):
        cls.variants = pvr_client.parse_variants(
            context.load_fixture("nowshowing_gurugram.json")["output"])
        cls.shows = pvr_client.extract_shows(
            context.load_fixture("csessions_470.json")["output"],
            cls.variants)

    def test_per_show_movie_id_resolves_to_its_print(self):
        self.assertGreater(len(self.variants), 0)
        for show in self.shows:
            variant = self.variants.get(str(show["movieId"]))
            if variant:
                self.assertEqual(show["film"], variant["name"])

    def test_language_comes_from_the_variant_map(self):
        resolved = {s["language_source"] for s in self.shows
                    if s["language"]}
        self.assertEqual(resolved, {"variant"})
        self.assertFalse(any(s["language_disputed"] for s in self.shows))

    def test_manufactured_disagreement_sets_disputed(self):
        language, source, disputed = pvr_client.resolve_language(
            "Hindi", "English", "FILM (TAMIL)")
        self.assertEqual(language, "Hindi")
        self.assertEqual(source, "variant")
        self.assertTrue(disputed)

    def test_block_title_never_overwrites_variant_language(self):
        language, source, _ = pvr_client.resolve_language(
            "Telugu", None, "FILM (HINDI)")
        self.assertEqual((language, source), ("Telugu", "variant"))


class TestNormalizationAndStatus(unittest.TestCase):
    def test_screen_type_norm(self):
        """R110: the venues payload mixes case; csessions is upper case."""
        self.assertEqual(pvr_client.screen_type_norm("Premium"), "PREMIUM")
        self.assertEqual(pvr_client.screen_type_norm("Atmos"), "ATMOS")
        self.assertEqual(pvr_client.screen_type_norm("  imax "), "IMAX")
        self.assertEqual(pvr_client.screen_type_norm(None), "")

    def test_cinemas_fixture_formats_are_normalized(self):
        output = context.load_fixture("cinemas_gurugram.json")["output"]
        venues = pvr_client.parse_cinemas(output, None, "none")
        self.assertEqual(len(venues), 14)
        for venue in venues:
            for fmt in venue["formats"]:
                self.assertEqual(fmt, fmt.strip().upper())

    def test_status_category(self):
        """R111: 76BE43 is available; unseen hexes are unknown and the raw
        hex still travels with the show."""
        self.assertEqual(pvr_client.status_category("76BE43"), "available")
        self.assertEqual(pvr_client.status_category("#76be43"), "available")
        self.assertEqual(pvr_client.status_category("AB1234"), "unknown")
        self.assertEqual(pvr_client.status_category(""), "unknown")
        shows = pvr_client.extract_shows(
            context.load_fixture("csessions_470.json")["output"])
        for show in shows:
            self.assertEqual(show["statusCode"], "76BE43")
            self.assertEqual(show["status_category"], "available")


class TestBudgetExhaustion(ClientCase):
    """The call ceiling raises BudgetExhausted through the REAL pvr_client
    wrappers (which re-raise it like Blocked), so in live mode exhaustion
    surfaces as itself and never as a fake per-venue transport error."""

    def tearDown(self):
        # Undo the LiveClient counting wrapper so later tests see the
        # original _post.
        original = getattr(pvr_client, "_pvr_original_post", None)
        if original is not None:
            pvr_client._post = original
        super().tearDown()

    def test_exhaustion_raises_out_of_day_sessions(self):
        with mock.patch.object(radar, "CALL_CEILING", 2), \
                mock.patch("urllib.request.urlopen",
                           return_value=FakeResponse(OK_ENVELOPE)):
            client = radar.LiveClient()
            for _ in range(2):
                pvr_client.day_sessions("Gurugram", 470, "2026-08-22",
                                        "28.4595", "77.0266", {})
            with self.assertRaises(pvr_client.BudgetExhausted):
                client.sessions("Gurugram", 470, "2026-08-22",
                                "28.4595", "77.0266", {})
        self.assertEqual(client.calls, 2)

    def test_exhaustion_raises_out_of_film_variants(self):
        pvr_client.save_cache("city_coords.json",
                              {"gurugram": ["28.4595", "77.0266"]})
        with mock.patch.object(radar, "CALL_CEILING", 0), \
                mock.patch("urllib.request.urlopen",
                           return_value=FakeResponse(OK_ENVELOPE)):
            client = radar.LiveClient()
            with self.assertRaises(pvr_client.BudgetExhausted):
                client.variants("Gurugram", "28.4595", "77.0266",
                                "2026-08-22")

    def test_solve_reports_exhaustion_as_partial(self):
        """End to end through LiveClient: exhaustion mid-venue-loop sets
        meta.partial and CALL_BUDGET_EXHAUSTED, and the unreached venues are
        reported as skipped."""
        pvr_client.save_cache("city_coords.json",
                              {"gurugram": ["28.4595", "77.0266"]})
        pvr_client.save_cache(
            "variants.json", {"gurugram|2026-08-22": {}})
        cinemas_payload = {"status": 302, "output": {"cinemas": [
            {"theatreId": "470", "name": "Near", "latitude": 28.46,
             "longitude": 77.03},
            {"theatreId": "241", "name": "Far", "latitude": 28.47,
             "longitude": 77.04}]}}
        sessions_payload = {"status": 302, "output": {
            "cinemaMovieSessions": [{
                "movieRe": {"filmName": "DUNE", "id": "1"},
                "experienceSessions": [{"experienceKey": "", "shows": [{
                    "sessionId": 9, "encrypted": "tok",
                    "showTime": "06:00 PM", "showDate": "2026-08-22",
                    "showTimeStamp": 1, "theatreId": "470",
                    "statusCode": "76BE43"}]}]}]}}

        def by_url(request, timeout=None):
            if request.full_url.endswith("/content/cinemas"):
                return FakeResponse(cinemas_payload)
            return FakeResponse(sessions_payload)

        query = {
            "city": "Gurugram", "movie": None, "format": None,
            "date": "2026-08-22", "time_from": None, "time_to": None,
            "time_from_min": None, "time_to_min": None, "party_size": 1,
            "max_km": 60.0, "limit": 40, "seat_detail": 0,
            "no_osrm": True, "fixtures": None,
            "origin": {"lat": 28.4595, "lng": 77.0266,
                       "label": "test", "source": "caller"},
        }
        with mock.patch.object(radar, "CALL_CEILING", 2), \
                mock.patch("urllib.request.urlopen", side_effect=by_url):
            client = radar.LiveClient()
            document = radar.solve(client, query)
        self.assertTrue(document["meta"]["partial"])
        self.assertEqual(document["meta"]["error"], "CALL_BUDGET_EXHAUSTED")
        self.assertEqual(len(document["shows"]), 1)
        skipped = {s["theatreId"]: s["reason"]
                   for s in document["meta"]["cinemas_skipped"]}
        self.assertIn("not reached", skipped["241"])

    def test_second_client_gets_a_fresh_counter(self):
        """A second LiveClient in the same process must not chain onto the
        first one's spent ceiling (the sentinel keeps the true _post)."""
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(OK_ENVELOPE)):
            first = radar.LiveClient()
            pvr_client._post("content/city", {})
            self.assertEqual(first.calls, 1)
            second = radar.LiveClient()
            self.assertEqual(second.calls, 0)
            pvr_client._post("content/city", {})
            self.assertEqual(second.calls, 1)
            # the second client's wrapper delegates to the TRUE original,
            # not to the first client's wrapper
            self.assertIs(second._real_post, first._real_post)


class TestShowDedup(unittest.TestCase):
    """Payload-internal duplicates collapse on the reference's dedup key
    (cinema, date, time, screen); distinct shows never do."""

    @staticmethod
    def payload(shows):
        return {"cinemaMovieSessions": [{
            "movieRe": {"filmName": "DUNE", "id": "1"},
            "experienceSessions": [{"experienceKey": "", "shows": shows}]}]}

    @staticmethod
    def show(session_id, time_text, screen="AUDI 1"):
        return {"sessionId": session_id, "encrypted": "tok%s" % session_id,
                "showTime": time_text, "showDate": "2026-08-22",
                "showTimeStamp": session_id, "theatreId": "470",
                "screenName": screen, "statusCode": "76BE43"}

    def test_duplicate_listing_kept_once(self):
        shows = pvr_client.extract_shows(self.payload(
            [self.show(1, "06:00 PM"), self.show(2, "06:00 PM")]))
        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0]["sessionId"], 1)

    def test_distinct_shows_survive(self):
        shows = pvr_client.extract_shows(self.payload(
            [self.show(1, "06:00 PM"), self.show(2, "09:00 PM"),
             self.show(3, "06:00 PM", screen="AUDI 2")]))
        self.assertEqual(len(shows), 3)


class TestBannedEndpoint(unittest.TestCase):
    """R112 / R24: content/cinemasessions appears nowhere; a full offline
    radar flow performs zero urlopen calls."""

    def test_endpoint_name_absent_from_sources(self):
        banned = "cinema" + "sessions"
        for name in ("pvr_client.py", "radar.py", "geocode.py",
                     "render_map.py", "selftest.py"):
            with open(os.path.join(context.SCRIPTS_DIR, name)) as fh:
                self.assertNotIn(banned, fh.read(), name)

    def test_fixture_radar_flow_never_touches_urllib(self):
        spy = mock.Mock(side_effect=AssertionError("network touched"))
        client = radar.FixtureClient(context.FIXTURES_DIR)
        query = {
            "city": "Gurugram", "movie": None, "format": None,
            "date": "2026-08-22", "time_from": None, "time_to": None,
            "time_from_min": None, "time_to_min": None, "party_size": 1,
            "max_km": 60.0, "limit": 40, "seat_detail": 0,
            "no_osrm": True, "fixtures": context.FIXTURES_DIR,
            "origin": {"lat": 28.4595, "lng": 77.0266,
                       "label": "test", "source": "caller"},
        }
        with mock.patch("urllib.request.urlopen", spy):
            document = radar.solve(client, query)
        self.assertEqual(spy.call_count, 0)
        self.assertGreater(len(document["shows"]), 0)


if __name__ == "__main__":
    unittest.main()
