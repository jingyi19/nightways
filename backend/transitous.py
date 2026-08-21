import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unicodedata import combining as unicode_combining
from unicodedata import normalize as unicode_normalize
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TRANSITOUS_GEOCODE_URL = "https://api.transitous.org/api/v1/geocode"
TRANSITOUS_API_URL = "https://api.transitous.org/api/v6/stoptimes"
TRANSITOUS_USER_AGENT = (
    "Nightways/0.1 (contact: replace-me@example.invalid)"
)

# ISO 3166-1 alpha-2 codes accepted as European Nightways origins.
# Keep this explicit: a place's IANA timezone does not define its continent.
EUROPEAN_COUNTRY_CODES = frozenset(
    """
    AD AL AM AT AX AZ BA BE BG BY CH CY CZ DE DK EE ES FI FO FR
    GB GE GG GI GR HR HU IE IM IS IT JE LI LT LU LV MC MD ME MK
    MT NL NO PL PT RO RS RU SE SI SJ SK SM TR UA VA
    """.split()
)

DRESDEN_CENTER = (51.0504, 13.7373)
DRESDEN_RADIUS_METRES = 8000

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


class OriginResolutionError(ValueError):
    """Base class for city-name resolution errors."""


class OriginNotFoundError(OriginResolutionError):
    """Raised when no usable European city result is available."""


class AmbiguousOriginError(OriginResolutionError):
    """Raised when a city name has multiple plausible European matches."""

    def __init__(self, city_name: str, candidates):
        self.candidates = tuple(candidates)
        super().__init__(f"Multiple European places match {city_name!r}.")


@dataclass(frozen=True, slots=True)
class ResolvedOrigin:
    name: str
    country: str
    country_code: str
    lat: float
    lon: float
    timezone: str


DRESDEN_ORIGIN = ResolvedOrigin(
    name="Dresden",
    country="Germany",
    country_code="DE",
    lat=DRESDEN_CENTER[0],
    lon=DRESDEN_CENTER[1],
    timezone="Europe/Berlin",
)


ORIGIN_LOCALITY_CATEGORIES = frozenset(
    {
        "city",
        "hamlet",
        "locality",
        "municipality",
        "town",
        "village",
    }
)
URBAN_LOCALITY_CATEGORIES = frozenset({"city", "town"})
LOWER_LEVEL_LOCALITY_CATEGORIES = frozenset(
    {"hamlet", "locality", "village"}
)


def resolve_origin(city_name: str) -> ResolvedOrigin:
    """Resolve one unambiguous European city using Transitous PLACE data."""

    if not isinstance(city_name, str):
        raise OriginResolutionError("City name must be text.")

    query = " ".join(city_name.split())
    if not query:
        raise OriginResolutionError("City name cannot be empty.")

    candidates = []
    for match in _request_place_matches(query):
        candidate = _origin_from_match(match)
        if candidate is not None:
            candidates.append((candidate, match))

    if not candidates:
        raise OriginNotFoundError(f"No European city found for {query!r}.")

    normalized_name, query_qualifiers = _normalized_origin_query(query)
    name_candidates = [
        (candidate, match)
        for candidate, match in candidates
        if (
            _normalized_city_name(candidate.name) == normalized_name
            or _represents_default_locality(match, normalized_name)
        )
        and _matches_query_qualifiers(match, query_qualifiers)
    ]

    if not name_candidates:
        raise OriginNotFoundError(f"No European city found for {query!r}.")

    plausible_candidates = name_candidates

    if len(name_candidates) > 1:
        urban_candidates = [
            (candidate, match)
            for candidate, match in name_candidates
            if _is_urban_locality(match)
        ]
        lower_level_candidates = [
            (candidate, match)
            for candidate, match in name_candidates
            if _is_lower_level_locality(match)
        ]
        if len(urban_candidates) > 1:
            plausible_candidates = urban_candidates
        elif (
            len(urban_candidates) == 1
            and len(lower_level_candidates) == len(name_candidates) - 1
        ):
            plausible_candidates = urban_candidates

    if len(plausible_candidates) > 1:
        raise AmbiguousOriginError(
            query,
            [candidate for candidate, _ in plausible_candidates],
        )

    return plausible_candidates[0][0]


