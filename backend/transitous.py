import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TRANSITOUS_API_URL = "https://api.transitous.org/api/v6/stoptimes"
TRANSITOUS_USER_AGENT = (
    "Nightways/0.1 (contact: replace-me@example.invalid)"
)

DRESDEN_CENTER = (51.0504, 13.7373)
DRESDEN_RADIUS_METRES = 8000
DRESDEN_TIMEZONE = ZoneInfo("Europe/Berlin")

DEPARTURE_START = time(18, 0)
DEPARTURE_END = time(23, 59, 59)
ARRIVAL_START = time(5, 0)
ARRIVAL_END = time(11, 0)

MOTIS_MODES = (
    "HIGHSPEED_RAIL",
    "LONG_DISTANCE",
    "NIGHT_RAIL",
    "REGIONAL_FAST_RAIL",
    "REGIONAL_RAIL",
    "COACH",
)

MODE_FOR_FRONTEND = {
    "HIGHSPEED_RAIL": "LONG_DISTANCE",
    "LONG_DISTANCE": "LONG_DISTANCE",
    "NIGHT_RAIL": "LONG_DISTANCE",
    "REGIONAL_FAST_RAIL": "REGIONAL_RAIL",
    "REGIONAL_RAIL": "REGIONAL_RAIL",
    "COACH": "COACH",
}

TIMEZONE_COUNTRIES = {
    "Europe/Amsterdam": "Netherlands",
    "Europe/Berlin": "Germany",
    "Europe/Bratislava": "Slovakia",
    "Europe/Brussels": "Belgium",
    "Europe/Bucharest": "Romania",
    "Europe/Budapest": "Hungary",
    "Europe/Copenhagen": "Denmark",
    "Europe/Ljubljana": "Slovenia",
    "Europe/Paris": "France",
    "Europe/Prague": "Czechia",
    "Europe/Rome": "Italy",
    "Europe/Vienna": "Austria",
    "Europe/Warsaw": "Poland",
    "Europe/Zurich": "Switzerland",
}


class TransitousError(RuntimeError):
    """Raised when Transitous cannot supply usable timetable data."""


class DestinationCatalog:
    """Reuses the prototype JSON's existing destination display names."""

    def __init__(self, reference_file: Path):

        self.by_station = defaultdict(set)
        self.by_coordinates = defaultdict(set)
        self.known_stops = []

        try:

            with reference_file.open("r", encoding="utf-8") as data_file:

                reference_data = json.load(data_file)

        except (OSError, json.JSONDecodeError):

            return


        for destination in reference_data.get("destinations", []):

            display_name = (
                destination.get("city", "Unknown destination"),
                destination.get("country", "Unknown country"),
            )

            for service in destination.get("services", []):

                for stop in service.get("stops", []):

                    station = stop.get("station")
                    lat = stop.get("lat")
                    lon = stop.get("lon")

                    if station:

                        self.by_station[station.casefold()].add(display_name)

                    if isinstance(lat, (int, float)) and isinstance(
                        lon,
                        (int, float),
                    ):

                        coordinates = (round(lat, 4), round(lon, 4))

                        self.by_coordinates[coordinates].add(display_name)
                        self.known_stops.append((lat, lon, display_name))


    def lookup(self, stop: dict) -> tuple[str, str]:

        station = str(stop.get("name") or "Unknown destination").strip()
        lat = stop.get("lat")
        lon = stop.get("lon")

        station_matches = self.by_station.get(station.casefold(), set())

        if len(station_matches) == 1:

            return next(iter(station_matches))


        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):

            coordinate_matches = self.by_coordinates.get(
                (round(lat, 4), round(lon, 4)),
                set(),
            )

            if len(coordinate_matches) == 1:

                return next(iter(coordinate_matches))


            nearest = self._nearest_known_stop(lat, lon)

            if nearest is not None:

                return nearest


        country = TIMEZONE_COUNTRIES.get(
            stop.get("tz"),
            "Unknown country",
        )

        return station, country


    def _nearest_known_stop(
        self,
        lat: float,
        lon: float,
    ) -> tuple[str, str] | None:

        nearest_distance = math.inf
        nearest_display_name = None

        for known_lat, known_lon, display_name in self.known_stops:

            distance = _distance_metres(lat, lon, known_lat, known_lon)

            if distance < nearest_distance:

                nearest_distance = distance
                nearest_display_name = display_name


        if nearest_distance <= 750:

            return nearest_display_name


        return None


