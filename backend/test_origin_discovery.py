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
    MAX_FALLBACK_BATCH_RADIUS_METRES,
    MAX_FALLBACK_MEMBER_RADIUS_METRES,
    TRANSITOUS_MAP_STOPS_URL,
    CandidateBatch,
    CandidateBatchCenter,
    CandidateStop,
    CandidateStopLimitError,
    DenseOriginResponseLimitError,
    _candidate_distance_metres,
    _dense_boarding_stop_is_eligible,
    _plan_candidate_batches,
    _request_candidate_stops,
    _request_dense_stop_departures,
    _stop_is_inside,
    _validate_candidate_batches,
    get_nightways_for_origin,
    get_nightways_for_origin_by_locality,
    request_origin_departures,
)
from backend.transitous import (
    MOTIS_MODES,
    TRANSITOUS_API_URL,
    TRANSITOUS_USER_AGENT,
    ResolvedOrigin,
    TransitousError,
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
    def test_more_than_125_candidates_stops_before_fanout(self, urlopen):
        urlopen.return_value = _response(
            [
                _stop(
                    f"dense-{index:03d}",
                    51.0 + index * 0.000001,
                    13.0,
                    ["COACH"],
                )
                for index in range(126)
            ]
        )

        with self.assertRaises(CandidateStopLimitError) as error:
            request_origin_departures(
                _origin(),
                _boundary(),
                date(2026, 8, 14),
            )

        self.assertEqual(error.exception.candidate_count, 126)
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
    def test_normal_sized_origin_keeps_stop_specific_requests(self, urlopen):
        candidates = [
            _stop("dresden-hbf", 51.0403, 13.7327, ["LONG_DISTANCE"]),
            _stop("dresden-neustadt", 51.0658, 13.7408, ["NIGHT_RAIL"]),
            _stop("dresden-coach", 51.0444, 13.7309, ["COACH"]),
        ]
        urlopen.side_effect = [
            _response(candidates),
            *(
                _response({"stopTimes": [{"tripId": stop["stopId"]}]})
                for stop in candidates
            ),
        ]

        result = request_origin_departures(
            ResolvedOrigin(
                name="Dresden",
                country="Germany",
                country_code="DE",
                lat=51.0504,
                lon=13.7373,
                timezone="Europe/Berlin",
            ),
            _boundary(),
            date(2026, 8, 14),
        )

        self.assertEqual(
            [stop_time["tripId"] for stop_time in result["stopTimes"]],
            [stop["stopId"] for stop in candidates],
        )
        self.assertEqual(urlopen.call_count, 1 + len(candidates))
        for call, stop in zip(urlopen.call_args_list[1:], candidates):
            parameters = parse_qs(urlparse(call.args[0].full_url).query)
            self.assertEqual(parameters["stopId"], [stop["stopId"]])
            self.assertNotIn("center", parameters)

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

    def test_frozen_london_candidates_use_bounded_fallback_plan(self):
        candidates = _london_candidates()

        batches = _plan_candidate_batches(candidates)

        self.assertEqual(len(candidates), 85)
        self.assertEqual(
            sum("COACH" in candidate.modes for candidate in candidates),
            71,
        )
        self.assertEqual(
            sum(
                "LONG_DISTANCE" in candidate.modes
                for candidate in candidates
            ),
            13,
        )
        self.assertEqual(
            sum(
                "HIGHSPEED_RAIL" in candidate.modes
                for candidate in candidates
            ),
            2,
        )
        self.assertEqual(
            sum("NIGHT_RAIL" in candidate.modes for candidate in candidates),
            1,
        )
        self.assertTrue(
            all(candidate.parent_id is None for candidate in candidates)
        )
        self.assertEqual(len(batches), 24)
        assigned_ids = [
            member.stop_id
            for batch in batches
            for member in batch.members
        ]
        candidate_ids = {candidate.stop_id for candidate in candidates}
        self.assertEqual(len(assigned_ids), len(set(assigned_ids)))
        self.assertEqual(set(assigned_ids), candidate_ids)
        self.assertEqual(
            max(batch.radius_metres for batch in batches),
            MAX_FALLBACK_BATCH_RADIUS_METRES,
        )
        self.assertTrue(
            any(
                batch.radius_metres > MAX_BATCH_RADIUS_METRES
                for batch in batches
            )
        )
        for batch in batches:
            for member in batch.members:
                self.assertLessEqual(
                    _candidate_distance_metres(batch.center, member),
                    MAX_FALLBACK_MEMBER_RADIUS_METRES,
                )
        self.assertTrue(
            {
                "nl-OpenOV_stoparea:279985",
                "fr-eurostar-gtfs-plan-de-transport-et-temps-reel_"
                "st_pancras_international_station_area",
                "gb-great-britain_910GEUSTON",
                "gb-great-britain_910GKNGX",
                "gb-great-britain_910GSTPX",
                "eu-blablacar-bus_ZEP",
                "gb-great-britain_490G00248G",
                "gb-great-britain_490G00003045",
                "gb-flixbus_9b6a8a0d-3ecb-11ea-8017-02437075395e",
                "gb-flixbus_cab329fd-7882-4cc0-a292-d796d45b7036",
            }.issubset(set(assigned_ids))
        )

    def test_london_fallback_plan_is_independent_of_input_order(self):
        candidates = _london_candidates()

        forward = _batch_signature(_plan_candidate_batches(candidates))
        reversed_plan = _batch_signature(
            _plan_candidate_batches(tuple(reversed(candidates)))
        )

        self.assertEqual(forward, reversed_plan)

    def test_fallback_retains_same_name_parent_and_coach_candidates(self):
        candidates = tuple(
            _candidate(
                candidate.stop_id,
                candidate.lat,
                candidate.lon,
                "shared-parent" if index < 8 else None,
                name="Shared terminal" if index < 12 else candidate.name,
                modes=("COACH",),
            )
            for index, candidate in enumerate(_london_candidates())
        )

        batches = _plan_candidate_batches(candidates)

        assigned_ids = [
            member.stop_id
            for batch in batches
            for member in batch.members
        ]
        self.assertEqual(len(batches), 24)
        self.assertEqual(len(assigned_ids), 85)
        self.assertEqual(len(set(assigned_ids)), 85)
        self.assertEqual(
            set(assigned_ids),
            {candidate.stop_id for candidate in candidates},
        )
        self.assertTrue(
            all(
                member.modes == ("COACH",)
                for batch in batches
                for member in batch.members
            )
        )
        self.assertEqual(
            sum(
                member.parent_id == "shared-parent"
                for batch in batches
                for member in batch.members
            ),
            8,
        )

    def test_paris_style_dense_fixture_keeps_legacy_1500m_plan(self):
        candidates = _paris_shaped_candidates()

        batches = _plan_candidate_batches(candidates)

        self.assertEqual(len(candidates), 30)
        self.assertEqual(len(batches), 6)
        self.assertTrue(
            all(isinstance(batch.center, CandidateStop) for batch in batches)
        )
        self.assertTrue(
            all(
                batch.radius_metres <= MAX_BATCH_RADIUS_METRES
                for batch in batches
            )
        )

    def test_colocated_same_name_parent_candidates_remain_distinct(self):
        candidates = tuple(
            _candidate(
                f"complex-{index:02d}",
                51.0,
                13.0,
                "shared-parent",
                name="Shared terminal",
            )
            for index in range(30)
        )

        batches = _plan_candidate_batches(candidates)

        assigned_ids = [
            member.stop_id
            for batch in batches
            for member in batch.members
        ]
        self.assertEqual(len(batches), 6)
        self.assertEqual(len(assigned_ids), 30)
        self.assertEqual(len(set(assigned_ids)), 30)
        self.assertTrue(
            all(len(batch.members) == MAX_BATCH_CANDIDATES for batch in batches)
        )

    def test_admission_ceiling_scales_with_request_limit(self):
        candidates = tuple(
            _candidate(f"limited-{index:02d}", 51.0, 13.0)
            for index in range(11)
        )

        with self.assertRaises(CandidateStopLimitError) as error:
            _plan_candidate_batches(candidates, request_limit=2)

        self.assertEqual(error.exception.candidate_count, 11)
        self.assertEqual(error.exception.limit, 2)
        self.assertEqual(error.exception.required_request_count, 3)

    def test_fallback_validation_rejects_member_beyond_1975m(self):
        center = CandidateBatchCenter(lat=51.0, lon=13.0)
        member = _candidate("too-far", 51.0179, 13.0)
        distance = _candidate_distance_metres(center, member)
        self.assertGreater(distance, MAX_FALLBACK_MEMBER_RADIUS_METRES)
        self.assertLess(distance, MAX_FALLBACK_BATCH_RADIUS_METRES)

        with self.assertRaises(TransitousError):
            _validate_candidate_batches(
                (member,),
                (
                    CandidateBatch(
                        center=center,
                        members=(member,),
                        radius_metres=MAX_FALLBACK_BATCH_RADIUS_METRES,
                    ),
                ),
            )

    def test_125_candidates_remain_within_admission_ceiling(self):
        candidates = tuple(
            _candidate(
                f"admitted-{index:03d}",
                51.0 + index * 0.000001,
                13.0,
            )
            for index in range(125)
        )

        batches = _plan_candidate_batches(candidates)

        assigned_ids = [
            member.stop_id
            for batch in batches
            for member in batch.members
        ]
        self.assertEqual(len(batches), MAX_CANDIDATE_STOPS)
        self.assertEqual(len(assigned_ids), 125)
        self.assertEqual(len(set(assigned_ids)), 125)
        self.assertLessEqual(
            max(batch.radius_metres for batch in batches),
            MAX_BATCH_RADIUS_METRES,
        )


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

    def test_fallback_fetch_radius_does_not_broaden_stop_acceptance(self):
        fallback_batch = CandidateBatch(
            center=CandidateBatchCenter(lat=51.0, lon=13.0),
            members=(self.candidate,),
            radius_metres=MAX_FALLBACK_BATCH_RADIUS_METRES,
        )
        unrelated = _boarding_stop("unrelated", 51.01, 13.0)

        self.assertFalse(
            _dense_boarding_stop_is_eligible(
                unrelated,
                _boundary(),
                fallback_batch,
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


def _candidate(
    stop_id,
    lat,
    lon,
    parent_id=None,
    *,
    name=None,
    modes=("COACH",),
):
    return CandidateStop(
        stop_id=stop_id,
        name=name or stop_id,
        lat=lat,
        lon=lon,
        modes=modes,
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


def _paris_shaped_candidates():
    candidates = []
    stop_number = 0
    for cluster_number in range(6):
        cluster_lon = 2.20 + cluster_number * 0.04
        for member_number in range(MAX_BATCH_CANDIDATES):
            candidates.append(
                _candidate(
                    f"paris-{stop_number:02d}",
                    48.85 + member_number * 0.0001,
                    cluster_lon,
                )
            )
            stop_number += 1
    return tuple(candidates)


def _london_candidates():
    # Frozen from one controlled Transitous map/stops capture on 2026-09-23.
    # Names are deliberately omitted: batching must depend on identity and
    # geometry, not presentation text.
    rows = (
        ("nl-OpenOV_stoparea:279985", 51.530540466308594, -0.12514999508857727, ("HIGHSPEED_RAIL",), None),
        ("eu-blablacar-bus_ZEP", 51.49251174926758, -0.14834900200366974, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00019793", 51.5435905456543, -0.0042455000802874565, ("COACH", "BUS"), None),
        ("gb-great-britain_900057897", 51.60688018798828, -0.06096100062131882, ("COACH",), None),
        ("gb-great-britain_490G00236U", 51.588470458984375, -0.06085079908370972, ("COACH", "REGIONAL_RAIL", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G000796", 51.519100189208984, -0.05990239977836609, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00019069", 51.50672912597656, -0.10341329872608185, ("COACH",), None),
        ("gb-great-britain_490G00254J", 51.50349044799805, -0.11335650086402893, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00012524", 51.57280731201172, -0.07225339859724045, ("COACH", "BUS"), None),
        ("gb-great-britain_490GA00023", 51.48860168457031, -0.07756079733371735, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00006838", 51.57207107543945, -0.1967816948890686, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00006858", 51.55806350708008, -0.2756325900554657, ("COACH", "BUS"), None),
        ("gb-great-britain_490G01109FF", 51.550254821777344, -0.1827256977558136, ("COACH", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G00082CH", 51.548099517822266, -0.1805008053779602, ("COACH", "SUBWAY", "BUS"), None),
        ("gb-great-britain_910GDRAYGRN", 51.51638412475586, -0.33019429445266724, ("LONG_DISTANCE", "BUS"), None),
        ("gb-great-britain_490G00015440", 51.45890426635742, -0.4680984914302826, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00004355", 51.45867919921875, -0.4604771137237549, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00104A", 51.45924758911133, -0.44682639837265015, ("COACH", "SUBURBAN", "SUBWAY", "BUS"), None),
        ("gb-flixbus_9b6a34b8-3ecb-11ea-8017-02437075395e", 51.47126770019531, -0.4533329904079437, ("COACH",), None),
        ("gb-great-britain_910GHTRWCBS", 51.47124481201172, -0.4534527063369751, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00003383", 51.45841598510742, -0.1962520033121109, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00103G", 51.46697998046875, -0.4233272075653076, ("COACH", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G01221E", 51.51738739013672, -0.1795486956834793, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00014256", 51.4931640625, -0.19960129261016846, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00002311", 51.555633544921875, -0.27063238620758057, ("COACH",), None),
        ("gb-great-britain_910GWEALING", 51.5135383605957, -0.32068929076194763, ("LONG_DISTANCE", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G00016416", 51.57598114013672, -0.21910129487514496, ("COACH", "BUS"), None),
        ("gb-great-britain_910GWDRYTON", 51.509735107421875, -0.47182929515838623, ("LONG_DISTANCE", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G00000392", 51.50081253051758, -0.15025480091571808, ("COACH", "BUS"), None),
        ("gb-great-britain_490G000898", 51.499698638916016, -0.39063191413879395, ("COACH",), None),
        ("gb-great-britain_490G000487", 51.52336502075195, -0.16036880016326904, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00010367", 51.510650634765625, -0.3211173117160797, ("COACH", "BUS"), None),
        ("gb-great-britain_490G16266N", 51.524322509765625, -0.07718110084533691, ("COACH", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G00006411", 51.524322509765625, -0.07716669887304306, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00003045", 51.4573860168457, -0.1962593048810959, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00014011", 51.45850372314453, -0.34070780873298645, ("COACH", "BUS"), None),
        ("gb-great-britain_910GHTRBUS5", 51.472198486328125, -0.4895952045917511, ("COACH", "REGIONAL_RAIL", "SUBURBAN", "BUS"), None),
        ("gb-flixbus_9b6a8a0d-3ecb-11ea-8017-02437075395e", 51.54356384277344, -0.0038040000945329666, ("COACH",), None),
        ("gb-great-britain_490G00146C", 51.525360107421875, -0.033528901636600494, ("COACH", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G00038F", 51.50538635253906, -0.020512400195002556, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00156M", 51.47503662109375, -0.0399153009057045, ("COACH", "REGIONAL_RAIL", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G00009859", 51.54127883911133, -0.07643330097198486, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00016331", 51.590816497802734, -0.05968770012259483, ("COACH", "BUS"), None),
        ("gb-great-britain_490GA00025", 51.48860168457031, -0.07756079733371735, ("COACH", "BUS"), None),
        ("fr-eurostar-gtfs-plan-de-transport-et-temps-reel_st_pancras_international_station_area", 51.53142547607422, -0.126132994890213, ("HIGHSPEED_RAIL", "BUS"), None),
        ("gb-great-britain_490G00119F", 51.50185012817383, -0.15096209943294525, ("COACH", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G00015317", 51.517276763916016, -0.15643419325351715, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00011B", 51.52307891845703, -0.15777109563350677, ("COACH", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490GA00007", 51.52293014526367, -0.1602277010679245, ("COACH", "BUS"), None),
        ("gb-flixbus_8ba1bb5b-593c-4005-9f85-e8f2aefb0f48", 51.51199722290039, -0.15744200348854065, ("COACH",), None),
        ("gb-flixbus_ac6eb6fd-bac5-406f-8af4-1eeb5d08c3d8", 51.54791259765625, -0.18074199557304382, ("COACH",), None),
        ("gb-flixbus_3957a41f-0af3-42a3-8143-cad7c66e0acb", 51.493247985839844, -0.19953900575637817, ("COACH",), None),
        ("gb-flixbus_bdf4f36e-aafb-4635-b0a9-3085cb2d0e52", 51.49054718017578, -0.1962440013885498, ("COACH",), None),
        ("gb-great-britain_490G00087GB", 51.57212448120117, -0.19462400674819946, ("COACH", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G000563", 51.492942810058594, -0.2246755063533783, ("COACH", "BUS"), None),
        ("gb-flixbus_9b6be210-3ecb-11ea-8017-02437075395e", 51.49223327636719, -0.22571499645709991, ("COACH",), None),
        ("gb-great-britain_910GGFORD", 51.542118072509766, -0.34479960799217224, ("LONG_DISTANCE", "SUBWAY", "BUS"), None),
        ("gb-great-britain_910GSGFORD", 51.533203125, -0.3364379107952118, ("LONG_DISTANCE", "BUS"), None),
        ("gb-great-britain_910GHAYESAH", 51.502925872802734, -0.41915640234947205, ("LONG_DISTANCE", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G01144N", 51.503257751464844, -0.42070791125297546, ("COACH", "BUS"), None),
        ("gb-great-britain_490G000645", 51.49496078491211, -0.09861470013856888, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00248G", 51.49592971801758, -0.14439250528812408, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00007760", 51.5087890625, -0.3360537886619568, ("COACH", "BUS"), None),
        ("gb-great-britain_490G01177G", 51.46521759033203, -0.01226009987294674, ("COACH", "REGIONAL_RAIL", "SUBURBAN", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G00009106", 51.46251678466797, -0.009900899603962898, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00015018", 51.4528923034668, 0.034717198461294174, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00009462", 51.55949783325195, -0.1971558928489685, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00009471", 51.551231384277344, -0.17587129771709442, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00009359", 51.53074645996094, -0.16988900303840637, ("COACH", "BUS"), None),
        ("gb-great-britain_910GPADTON", 51.517093658447266, -0.17731699347496033, ("LONG_DISTANCE", "REGIONAL_RAIL", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_940GZZLUMBA", 51.513423919677734, -0.15895339846611023, ("COACH", "SUBWAY", "BUS"), None),
        ("gb-great-britain_940GZZLULVT", 51.517677307128906, -0.0825204998254776, ("COACH", "SUBURBAN", "SUBWAY", "BUS"), None),
        ("gb-great-britain_910GEUSTON", 51.52885437011719, -0.1341909021139145, ("LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_RAIL", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_910GKNGX", 51.53239440917969, -0.1230223998427391, ("LONG_DISTANCE", "REGIONAL_RAIL", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_910GSTPX", 51.532718658447266, -0.12700270116329193, ("LONG_DISTANCE", "REGIONAL_RAIL", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G00009275", 51.501670837402344, -0.11821909993886948, ("COACH",), None),
        ("gb-great-britain_490G02019A", 51.52801513671875, -0.02048810012638569, ("COACH", "BUS"), None),
        ("gb-great-britain_490G00022D", 51.527503967285156, -0.056762900203466415, ("COACH", "SUBURBAN", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G000130", 51.497711181640625, -0.14399810135364532, ("COACH", "BUS"), None),
        ("gb-great-britain_910GEALINGB", 51.51498031616211, -0.30040669441223145, ("LONG_DISTANCE", "COACH", "SUBURBAN", "SUBWAY", "BUS"), None),
        ("gb-great-britain_490G00007939", 51.51560974121094, -0.3022131025791168, ("COACH", "BUS"), None),
        ("gb-great-britain_910GSTHALL", 51.506019592285156, -0.3775014877319336, ("LONG_DISTANCE", "SUBURBAN", "BUS"), None),
        ("gb-great-britain_490G00019182", 51.51582717895508, -0.3732011020183563, ("COACH", "BUS"), None),
        ("gb-great-britain_910GCBARPAR", 51.522865295410156, -0.33150240778923035, ("LONG_DISTANCE", "BUS"), None),
        ("gb-flixbus_cab329fd-7882-4cc0-a292-d796d45b7036", 51.471290588378906, -0.4896239936351776, ("COACH",), None),
    )
    return tuple(
        _candidate(stop_id, lat, lon, parent_id, modes=modes)
        for stop_id, lat, lon, modes, parent_id in rows
    )


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
            getattr(batch.center, "stop_id", None),
            batch.center.lat,
            batch.center.lon,
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
