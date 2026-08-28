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
    "unpkg.com",                     # Leaflet 1.9.4, SRI-pinned
    "tile.openstreetmap.org",        # OSM basemap tiles (the only basemap:
                                     # CARTO watermarks keyless requests)
    "www.openstreetmap.org",         # attribution link
    "openstreetmap.org",
    "www.pvrcinemas.com",            # per-show deep links
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
        self.assertIn(">Venue</th>", self.page)
        self.assertIn(">Seats</th>", self.page)

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

    def test_format_words_dedupe_case_insensitively(self):
        """Real capture 23 Aug 2026: screenType 'Atmos' + movieFormat
        'ATMOS' rendered as 'Atmos ATMOS'. One format, one word."""
        show = {"theatreId": "1", "sessionId": 5, "film": "AWARAPAN 2",
                "showTime": "07:45 PM", "showTimeStamp": 1,
                "screenType": "Atmos", "movieFormat": "ATMOS",
                "soundFormat": "", "statusCode": "76BE43",
                "status_category": "available", "statusTxt": "Available",
                "deep_link": None, "time_unparsed": False, "seats": None,
                "seat_error": None, "rank": 1}
        table = render_map.fallback_table(
            self.base_radar([self.venue("1", True)], [show]))
        self.assertIn("Atmos", table)
        self.assertNotIn("Atmos ATMOS", table)

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




class TestSeatVerdictWording(unittest.TestCase):
    """A verified 'no good pair' must never read as sold out. First-user
    receipt: a brown 'zone full' chip clicked through to PVR's page showing
    open front-row recliners and read as a contradiction; the wording now
    names the hall fallback in plain words everywhere it appears."""

    def test_hall_yes_names_the_fallback_in_the_table(self):
        show = {"seats": {"meets_party_size": False,
                          "hall_meets_party_size": True,
                          "hall_best_run": 3, "best_run": 1, "free": 5}}
        text = render_map.seats_cell(show, 2)
        self.assertIn("good rows full", text)
        self.assertIn("fits elsewhere in hall", text)
        self.assertNotIn("zone", text)

    def test_hall_full_still_says_no_plainly(self):
        show = {"seats": {"meets_party_size": False,
                          "hall_meets_party_size": False,
                          "best_run": 1, "free": 2}}
        self.assertIn("no", render_map.seats_cell(show, 2))

    def test_chip_badge_and_legend_tell_the_same_story(self):
        page = render_map.render(context.load_asset("sample_radar.json"))
        self.assertIn("good rows full · fits elsewhere", page)
        self.assertIn("other rows in the hall can", page)
        self.assertNotIn('"zone full"', page)


class TestNoticeBannerExclusions(unittest.TestCase):
    """Indiranagar, 23 Aug 2026: a recliner ask spent its whole seat budget,
    every verified show was dropped (3 halls without the tier, 3 that could
    not seat 3 together), and the page said nothing about the tier drops.
    Every filter that can drop a verified show must announce itself, and an
    all-dropped run must explain why everything left reads unverified."""

    @staticmethod
    def make(meta_extra, shows=(), query_extra=None):
        radar = TestHonestEmptyStates.base_radar(
            [TestHonestEmptyStates.venue("1", True)], list(shows),
            meta_extra)
        radar["query"].update(query_extra or {})
        return radar

    def test_tier_drops_are_announced(self):
        banner = render_map.notice_banner(self.make(
            {"excluded_by_tier": 3}, query_extra={"tier": "recliner"}))
        self.assertIn("3 verified show(s) dropped", banner)
        self.assertIn("recliner", banner)

    def test_price_drops_are_announced(self):
        banner = render_map.notice_banner(self.make(
            {"excluded_by_price": 2}, query_extra={"max_price": 500}))
        self.assertIn("2 verified show(s) dropped", banner)
        self.assertIn("500", banner)

    def test_all_verified_dropped_explains_the_unverified_page(self):
        show = {"theatreId": "1", "sessionId": 5, "film": "DUNE",
                "showTime": "06:00 PM", "showTimeStamp": 1,
                "screenType": "", "movieFormat": "", "soundFormat": "",
                "statusCode": "76BE43", "status_category": "available",
                "statusTxt": "Available", "deep_link": None,
                "time_unparsed": False, "seats": None, "seat_error": None,
                "rank": 1}
        banner = render_map.notice_banner(self.make(
            {"excluded_by_tier": 3, "excluded_by_party": 3}, [show],
            {"tier": "recliner"}))
        self.assertIn("honestly", banner)
        self.assertIn("unverified", banner)
        self.assertIn("seat-detail", banner)

    def test_verified_show_present_means_no_all_dropped_note(self):
        show = {"theatreId": "1", "sessionId": 5, "film": "DUNE",
                "showTime": "06:00 PM", "showTimeStamp": 1,
                "screenType": "", "movieFormat": "", "soundFormat": "",
                "statusCode": "76BE43", "status_category": "available",
                "statusTxt": "Available", "deep_link": None,
                "time_unparsed": False,
                "seats": {"meets_party_size": True, "best_run": 4,
                          "free": 10},
                "seat_error": None, "rank": 1}
        banner = render_map.notice_banner(self.make(
            {"excluded_by_tier": 1}, [show], {"tier": "recliner"}))
        self.assertNotIn("Every seat map", banner)


