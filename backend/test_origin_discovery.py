import json
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from backend.boundaries import BoundaryGeometry, OriginBoundary
from backend.localities import LocalityDatasetUnavailableError
from backend.discovery import (
    CORE_CANDIDATE_MODES,
    DEPARTURE_WINDOW_SECONDS,
    MAX_CANDIDATE_STOPS,
    TRANSITOUS_MAP_STOPS_URL,
    CandidateStopLimitError,
    _request_candidate_stops,
    get_nightways_for_origin,
    get_nightways_for_origin_by_locality,
    request_origin_departures,
)
from backend.transitous import (
    MOTIS_MODES,
    TRANSITOUS_API_URL,
    TRANSITOUS_USER_AGENT,
    ResolvedOrigin,
)


class CandidateStopRequestTests(unittest.TestCase):

    @patch("backend.discovery.urlopen")
    def test_map_stops_are_core_mode_polygon_filtered_and_deduplicated(
        self,
        urlopen,
    ):
        urlopen.return_value = _response(
            [
                _stop("inside", 51.0, 13.0, ["COACH"]),
                _stop("outside", 53.0, 13.0, ["COACH"]),
                _stop("local", 51.1, 13.1, ["BUS"]),
                _stop("inside", 51.0, 13.0, ["COACH"]),
            ]
        )

        result = _request_candidate_stops(_boundary())

        self.assertEqual([stop.stop_id for stop in result], ["inside"])
        request = urlopen.call_args.args[0]
        parsed = urlparse(request.full_url)
        parameters = parse_qs(parsed.query)
        self.assertEqual(
            request.full_url.split("?", 1)[0],
            TRANSITOUS_MAP_STOPS_URL,
        )
        self.assertEqual(parameters["min"], ["50.0,12.0"])
        self.assertEqual(parameters["max"], ["52.0,14.0"])
        self.assertEqual(parameters["grouped"], ["true"])
        self.assertEqual(
            parameters["modes"],
            [",".join(CORE_CANDIDATE_MODES)],
        )
        self.assertEqual(parameters["language"], ["en"])
        self.assertEqual(
            request.get_header("User-agent"),
            TRANSITOUS_USER_AGENT,
        )

    @patch("backend.discovery.urlopen")
    def test_safety_cap_stops_fanout_before_stoptimes_requests(
        self,
        urlopen,
    ):
        urlopen.return_value = _response(
            [
                _stop("one", 51.0, 13.0, ["COACH"]),
                _stop("two", 51.1, 13.1, ["LONG_DISTANCE"]),
            ]
        )

        with self.assertRaises(CandidateStopLimitError) as error:
            request_origin_departures(
                _origin(),
                _boundary(),
                date(2026, 8, 14),
                candidate_stop_limit=1,
            )

        self.assertEqual(error.exception.candidate_count, 2)
        self.assertEqual(error.exception.limit, 1)
        self.assertEqual(urlopen.call_count, 1)

    @patch("backend.discovery.urlopen")
    def test_default_safety_cap_rejects_26_stops_before_fanout(
        self,
        urlopen,
    ):
        urlopen.return_value = _response(
            [
                _stop(f"stop-{index}", 51.0, 13.0, ["COACH"])
                for index in range(26)
            ]
        )

        with self.assertRaises(CandidateStopLimitError) as error:
            request_origin_departures(
                _origin(),
                _boundary(),
                date(2026, 8, 14),
            )

        self.assertEqual(error.exception.candidate_count, 26)
        self.assertEqual(error.exception.limit, MAX_CANDIDATE_STOPS)
        self.assertEqual(urlopen.call_count, 1)

    @patch("backend.discovery.urlopen")
    def test_each_candidate_uses_stop_specific_stoptimes_request(
        self,
        urlopen,
    ):
        urlopen.side_effect = [
            _response([_stop("grouped-stop", 51.0, 13.0, ["COACH"])]),
            _response({"stopTimes": [{"tripId": "trip-1"}]}),
        ]

        result = request_origin_departures(
            _origin(),
            _boundary(),
            date(2026, 8, 14),
        )

        self.assertEqual(result, {"stopTimes": [{"tripId": "trip-1"}]})
        self.assertEqual(MAX_CANDIDATE_STOPS, 25)
        request = urlopen.call_args_list[1].args[0]
        parameters = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(
            request.full_url.split("?", 1)[0],
            TRANSITOUS_API_URL,
        )
        self.assertEqual(parameters["stopId"], ["grouped-stop"])
        self.assertEqual(parameters["time"], ["2026-08-14T18:00:00+02:00"])
        self.assertEqual(parameters["window"], [str(DEPARTURE_WINDOW_SECONDS)])
        self.assertEqual(parameters["mode"], [",".join(MOTIS_MODES)])
        self.assertEqual(parameters["fetchStops"], ["true"])
        self.assertEqual(parameters["withAlerts"], ["false"])


