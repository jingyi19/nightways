import json
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from backend.boundaries import (
    BoundaryGeometry,
    OriginBoundary,
    OriginBoundaryContainment,
)
from backend.localities import LocalityDatasetUnavailableError
from backend.origin_metadata import CoordinateOriginMetadataEnricher
from backend.discovery import (
    BATCH_ASSOCIATION_RADIUS_METRES,
    BATCH_RADIUS_PADDING_METRES,
    CORE_CANDIDATE_MODES,
    DEPARTURE_WINDOW_SECONDS,
    MAX_BATCH_CANDIDATES,
    MAX_BATCH_RADIUS_METRES,
    MAX_CANDIDATE_STOPS,
    TRANSITOUS_MAP_STOPS_URL,
    CandidateBatch,
    CandidateStop,
    CandidateStopLimitError,
    DenseOriginResponseLimitError,
    _candidate_distance_metres,
    _dense_boarding_stop_is_eligible,
    _plan_candidate_batches,
    _request_candidate_stops,
    _request_dense_stop_departures,
    _stop_is_inside,
    get_nightways_for_origin,
    get_nightways_for_origin_by_locality,
    request_origin_departures,
)
from backend.transitous import (
    MOTIS_MODES,
    TRANSITOUS_API_URL,
    TRANSITOUS_USER_AGENT,
    ResolvedOrigin,
    _collect_qualified_trips,
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
    def test_plan_requiring_more_than_25_batches_stops_before_fanout(
        self,
        urlopen,
    ):
        urlopen.return_value = _response(
            [
                _stop(
                    f"stop-{index:02d}",
                    50.1 + (index % 13) * 0.1,
                    12.1 + (index // 13) * 0.8,
                    ["COACH"],
                )
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
        self.assertEqual(error.exception.required_request_count, 26)
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

    @patch("backend.discovery.urlopen")
    def test_dense_requests_use_center_radius_and_exact_radius(self, urlopen):
        candidates = [
            _stop(
                f"dense-{index:02d}",
                51.0 + index * 0.00001,
                13.0,
                ["COACH"],
            )
            for index in range(26)
        ]
        batches = _plan_candidate_batches(
            tuple(_candidate_from_payload(stop) for stop in candidates)
        )
        urlopen.side_effect = [
            _response(candidates),
            *(_response({"stopTimes": []}) for _ in batches),
        ]

        result = request_origin_departures(
            _origin(),
            _boundary(),
            date(2026, 8, 14),
        )

        self.assertEqual(result, {"stopTimes": []})
        self.assertEqual(urlopen.call_count, 1 + len(batches))
        request = urlopen.call_args_list[1].args[0]
        parameters = parse_qs(urlparse(request.full_url).query)
        self.assertNotIn("stopId", parameters)
        self.assertEqual(parameters["center"], ["51.0,13.0"])
        self.assertEqual(
            parameters["radius"],
            [str(batches[0].radius_metres)],
        )
        self.assertEqual(parameters["exactRadius"], ["true"])
        self.assertEqual(parameters["arriveBy"], ["false"])
        self.assertEqual(parameters["direction"], ["LATER"])
        self.assertEqual(parameters["fetchStops"], ["true"])
        self.assertEqual(parameters["mode"], [",".join(MOTIS_MODES)])


class CandidateBatchPlanningTests(unittest.TestCase):

    def test_prague_shaped_fixture_has_complete_bounded_plan(self):
        candidates = _prague_shaped_candidates()

        batches = _plan_candidate_batches(candidates)

        self.assertEqual(len(candidates), 51)
        self.assertEqual(len(batches), 24)
        original_ids = {candidate.stop_id for candidate in candidates}
        assigned_ids = [
            member.stop_id
            for batch in batches
            for member in batch.members
        ]
        self.assertEqual(len(assigned_ids), len(set(assigned_ids)))
        self.assertEqual(set(assigned_ids), original_ids)
        for batch in batches:
            self.assertIn(batch.center.stop_id, original_ids)
            self.assertIn(batch.center, batch.members)
            self.assertLessEqual(len(batch.members), MAX_BATCH_CANDIDATES)
            self.assertLessEqual(
                batch.radius_metres,
                MAX_BATCH_RADIUS_METRES,
            )
            self.assertGreaterEqual(
                batch.radius_metres,
                BATCH_RADIUS_PADDING_METRES,
            )
            for member in batch.members:
                self.assertLessEqual(
                    _candidate_distance_metres(batch.center, member),
                    batch.radius_metres,
                )

    def test_batch_planning_is_independent_of_input_order(self):
        candidates = _prague_shaped_candidates()

        forward = _batch_signature(_plan_candidate_batches(candidates))
        reversed_plan = _batch_signature(
            _plan_candidate_batches(tuple(reversed(candidates)))
        )

        self.assertEqual(forward, reversed_plan)


class DenseBoardingStopTests(unittest.TestCase):

    def setUp(self):
        self.candidate = _candidate("candidate", 51.0, 13.0)
        self.batch = CandidateBatch(
            center=self.candidate,
            members=(self.candidate,),
            radius_metres=1000,
        )

    def test_outside_origin_boundary_is_rejected(self):
        stop = _boarding_stop("candidate", 52.1, 13.0)

        self.assertFalse(
            _dense_boarding_stop_is_eligible(
                stop,
                _boundary(),
                self.batch,
            )
        )

    def test_outside_requested_batch_circle_is_rejected(self):
        stop = _boarding_stop("candidate", 51.02, 13.0)

        self.assertFalse(
            _dense_boarding_stop_is_eligible(
                stop,
                _boundary(),
                self.batch,
            )
        )

    def test_unrelated_stop_inside_circle_is_rejected(self):
        stop = _boarding_stop("unrelated", 51.004, 13.0)

        self.assertFalse(
            _dense_boarding_stop_is_eligible(
                stop,
                _boundary(),
                self.batch,
            )
        )

    def test_exact_candidate_id_is_not_subject_to_association_distance(self):
        stop = _boarding_stop("candidate", 51.004, 13.0)

        self.assertTrue(
            _dense_boarding_stop_is_eligible(
                stop,
                _boundary(),
                self.batch,
            )
        )

    def test_nearby_cross_feed_stop_is_eligible(self):
        stop = _boarding_stop("other-feed", 51.001, 13.0)

        self.assertTrue(
            _dense_boarding_stop_is_eligible(
                stop,
                _boundary(),
                self.batch,
            )
        )
        self.assertEqual(BATCH_ASSOCIATION_RADIUS_METRES, 250)

    def test_child_stop_identity_is_eligible(self):
        stop = {
            **_boarding_stop("platform", 51.004, 13.0),
            "parentId": "candidate",
        }

        self.assertTrue(
            _dense_boarding_stop_is_eligible(
                stop,
                _boundary(),
                self.batch,
            )
        )


class DenseOriginResourceLimitTests(unittest.TestCase):

    @patch("backend.discovery.MAX_DENSE_RESPONSE_BYTES", 32)
    @patch("backend.discovery.urlopen")
    def test_per_response_byte_limit_is_explicit(self, urlopen):
        urlopen.return_value = BytesIO(b"x" * 33)
        batch = CandidateBatch(
            center=_candidate("center", 51.0, 13.0),
            members=(_candidate("center", 51.0, 13.0),),
            radius_metres=25,
        )

        with self.assertRaises(DenseOriginResponseLimitError) as error:
            _request_dense_stop_departures(
                _origin(),
                date(2026, 8, 14),
                batch,
            )

        self.assertEqual(error.exception.resource, "response body bytes")
        self.assertEqual(error.exception.limit, 32)

    @patch("backend.discovery.MAX_DENSE_STOP_TIMES", 1)
    @patch("backend.discovery._request_dense_stop_departures")
    @patch("backend.discovery._request_candidate_stops")
    def test_per_response_event_limit_is_explicit(
        self,
        request_candidates,
        request_dense,
    ):
        request_candidates.return_value = _dense_candidates()
        request_dense.return_value = (
            {"stopTimes": [{"tripId": "one"}, {"tripId": "two"}]},
            100,
        )

        with self.assertRaises(DenseOriginResponseLimitError) as error:
            request_origin_departures(
                _origin(),
                _boundary(),
                date(2026, 8, 14),
            )

        self.assertEqual(error.exception.resource, "stoptimes in one response")
        self.assertEqual(request_dense.call_count, 1)

    @patch("backend.discovery.MAX_DENSE_AGGREGATE_BYTES", 10)
    @patch("backend.discovery._request_dense_stop_departures")
    @patch("backend.discovery._request_candidate_stops")
    def test_aggregate_byte_limit_is_explicit(
        self,
        request_candidates,
        request_dense,
    ):
        request_candidates.return_value = _dense_candidates()
        request_dense.return_value = ({"stopTimes": []}, 6)

        with self.assertRaises(DenseOriginResponseLimitError) as error:
            request_origin_departures(
                _origin(),
                _boundary(),
                date(2026, 8, 14),
            )

        self.assertEqual(error.exception.resource, "aggregate response bytes")
        self.assertEqual(request_dense.call_count, 2)

    @patch("backend.discovery.MAX_DENSE_AGGREGATE_STOP_TIMES", 1)
    @patch("backend.discovery._request_dense_stop_departures")
    @patch("backend.discovery._request_candidate_stops")
    def test_aggregate_event_limit_is_explicit(
        self,
        request_candidates,
        request_dense,
    ):
        request_candidates.return_value = _dense_candidates()
        request_dense.return_value = (
            {"stopTimes": [{"tripId": "one"}]},
            1,
        )

        with self.assertRaises(DenseOriginResponseLimitError) as error:
            request_origin_departures(
                _origin(),
                _boundary(),
                date(2026, 8, 14),
            )

        self.assertEqual(error.exception.resource, "aggregate stoptimes")
        self.assertEqual(request_dense.call_count, 2)


class DenseOriginOverlapTests(unittest.TestCase):

    @patch("backend.discovery._dense_boarding_stop_is_eligible")
    @patch("backend.discovery._request_dense_stop_departures")
    @patch("backend.discovery._request_candidate_stops")
    def test_request_containment_is_shared_across_dense_stages(
        self,
        request_candidates,
        request_dense,
        boarding_stop_is_eligible,
    ):
        boundary = _boundary()
        containment = OriginBoundaryContainment(boundary)
        candidates = _dense_candidates()
        batches = _plan_candidate_batches(candidates)
        request_candidates.return_value = candidates
        request_dense.return_value = (
            {"stopTimes": [{"place": {}}]},
            1,
        )
        boarding_stop_is_eligible.return_value = True

        result = request_origin_departures(
            _origin(),
            boundary,
            date(2026, 8, 14),
            containment=containment,
        )

        request_candidates.assert_called_once_with(boundary, containment)
        self.assertEqual(len(result["stopTimes"]), len(batches))
        self.assertEqual(boarding_stop_is_eligible.call_count, len(batches))
        for boarding_call in boarding_stop_is_eligible.call_args_list:
            self.assertIs(boarding_call.args[3], containment)

    def test_duplicate_trips_from_overlapping_batches_merge_once(self):
        stop_time = _qualifying_stop_time()
        response = {"stopTimes": [stop_time, dict(stop_time)]}

        trips = _collect_qualified_trips(
            response,
            date(2026, 8, 14),
            lambda stop: _stop_is_inside(_boundary(), stop),
            "Europe/Berlin",
        )

        self.assertEqual(tuple(trips), ("trip-1",))
        self.assertEqual(len(trips["trip-1"]["arrivals"]), 1)


class InternalOriginFlowTests(unittest.TestCase):

    @patch("backend.discovery.resolve_origin")
    @patch("backend.discovery.CompositeLocalityIndex.from_environment")
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
        containment = request_departures.call_args.kwargs["containment"]
        request_departures.assert_called_once_with(
            origin,
            boundary,
            date(2026, 8, 14),
            MAX_CANDIDATE_STOPS,
            containment=containment,
        )
        self.assertIsInstance(containment, OriginBoundaryContainment)
        self.assertIs(containment.boundary, boundary)
        arguments = build_response.call_args.args
        self.assertEqual(arguments[:3], ("Berlin", date(2026, 8, 14), response))
        with patch.object(
            OriginBoundaryContainment,
            "contains",
            autospec=True,
            return_value=True,
        ) as contains:
            self.assertTrue(arguments[4]({"lat": 51.0, "lon": 13.0}))
        contains.assert_called_once_with(containment, 51.0, 13.0)
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
        containment = request_departures.call_args.kwargs["containment"]
        request_departures.assert_called_once_with(
            origin,
            boundary,
            date(2026, 8, 14),
            MAX_CANDIDATE_STOPS,
            containment=containment,
        )
        self.assertIsInstance(containment, OriginBoundaryContainment)
        self.assertIs(containment.boundary, boundary)
        collect_arguments = collect_trips.call_args.args
        self.assertEqual(
            collect_arguments[:2],
            (response, date(2026, 8, 14)),
        )
        with patch.object(
            OriginBoundaryContainment,
            "contains",
            autospec=True,
            return_value=True,
        ) as contains:
            self.assertTrue(
                collect_arguments[2]({"lat": 51.0, "lon": 13.0})
            )
        contains.assert_called_once_with(containment, 51.0, 13.0)
        self.assertFalse(collect_arguments[2]({"lat": 53.0, "lon": 13.0}))
        self.assertEqual(collect_arguments[3], "Europe/Berlin")
        build_arguments = build_response.call_args.args
        self.assertEqual(
            build_arguments[:2],
            ("Berlin", date(2026, 8, 14)),
        )
        self.assertEqual(tuple(build_arguments[2]), tuple(trips.values()))
        self.assertIs(build_arguments[3], locality_index)
        resolve_arguments = resolve.call_args.args
        self.assertEqual(resolve_arguments[0], "Berlin")
        self.assertIsInstance(
            resolve_arguments[1],
            CoordinateOriginMetadataEnricher,
        )
        self.assertIs(resolve_arguments[1].locality_index, locality_index)


def _candidate(stop_id, lat, lon, parent_id=None):
    return CandidateStop(
        stop_id=stop_id,
        name=stop_id,
        lat=lat,
        lon=lon,
        modes=("COACH",),
        parent_id=parent_id,
    )


def _candidate_from_payload(stop):
    return _candidate(
        stop["stopId"],
        stop["lat"],
        stop["lon"],
        stop.get("parentId"),
    )


def _prague_shaped_candidates():
    candidates = []
    stop_number = 0
    for cluster_number in range(24):
        row, column = divmod(cluster_number, 6)
        cluster_size = 3 if cluster_number < 3 else 2
        cluster_lat = 50.02 + row * 0.02
        cluster_lon = 14.25 + column * 0.025
        for member_number in range(cluster_size):
            candidates.append(
                _candidate(
                    f"prague-{stop_number:02d}",
                    cluster_lat + member_number * 0.0002,
                    cluster_lon,
                )
            )
            stop_number += 1
    return tuple(candidates)


def _dense_candidates():
    return tuple(
        _candidate(
            f"dense-{index:02d}",
            51.0 + index * 0.00001,
            13.0,
        )
        for index in range(26)
    )


def _batch_signature(batches):
    return tuple(
        (
            batch.center.stop_id,
            tuple(member.stop_id for member in batch.members),
            batch.radius_metres,
        )
        for batch in batches
    )


def _boarding_stop(stop_id, lat, lon):
    return {
        "stopId": stop_id,
        "name": stop_id,
        "lat": lat,
        "lon": lon,
    }


def _qualifying_stop_time():
    return {
        "tripId": "trip-1",
        "mode": "COACH",
        "place": {
            **_boarding_stop("origin", 51.0, 13.0),
            "departure": "2026-08-14T20:00:00+02:00",
            "tz": "Europe/Berlin",
        },
        "nextStops": [
            {
                **_boarding_stop("destination", 48.0, 11.0),
                "arrival": "2026-08-15T07:00:00+02:00",
                "tz": "Europe/Berlin",
            }
        ],
    }


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