class TestSortableTable(unittest.TestCase):
    """The no-JS table stays in the radar's ranked order; with JS the
    column headers re-sort it client-side, and the page names the default
    order instead of leaving it a mystery."""

    @classmethod
    def setUpClass(cls):
        cls.page = render_map.render(
            context.load_fixture("radar_real_capture.json"))

    def test_headers_carry_sort_keys(self):
        for attr in ('data-key="venue"', 'data-key="km"', 'data-key="t"',
                     'data-key="seats"', 'data-key="status"'):
            self.assertIn(attr, self.page, attr)

    def test_rows_carry_sort_values_and_original_order(self):
        self.assertIn('data-i="0"', self.page)
        self.assertIn("data-km=", self.page)
        self.assertIn("data-t=", self.page)
        self.assertIn("data-seats=", self.page)

    def test_sorter_and_indicator_wired(self):
        self.assertIn("aria-sort", self.page)
        self.assertIn('table.getElementsByTagName("tr")', self.page)

    def test_default_order_is_named(self):
        self.assertIn("soonest show first, then nearest venue", self.page)
        self.assertIn("third click restores this order", self.page)


class TestLegendCoversEveryChipState(unittest.TestCase):
    """Every chip state the JS can emit is explained in the legend. The brown
    'zone full' chip shipped without a legend entry once and a first user
    read it as sold out; this pins the invariant, not just that instance."""

    def test_every_js_chip_class_has_a_legend_demo(self):
        page = render_map.render(context.load_asset("sample_radar.json"))
        emitted = set(re.findall(r'return "chip (chip-[^"]+)"', page))
        self.assertGreaterEqual(len(emitted), 4)
        for cls in emitted:
            self.assertIn("chipdemo %s" % cls, page, cls)


class TestRealCaptureRender(unittest.TestCase):
    """The renderer's honesty invariants hold on a REAL radar run (captured
    21 Aug 2026, 3 venues / 8 shows / 3 verified), not only on authored
    samples: verified and unverified stay countably distinct, every deep
    link survives into both the chips and the table, and the page stays
    clean (hosts, dashes) on live-shaped data."""

    @classmethod
    def setUpClass(cls):
        cls.radar = context.load_fixture("radar_real_capture.json")
        cls.page = render_map.render(cls.radar)

    def test_verified_rows_match_shows_with_seat_data(self):
        verified = sum(1 for s in self.radar["shows"] if s.get("seats"))
        self.assertEqual(self.page.count("verified seat count"), verified)
        self.assertEqual(self.page.count(">unverified<"),
                         len(self.radar["shows"]) - verified)

    def test_every_deep_link_survives_twice(self):
        for show in self.radar["shows"]:
            link = show.get("deep_link")
            if link:
                self.assertGreaterEqual(self.page.count(link), 2, link)

    def test_every_venue_is_on_the_page(self):
        for venue in self.radar["venues"]:
            self.assertIn(str(venue["theatreId"]), self.page)

    def test_real_page_stays_clean(self):
        self.assertNotIn("\u2014", self.page)
        self.assertNotIn("\u2013", self.page)
        for url in set(re.findall(r"https?://[^\s\"'<>\\)]+", self.page)):
            host = urllib.parse.urlparse(url).netloc
            self.assertIn(host, ALLOWED_HOSTS, url)


class TestSkillDescriptionRouting(unittest.TestCase):
    """The SKILL.md description is what routes a user's ask to this skill;
    a weak model skipped the skill on 'what recliners are available near me
    tonight, cheapest first' (23 Aug 2026) because those words were missing.
    Pin the routing vocabulary so an edit never drops it again."""

    def test_description_carries_the_routing_phrases(self):
        path = os.path.join(context.REPO_ROOT, "skills", "pvr-inox-radar",
                            "SKILL.md")
        with open(path) as fh:
            frontmatter = fh.read().split("---")[1].lower()
        for phrase in ("recliner", "cheapest", "seats together", "showtimes",
                       "imax", "map", "tonight"):
            self.assertIn(phrase, frontmatter, phrase)


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

    def test_labels_cycle_four_directions(self):
        """Dogfood 23 Aug 2026: Mumbai's 12-venue map collided labels with
        only left/right alternation; four directions must stay wired."""
        page = render_map.render(self.RADAR)
        for side in ('"right"', '"left"', '"top"', '"bottom"'):
            self.assertIn(side, page)


class TestStatsStrip(unittest.TestCase):
    """The films tile: 32 shows of ONE film (a real advance-sale date,
    captured 23 Aug 2026 for Sat 29 Aug) must be legible from the header."""

    def test_single_film_date_shows_film_count(self):
        radar = TestHonestEmptyStates.base_radar(
            [TestHonestEmptyStates.venue("1", True)], [])
        radar["shows"] = [
            {"theatreId": "1", "sessionId": i, "film": "TOXIC (HINDI)",
             "showTime": "09:00 AM", "showTimeStamp": i, "screenType": "",
             "movieFormat": "", "soundFormat": "", "statusCode": "76BE43",
             "status_category": "available", "statusTxt": "Available",
             "deep_link": None, "time_unparsed": False, "seats": None,
             "seat_error": None, "rank": i} for i in range(3)]
        strip = render_map.stats_strip(radar)
        self.assertIn(">film<", strip)
        self.assertIn(">3</div>", strip)

    def test_no_shows_means_no_films_tile(self):
        radar = TestHonestEmptyStates.base_radar(
            [TestHonestEmptyStates.venue("1", True)], [])
        self.assertNotIn("film", render_map.stats_strip(radar))


if __name__ == "__main__":
    unittest.main()
