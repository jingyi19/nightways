import io
import math
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from fastapi import HTTPException

from backend.main import app
from backend.main import get_origin_suggestions as get_origin_suggestions_api
from backend.transitous import (
    ORIGIN_SUGGESTION_MAX_QUERY_LENGTH,
    ORIGIN_SUGGESTION_RESULT_LIMIT,
    ORIGIN_SUGGESTION_TIMEOUT_SECONDS,
    ORIGIN_SUGGESTION_UPSTREAM_RESULT_LIMIT,
    OriginSuggestionQueryError,
    OriginSuggestionServiceError,
    OriginSuggestionTimeoutError,
    _request_origin_suggestion_matches,
    find_origin_suggestions,
)


class OriginSuggestionQueryTests(unittest.TestCase):

    def test_fewer_than_three_unicode_characters_returns_empty_without_request(self):
        with patch(
            "backend.transitous._request_origin_suggestion_matches"
        ) as request:
            self.assertEqual(find_origin_suggestions("Åß"), [])

        request.assert_not_called()

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_query_whitespace_is_normalized(self, request):
        request.return_value = []

        result = find_origin_suggestions("  New   Town  ")

        self.assertEqual(result, [])
        request.assert_called_once_with("New Town")

    def test_query_length_is_bounded_without_request(self):
        with patch(
            "backend.transitous._request_origin_suggestion_matches"
        ) as request:
            with self.assertRaises(OriginSuggestionQueryError):
                find_origin_suggestions(
                    "x" * (ORIGIN_SUGGESTION_MAX_QUERY_LENGTH + 1)
                )

        request.assert_not_called()


class OriginSuggestionRequestTests(unittest.TestCase):

    @patch("backend.transitous.urlopen")
    def test_request_uses_fixed_geocoding_parameters_and_short_timeout(
        self,
        urlopen,
    ):
        urlopen.return_value = io.BytesIO(b"[]")

        result = _request_origin_suggestion_matches("Pra")

        self.assertEqual(result, [])
        request = urlopen.call_args.args[0]
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(
            query,
            {
                "text": ["Pra"],
                "type": ["PLACE"],
                "language": ["en"],
                "numResults": [
                    str(ORIGIN_SUGGESTION_UPSTREAM_RESULT_LIMIT)
                ],
            },
        )
        self.assertEqual(
            urlopen.call_args.kwargs["timeout"],
            ORIGIN_SUGGESTION_TIMEOUT_SECONDS,
        )
        self.assertGreaterEqual(ORIGIN_SUGGESTION_TIMEOUT_SECONDS, 3)
        self.assertLessEqual(ORIGIN_SUGGESTION_TIMEOUT_SECONDS, 5)
        self.assertGreaterEqual(ORIGIN_SUGGESTION_UPSTREAM_RESULT_LIMIT, 10)
        self.assertLessEqual(ORIGIN_SUGGESTION_UPSTREAM_RESULT_LIMIT, 20)

    @patch("backend.transitous.urlopen")
    def test_http_failure_is_service_failure(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://example.invalid",
            503,
            "unavailable",
            None,
            None,
        )

        with self.assertRaises(OriginSuggestionServiceError):
            _request_origin_suggestion_matches("Pra")

    @patch("backend.transitous.urlopen")
    def test_timeout_is_distinct_from_other_network_failures(self, urlopen):
        for error in (
            TimeoutError("timed out"),
            URLError(TimeoutError("timed out")),
        ):
            with self.subTest(error=type(error).__name__):
                urlopen.side_effect = error

                with self.assertRaises(OriginSuggestionTimeoutError):
                    _request_origin_suggestion_matches("Pra")

    @patch("backend.transitous.urlopen")
    def test_other_network_failure_is_service_failure(self, urlopen):
        urlopen.side_effect = URLError("network unavailable")

        with self.assertRaises(OriginSuggestionServiceError):
            _request_origin_suggestion_matches("Pra")

    @patch("backend.transitous.urlopen")
    def test_malformed_json_is_service_failure(self, urlopen):
        urlopen.return_value = io.BytesIO(b"{not-json")

        with self.assertRaises(OriginSuggestionServiceError):
            _request_origin_suggestion_matches("Pra")

    @patch("backend.transitous.urlopen")
    def test_non_list_json_is_service_failure(self, urlopen):
        urlopen.return_value = io.BytesIO(b"{}")

        with self.assertRaises(OriginSuggestionServiceError):
            _request_origin_suggestion_matches("Pra")