def get_nightways_for_dresden(
    travel_date: date,
    reference_file: Path,
) -> dict:

    response = _request_dresden_departures(travel_date)
    catalog = DestinationCatalog(reference_file)
    target_arrival_date = travel_date + timedelta(days=1)
    trips = {}

    for stop_time in response.get("stopTimes", []):

        trip = _extract_trip(
            stop_time,
            travel_date,
            target_arrival_date,
            catalog,
        )

        if trip is None:

            continue


        existing_trip = trips.get(trip["trip_id"])

        if existing_trip is None:

            trips[trip["trip_id"]] = trip

        else:

            _merge_trip(existing_trip, trip)


    destinations = _build_destinations(trips.values())

    return {
        "origin": "Dresden",
        "date": travel_date.isoformat(),
        "destination_count": len(destinations),
        "destinations": destinations,
    }


def _request_dresden_departures(travel_date: date) -> dict:

    local_start = datetime.combine(
        travel_date,
        DEPARTURE_START,
        tzinfo=DRESDEN_TIMEZONE,
    )

    query = urlencode(
        {
            "center": f"{DRESDEN_CENTER[0]},{DRESDEN_CENTER[1]}",
            "time": local_start.isoformat(),
            "arriveBy": "false",
            "direction": "LATER",
            "window": 6 * 60 * 60 - 1,
            "mode": ",".join(MOTIS_MODES),
            "radius": DRESDEN_RADIUS_METRES,
            "exactRadius": "true",
            "fetchStops": "true",
            "withAlerts": "false",
            "language": "en",
        }
    )

    request = Request(
        f"{TRANSITOUS_API_URL}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": TRANSITOUS_USER_AGENT,
        },
    )

    try:

        with urlopen(request, timeout=60) as response:

            return json.load(response)

    except HTTPError as error:

        raise TransitousError(
            f"Transitous returned HTTP {error.code}. Please try again later."
        ) from error

    except (URLError, TimeoutError) as error:

        raise TransitousError(
            "Could not reach Transitous. Please try again later."
        ) from error

    except (OSError, json.JSONDecodeError) as error:

        raise TransitousError(
            "Transitous returned an unreadable response."
        ) from error


def _extract_trip(
    stop_time: dict,
    travel_date: date,
    target_arrival_date: date,
    catalog: DestinationCatalog,
) -> dict | None:

    trip_id = stop_time.get("tripId")
    motis_mode = stop_time.get("mode")
    origin_stop = stop_time.get("place") or {}

    if not trip_id or motis_mode not in MODE_FOR_FRONTEND:

        return None


    if not _is_dresden_stop(origin_stop):

        return None


    if stop_time.get("cancelled") or stop_time.get("tripCancelled"):

        return None


    if stop_time.get("pickupDropoffType") == "NOT_ALLOWED":

        return None


    departure = _local_datetime(
        origin_stop.get("departure")
        or origin_stop.get("scheduledDeparture"),
        origin_stop.get("tz") or "Europe/Berlin",
    )

    if departure is None or departure.date() != travel_date:

        return None


    if not DEPARTURE_START <= departure.time() <= DEPARTURE_END:

        return None


    destinations = defaultdict(list)

    for arrival_stop in stop_time.get("nextStops") or []:

        if arrival_stop.get("dropoffType") == "NOT_ALLOWED":

            continue


        if arrival_stop.get("cancelled"):

            continue


        arrival = _local_datetime(
            arrival_stop.get("arrival")
            or arrival_stop.get("scheduledArrival"),
            arrival_stop.get("tz"),
        )

        if arrival is None or arrival.date() != target_arrival_date:

            continue


        if not ARRIVAL_START <= arrival.time() <= ARRIVAL_END:

            continue


        if _is_dresden_stop(arrival_stop):

            continue


        destination_name = catalog.lookup(arrival_stop)
        destinations[destination_name].append(
            {
                "station": arrival_stop.get("name") or destination_name[0],
                "arrival": arrival.isoformat(timespec="seconds"),
                "lat": arrival_stop.get("lat"),
                "lon": arrival_stop.get("lon"),
            }
        )


    if not destinations:

        return None


    service_name = (
        stop_time.get("displayName")
        or stop_time.get("routeShortName")
        or stop_time.get("tripShortName")
        or stop_time.get("headsign")
        or "Unnamed service"
    )

    if (
        "flixbus" in str(stop_time.get("agencyName", "")).casefold()
        and "flixbus" not in service_name.casefold()
    ):

        service_name = f"FlixBus {service_name}"


    return {
        "trip_id": trip_id,
        "service": service_name,
        "operator": stop_time.get("agencyName") or "Unknown operator",
        "mode": MODE_FOR_FRONTEND[motis_mode],
        "departure": departure.isoformat(timespec="seconds"),
        "destinations": dict(destinations),
    }


