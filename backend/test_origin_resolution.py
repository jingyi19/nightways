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
    "Leipzig": ("Leipzig", "DE", "Europe/Berlin"),
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


    def test_qualified_leipzig(self):

        self.assert_resolves_to(
            "Leipzig, Germany",
            EXPECTED_CITIES["Leipzig"],
        )


    def test_reported_nonsense_input(self):

        with self.assertRaises(OriginNotFoundError):

            resolve_origin("sdgdngfn")


    def test_genuinely_ambiguous_city(self):

        with self.assertRaises(AmbiguousOriginError):

            resolve_origin("Neustadt")


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
    def test_default_unique_locality_resolves_exact_names(self, request):

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
                "default": True,
                "unique": True,
            }
        ]
        request.return_value = [
            capital,
            _place("Example", "FR", 48.0, 2.0, "Europe/Paris"),
        ]

        result = resolve_origin("Example")

        self.assertEqual(result.country_code, "DE")


    @patch("backend.transitous._request_place_matches")
    def test_leipzig_prefers_city_over_pois_and_other_localities(
        self,
        request,
    ):

        city = _place(
            "Leipzig",
            "DE",
            51.3406321,
            12.3747329,
            "Europe/Berlin",
        )
        city["areas"] = [
            {
                "name": "Germany",
                "adminLevel": 2,
            },
            {
                "name": "Leipzig",
                "adminLevel": 6,
                "default": True,
                "unique": True,
            },
        ]
        casino = _place(
            "Leipzig",
            "DE",
            51.3844702,
            12.3160085,
            "Europe/Berlin",
            category="casino_14",
        )
        russian_village = _place(
            "Leipzig",
            "RU",
            53.5683129,
            61.0473018,
            "Asia/Yekaterinburg",
            category="village",
        )
        russian_village["areas"] = [
            {
                "name": "Varna municipal district",
                "adminLevel": 6,
                "default": True,
                "unique": True,
            }
        ]
        request.return_value = [city, casino, russian_village]

        result = resolve_origin("Leipzig")

        self.assertEqual(result.name, "Leipzig")
        self.assertEqual(result.country_code, "DE")
        self.assertEqual((result.lat, result.lon), (51.3406321, 12.3747329))


    @patch("backend.transitous._request_place_matches")
    def test_country_qualifier_uses_matched_area_metadata(self, request):

        germany = _place(
            "Leipzig",
            "DE",
            51.3406321,
            12.3747329,
            "Europe/Berlin",
        )
        germany["areas"] = [
            {
                "name": "Germany",
                "adminLevel": 2,
                "matched": True,
            }
        ]
        france = _place(
            "Leipzig",
            "FR",
            49.1379318,
            6.0543551,
            "Europe/Paris",
        )
        france["areas"] = [
            {
                "name": "France",
                "adminLevel": 2,
                "matched": False,
            }
        ]
        request.return_value = [germany, france]

        result = resolve_origin("Leipzig, Germany")

        self.assertEqual(result.country_code, "DE")


    @patch("backend.transitous._request_place_matches")
    def test_default_unique_area_supports_canonical_local_name(self, request):

        zurich = _place(
            "Zürich",
            "CH",
            47.3744489,
            8.5410422,
            "Europe/Zurich",
        )
        zurich["areas"] = [
            {
                "name": "Zurich",
                "adminLevel": 8,
                "default": True,
                "unique": True,
            }
        ]
        request.return_value = [zurich]

        result = resolve_origin("Zurich")

        self.assertEqual(result.name, "Zürich")
        self.assertEqual(result.country_code, "CH")


    @patch("backend.transitous._request_place_matches")
    def test_primary_city_beats_same_named_villages(self, request):

        city = _place(
            "Cologne",
            "DE",
            50.938361,
            6.959974,
            "Europe/Berlin",
        )
        city["areas"] = [
            {
                "name": "Cologne",
                "adminLevel": 6,
                "default": True,
                "unique": True,
            }
        ]
        village = _place(
            "Cologne",
            "IT",
            45.5815203,
            9.9411725,
            "Europe/Rome",
            category="village",
        )
        village["areas"] = [
            {
                "name": "Cologne",
                "adminLevel": 8,
                "default": True,
                "unique": True,
            }
        ]
        request.return_value = [city, village]

        result = resolve_origin("Cologne")

        self.assertEqual(result.country_code, "DE")


    @patch("backend.transitous._request_place_matches")
    def test_primary_city_beats_qualified_same_country_hamlets(self, request):

        city = _place(
            "Stockholm",
            "SE",
            59.3251172,
            18.0710935,
            "Europe/Stockholm",
        )
        city["areas"] = [
            {
                "name": "Sweden",
                "adminLevel": 2,
                "matched": True,
            },
            {
                "name": "Stockholm Municipality",
                "adminLevel": 7,
                "default": True,
                "unique": True,
            },
        ]
        hamlet = _place(
            "Stockholm",
            "SE",
            60.5640539,
            14.663435,
            "Europe/Stockholm",
            category="hamlet",
        )
        hamlet["areas"] = [
            {
                "name": "Sweden",
                "adminLevel": 2,
                "matched": True,
            },
            {
                "name": "Leksands kommun",
                "adminLevel": 7,
                "default": True,
                "unique": True,
            },
        ]
        request.return_value = [city, hamlet]

        result = resolve_origin("Stockholm, Sweden")

        self.assertEqual(result.country_code, "SE")
        self.assertEqual((result.lat, result.lon), (59.3251172, 18.0710935))


    @patch("backend.transitous._request_place_matches")
    def test_weak_fuzzy_and_non_locality_matches_are_not_accepted(
        self,
        request,
    ):

        request.return_value = [
            _place(
                "Something Else",
                "DE",
                50.0,
                10.0,
                "Europe/Berlin",
            ),
            _place(
                "sdgdngfn",
                "DE",
                50.0,
                10.0,
                "Europe/Berlin",
                category="office_16",
            ),
        ]

        with self.assertRaises(OriginNotFoundError):

            resolve_origin("sdgdngfn")


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


def _place(
    name,
    country,
    lat,
    lon,
    timezone,
    *,
    category="place_6",
):

    return {
        "type": "PLACE",
        "category": category,
        "name": name,
        "country": country,
        "lat": lat,
        "lon": lon,
        "tz": timezone,
        "areas": [],
    }


if __name__ == "__main__":

    unittest.main(verbosity=2)
