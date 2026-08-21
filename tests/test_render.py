# Renderer asserts (SPEC R119, R120) on render() output for the authored
# sample radar document plus a hostile synthetic one. Pure string checks;
# no filesystem writes, no network.

import os
import re
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context  # noqa: E402

import render_map  # noqa: E402

ALLOWED_HOSTS = {
    "unpkg.com",                 # Leaflet 1.9.4, SRI-pinned
    "tile.openstreetmap.org",    # map tiles
    "www.openstreetmap.org",     # attribution link
    "openstreetmap.org",
    "www.pvrcinemas.com",        # per-show deep links
}


class TestRenderedPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.radar = context.load_asset("sample_radar.json")
        cls.page = render_map.render(cls.radar)

    def test_only_allowed_external_hosts(self):
        """R119/R68: Leaflet from unpkg, OSM tiles and attribution, and
        PVR deep links are the ONLY external references."""
        urls = set(re.findall(r"https?://[^\s\"'<>\\)]+", self.page))
        self.assertTrue(urls)
        for url in urls:
            host = urllib.parse.urlparse(url).netloc
            self.assertIn(host, ALLOWED_HOSTS, url)

    def test_leaflet_pinned_with_integrity(self):
        self.assertIn(render_map.LEAFLET_CSS, self.page)
        self.assertIn(render_map.LEAFLET_JS, self.page)
        self.assertIn('integrity="%s"' % render_map.LEAFLET_CSS_SRI,
                      self.page)
        self.assertIn('integrity="%s"' % render_map.LEAFLET_JS_SRI,
                      self.page)
        self.assertIn('crossorigin="anonymous"', self.page)

    def test_venue_pins_in_embedded_data(self):
        for venue in self.radar["venues"]:
            self.assertIn(str(venue["theatreId"]), self.page)
            self.assertIn(str(venue["lat"]), self.page)
            self.assertIn(str(venue["lng"]), self.page)

    def test_deep_links_in_embedded_data_and_fallback_table(self):
        """Each show's deep link appears at least twice: once in the
        embedded JSON that feeds the popup chips, once as the fallback
        table's href."""
        for show in self.radar["shows"]:
            link = show["deep_link"]
            self.assertGreaterEqual(self.page.count(link), 2, link)
        self.assertIn('href="https://www.pvrcinemas.com/seatlayout/',
                      self.page)

    def test_legend_present(self):
        self.assertIn(render_map.LEGEND_LINE, self.page)
        self.assertIn("verified seats for your party", self.page)
        self.assertIn("booking not open yet (not sold out)", self.page)

    def test_header_restates_the_query(self):
        self.assertIn("Sat 22 Aug", self.page)
        self.assertIn("4 seats together", self.page)
        self.assertIn("Sector 56 Gurgaon", self.page)
        self.assertIn(self.radar["query"]["generated_at"], self.page)

    def test_footer_credits(self):
        self.assertIn("pvr-inox-mcp (MIT)", self.page)
        self.assertIn("OpenStreetMap", self.page)
        self.assertIn("Drive times are estimates", self.page)

    def test_fallback_table_in_static_markup(self):
        self.assertIn("<table>", self.page)
        self.assertIn("<th>Venue</th>", self.page)
        self.assertIn("<th>Seats</th>", self.page)

    def test_verified_and_unverified_chips_distinct(self):
        self.assertIn("chip-verified", self.page)
        self.assertIn("chip-unverified", self.page)

    def test_no_em_or_en_dashes(self):
        """R120 / R77 (codepoints written as escapes so this file itself
        stays clean for the repo-wide scanner)."""
        self.assertNotIn("\u2014", self.page)
        self.assertNotIn("\u2013", self.page)


