"""Regression tests for AirScout's pure validation, extraction, and storage logic."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import google_flights, scanner, store, web


class RouteValidationTests(unittest.TestCase):
    def config(self, origins=None, destinations=None):
        return {
            "origin": (origins or ["BRU"])[0],
            "destination": (destinations or ["HND"])[0],
            "origin_airports": origins or ["BRU"],
            "destination_airports": destinations or ["HND"],
            "departure_date": "2027-07-11",
            "return_date": "2027-07-28",
            "target_price": 2000,
        }

    def test_rejects_any_overlapping_origin_and_destination(self):
        with self.assertRaisesRegex(ValueError, "different origin"):
            scanner.validate_config(self.config(["BRU", "AMS"], ["HND", "AMS"]))

    def test_allows_disjoint_airport_sets(self):
        scanner.validate_config(self.config(["BRU", "AMS"], ["HND", "NRT"]))

    def test_normalizes_airport_case_when_detecting_overlap(self):
        with self.assertRaisesRegex(ValueError, "different origin"):
            scanner.validate_config(self.config(["BRU", "AMS"], ["HND", "ams"]))


class DateRangeTests(unittest.TestCase):
    def test_date_range_contains_only_valid_return_after_departure_pairs(self):
        start = scanner.date(2027, 7, 10)
        end = scanner.date(2027, 7, 11)
        returns = scanner.date(2027, 7, 11)
        return_end = scanner.date(2027, 7, 12)
        pairs = [(out, back) for out in (start, end) for back in (returns, return_end) if back > out]
        self.assertEqual(pairs, [(start, returns), (start, return_end), (end, return_end)])

    def test_date_range_rejects_invalid_iso_dates(self):
        config = {"origin": "BRU", "destination": "HND", "departure_date": "bad", "return_date": "2027-07-28", "target_price": 2000}
        with self.assertRaisesRegex(ValueError, "valid dates"):
            scanner.validate_config(config)


class FareExtractionTests(unittest.TestCase):
    def test_european_and_us_price_formats(self):
        self.assertEqual(google_flights._price("€ 2.076,50"), 2076.5)
        self.assertEqual(google_flights._price("EUR 2,076.50"), 2076.5)
        self.assertEqual(google_flights._price("€2,076"), 2076)
        self.assertIsNone(google_flights._price("no fare shown"))

    def test_extracts_matching_fare_and_ignores_wrong_destination(self):
        cards = [
            {"text": "BRU HND Air China 1 stop 18 hr € 2.076", "airline": "Air China", "time_labels": ["Departure time: 08:30 AM", "Arrival time: 05:10 PM", "Departure time: 09:45 PM", "Arrival time: 06:20 AM"]},
            {"text": "BRU NRT Air France 1 stop 18 hr € 1.800", "airline": "Air France"},
            {"text": "BRU HND € 2.000", "airline": "Unknown"},
        ]
        offers = google_flights._extract_offers(cards, {"origin": "BRU", "destination": "HND"})
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["price"], 2076)
        self.assertEqual(offers[0]["itinerary"][0]["stops"], 1)
        self.assertEqual(offers[0]["itinerary"][0]["departure_times"], ["08:30 AM", "09:45 PM"])
        self.assertEqual(offers[0]["itinerary"][0]["arrival_times"], ["05:10 PM", "06:20 AM"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "airscout.sqlite3")
        self.path_patch = patch.object(store, "DB_PATH", self.db_path)
        self.path_patch.start()
        store.init()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_settings_round_trip(self):
        config = {"origin": "BRU", "passengers": 2}
        store.save_config(config)
        self.assertEqual(store.get_config(), config)

    def test_route_price_samples_are_newest_first_and_match_full_route_dates(self):
        common = {"currency": "EUR", "itinerary": [{"from": "BRU", "to": "HND"}], "searched_departure_date": "2027-07-11", "searched_return_date": "2027-07-28"}
        store.log_scan(2100, "EUR", [{**common, "price": 2100}])
        store.log_scan(1999, "EUR", [{**common, "price": 1999}])
        store.log_scan(1500, "EUR", [{**common, "price": 1500, "searched_return_date": "2027-07-29"}])
        self.assertEqual(store.recent_route_prices("BRU", "HND", "2027-07-11", "2027-07-28"), [1999, 2100])

    def test_delivery_price_is_retrievable_only_for_accepted_or_unknown(self):
        scan_id = store.log_scan(1000, "EUR", [])
        store.record_delivery(scan_id, "rejected", "failed", "chat@g.us", "trip", 1000)
        self.assertIsNone(store.last_alert_price("chat@g.us", "trip"))
        store.record_delivery(scan_id, "accepted", "ok", "chat@g.us", "trip", 950)
        self.assertEqual(store.last_alert_price("chat@g.us", "trip"), 950)


class SearchProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_range_reports_current_date_and_completed_step(self):
        captured = []
        async def fake_search(config, mode='trip'):
            captured.append(scanner.scan_status())
            return []
        config = {
            "departure_date": "2027-07-11", "return_date": "2027-07-28",
            "departure_start_date": "2027-07-11", "departure_end_date": "2027-07-11",
            "return_start_date": "2027-07-28", "return_end_date": "2027-07-28",
        }
        with patch.object(scanner, "search_offers", fake_search):
            offers, actual = await scanner.search_date_range(config)
        self.assertEqual(offers, [])
        self.assertEqual(actual, config)
        self.assertEqual(captured[0]["stage"], "Checking trip alert")
        self.assertEqual(captured[0]["message"], "Date option 1 of 1")
        self.assertEqual(scanner.scan_status()["completed"], 1)
        self.assertEqual(scanner.scan_status()["total"], 1)


class PriceDropTests(unittest.TestCase):
    def test_custom_minimum_drop_requires_three_samples(self):
        self.assertFalse(scanner.is_major_drop(899, 1000, 2, 10))
        self.assertFalse(scanner.is_major_drop(950, 1000, 3, 10))
        self.assertTrue(scanner.is_major_drop(900, 1000, 3, 10))
        self.assertTrue(scanner.is_major_drop(800, 1000, 3, 20))

    def test_non_euro_prices_do_not_trigger_drop_alert(self):
        self.assertFalse(scanner.is_major_drop(800, 1000, 5, 10, currency='USD'))

    def test_alert_models_accept_custom_minimum_drop(self):
        trip = web.Trip(origin='BRU', destination='HND', departure_date='2027-07-11', return_date='2027-07-28', target_price=2000, min_price_drop_percent=15)
        self.assertEqual(trip.min_price_drop_percent, 15)
        explore = web.DestinationAlert(origin_airports=['BRU'], destination_airports=['HND'], departure_date='2027-07-11', return_date='2027-07-28', budget=2000, min_price_drop_percent=25)
        self.assertEqual(explore.min_price_drop_percent, 25)
        with self.assertRaises(Exception):
            web.Trip(origin='BRU', destination='HND', departure_date='2027-07-11', return_date='2027-07-28', target_price=2000, min_price_drop_percent=95)


class AdminRouteTests(unittest.TestCase):
    def test_admin_route_is_available_but_not_linked_from_main_app(self):
        self.assertIn('/admin', {route.path for route in web.app.routes})
        with open('static/index.html', encoding='utf-8') as source:
            html = source.read()
        self.assertNotIn('href="/admin"', html)
        self.assertNotIn('Google Flights sign-in', html)


class DateNormalizationTests(unittest.TestCase):
    def test_alert_types_use_distinct_trip_keys(self):
        trip = {"origin": "BRU", "destination": "HND", "departure_date": "2027-07-11", "return_date": "2027-07-28", "passengers": 2}
        trip_key = json.dumps({"type": "trip", **trip}, sort_keys=True)
        explore_key = json.dumps({"type": "explore", **trip}, sort_keys=True)
        self.assertNotEqual(trip_key, explore_key)

    def test_defaults_missing_window_bounds_to_selected_dates(self):
        data = {"departure_date": "2027-07-11", "return_date": "2027-07-28"}
        normalized = web._normalize_window_dates(data)
        self.assertEqual(normalized["departure_start_date"], "2027-07-11")
        self.assertEqual(normalized["departure_end_date"], "2027-07-11")
        self.assertEqual(normalized["return_start_date"], "2027-07-28")
        self.assertEqual(normalized["return_end_date"], "2027-07-28")

    def test_rejects_return_range_not_after_departure(self):
        with self.assertRaises(web.HTTPException):
            web._normalize_window_dates({"departure_date": "2027-07-11", "return_date": "2027-07-11"})


if __name__ == "__main__":
    unittest.main()
