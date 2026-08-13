import json
import unittest
from datetime import date
from io import BytesIO
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from backend.transitous import (
    DEPARTURE_START,
    DRESDEN_ORIGIN,
    DRESDEN_RADIUS_METRES,
    MOTIS_MODES,
    TRANSITOUS_API_URL,
    TRANSITOUS_USER_AGENT,
    ResolvedOrigin,
    _request_departures,
    _request_dresden_departures,
)


class DepartureRequestTests(unittest.TestCase):

    @patch("backend.transitous.urlopen")
    def test_general_request_uses_origin_date_and_radius(self, urlopen):

        urlopen.return_value = BytesIO(
            json.dumps({"stopTimes": []}).encode("utf-8")
        )
        origin = ResolvedOrigin(
            name="Amsterdam",
            country="Netherlands",
            country_code="NL",
            lat=52.3676,
            lon=4.9041,
            timezone="Europe/Amsterdam",
        )

        result = _request_departures(origin, date(2026, 8, 14), 12_000)

        self.assertEqual(result, {"stopTimes": []})
        request = urlopen.call_args.args[0]
        parameters = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(urlparse(request.full_url).path, "/api/v6/stoptimes")
        self.assertEqual(request.full_url.split("?", 1)[0], TRANSITOUS_API_URL)
        self.assertEqual(parameters["center"], ["52.3676,4.9041"])
        self.assertEqual(parameters["time"], ["2026-08-14T18:00:00+02:00"])
        self.assertEqual(parameters["radius"], ["12000"])
        self.assertEqual(parameters["exactRadius"], ["true"])
        self.assertEqual(parameters["window"], [str(6 * 60 * 60 - 1)])
        self.assertEqual(parameters["mode"], [",".join(MOTIS_MODES)])
        self.assertEqual(parameters["arriveBy"], ["false"])
        self.assertEqual(parameters["direction"], ["LATER"])
        self.assertEqual(parameters["fetchStops"], ["true"])
        self.assertEqual(parameters["withAlerts"], ["false"])
        self.assertEqual(parameters["language"], ["en"])
        self.assertEqual(request.get_header("User-agent"), TRANSITOUS_USER_AGENT)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)
        self.assertEqual(DEPARTURE_START.hour, 18)


    @patch("backend.transitous._request_departures")
    def test_dresden_wrapper_uses_existing_origin_and_radius(self, request):

        request.return_value = {"stopTimes": []}
        travel_date = date(2026, 8, 14)

        result = _request_dresden_departures(travel_date)

        self.assertEqual(result, {"stopTimes": []})
        request.assert_called_once_with(
            DRESDEN_ORIGIN,
            travel_date,
            DRESDEN_RADIUS_METRES,
        )


if __name__ == "__main__":

    unittest.main(verbosity=2)