def _merge_trip(existing_trip: dict, incoming_trip: dict) -> None:

    if incoming_trip["departure"] < existing_trip["departure"]:

        existing_trip["departure"] = incoming_trip["departure"]


    for destination_name, stops in incoming_trip["destinations"].items():

        existing_stops = existing_trip["destinations"].setdefault(
            destination_name,
            [],
        )

        existing_keys = {_stop_key(stop) for stop in existing_stops}

        for stop in stops:

            if _stop_key(stop) not in existing_keys:

                existing_stops.append(stop)
                existing_keys.add(_stop_key(stop))


def _build_destinations(trips) -> list[dict]:

    destination_services = defaultdict(list)

    for trip in trips:

        for destination_name, stops in trip["destinations"].items():

            sorted_stops = sorted(stops, key=lambda stop: stop["arrival"])

            destination_services[destination_name].append(
                {
                    "trip_id": trip["trip_id"],
                    "service": trip["service"],
                    "operator": trip["operator"],
                    "mode": trip["mode"],
                    "departure": trip["departure"],
                    "stops": sorted_stops,
                }
            )


    destinations = []

    for (city, country), services in destination_services.items():

        services.sort(key=lambda service: service["departure"])
        all_stops = [
            stop
            for service in services
            for stop in service["stops"]
        ]
        earliest_arrival = min(
            all_stops,
            key=lambda stop: _parse_datetime(stop["arrival"]),
        )["arrival"]
        station_count = len({_station_key(stop) for stop in all_stops})

        destinations.append(
            {
                "city": city,
                "country": country,
                "earliest_arrival": earliest_arrival,
                "service_count": len(services),
                "station_count": station_count,
                "services": services,
            }
        )


    destinations.sort(
        key=lambda destination: (
            _parse_datetime(destination["earliest_arrival"]),
            destination["city"].casefold(),
        )
    )

    return destinations


def _local_datetime(value: str | None, timezone_name: str | None):

    if not value or not timezone_name:

        return None


    try:

        timezone = ZoneInfo(timezone_name)

    except ZoneInfoNotFoundError:

        return None


    try:

        parsed_value = _parse_datetime(value)

    except ValueError:

        return None


    if parsed_value.tzinfo is None:

        return None


    return parsed_value.astimezone(timezone)


def _parse_datetime(value: str) -> datetime:

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_dresden_stop(stop: dict) -> bool:

    return "dresden" in str(stop.get("name", "")).casefold()


def _stop_key(stop: dict):

    return (
        stop.get("station"),
        stop.get("arrival"),
        stop.get("lat"),
        stop.get("lon"),
    )


def _station_key(stop: dict):

    return (
        stop.get("station"),
        stop.get("lat"),
        stop.get("lon"),
    )


def _distance_metres(
    lat_a: float,
    lon_a: float,
    lat_b: float,
    lon_b: float,
) -> float:

    earth_radius = 6_371_000
    lat_a_radians = math.radians(lat_a)
    lat_b_radians = math.radians(lat_b)
    delta_latitude = math.radians(lat_b - lat_a)
    delta_longitude = math.radians(lon_b - lon_a)

    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(lat_a_radians)
        * math.cos(lat_b_radians)
        * math.sin(delta_longitude / 2) ** 2
    )

    return earth_radius * 2 * math.atan2(
        math.sqrt(haversine),
        math.sqrt(1 - haversine),
    )
