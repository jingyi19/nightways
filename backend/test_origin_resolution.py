import unittest
from unittest.mock import patch

from backend.transitous import (
    AmbiguousOriginError,
    OriginNotFoundError,
    OriginResolutionError,
    ResolvedOrigin,
    TransitousError,
    resolve_origin,
)


EXPECTED_CITIES = {
    "Dresden": ("Dresden", "DE", "Europe/Berlin"),
    "Berlin": ("Berlin", "DE", "Europe/Berlin"),
    "Hamburg": ("Hamburg", "DE", "Europe/Berlin"),
    "Amsterdam": ("Amsterdam", "NL", "Europe/Amsterdam"),
    "Vienna": ("Vienna", "AT", "Europe/Vienna"),
    "Prague": ("Prague", "CZ", "Europe/Prague"),
    "Paris": ("Paris", "FR", "Europe/Paris"),
}


class LiveOriginResolutionTests(unittest.TestCase):

    def assert_resolves_to(self, query, expected):

        result = resolve_origin(query)

        self.assertIsInstance(result, ResolvedOrigin)
        self.assertEqual(
            (result.name, result.country_code, result.timezone),
            expected,
        )
        self.assertTrue(-90 <= result.lat <= 90)
        self.assertTrue(-180 <= result.lon <= 180)


    def test_requested_cities(self):

        for query, expected in EXPECTED_CITIES.items():

            with self.subTest(city=query):

                self.assert_resolves_to(query, expected)


    def test_whitespace_is_trimmed(self):

        self.assert_resolves_to(
            "  Berlin  ",
            EXPECTED_CITIES["Berlin"],
        )


    def test_matching_is_case_insensitive(self):

        self.assert_resolves_to(
            "berlin",
            EXPECTED_CITIES["Berlin"],
        )


    def test_nonexistent_city(self):

        with self.assertRaises(OriginNotFoundError):

            resolve_origin("zzzz-nightways-no-such-city-4f1f2b")


class OriginSelectionTests(unittest.TestCase):

    def test_empty_city_is_rejected_without_a_request(self):

        with patch("backend.transitous._request_place_matches") as request:

            with self.assertRaises(OriginResolutionError):

                resolve_origin("   ")

            request.assert_not_called()


    @patch("backend.transitous._request_place_matches")
    def test_multiple_exact_european_matches_are_ambiguous(self, request):

        request.return_value = [
            _place("Example", "DE", 50.0, 10.0, "Europe/Berlin"),
            _place("Example", "FR", 48.0, 2.0, "Europe/Paris"),
        ]

        with self.assertRaises(AmbiguousOriginError) as error:

            resolve_origin("Example")

        self.assertEqual(len(error.exception.candidates), 2)


    @patch("backend.transitous._request_place_matches")
    def test_matching_administrative_area_resolves_exact_names(self, request):

        capital = _place(
            "Example",
            "DE",
            50.0,
            10.0,
            "Europe/Berlin",
        )
        capital["areas"] = [
            {
                "name": "Example",
                "adminLevel": 6,
            }
        ]
        request.return_value = [
            capital,
            _place("Example", "FR", 48.0, 2.0, "Europe/Paris"),
        ]

        result = resolve_origin("Example")

        self.assertEqual(result.country_code, "DE")


    @patch("backend.transitous._request_place_matches")
    def test_europe_membership_uses_country_code_not_timezone(self, request):

        examples = (
            ("Reykjavik", "IS", "Atlantic/Reykjavik"),
            ("Nicosia", "CY", "Asia/Nicosia"),
        )

        for city, country_code, timezone in examples:

            with self.subTest(city=city):

                request.return_value = [
                    _place(city, country_code, 50.0, 10.0, timezone)
                ]
                result = resolve_origin(city)

                self.assertEqual(result.country_code, country_code)
                self.assertEqual(result.timezone, timezone)

        request.return_value = [
            _place("Example", "US", 50.0, 10.0, "Europe/Berlin")
        ]

        with self.assertRaises(OriginNotFoundError):

            resolve_origin("Example")


    @patch("backend.transitous._request_place_matches")
    def test_network_failure_is_distinct(self, request):

        request.side_effect = TransitousError("network unavailable")

        with self.assertRaises(TransitousError):

            resolve_origin("Berlin")


def _place(name, country, lat, lon, timezone):

    return {
        "type": "PLACE",
        "name": name,
        "country": country,
        "lat": lat,
        "lon": lon,
        "tz": timezone,
        "areas": [],
    }


if __name__ == "__main__":

    unittest.main(verbosity=2)