def _request_place_matches(city_name: str) -> list[dict]:
    query = urlencode(
        {
            "text": city_name,
            "type": "PLACE",
            "language": "en",
            "numResults": 10,
        }
    )
    request = Request(
        f"{TRANSITOUS_GEOCODE_URL}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": TRANSITOUS_USER_AGENT,
        },
    )

    try:
        with urlopen(request, timeout=60) as response:
            matches = json.load(response)
    except HTTPError as error:
        raise TransitousError(
            "Transitous geocoding returned "
            f"HTTP {error.code}. Please try again later."
        ) from error
    except (URLError, TimeoutError) as error:
        raise TransitousError(
            "Could not reach Transitous geocoding. Please try again later."
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise TransitousError(
            "Transitous geocoding returned an unreadable response."
        ) from error

    if not isinstance(matches, list):
        raise TransitousError(
            "Transitous geocoding returned an unreadable response."
        )

    return matches


def _origin_from_match(match: dict) -> ResolvedOrigin | None:
    if not isinstance(match, dict) or match.get("type") != "PLACE":
        return None

    category = match.get("category")
    if not isinstance(category, str) or not (
        category.startswith("place_")
        or category in ORIGIN_LOCALITY_CATEGORIES
    ):
        return None

    name = match.get("name")
    country_code = match.get("country")
    timezone = match.get("tz")
    lat = match.get("lat")
    lon = match.get("lon")

    if not all(
        isinstance(value, str) and value.strip()
        for value in (name, country_code, timezone)
    ):
        return None

    country_code = country_code.strip().upper()
    if country_code not in EUROPEAN_COUNTRY_CODES:
        return None

    if (
        isinstance(lat, bool)
        or isinstance(lon, bool)
        or not isinstance(lat, (int, float))
        or not isinstance(lon, (int, float))
        or not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        return None

    country = country_code
    for area in match.get("areas") or []:
        if not isinstance(area, dict) or area.get("adminLevel") != 2:
            continue

        area_name = area.get("name")
        if isinstance(area_name, str) and area_name.strip():
            country = area_name.strip()
            break

    return ResolvedOrigin(
        name=name.strip(),
        country=country,
        country_code=country_code,
        lat=float(lat),
        lon=float(lon),
        timezone=timezone.strip(),
    )


def _normalized_city_name(city_name: str) -> str:
    normalized = unicode_normalize("NFKC", city_name)
    decomposed = unicode_normalize("NFD", normalized)
    without_diacritics = "".join(
        character
        for character in decomposed
        if not unicode_combining(character)
    )
    return " ".join(without_diacritics.split()).casefold()


def _normalized_origin_query(city_name: str) -> tuple[str, tuple[str, ...]]:
    parts = tuple(
        normalized
        for part in city_name.split(",")
        if (normalized := _normalized_city_name(part))
    )
    return parts[0], parts[1:]


def _matches_query_qualifiers(
    match: dict,
    query_qualifiers: tuple[str, ...],
) -> bool:
    if not query_qualifiers:
        return True

    matched_areas = {
        _normalized_city_name(area_name)
        for area in match.get("areas") or []
        if isinstance(area, dict)
        and area.get("matched") is True
        and isinstance((area_name := area.get("name")), str)
        and area_name.strip()
    }
    return all(qualifier in matched_areas for qualifier in query_qualifiers)


def _is_urban_locality(match: dict) -> bool:
    category = match.get("category")
    return isinstance(category, str) and (
        category.startswith("place_")
        or category in URBAN_LOCALITY_CATEGORIES
    )


def _is_lower_level_locality(match: dict) -> bool:
    category = match.get("category")
    return (
        isinstance(category, str)
        and category in LOWER_LEVEL_LOCALITY_CATEGORIES
    )


def _represents_default_locality(
    match: dict,
    normalized_city_name: str,
) -> bool:
    category = match.get("category")
    if not isinstance(category, str) or not category.startswith("place_"):
        return False

    for area in match.get("areas") or []:
        if not isinstance(area, dict):
            continue

        area_name = area.get("name")
        if not (
            area.get("default") is True
            and area.get("unique") is True
            and isinstance(area_name, str)
        ):
            continue

        normalized_area_name = _normalized_city_name(area_name)
        if normalized_area_name == normalized_city_name or (
            normalized_area_name.startswith(f"{normalized_city_name} ")
        ):
            return True

    return False


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

    return _build_nightways_response(
        "Dresden",
        travel_date,
        response,
        catalog,
        _is_dresden_stop,
        DRESDEN_ORIGIN.timezone,
    )


def _build_nightways_response(
    origin_name: str,
    travel_date: date,
    response: dict,
    catalog: DestinationCatalog,
    origin_stop_matches,
    origin_timezone: str,
) -> dict:

    target_arrival_date = travel_date + timedelta(days=1)
    trips = {}

    for stop_time in response.get("stopTimes", []):

        trip = _extract_trip(
            stop_time,
            travel_date,
            target_arrival_date,
            catalog,
            origin_stop_matches,
            origin_timezone,
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
        "origin": origin_name,
        "date": travel_date.isoformat(),
        "destination_count": len(destinations),
        "destinations": destinations,
    }


def _request_dresden_departures(travel_date: date) -> dict:

    return _request_departures(
        DRESDEN_ORIGIN,
        travel_date,
        DRESDEN_RADIUS_METRES,
    )


def _request_departures(
    origin: ResolvedOrigin,
    travel_date: date,
    radius_metres: int,
) -> dict:

    try:

        origin_timezone = ZoneInfo(origin.timezone)

    except ZoneInfoNotFoundError as error:

        raise TransitousError(
            f"Unknown origin timezone: {origin.timezone}."
        ) from error

    local_start = datetime.combine(
        travel_date,
        DEPARTURE_START,
        tzinfo=origin_timezone,
    )

    query = urlencode(
        {
            "center": f"{origin.lat},{origin.lon}",
            "time": local_start.isoformat(),
            "arriveBy": "false",
            "direction": "LATER",
            "window": 6 * 60 * 60 - 1,
            "mode": ",".join(MOTIS_MODES),
            "radius": radius_metres,
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
    origin_stop_matches,
    origin_timezone: str,
) -> dict | None:

    qualified_trip = _extract_qualified_trip(
        stop_time,
        travel_date,
        target_arrival_date,
        origin_stop_matches,
        origin_timezone,
    )

    if qualified_trip is None:

        return None


    destinations = defaultdict(list)

    for arrival in qualified_trip.pop("arrivals"):

        arrival_stop = arrival["_source_stop"]
        destination_name = catalog.lookup(arrival_stop)
        destinations[destination_name].append(
            {
                "station": arrival_stop.get("name") or destination_name[0],
                "arrival": arrival["arrival"],
                "lat": arrival["lat"],
                "lon": arrival["lon"],
            }
        )


    qualified_trip["destinations"] = dict(destinations)
    return qualified_trip


def _extract_qualified_trip(
    stop_time: dict,
    travel_date: date,
    target_arrival_date: date,
    origin_stop_matches,
    origin_timezone: str,
) -> dict | None:
    """Return one trip with qualified, but geographically ungrouped, arrivals."""

    trip_id = stop_time.get("tripId")
    motis_mode = stop_time.get("mode")
    origin_stop = stop_time.get("place") or {}

    if not trip_id or motis_mode not in MODE_FOR_FRONTEND:

        return None


    if not origin_stop_matches(origin_stop):

        return None


    if stop_time.get("cancelled") or stop_time.get("tripCancelled"):

        return None


    if stop_time.get("pickupDropoffType") == "NOT_ALLOWED":

        return None


    departure = _local_datetime(
        origin_stop.get("departure")
        or origin_stop.get("scheduledDeparture"),
        origin_stop.get("tz") or origin_timezone,
    )

    if departure is None or departure.date() != travel_date:

        return None


    if not DEPARTURE_START <= departure.time() <= DEPARTURE_END:

        return None


    arrivals = []

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


        if origin_stop_matches(arrival_stop):

            continue


        arrivals.append(
            {
                "station": (
                    arrival_stop.get("name") or "Unknown arrival station"
                ),
                "arrival": arrival.isoformat(timespec="seconds"),
                "lat": arrival_stop.get("lat"),
                "lon": arrival_stop.get("lon"),
                "country_code": _stop_country_code(arrival_stop),
                "_source_stop": arrival_stop,
            }
        )


    if not arrivals:

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
        "arrivals": arrivals,
    }


def _collect_qualified_trips(
    response: dict,
    travel_date: date,
    origin_stop_matches,
    origin_timezone: str,
) -> dict:
    """Collect and deduplicate trips before geographic destination grouping."""

    target_arrival_date = travel_date + timedelta(days=1)
    trips = {}

    for stop_time in response.get("stopTimes", []):

        trip = _extract_qualified_trip(
            stop_time,
            travel_date,
            target_arrival_date,
            origin_stop_matches,
            origin_timezone,
        )

        if trip is None:

            continue


        existing_trip = trips.get(trip["trip_id"])

        if existing_trip is None:

            trips[trip["trip_id"]] = trip

        else:

            _merge_qualified_trip(existing_trip, trip)


    return trips


def _merge_qualified_trip(existing_trip: dict, incoming_trip: dict) -> None:

    if incoming_trip["departure"] < existing_trip["departure"]:

        existing_trip["departure"] = incoming_trip["departure"]


    existing_keys = {
        _qualified_arrival_key(arrival)
        for arrival in existing_trip["arrivals"]
    }

    for arrival in incoming_trip["arrivals"]:

        key = _qualified_arrival_key(arrival)

        if key not in existing_keys:

            existing_trip["arrivals"].append(arrival)
            existing_keys.add(key)


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


def _qualified_arrival_key(arrival: dict):

    return (
        arrival.get("station"),
        arrival.get("arrival"),
        arrival.get("lat"),
        arrival.get("lon"),
    )


def _stop_country_code(stop: dict) -> str | None:

    values = [
        stop.get("countryCode"),
        stop.get("country_code"),
        stop.get("country"),
    ]

    for area in stop.get("areas") or []:

        if isinstance(area, dict):

            values.extend(
                (area.get("countryCode"), area.get("country_code"))
            )


    for value in values:

        if isinstance(value, str):

            normalized = value.strip().upper()

            if len(normalized) == 2 and normalized.isalpha():

                return normalized


    return None


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