class InternalOriginFlowTests(unittest.TestCase):

    @patch("backend.discovery.resolve_origin")
    @patch("backend.discovery.GiscoLauIndex.from_environment")
    def test_missing_locality_dataset_fails_before_network_discovery(
        self,
        from_environment,
        resolve,
    ):
        from_environment.side_effect = LocalityDatasetUnavailableError(
            "GISCO fixture missing"
        )

        with self.assertRaises(LocalityDatasetUnavailableError):
            get_nightways_for_origin_by_locality(
                "Berlin",
                date(2026, 8, 14),
            )

        resolve.assert_not_called()

    @patch("backend.discovery._build_nightways_response")
    @patch("backend.discovery.request_origin_departures")
    @patch("backend.discovery.resolve_origin_boundary")
    @patch("backend.discovery.resolve_origin")
    def test_internal_flow_resolves_boundary_and_builds_response(
        self,
        resolve,
        resolve_boundary,
        request_departures,
        build_response,
    ):
        origin = _origin()
        boundary = _boundary()
        response = {"stopTimes": []}
        expected = {"origin": "Berlin", "destinations": []}
        resolve.return_value = origin
        resolve_boundary.return_value = boundary
        request_departures.return_value = response
        build_response.return_value = expected

        result = get_nightways_for_origin(
            "  Berlin  ",
            date(2026, 8, 14),
            Path("reference.json"),
        )

        self.assertEqual(result, expected)
        resolve.assert_called_once_with("  Berlin  ")
        resolve_boundary.assert_called_once_with(origin)
        request_departures.assert_called_once_with(
            origin,
            boundary,
            date(2026, 8, 14),
            MAX_CANDIDATE_STOPS,
        )
        arguments = build_response.call_args.args
        self.assertEqual(arguments[:3], ("Berlin", date(2026, 8, 14), response))
        self.assertTrue(arguments[4]({"lat": 51.0, "lon": 13.0}))
        self.assertFalse(arguments[4]({"lat": 53.0, "lon": 13.0}))
        self.assertEqual(arguments[5], "Europe/Berlin")

    @patch("backend.discovery.build_locality_response")
    @patch("backend.discovery._collect_qualified_trips")
    @patch("backend.discovery.request_origin_departures")
    @patch("backend.discovery.resolve_origin_boundary")
    @patch("backend.discovery.resolve_origin")
    def test_internal_locality_flow_uses_qualified_arrivals_and_given_index(
        self,
        resolve,
        resolve_boundary,
        request_departures,
        collect_trips,
        build_response,
    ):
        origin = _origin()
        boundary = _boundary()
        response = {"stopTimes": []}
        trips = {"trip-1": {"trip_id": "trip-1"}}
        locality_index = object()
        expected = {"origin": "Berlin", "destinations": []}
        resolve.return_value = origin
        resolve_boundary.return_value = boundary
        request_departures.return_value = response
        collect_trips.return_value = trips
        build_response.return_value = expected

        result = get_nightways_for_origin_by_locality(
            "Berlin",
            date(2026, 8, 14),
            locality_index,
        )

        self.assertEqual(
            result,
            {
                **expected,
                "origin_coordinates": {
                    "latitude": origin.lat,
                    "longitude": origin.lon,
                },
            },
        )
        self.assertEqual(result["origin"], "Berlin")
        request_departures.assert_called_once_with(
            origin,
            boundary,
            date(2026, 8, 14),
            MAX_CANDIDATE_STOPS,
        )
        collect_arguments = collect_trips.call_args.args
        self.assertEqual(
            collect_arguments[:2],
            (response, date(2026, 8, 14)),
        )
        self.assertTrue(collect_arguments[2]({"lat": 51.0, "lon": 13.0}))
        self.assertFalse(collect_arguments[2]({"lat": 53.0, "lon": 13.0}))
        self.assertEqual(collect_arguments[3], "Europe/Berlin")
        build_arguments = build_response.call_args.args
        self.assertEqual(
            build_arguments[:2],
            ("Berlin", date(2026, 8, 14)),
        )
        self.assertEqual(tuple(build_arguments[2]), tuple(trips.values()))
        self.assertIs(build_arguments[3], locality_index)


def _origin():
    return ResolvedOrigin(
        name="Berlin",
        country="Germany",
        country_code="DE",
        lat=52.52,
        lon=13.405,
        timezone="Europe/Berlin",
    )


def _boundary():
    ring = (
        (12.0, 50.0),
        (14.0, 50.0),
        (14.0, 52.0),
        (12.0, 52.0),
        (12.0, 50.0),
    )
    return OriginBoundary(
        osm_type="relation",
        osm_id=1,
        category="boundary",
        display_name="Test boundary",
        country_code="DE",
        geometry=BoundaryGeometry("Polygon", ((ring,),)),
    )


def _stop(stop_id, lat, lon, modes):
    return {
        "stopId": stop_id,
        "name": stop_id,
        "lat": lat,
        "lon": lon,
        "modes": modes,
    }


def _response(payload):
    return BytesIO(json.dumps(payload).encode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
