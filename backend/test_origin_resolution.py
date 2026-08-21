import unittest
from unittest.mock import Mock, patch

from backend.transitous import (
    AmbiguousOriginError,
    OriginNotFoundError,
    OriginMetadata,
    OriginMetadataUnavailableError,
    OriginResolutionError,
    ResolvedOrigin,
    TransitousError,
    _normalized_city_name,
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
    "Praha": ("Praha", "CZ", "Europe/Prague"),
    "G\u00f6teborg": ("G\u00f6teborg", "SE", "Europe/Stockholm"),
    "M\u00fcnchen": ("M\u00fcnchen", "DE", "Europe/Berlin"),
    "K\u00f6ln": ("K\u00f6ln", "DE", "Europe/Berlin"),
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

    @patch("backend.transitous._request_place_matches")
    def test_selected_candidate_missing_both_fields_is_enriched(self, request):
        krakow = _place(
            "Kraków",
            None,
            50.0619474,
            19.9368564,
            None,
        )
        request.return_value = [krakow]
        enricher = Mock(
            return_value=OriginMetadata("PL", "Europe/Warsaw")
        )

        result = resolve_origin("Krakow", enricher)

        self.assertEqual(result.name, "Kraków")
        self.assertEqual(result.country, "PL")
        self.assertEqual(result.country_code, "PL")
        self.assertEqual(result.timezone, "Europe/Warsaw")
        self.assertEqual((result.lat, result.lon), (50.0619474, 19.9368564))
        enricher.assert_called_once()

    @patch("backend.transitous._request_place_matches")
    def test_missing_country_preserves_transitous_timezone_and_area_name(
        self,
        request,
    ):
        match = _place(
            "Example",
            None,
            50.0,
            10.0,
            "Europe/Berlin",
        )
        match["areas"] = [{"name": "Germany", "adminLevel": 2}]
        request.return_value = [match]
        enricher = Mock(
            return_value=OriginMetadata("DE", "Europe/Paris")
        )

        result = resolve_origin("Example", enricher)

        self.assertEqual(result.country, "Germany")
        self.assertEqual(result.country_code, "DE")
        self.assertEqual(result.timezone, "Europe/Berlin")

    @patch("backend.transitous._request_place_matches")
    def test_missing_timezone_preserves_transitous_country(self, request):
        request.return_value = [
            _place("Example", "DE", 50.0, 10.0, None)
        ]
        enricher = Mock(
            return_value=OriginMetadata("FR", "Europe/Berlin")
        )

        result = resolve_origin("Example", enricher)

        self.assertEqual(result.country_code, "DE")
        self.assertEqual(result.timezone, "Europe/Berlin")

    @patch("backend.transitous._request_place_matches")
    def test_complete_candidate_does_not_call_enricher(self, request):
        request.return_value = [
            _place("Example", "DE", 50.0, 10.0, "Europe/Berlin")
        ]
        enricher = Mock()

        result = resolve_origin("Example", enricher)

        self.assertEqual(result.country_code, "DE")
        self.assertEqual(result.timezone, "Europe/Berlin")
        enricher.assert_not_called()

    @patch("backend.transitous._request_place_matches")
    def test_incomplete_urban_candidate_participates_in_ambiguity(
        self,
        request,
    ):
        request.return_value = [
            _place("Example", None, 50.0, 10.0, None),
            _place("Example", "FR", 48.0, 2.0, "Europe/Paris"),
        ]

        with self.assertRaises(AmbiguousOriginError):
            resolve_origin("Example", Mock())

    @patch("backend.transitous._request_place_matches")
    def test_only_selected_incomplete_urban_candidate_is_enriched(
        self,
        request,
    ):
        request.return_value = [
            _place("Kraków", None, 50.0619474, 19.9368564, None),
            _place(
                "Krakow",
                "DE",
                54.1241012,
                12.7904098,
                "Europe/Berlin",
                category="hamlet",
            ),
        ]
        enricher = Mock(
            return_value=OriginMetadata("PL", "Europe/Warsaw")
        )

        result = resolve_origin("Krakow", enricher)

        self.assertEqual(result.name, "Kraków")
        self.assertEqual(result.country_code, "PL")
        self.assertEqual(enricher.call_args.args[0].name, "Kraków")
        enricher.assert_called_once()

    @patch("backend.transitous._request_place_matches")
    def test_enrichment_inability_preserves_origin_not_found_contract(
        self,
        request,
    ):
        request.return_value = [
            _place("Kraków", None, 50.0619474, 19.9368564, None)
        ]
        enricher = Mock(
            side_effect=OriginMetadataUnavailableError("no unique metadata")
        )

        with self.assertRaisesRegex(
            OriginNotFoundError,
            "No European city found for 'Krakow'",
        ):
            resolve_origin("Krakow", enricher)

    @patch("backend.transitous._request_place_matches")
    def test_liege_ascii_input_resolves_to_canonical_name(self, request):

        request.return_value = [
            _place("Liège", "BE", 50.8466, 5.5797, "Europe/Brussels")
        ]

        result = resolve_origin("Liege")

        self.assertEqual(result.name, "Liège")
        self.assertEqual(result.country_code, "BE")
        self.assertEqual(result.timezone, "Europe/Brussels")


    @patch("backend.transitous._request_place_matches")
    def test_malmo_ascii_input_resolves_to_canonical_name(self, request):

        request.return_value = [
            _place("Malmö", "SE", 55.6050, 13.0038, "Europe/Stockholm")
        ]

        result = resolve_origin("Malmo")

        self.assertEqual(result.name, "Malmö")
        self.assertEqual(result.country_code, "SE")
        self.assertEqual(result.timezone, "Europe/Stockholm")


    @patch("backend.transitous._request_place_matches")
    def test_kosice_prefers_diacritic_equivalent_urban_candidate(self, request):

        request.return_value = [
            _place("Košice", "SK", 48.7164, 21.2611, "Europe/Bratislava"),
            _place(
                "Kosice",
                "HR",
                45.3050,
                15.9700,
                "Europe/Zagreb",
                category="hamlet",
            ),
        ]

        result = resolve_origin("Kosice")

        self.assertEqual(result.name, "Košice")
        self.assertEqual(result.country_code, "SK")
        self.assertEqual(result.timezone, "Europe/Bratislava")
        self.assertEqual((result.lat, result.lon), (48.7164, 21.2611))


    @patch("backend.transitous._request_place_matches")
    def test_incomplete_urban_match_blocks_lower_level_fallback(self, request):

        krakow = _place(
            "Kraków",
            None,
            50.0619474,
            19.9368564,
            None,
        )
        request.return_value = [
            krakow,
            _place(
                "Krakow",
                "DE",
                54.1241012,
                12.7904098,
                "Europe/Berlin",
                category="hamlet",
            ),
        ]

        with self.assertRaises(OriginNotFoundError):

            resolve_origin("Krakow")


    @patch("backend.transitous._request_place_matches")
    def test_unrelated_incomplete_urban_match_does_not_block_lower_level(
        self,
        request,
    ):

        request.return_value = [
            _place("Different", None, 50.0, 10.0, None),
            _place(
                "Example",
                "DE",
                51.0,
                11.0,
                "Europe/Berlin",
                category="hamlet",
            ),
        ]

        result = resolve_origin("Example")

        self.assertEqual(result.country_code, "DE")
        self.assertEqual((result.lat, result.lon), (51.0, 11.0))


    @patch("backend.transitous._request_place_matches")
    def test_unmatched_qualifier_does_not_block_lower_level(self, request):

        incomplete_urban = _place("Example", "PL", 50.0, 20.0, None)
        incomplete_urban["areas"] = [
            {
                "name": "Poland",
                "adminLevel": 2,
                "matched": True,
            }
        ]
        german_hamlet = _place(
            "Example",
            "DE",
            51.0,
            11.0,
            "Europe/Berlin",
            category="hamlet",
        )
        german_hamlet["areas"] = [
            {
                "name": "Germany",
                "adminLevel": 2,
                "matched": True,
            }
        ]
        request.return_value = [incomplete_urban, german_hamlet]

        result = resolve_origin("Example, Germany")

        self.assertEqual(result.country_code, "DE")
        self.assertEqual((result.lat, result.lon), (51.0, 11.0))


    @patch("backend.transitous._request_place_matches")
    def test_diacritic_equivalent_urban_candidates_remain_ambiguous(
        self,
        request,
    ):

        request.return_value = [
            _place("Malmö", "SE", 55.6050, 13.0038, "Europe/Stockholm"),
            _place("Málmo", "IT", 45.0000, 9.0000, "Europe/Rome", category="town"),
        ]

        with self.assertRaises(AmbiguousOriginError) as error:

            resolve_origin("Malmo")

        self.assertEqual(len(error.exception.candidates), 2)


    def test_name_normalization_removes_combining_diacritics_only(self):

        self.assertEqual(_normalized_city_name("Liège"), "liege")
        self.assertEqual(_normalized_city_name("Malmö"), "malmo")
        self.assertEqual(_normalized_city_name("Košice"), "kosice")
        self.assertEqual(_normalized_city_name("Kraków"), "krakow")
        self.assertNotEqual(_normalized_city_name("Łódź"), "lodz")

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
    def test_default_unique_area_does_not_override_two_urban_matches(
        self,
        request,
    ):

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

        with self.assertRaises(AmbiguousOriginError):

            resolve_origin("Example")


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
    def test_local_name_city_beats_only_lower_level_settlements(self, request):

        examples = (
            ("Praha", "CZ", "SK", "village"),
            ("G\u00f6teborg", "SE", "SE", "hamlet"),
            ("M\u00fcnchen", "DE", "DE", "hamlet"),
            ("K\u00f6ln", "DE", "PL", "hamlet"),
        )

        for city, country_code, lower_country_code, lower_category in examples:

            with self.subTest(city=city):

                request.return_value = [
                    _place(
                        city,
                        country_code,
                        50.0,
                        10.0,
                        "Europe/Berlin",
                    ),
                    _place(
                        city,
                        lower_country_code,
                        51.0,
                        11.0,
                        "Europe/Berlin",
                        category=lower_category,
                    ),
                ]

                result = resolve_origin(city)

                self.assertEqual(result.country_code, country_code)
                self.assertEqual((result.lat, result.lon), (50.0, 10.0))


    @patch("backend.transitous._request_place_matches")
    def test_single_city_or_town_beats_lower_level_settlement(self, request):

        for urban_category in ("city", "town"):

            with self.subTest(category=urban_category):

                request.return_value = [
                    _place(
                        "Example",
                        "DE",
                        50.0,
                        10.0,
                        "Europe/Berlin",
                        category=urban_category,
                    ),
                    _place(
                        "Example",
                        "FR",
                        48.0,
                        2.0,
                        "Europe/Paris",
                        category="village",
                    ),
                ]

                result = resolve_origin("Example")

                self.assertEqual(result.country_code, "DE")


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
