import unittest
from io import BytesIO
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from unittest.mock import call, patch

from backend.boundaries import (
    NOMINATIM_ACCEPT_LANGUAGE,
    NOMINATIM_BASE_URL,
    NOMINATIM_USER_AGENT,
    AmbiguousBoundaryError,
    BoundaryNotFoundError,
    BoundaryServiceError,
    OriginBoundary,
    OriginBoundaryContainment,
    _request_nominatim_matches,
    resolve_origin_boundary,
)
from backend.transitous import ResolvedOrigin


class BoundaryResolutionTests(unittest.TestCase):

    @patch("backend.boundaries._request_nominatim_matches")
    def test_berlin_administrative_multipolygon_is_selected(self, request):
        origin = _origin("Berlin", "DE")
        request.return_value = [
            _boundary_match(
                "Berlin",
                "DE",
                62422,
                _multipolygon(_rectangle(13.1, 52.3, 13.6, 52.7)),
                wikidata="Q64",
            )
        ]

        boundary = resolve_origin_boundary(origin)

        self.assertIsInstance(boundary, OriginBoundary)
        self.assertEqual(boundary.osm_type, "relation")
        self.assertEqual(boundary.osm_id, 62422)
        self.assertEqual(boundary.category, "boundary")
        self.assertEqual(boundary.country_code, "DE")
        self.assertEqual(boundary.geometry.geojson_type, "MultiPolygon")
        self.assertEqual(boundary.wikidata, "Q64")
        request.assert_called_once_with(origin)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_dresden_administrative_polygon_is_selected(self, request):
        request.return_value = [
            _boundary_match(
                "Dresden",
                "DE",
                191645,
                _rectangle(13.6, 50.9, 14.0, 51.2),
            )
        ]

        boundary = resolve_origin_boundary(_origin("Dresden", "DE"))

        self.assertEqual(boundary.osm_id, 191645)
        self.assertEqual(boundary.geometry.geojson_type, "Polygon")

    @patch("backend.boundaries._request_nominatim_matches")
    def test_vienna_localized_name_does_not_affect_selection(self, request):
        result = _boundary_match(
            "Wien",
            "AT",
            109166,
            _rectangle(16.1, 48.1, 16.6, 48.4),
        )
        result["namedetails"] = {"name": "Wien", "name:en": "Vienna"}
        request.return_value = [result]

        boundary = resolve_origin_boundary(_origin("Vienna", "AT"))

        self.assertEqual(boundary.osm_id, 109166)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_prague_localized_name_does_not_affect_selection(self, request):
        result = _boundary_match(
            "Praha",
            "CZ",
            435514,
            _rectangle(14.2, 49.9, 14.8, 50.2),
        )
        result["namedetails"] = {"name": "Praha", "name:en": "Prague"}
        request.return_value = [result]

        boundary = resolve_origin_boundary(_origin("Prague", "CZ"))

        self.assertEqual(boundary.osm_id, 435514)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_london_selects_greater_london_in_result_order(self, request):
        request.return_value = [
            _boundary_match(
                "Greater London",
                "GB",
                175342,
                _rectangle(-0.6, 51.2, 0.4, 51.8),
                wikidata="Q84",
            ),
            _boundary_match(
                "City of London",
                "GB",
                51800,
                _rectangle(-0.12, 51.50, -0.07, 51.53),
                wikidata="Q23311",
            ),
        ]

        boundary = resolve_origin_boundary(_origin("London", "GB"))

        self.assertEqual(boundary.osm_id, 175342)
        self.assertEqual(boundary.display_name, "Greater London")

    @patch("backend.boundaries._request_nominatim_matches")
    def test_initial_oslo_results_use_unique_municipality(self, request):
        origin = _origin("Oslo", "NO")
        request.return_value = [
            _boundary_match(
                "Oslo County",
                "NO",
                406091,
                _rectangle(10.4, 59.8, 11.0, 60.0),
                address_type="county",
            ),
            _boundary_match(
                "Oslo Municipality",
                "NO",
                2775550,
                _rectangle(10.4, 59.8, 11.0, 60.0),
                address_type="municipality",
            ),
        ]

        boundary = resolve_origin_boundary(origin)

        self.assertEqual(boundary.osm_id, 2775550)
        request.assert_called_once_with(origin)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_initial_trondheim_result_uses_unique_municipality(self, request):
        origin = _origin("Trondheim", "NO")
        request.return_value = [
            _boundary_match(
                "Trondheim Municipality",
                "NO",
                10143487,
                _rectangle(10.0, 63.2, 10.8, 63.6),
                address_type="municipality",
            )
        ]

        boundary = resolve_origin_boundary(origin)

        self.assertEqual(boundary.osm_id, 10143487)
        request.assert_called_once_with(origin)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_initial_city_boundary_retains_precedence_over_municipality(
        self,
        request,
    ):
        request.return_value = [
            _boundary_match(
                "Example Municipality",
                "DE",
                200,
                _rectangle(10, 50, 12, 52),
                address_type="municipality",
            ),
            _boundary_match(
                "Example City",
                "DE",
                100,
                _rectangle(10.5, 50.5, 11.5, 51.5),
            ),
        ]

        boundary = resolve_origin_boundary(_origin("Example", "DE"))

        self.assertEqual(boundary.osm_id, 100)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_multiple_initial_municipalities_are_ambiguous(self, request):
        origin = _origin("Example", "DE")
        request.return_value = [
            _boundary_match(
                "Example Municipality One",
                "DE",
                201,
                _rectangle(10, 50, 11, 51),
                address_type="municipality",
            ),
            _boundary_match(
                "Example Municipality Two",
                "DE",
                202,
                _rectangle(11, 50, 12, 51),
                address_type="municipality",
            ),
        ]

        with self.assertRaises(AmbiguousBoundaryError) as error:
            resolve_origin_boundary(origin)

        self.assertEqual(len(error.exception.candidates), 2)
        request.assert_called_once_with(origin)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_initial_municipality_with_wrong_country_is_rejected(self, request):
        request.return_value = [
            _boundary_match(
                "Example Municipality",
                "FR",
                300,
                _rectangle(2, 48, 3, 49),
                address_type="municipality",
            )
        ]

        with self.assertRaises(BoundaryNotFoundError):
            resolve_origin_boundary(_origin("Example", "DE"))

    @patch("backend.boundaries._request_nominatim_matches")
    def test_initial_municipality_without_polygon_is_rejected(self, request):
        request.return_value = [
            _boundary_match(
                "Example Municipality",
                "DE",
                300,
                {"type": "Point", "coordinates": [11, 51]},
                address_type="municipality",
            )
        ]

        with self.assertRaises(BoundaryNotFoundError):
            resolve_origin_boundary(_origin("Example", "DE"))

    @patch("backend.boundaries._request_nominatim_matches")
    def test_copenhagen_point_first_uses_municipality_fallback(self, request):
        origin = _origin("Copenhagen", "DK")
        city_point = _city_point("Copenhagen", "DK", 13707878)
        municipality = _boundary_match(
            "Copenhagen Municipality",
            "DK",
            2192363,
            _multipolygon(_rectangle(12.4, 55.6, 12.8, 55.8)),
            address_type="municipality",
            wikidata="Q504125",
        )
        request.side_effect = [[city_point], [municipality]]

        boundary = resolve_origin_boundary(origin)

        self.assertEqual(boundary.osm_id, 2192363)
        self.assertEqual(boundary.geometry.geojson_type, "MultiPolygon")
        self.assertEqual(
            request.call_args_list,
            [
                call(origin),
                call(
                    origin,
                    excluded_osm_reference="N13707878",
                ),
            ],
        )

    @patch("backend.boundaries._request_nominatim_matches")
    def test_local_city_name_retries_missing_canonical_boundary(self, request):
        origin = _origin("Gothenburg", "SE")
        city_point = _city_point("Gothenburg", "SE", 25930131)
        city_point["namedetails"] = {
            "name": "G\u00f6teborg",
            "name:en": "Gothenburg",
        }
        municipality = _boundary_match(
            "G\u00f6teborgs Stad",
            "SE",
            935611,
            _rectangle(11.7, 57.5, 12.2, 57.9),
            address_type="municipality",
            wikidata="Q52502",
        )
        local_origin = ResolvedOrigin(
            name="G\u00f6teborg",
            country=origin.country,
            country_code=origin.country_code,
            lat=origin.lat,
            lon=origin.lon,
            timezone=origin.timezone,
        )
        request.side_effect = [
            [city_point],
            [],
            [municipality],
        ]

        boundary = resolve_origin_boundary(origin)

        self.assertEqual(boundary.osm_id, 935611)
        self.assertEqual(boundary.country_code, "SE")
        self.assertEqual(boundary.geometry.geojson_type, "Polygon")
        self.assertEqual(
            request.call_args_list,
            [
                call(origin),
                call(
                    origin,
                    excluded_osm_reference="N25930131",
                ),
                call(
                    local_origin,
                    excluded_osm_reference="N25930131",
                ),
            ],
        )

    @patch("backend.boundaries._request_nominatim_matches")
    def test_multiple_fallback_municipalities_are_ambiguous(self, request):
        origin = _origin("Example", "DE")
        request.side_effect = [
            [_city_point("Example", "DE", 100)],
            [
                _boundary_match(
                    "Example Municipality One",
                    "DE",
                    201,
                    _rectangle(10, 50, 11, 51),
                    address_type="municipality",
                ),
                _boundary_match(
                    "Example Municipality Two",
                    "DE",
                    202,
                    _rectangle(11, 50, 12, 51),
                    address_type="municipality",
                ),
            ],
        ]

        with self.assertRaises(AmbiguousBoundaryError) as error:
            resolve_origin_boundary(origin)

        self.assertEqual(len(error.exception.candidates), 2)

    @patch("backend.boundaries._request_nominatim_matches")
    def test_no_polygon_result_fails_conservatively(self, request):
        request.return_value = [
            {
                "category": "place",
                "type": "town",
                "addresstype": "town",
                "address": {"country_code": "de"},
                "geojson": {"type": "Point", "coordinates": [13, 51]},
            }
        ]

        with self.assertRaises(BoundaryNotFoundError):
            resolve_origin_boundary(_origin("Example", "DE"))

        request.assert_called_once()

    @patch("backend.boundaries._request_nominatim_matches")
    def test_wrong_country_boundary_is_rejected(self, request):
        request.return_value = [
            _boundary_match(
                "Example",
                "FR",
                300,
                _rectangle(2, 48, 3, 49),
            )
        ]

        with self.assertRaises(BoundaryNotFoundError):
            resolve_origin_boundary(_origin("Example", "DE"))


