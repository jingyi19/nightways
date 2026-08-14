import unittest
from datetime import date
from unittest.mock import patch

from fastapi import HTTPException

from backend.discovery import CandidateStopLimitError
from backend.localities import LocalityDatasetUnavailableError
from backend.main import GISCO_COUNTRY_NAMES, get_nightways
from backend.transitous import OriginNotFoundError


class NightwaysApiTests(unittest.TestCase):

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_dresden_uses_general_pipeline_and_preserves_response(self, pipeline):
        pipeline.return_value = _response("Dresden", destination_count=60)

        result = get_nightways("Dresden", "2026-08-14")

        pipeline.assert_called_once_with("Dresden", date(2026, 8, 14))
        self.assertEqual(result["origin"], "Dresden")
        self.assertEqual(result["destination_count"], 60)
        self.assertEqual(result["destinations"][0]["country"], "Germany")
        self.assertEqual(result["destinations"][0]["country_code"], "DE")
        self.assertEqual(result["destinations"][0]["gisco_id"], "DE_TEST")
        self.assertEqual(result["destinations"][0]["lau_name"], "Teststadt")
        self.assertEqual(result["destinations"][0]["dataset_year"], 2024)

    def test_country_names_preserve_legacy_api_spellings(self):
        self.assertEqual(
            {
                code: GISCO_COUNTRY_NAMES[code]
                for code in ("DE", "FR", "CZ", "NL", "AT")
            },
            {
                "DE": "Germany",
                "FR": "France",
                "CZ": "Czechia",
                "NL": "Netherlands",
                "AT": "Austria",
            },
        )

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_missing_country_name_is_a_gisco_configuration_error(self, pipeline):
        pipeline.return_value = _response("Berlin", destination_count=1)

        with patch.dict("backend.main.GISCO_COUNTRY_NAMES", {}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                get_nightways("Berlin", "2026-08-14")

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "gisco_dataset_invalid")
        self.assertIn("GISCO country code 'DE'", caught.exception.detail["message"])

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_berlin_is_publicly_supported(self, pipeline):
        pipeline.return_value = _response("Berlin", destination_count=120)

        result = get_nightways("Berlin", "2026-08-14")

        pipeline.assert_called_once_with("Berlin", date(2026, 8, 14))
        self.assertEqual(result["origin"], "Berlin")
        self.assertEqual(result["destination_count"], 120)

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_invalid_date_preserves_existing_error(self, pipeline):
        with self.assertRaises(HTTPException) as caught:
            get_nightways("Berlin", "2026-8-14")

        pipeline.assert_not_called()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(
            caught.exception.detail,
            {"message": "Date must use the YYYY-MM-DD format."},
        )

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_non_european_or_unresolved_origin_is_explicit(self, pipeline):
        pipeline.side_effect = OriginNotFoundError(
            "No European city found for 'Toronto'."
        )

        with self.assertRaises(HTTPException) as caught:
            get_nightways("Toronto", "2026-08-14")

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail["code"], "origin_not_found")

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_candidate_stop_limit_is_operationally_unavailable(self, pipeline):
        pipeline.side_effect = CandidateStopLimitError(26, 25)

        with self.assertRaises(HTTPException) as caught:
            get_nightways("Berlin", "2026-08-14")

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail,
            {
                "message": (
                    "Origin produced 26 candidate stops; "
                    "the safety limit is 25."
                ),
                "code": "origin_candidate_limit_exceeded",
                "candidate_count": 26,
                "limit": 25,
            },
        )

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_missing_gisco_dataset_is_operationally_unavailable(self, pipeline):
        pipeline.side_effect = LocalityDatasetUnavailableError(
            "GISCO LAU 2024 GeoPackage not found."
        )

        with self.assertRaises(HTTPException) as caught:
            get_nightways("Berlin", "2026-08-14")

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"],
            "gisco_dataset_unavailable",
        )

    @patch("backend.main.get_nightways_for_origin_by_locality")
    def test_unsupported_destination_country_details_are_preserved(self, pipeline):
        response = _response("Berlin", destination_count=0)
        response["destinations"] = []
        response["unsupported_country_arrival_count"] = 1
        response["unsupported_country_arrivals"] = [
            {
                "status": "unsupported_country",
                "trip_id": "trip-1",
                "station": "London Victoria",
                "arrival": "2026-08-15T08:00:00+01:00",
                "lat": 51.4952,
                "lon": -0.1439,
                "country_code": "GB",
            }
        ]
        pipeline.return_value = response

        result = get_nightways("Berlin", "2026-08-14")

        self.assertEqual(result["unsupported_country_arrival_count"], 1)
        self.assertEqual(
            result["unsupported_country_arrivals"][0]["status"],
            "unsupported_country",
        )


def _response(origin: str, destination_count: int) -> dict:
    return {
        "origin": origin,
        "date": "2026-08-14",
        "qualifying_trip_count": 18,
        "raw_arrival_event_count": 82,
        "destination_count": destination_count,
        "destinations": [
            {
                "gisco_id": "DE_TEST",
                "country_code": "DE",
                "lau_name": "Teststadt",
                "dataset_year": 2024,
                "city": "Teststadt",
                "earliest_arrival": "2026-08-15T06:00:00+02:00",
                "service_count": 1,
                "station_count": 1,
                "services": [
                    {
                        "trip_id": "trip-1",
                        "service": "NJ 1",
                        "operator": "Test operator",
                        "mode": "train",
                        "departure": "2026-08-14T20:00:00+02:00",
                        "stops": [
                            {
                                "station": "Teststadt Hbf",
                                "arrival": "2026-08-15T06:00:00+02:00",
                                "lat": 50.0,
                                "lon": 10.0,
                            }
                        ],
                    }
                ],
            }
        ],
        "unresolved_arrival_count": 0,
        "unresolved_arrivals": [],
        "unsupported_country_arrival_count": 0,
        "unsupported_country_arrivals": [],
    }


if __name__ == "__main__":
    unittest.main(verbosity=2)
