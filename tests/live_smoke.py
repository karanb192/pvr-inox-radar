# Live smoke test (SPEC R123, R124). EXCLUDED from default discovery: the
# filename does not match the test*.py pattern, so
# `python3 -m unittest discover -s tests` never touches the network.
#
# Run it deliberately, and only with an explicit live-call budget:
#
#     PVR_LIVE=1 python3 -m unittest tests.live_smoke
#
# Budget: at most 3 live calls (content/city, content/cinemas for Gurugram,
# one content/csessions), strictly sequential through the client's own
# pacing. An upstream Blocked SKIPS instead of failing and is never
# retried: a smoke test must not deepen a block.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402

import pvr_client  # noqa: E402

LIVE = os.environ.get("PVR_LIVE") == "1"


@unittest.skipUnless(LIVE, "live smoke disabled (set PVR_LIVE=1 to enable)")
class TestLiveSmoke(unittest.TestCase):
    """One test method, three paced calls, envelope-level asserts only."""

    def test_three_call_smoke(self):
        try:
            city_payload = pvr_client._post(
                "content/city",
                {"lat": pvr_client.DEFAULT_LATLNG[0],
                 "lng": pvr_client.DEFAULT_LATLNG[1]})
            self.assertEqual(city_payload.get("status"), 302)
            self.assertTrue(city_payload.get("output"))

            cinemas_payload = pvr_client._post(
                "content/cinemas",
                {"city": "Gurugram", "lat": "28.459469",
                 "lng": "77.026207", "text": ""},
                "Gurugram")
            self.assertEqual(cinemas_payload.get("status"), 302)
            self.assertTrue(cinemas_payload.get("output"))
            venues = pvr_client.parse_cinemas(
                cinemas_payload["output"],
                ("28.459469", "77.026207"), "city_centre")
            self.assertGreater(len(venues), 0)

            today = pvr_client.today_ist().isoformat()
            outcome = pvr_client.day_sessions(
                "Gurugram", venues[0]["theatreId"], today,
                "28.459469", "77.026207", variants={})
            # today is normally open; closed is still a valid envelope
            self.assertIn(outcome["outcome"], ("open", "closed"))
            if outcome["outcome"] == "open":
                self.assertGreater(len(outcome["shows"]), 0)
        except pvr_client.Blocked as exc:
            # Never retry, never fail: backing off is correct behavior.
            self.skipTest("upstream rate-limited us: %s" % exc)


if __name__ == "__main__":
    unittest.main()