class TestHonestEmptyStates(unittest.TestCase):
    """The renderer never flattens distinct answers: closed venues stay
    distinct from no-match venues in the no-JS table, party-filter drops are
    announced, and missing format data is left blank, never invented."""

    @staticmethod
    def base_radar(venues, shows, meta_extra=None):
        meta = {"partial": False, "booking_open": True, "message": None,
                "error": None, "excluded_by_party": 0}
        meta.update(meta_extra or {})
        return {
            "query": {"city": "Gurugram", "movie": None, "format": None,
                      "date": "2026-08-28", "time_from": None,
                      "time_to": None, "party_size": 4, "max_km": 10.0,
                      "limit": 40, "seat_detail": 0, "no_osrm": True,
                      "fixtures": None,
                      "origin": {"lat": 28.4, "lng": 77.0, "label": "test",
                                 "source": "caller"},
                      "generated_at": "2026-08-21T10:00:00+05:30"},
            "venues": venues, "shows": shows, "meta": meta,
        }

    @staticmethod
    def venue(theatre_id, booking_open):
        return {"theatreId": theatre_id, "name": "Venue %s" % theatre_id,
                "lat": "28.41", "lng": "77.01", "distance_km": 2.0,
                "distance_from": "caller", "drive_min_est": 12,
                "drive_min_source": "heuristic",
                "booking_open": booking_open,
                "best_status": "closed" if booking_open is False else "none",
                "formats": []}

    def test_empty_table_keeps_the_closed_venue_distinction(self):
        radar_doc = self.base_radar(
            [self.venue("1", True), self.venue("2", False)], [])
        table = render_map.fallback_table(radar_doc)
        self.assertIn("no shows matched the query", table)
        self.assertIn("booking not open yet for this date (not sold out)",
                      table)
        self.assertIn("Venue 1", table)
        self.assertIn("Venue 2", table)

    def test_empty_table_without_venues_still_says_something(self):
        table = render_map.fallback_table(self.base_radar([], []))
        self.assertIn("no shows matched", table)

    def test_party_filter_drops_are_announced(self):
        radar_doc = self.base_radar([self.venue("1", True)], [],
                                    {"excluded_by_party": 2})
        banner = render_map.notice_banner(radar_doc)
        self.assertIn("2 show(s) dropped", banner)
        self.assertIn("4 together", banner)

    def test_unknown_format_left_blank_not_invented(self):
        show = {"theatreId": "1", "sessionId": 5, "film": "DUNE",
                "showTime": "06:00 PM", "showTimeStamp": 1,
                "screenType": "", "movieFormat": "", "soundFormat": "",
                "statusCode": "76BE43", "status_category": "available",
                "statusTxt": "Available", "deep_link": None,
                "time_unparsed": False, "seats": None, "seat_error": None,
                "rank": 1}
        table = render_map.fallback_table(
            self.base_radar([self.venue("1", True)], [show]))
        self.assertNotIn("2D", table)


class TestEscaping(unittest.TestCase):
    """R120: hostile user text renders inert."""

    @staticmethod
    def hostile_radar():
        return {
            "query": {"city": "Gurugram", "movie": "<script>alert(1)</script>",
                      "format": None, "date": "2026-08-22",
                      "time_from": None, "time_to": None, "party_size": 2,
                      "max_km": 10.0, "limit": 40, "seat_detail": 0,
                      "no_osrm": True, "fixtures": None,
                      "origin": {"lat": 28.4, "lng": 77.0,
                                 "label": "<b>Sector & 56</b>",
                                 "source": "caller"},
                      "generated_at": "2026-08-21T10:00:00+05:30"},
            "venues": [{"theatreId": "9",
                        "name": "<script>alert(1)</script>Nasty & Co",
                        "lat": "28.41", "lng": "77.01", "distance_km": 1.0,
                        "distance_from": "caller", "drive_min_est": 9,
                        "drive_min_source": "heuristic",
                        "booking_open": True, "best_status": "available",
                        "formats": []}],
            "shows": [{"theatreId": "9", "sessionId": 1,
                       "film": "<img src=x onerror=alert(1)>",
                       "filmId": "1", "movieId": "1",
                       "language": "English", "language_disputed": False,
                       "showTime": "06:00 PM", "showTimeStamp": 1,
                       "screenName": "AUDI 1", "screenType": "",
                       "movieFormat": "", "soundFormat": "",
                       "statusCode": "76BE43",
                       "status_category": "available",
                       "statusTxt": "Available", "deep_link": None,
                       "time_unparsed": False, "seats": None,
                       "seat_error": None, "rank": 1}],
            "meta": {"partial": False, "booking_open": True,
                     "message": None, "error": None},
        }

    def test_script_in_names_renders_inert(self):
        page = render_map.render(self.hostile_radar())
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert", page)   # escaped in the table
        self.assertIn("\\u003cscript", page)         # escaped in the JSON

    def test_embedded_json_cannot_close_the_script_tag(self):
        page = render_map.render(self.hostile_radar())
        start = page.index("const RADAR = ")
        end = page.index(";</script>", start)
        blob = page[start + len("const RADAR = "):end]
        self.assertNotIn("<", blob)




class TestMapLabels(unittest.TestCase):
    """The map labels venues and the origin without clicks, and fits every
    pin up front (SPEC-level UX asks from first-user feedback)."""

    RADAR = {
        "query": {"party_size": 1,
                  "origin": {"lat": 28.4595, "lng": 77.0266,
                             "label": "Gurgaon city centre"}},
        "venues": [{"theatreId": "1", "name": "Venue 1", "lat": 28.4,
                    "lng": 77.0, "drive_min_est": 9}],
        "shows": [], "meta": {},
    }

    def test_permanent_venue_and_origin_labels(self):
        page = render_map.render(self.RADAR)
        self.assertIn("permanent: true", page)
        self.assertIn("venue-label", page)
        self.assertIn("origin-label", page)
        self.assertIn('"You: "', page)

    def test_fit_runs_again_after_load(self):
        page = render_map.render(self.RADAR)
        self.assertIn("invalidateSize", page)
        self.assertIn("fitAll", page)
        self.assertIn("maxZoom: 14", page)

    def test_short_name_logic_present(self):
        page = render_map.render(self.RADAR)
        self.assertIn("shortName", page)


if __name__ == "__main__":
    unittest.main()
