# geocode.py behavior: pure helpers, the cache-hit path (zero network),
# the mocked network paths, and the exit-code mapping. No test here touches
# the network: urllib.request.urlopen is patched at the seam.

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402

import geocode  # noqa: E402
import pvr_client  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self, *args):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


NOMINATIM_HIT = [{"lat": "28.4231", "lon": "77.0917",
                  "display_name": "Sector 56, Gurugram, Haryana, India"}]


class GeocodeCase(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="pvr-test-geo-")
        self._old_cache = os.environ.get("PVR_RADAR_CACHE_DIR")
        os.environ["PVR_RADAR_CACHE_DIR"] = self.cache
        patcher = mock.patch("time.sleep", mock.Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("PVR_RADAR_CACHE_DIR", None)
        else:
            os.environ["PVR_RADAR_CACHE_DIR"] = self._old_cache
        shutil.rmtree(self.cache, ignore_errors=True)


class TestPureHelpers(unittest.TestCase):
    def test_normalize_query(self):
        self.assertEqual(geocode.normalize_query("  Sector   56, Gurgaon "),
                         "sector 56, gurgaon")
        self.assertEqual(geocode.normalize_query(None), "")

    def test_first_result_takes_the_first_usable_hit(self):
        rows = [{"lat": "junk", "lon": "junk"},
                "not a dict",
                {"lat": "28.4", "lon": "77.0", "display_name": "Here"}]
        result = geocode.first_result(rows)
        self.assertEqual(result["lat"], 28.4)
        self.assertEqual(result["lng"], 77.0)
        self.assertEqual(result["display_name"], "Here")

    def test_first_result_none_on_empty_or_garbage(self):
        self.assertIsNone(geocode.first_result([]))
        self.assertIsNone(geocode.first_result(None))
        self.assertIsNone(geocode.first_result([{"lat": None, "lon": None}]))


class TestGeocodeFlow(GeocodeCase):
    def test_network_success_caches_and_reports_uncached(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(NOMINATIM_HIT)) as spy:
            result, error = geocode.geocode("Sector 56, Gurugram")
        self.assertIsNone(error)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(result["lat"], 28.4231)
        self.assertFalse(result["cached"])
        stored = pvr_client.load_cache(geocode.GEOCODE_FILE)
        self.assertIn("sector 56, gurugram", stored)

    def test_cache_hit_skips_the_network_entirely(self):
        pvr_client.save_cache(geocode.GEOCODE_FILE, {
            "sector 56, gurugram": {"lat": 28.4231, "lng": 77.0917,
                                    "display_name": "Cached"}})
        untouched = mock.Mock(side_effect=AssertionError("network touched"))
        with mock.patch("urllib.request.urlopen", untouched):
            result, error = geocode.geocode("  Sector  56,  GURUGRAM ")
        self.assertIsNone(error)
        self.assertEqual(untouched.call_count, 0)
        self.assertTrue(result["cached"])
        self.assertEqual(result["lat"], 28.4231)

    def test_no_result_maps_to_error(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse([])):
            result, error = geocode.geocode("nowhere at all")
        self.assertIsNone(result)
        self.assertEqual(error["error"], "no_result")

    def test_network_failure_maps_to_error(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("dns down")):
            result, error = geocode.geocode("Sector 56, Gurugram")
        self.assertIsNone(result)
        self.assertEqual(error["error"], "network")


class TestExitCodes(GeocodeCase):
    """0 success, 2 usage, 3 no result, 4 network, per the module contract."""

    def run_main(self, argv, urlopen):
        stdout = []
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch("builtins.print",
                           side_effect=lambda *a, **k: stdout.append(a)):
            code = geocode.main(argv)
        return code, json.loads(stdout[-1][0])

    def test_success_exits_0(self):
        code, payload = self.run_main(
            ["Sector 56, Gurugram"],
            mock.Mock(return_value=FakeResponse(NOMINATIM_HIT)))
        self.assertEqual(code, 0)
        self.assertEqual(payload["source"], "nominatim")

    def test_empty_query_exits_2(self):
        code, payload = self.run_main(
            ["   "], mock.Mock(side_effect=AssertionError("network")))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "usage")

    def test_no_result_exits_3(self):
        code, payload = self.run_main(
            ["nowhere"], mock.Mock(return_value=FakeResponse([])))
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"], "no_result")

    def test_network_failure_exits_4(self):
        code, payload = self.run_main(
            ["Sector 56"], mock.Mock(side_effect=OSError("down")))
        self.assertEqual(code, 4)
        self.assertEqual(payload["error"], "network")


class TestRateLimit(GeocodeCase):
    def test_cross_process_last_call_honored(self):
        """A nominatim_last written by a previous process delays this one."""
        import time as time_module
        pvr_client.save_cache(pvr_client.STATE_FILE,
                              {"nominatim_last": time_module.time()})
        sleeps = []
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse(NOMINATIM_HIT)), \
                mock.patch("time.sleep", sleeps.append):
            geocode.geocode("Sector 56, Gurugram")
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0.5)
        self.assertLessEqual(sleeps[0], 1.0)


if __name__ == "__main__":
    unittest.main()