class BoundaryRequestTests(unittest.TestCase):

    @patch("backend.boundaries.urlopen")
    def test_request_uses_structured_search_and_stable_exclusion(self, urlopen):
        urlopen.return_value = BytesIO(b"[]")
        origin = _origin("Copenhagen", "DK")

        result = _request_nominatim_matches(
            origin,
            excluded_osm_reference="N13707878",
        )

        self.assertEqual(result, [])
        request = urlopen.call_args.args[0]
        parsed_url = urlparse(request.full_url)
        parameters = parse_qs(parsed_url.query)
        self.assertEqual(
            request.full_url.split("?", 1)[0],
            f"{NOMINATIM_BASE_URL}/search",
        )
        self.assertEqual(parameters["city"], ["Copenhagen"])
        self.assertEqual(parameters["countrycodes"], ["dk"])
        self.assertEqual(parameters["featureType"], ["city"])
        self.assertEqual(parameters["addressdetails"], ["1"])
        self.assertEqual(parameters["extratags"], ["1"])
        self.assertEqual(parameters["namedetails"], ["1"])
        self.assertEqual(parameters["polygon_geojson"], ["1"])
        self.assertEqual(parameters["accept-language"], ["en"])
        self.assertEqual(parameters["exclude_place_ids"], ["N13707878"])
        self.assertEqual(
            request.get_header("User-agent"),
            NOMINATIM_USER_AGENT,
        )
        self.assertEqual(NOMINATIM_USER_AGENT, "Nightways/0.1")
        self.assertEqual(
            request.get_header("Accept-language"),
            NOMINATIM_ACCEPT_LANGUAGE,
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    @patch("backend.boundaries.urlopen")
    def test_nominatim_failure_has_distinct_error_type(self, urlopen):
        urlopen.side_effect = URLError("unavailable")

        with self.assertRaises(BoundaryServiceError):
            _request_nominatim_matches(_origin("Berlin", "DE"))


class BoundaryGeometryTests(unittest.TestCase):

    def test_point_inside_and_outside_polygon(self):
        boundary = _resolved_test_boundary(
            _rectangle(10, 50, 12, 52)
        )

        self.assertTrue(boundary.contains(51, 11))
        self.assertFalse(boundary.contains(53, 11))

    def test_point_inside_multipolygon(self):
        boundary = _resolved_test_boundary(
            _multipolygon(
                _rectangle(0, 0, 1, 1),
                _rectangle(10, 50, 12, 52),
            )
        )

        self.assertTrue(boundary.contains(51, 11))
        self.assertFalse(boundary.contains(25, 5))

    def test_point_inside_polygon_hole_is_outside(self):
        boundary = _resolved_test_boundary(
            {
                "type": "Polygon",
                "coordinates": [
                    _ring(0, 0, 10, 10),
                    _ring(4, 4, 6, 6),
                ],
            }
        )

        self.assertTrue(boundary.contains(2, 2))
        self.assertFalse(boundary.contains(5, 5))

    def test_representative_berlin_boundary(self):
        boundary = _resolved_test_boundary(
            _multipolygon(_rectangle(13.08, 52.33, 13.56, 52.68))
        )

        self.assertTrue(boundary.contains(52.52498, 13.369114))
        self.assertFalse(boundary.contains(52.580883, 13.573316))

    def test_representative_dresden_boundary(self):
        boundary = _resolved_test_boundary(
            _rectangle(13.70, 50.98, 13.90, 51.16)
        )

        self.assertTrue(boundary.contains(51.03993, 13.732932))
        self.assertFalse(boundary.contains(51.098293, 13.680163))


class OriginBoundaryContainmentTests(unittest.TestCase):

    def test_outside_bounding_box_skips_exact_polygon_scan(self):
        boundary = _resolved_test_boundary(_rectangle(10, 50, 12, 52))

        with _exact_contains_spy() as exact_contains:
            containment = OriginBoundaryContainment(boundary)

            self.assertFalse(containment.contains(53, 11))

        exact_contains.assert_not_called()

    def test_inside_bounding_box_uses_exact_polygon_scan(self):
        boundary = _resolved_test_boundary(_rectangle(10, 50, 12, 52))

        with _exact_contains_spy() as exact_contains:
            containment = OriginBoundaryContainment(boundary)

            self.assertTrue(containment.contains(51, 11))

        exact_contains.assert_called_once_with(boundary, 51.0, 11.0)

    def test_bounding_box_edge_preserves_exact_boundary_semantics(self):
        boundary = _resolved_test_boundary(_rectangle(10, 50, 12, 52))

        with _exact_contains_spy() as exact_contains:
            containment = OriginBoundaryContainment(boundary)

            self.assertTrue(containment.contains(50, 11))

        exact_contains.assert_called_once_with(boundary, 50.0, 11.0)

    def test_repeated_exact_coordinate_scans_only_once(self):
        boundary = _resolved_test_boundary(_rectangle(10, 50, 12, 52))

        with _exact_contains_spy() as exact_contains:
            containment = OriginBoundaryContainment(boundary)

            self.assertTrue(containment.contains(51, 11))
            self.assertTrue(containment.contains(51, 11))

        exact_contains.assert_called_once_with(boundary, 51.0, 11.0)

    def test_different_coordinates_do_not_share_cache_entries(self):
        boundary = _resolved_test_boundary(_rectangle(10, 50, 12, 52))

        with _exact_contains_spy() as exact_contains:
            containment = OriginBoundaryContainment(boundary)

            self.assertTrue(containment.contains(51, 11))
            self.assertTrue(containment.contains(51.5, 11.5))

        self.assertEqual(exact_contains.call_count, 2)

    def test_cache_is_local_to_one_containment_instance(self):
        boundary = _resolved_test_boundary(_rectangle(10, 50, 12, 52))

        with _exact_contains_spy() as exact_contains:
            first_request = OriginBoundaryContainment(boundary)
            second_request = OriginBoundaryContainment(boundary)

            self.assertTrue(first_request.contains(51, 11))
            self.assertTrue(first_request.contains(51, 11))
            self.assertTrue(second_request.contains(51, 11))

        self.assertEqual(exact_contains.call_count, 2)

    def test_exact_hole_and_multipolygon_semantics_are_unchanged(self):
        boundary_with_hole = _resolved_test_boundary(
            {
                "type": "Polygon",
                "coordinates": [
                    _ring(0, 0, 10, 10),
                    _ring(4, 4, 6, 6),
                ],
            }
        )
        multipolygon = _resolved_test_boundary(
            _multipolygon(
                _rectangle(0, 0, 1, 1),
                _rectangle(10, 50, 12, 52),
            )
        )

        hole_containment = OriginBoundaryContainment(boundary_with_hole)
        multipolygon_containment = OriginBoundaryContainment(multipolygon)

        self.assertTrue(hole_containment.contains(2, 2))
        self.assertFalse(hole_containment.contains(5, 5))
        self.assertTrue(multipolygon_containment.contains(51, 11))
        self.assertFalse(multipolygon_containment.contains(25, 5))


def _origin(name: str, country_code: str) -> ResolvedOrigin:
    return ResolvedOrigin(
        name=name,
        country=country_code,
        country_code=country_code,
        lat=0,
        lon=0,
        timezone="Europe/Berlin",
    )


def _boundary_match(
    name,
    country_code,
    osm_id,
    geometry,
    address_type="city",
    wikidata=None,
):
    extra_tags = {}
    if wikidata is not None:
        extra_tags["wikidata"] = wikidata

    return {
        "category": "boundary",
        "type": "administrative",
        "addresstype": address_type,
        "display_name": name,
        "name": name,
        "osm_type": "relation",
        "osm_id": osm_id,
        "address": {"country_code": country_code.lower()},
        "extratags": extra_tags,
        "geojson": geometry,
    }


def _city_point(name, country_code, osm_id):
    return {
        "category": "place",
        "type": "city",
        "addresstype": "city",
        "display_name": name,
        "name": name,
        "osm_type": "node",
        "osm_id": osm_id,
        "address": {"country_code": country_code.lower()},
        "geojson": {"type": "Point", "coordinates": [12, 55]},
    }


def _rectangle(west, south, east, north):
    return {
        "type": "Polygon",
        "coordinates": [_ring(west, south, east, north)],
    }


def _ring(west, south, east, north):
    return [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]


def _multipolygon(*polygons):
    return {
        "type": "MultiPolygon",
        "coordinates": [polygon["coordinates"] for polygon in polygons],
    }


def _resolved_test_boundary(geometry):
    with patch(
        "backend.boundaries._request_nominatim_matches",
        return_value=[
            _boundary_match("Example", "DE", 1, geometry)
        ],
    ):
        return resolve_origin_boundary(_origin("Example", "DE"))


def _exact_contains_spy():
    exact_contains = OriginBoundary.contains
    return patch.object(
        OriginBoundary,
        "contains",
        autospec=True,
        side_effect=lambda boundary, lat, lon: exact_contains(
            boundary,
            lat,
            lon,
        ),
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