class OriginSuggestionProjectionTests(unittest.TestCase):

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_valid_empty_upstream_result_is_empty(self, request):
        request.return_value = []

        self.assertEqual(find_origin_suggestions("None"), [])

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_all_resolver_locality_categories_are_accepted(self, request):
        categories = (
            "place_6",
            "place_capital_8",
            "city",
            "town",
            "municipality",
            "village",
            "hamlet",
            "locality",
        )
        request.return_value = [
            _place(
                f"Example {index}",
                category=category,
                identity=f"node/[{index}]",
            )
            for index, category in enumerate(categories)
        ]

        result = find_origin_suggestions("Example")

        self.assertEqual(len(result), ORIGIN_SUGGESTION_RESULT_LIMIT)
        self.assertEqual(
            [suggestion["name"] for suggestion in result],
            [f"Example {index}" for index in range(5)],
        )

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_non_place_non_locality_and_non_european_matches_are_rejected(
        self,
        request,
    ):
        wrong_type = _place("Wrong type", identity="node/[1]")
        wrong_type["type"] = "STOP"
        non_locality = _place(
            "Museum",
            category="museum_14",
            identity="node/[2]",
        )
        non_european = _place(
            "Outside",
            country="US",
            identity="node/[3]",
        )
        accepted = _place("Accepted", identity="node/[4]")
        request.return_value = [
            wrong_type,
            non_locality,
            non_european,
            accepted,
        ]

        result = find_origin_suggestions("Example")

        self.assertEqual([item["name"] for item in result], ["Accepted"])

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_missing_country_and_invalid_names_are_rejected(self, request):
        missing_country = _place("Missing country", identity="node/[1]")
        missing_country["country"] = None
        missing_name = _place("Missing name", identity="node/[2]")
        missing_name.pop("name")
        blank_name = _place("Blank name", identity="node/[3]")
        blank_name["name"] = "   "
        valid = _place("Valid", identity="node/[4]")
        request.return_value = [
            missing_country,
            missing_name,
            blank_name,
            valid,
        ]

        result = find_origin_suggestions("Example")

        self.assertEqual([item["name"] for item in result], ["Valid"])

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_invalid_coordinates_are_rejected(self, request):
        invalid_values = (
            (True, 10.0),
            (50.0, False),
            (None, 10.0),
            (50.0, "10"),
            (math.nan, 10.0),
            (50.0, math.inf),
            (91.0, 10.0),
            (50.0, 181.0),
        )
        matches = []
        for index, (lat, lon) in enumerate(invalid_values):
            match = _place(
                f"Invalid {index}",
                identity=f"node/[{index}]",
            )
            match["lat"] = lat
            match["lon"] = lon
            matches.append(match)
        matches.append(_place("Valid", identity="node/[valid]"))
        request.return_value = matches

        result = find_origin_suggestions("Example")

        self.assertEqual([item["name"] for item in result], ["Valid"])

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_filtering_happens_before_five_result_limit(self, request):
        rejected = [
            _place(
                f"Rejected {index}",
                category="office_16",
                identity=f"node/[rejected-{index}]",
            )
            for index in range(6)
        ]
        accepted = [
            _place(
                f"Accepted {index}",
                identity=f"node/[accepted-{index}]",
            )
            for index in range(6)
        ]
        request.return_value = rejected + accepted

        result = find_origin_suggestions("Example")

        self.assertEqual(len(result), ORIGIN_SUGGESTION_RESULT_LIMIT)
        self.assertEqual(
            [item["name"] for item in result],
            [f"Accepted {index}" for index in range(5)],
        )

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_repeated_opaque_identity_is_deduplicated_response_locally(
        self,
        request,
    ):
        first = _place("First", identity="node/[same]")
        duplicate = _place("Changed duplicate", identity="node/[same]")
        distinct = _place("Distinct", identity="node/[other]")
        request.return_value = [first, duplicate, distinct]

        result = find_origin_suggestions("Example")

        self.assertEqual(
            [item["name"] for item in result],
            ["First", "Distinct"],
        )

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_distinct_same_name_and_country_identities_are_preserved(
        self,
        request,
    ):
        northern = _place(
            "Example",
            identity="node/[north]",
            region="North Region",
        )
        southern = _place(
            "Example",
            identity="node/[south]",
            region="South Region",
        )
        request.return_value = [northern, southern]

        result = find_origin_suggestions("Example")

        self.assertEqual(len(result), 2)
        self.assertEqual(
            {item["value"] for item in result},
            {"Example, North Region", "Example, South Region"},
        )

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_public_projection_exposes_only_nightways_schema(self, request):
        request.return_value = [
            _place(
                "Example",
                identity="node/[1]",
                region="Example Region",
                extra={
                    "score": -19.2,
                    "tokens": [[0, 3]],
                    "zip": "12345",
                    "providerSecret": "not-public",
                },
            )
        ]

        result = find_origin_suggestions("Example")

        self.assertEqual(
            result,
            [
                {
                    "label": "Example — Example Region, Testland",
                    "value": "Example, Testland",
                    "name": "Example",
                    "region": "Example Region",
                    "country": "Testland",
                    "country_code": "DE",
                }
            ],
        )
        self.assertEqual(
            set(result[0]),
            {
                "label",
                "value",
                "name",
                "region",
                "country",
                "country_code",
            },
        )

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_comma_containing_area_is_not_used_as_value_qualifier(self, request):
        request.return_value = [
            _place(
                "Example",
                identity="node/[1]",
                country_name="Testland, Europe",
                region="North, Central",
            )
        ]

        result = find_origin_suggestions("Example")

        self.assertEqual(result[0]["value"], "Example")

    @patch("backend.transitous._request_origin_suggestion_matches")
    def test_country_code_fallback_is_not_used_as_a_query_qualifier(
        self,
        request,
    ):
        match = _place("Example", identity="node/[1]")
        match["areas"] = []
        request.return_value = [match]

        result = find_origin_suggestions("Example")

        self.assertEqual(result[0]["country"], "DE")
        self.assertEqual(result[0]["country_code"], "DE")
        self.assertEqual(result[0]["value"], "Example")


class OriginSuggestionApiTests(unittest.TestCase):

    def test_get_route_is_registered(self):
        route = next(
            route
            for route in app.routes
            if route.path == "/api/origin-suggestions"
        )

        self.assertIn("GET", route.methods)

    @patch("backend.main.find_origin_suggestions")
    def test_endpoint_wraps_stable_suggestion_schema(self, suggestions):
        suggestions.return_value = [
            {
                "label": "Example — Testland",
                "value": "Example, Testland",
                "name": "Example",
                "region": None,
                "country": "Testland",
                "country_code": "DE",
            }
        ]

        result = get_origin_suggestions_api("Example")

        self.assertEqual(result, {"suggestions": suggestions.return_value})
        suggestions.assert_called_once_with("Example")

    @patch("backend.main.find_origin_suggestions")
    def test_invalid_query_has_stable_api_error(self, suggestions):
        suggestions.side_effect = OriginSuggestionQueryError(
            "Origin suggestion query is too long."
        )

        with self.assertRaises(HTTPException) as caught:
            get_origin_suggestions_api("x" * 101)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(
            caught.exception.detail,
            {
                "message": "Origin suggestion query is too long.",
                "code": "origin_suggestions_invalid_query",
            },
        )

    @patch("backend.main.find_origin_suggestions")
    def test_timeout_has_stable_api_error(self, suggestions):
        suggestions.side_effect = OriginSuggestionTimeoutError(
            "Transitous origin suggestions timed out."
        )

        with self.assertRaises(HTTPException) as caught:
            get_origin_suggestions_api("Example")

        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(
            caught.exception.detail,
            {
                "message": "Transitous origin suggestions timed out.",
                "code": "origin_suggestions_timeout",
            },
        )

    @patch("backend.main.find_origin_suggestions")
    def test_service_failure_has_stable_api_error(self, suggestions):
        suggestions.side_effect = OriginSuggestionServiceError(
            "Transitous origin suggestions failed."
        )

        with self.assertRaises(HTTPException) as caught:
            get_origin_suggestions_api("Example")

        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(
            caught.exception.detail,
            {
                "message": "Transitous origin suggestions failed.",
                "code": "origin_suggestions_failed",
            },
        )


def _place(
    name,
    *,
    category="place_6",
    country="DE",
    country_name="Testland",
    identity=None,
    lat=50.0,
    lon=10.0,
    region=None,
    extra=None,
):
    areas = [
        {
            "name": country_name,
            "adminLevel": 2,
            "matched": False,
            "unique": False,
            "default": False,
        }
    ]
    if region is not None:
        areas.append(
            {
                "name": region,
                "adminLevel": 4,
                "matched": False,
                "unique": False,
                "default": False,
            }
        )

    match = {
        "type": "PLACE",
        "category": category,
        "name": name,
        "country": country,
        "lat": lat,
        "lon": lon,
        "tz": "Europe/Berlin",
        "areas": areas,
    }
    if identity is not None:
        match["id"] = identity
    if extra:
        match.update(extra)
    return match


if __name__ == "__main__":
    unittest.main(verbosity=2)
