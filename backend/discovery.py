"""Internal station-first discovery for arbitrary Nightways origins.

The public API remains Dresden-only. This module resolves an origin and its
boundary, discovers grouped core-mode stops inside that boundary, and then
requests detailed stoptimes only for those stops.
"""

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.boundaries import OriginBoundary, resolve_origin_boundary
from backend.localities import GiscoLauIndex, build_locality_response
from backend.transitous import (
    DEPARTURE_START,
    DestinationCatalog,
    MOTIS_MODES,
    TRANSITOUS_API_URL,
    TRANSITOUS_USER_AGENT,
    ResolvedOrigin,
    TransitousError,
    _build_nightways_response,
    _collect_qualified_trips,
    resolve_origin,
)


TRANSITOUS_MAP_STOPS_URL = "https://api.transitous.org/api/v6/map/stops"
CORE_CANDIDATE_MODES = (
    "HIGHSPEED_RAIL",
    "LONG_DISTANCE",
    "NIGHT_RAIL",
    "COACH",
)
MAX_CANDIDATE_STOPS = 25
DEPARTURE_WINDOW_SECONDS = 6 * 60 * 60 - 1


class CandidateStopLimitError(TransitousError):
    """Raised before fan-out when a boundary yields too many candidate stops."""

    def __init__(self, candidate_count: int, limit: int):
        self.candidate_count = candidate_count
        self.limit = limit
        super().__init__(
            f"Origin produced {candidate_count} candidate stops; "
            f"the safety limit is {limit}."
        )


@dataclass(frozen=True, slots=True)
class CandidateStop:
    stop_id: str
    name: str
    lat: float
    lon: float
    modes: tuple[str, ...]


def get_nightways_for_origin(
    city_name: str,
    travel_date: date,
    reference_file: Path,
    candidate_stop_limit: int = MAX_CANDIDATE_STOPS,
) -> dict:
    """Build a Nightways response without exposing arbitrary origins yet."""

    origin = resolve_origin(city_name)
    boundary = resolve_origin_boundary(origin)
    response = request_origin_departures(
        origin,
        boundary,
        travel_date,
        candidate_stop_limit,
    )
    catalog = DestinationCatalog(reference_file)

    return _build_nightways_response(
        origin.name,
        travel_date,
        response,
        catalog,
        lambda stop: _stop_is_inside(boundary, stop),
        origin.timezone,
    )


def get_nightways_for_origin_by_locality(
    city_name: str,
    travel_date: date,
    locality_index: GiscoLauIndex | None = None,
    candidate_stop_limit: int = MAX_CANDIDATE_STOPS,
) -> dict:
    """Build the internal GISCO-grouped response for an arbitrary origin.

    A caller can reuse an already-open index. Otherwise the local GeoPackage is
    opened from NIGHTWAYS_GISCO_LAU_PATH for this operation. This function is
    intentionally not wired to the public Dresden API.
    """

    if locality_index is None:
        with GiscoLauIndex.from_environment() as configured_index:
            return get_nightways_for_origin_by_locality(
                city_name,
                travel_date,
                configured_index,
                candidate_stop_limit,
            )

    origin = resolve_origin(city_name)
    boundary = resolve_origin_boundary(origin)
    response = request_origin_departures(
        origin,
        boundary,
        travel_date,
        candidate_stop_limit,
    )
    trips = _collect_qualified_trips(
        response,
        travel_date,
        lambda stop: _stop_is_inside(boundary, stop),
        origin.timezone,
    )

    result = build_locality_response(
        origin.name,
        travel_date,
        trips.values(),
        locality_index,
    )
    return {
        **result,
        "origin_coordinates": {
            "latitude": origin.lat,
            "longitude": origin.lon,
        },
    }


def request_origin_departures(
    origin: ResolvedOrigin,
    boundary: OriginBoundary,
    travel_date: date,
    candidate_stop_limit: int = MAX_CANDIDATE_STOPS,
) -> dict:
    """Request detailed departures for bounded, core-mode candidate stops."""

    if (
        isinstance(candidate_stop_limit, bool)
        or not isinstance(candidate_stop_limit, int)
        or candidate_stop_limit < 1
    ):
        raise ValueError("candidate_stop_limit must be a positive integer")

    candidates = _request_candidate_stops(boundary)
    if len(candidates) > candidate_stop_limit:
        raise CandidateStopLimitError(
            len(candidates),
            candidate_stop_limit,
        )

    stop_times = []
    for candidate in candidates:
        response = _request_stop_departures(
            origin,
            travel_date,
            candidate.stop_id,
        )
        stop_times.extend(response.get("stopTimes", []))

    return {"stopTimes": stop_times}


def _request_candidate_stops(
    boundary: OriginBoundary,
) -> tuple[CandidateStop, ...]:
    south, west, north, east = _boundary_bounds(boundary)
    query = urlencode(
        {
            "min": f"{south},{west}",
            "max": f"{north},{east}",
            "grouped": "true",
            "modes": ",".join(CORE_CANDIDATE_MODES),
            "language": "en",
        }
    )
    payload = _request_json(
        f"{TRANSITOUS_MAP_STOPS_URL}?{query}",
        "Transitous map stops",
    )
    if not isinstance(payload, list):
        raise TransitousError(
            "Transitous map stops returned an unreadable response."
        )

    candidates = []
    seen_stop_ids = set()
    core_modes = set(CORE_CANDIDATE_MODES)

    for stop in payload:
        if not isinstance(stop, dict):
            continue

        stop_id = stop.get("stopId")
        name = stop.get("name")
        modes = stop.get("modes") or []
        lat = stop.get("lat")
        lon = stop.get("lon")

        if (
            not isinstance(stop_id, str)
            or not stop_id.strip()
            or stop_id in seen_stop_ids
            or not isinstance(modes, list)
            or not core_modes.intersection(modes)
            or not _valid_coordinate(lat, lon)
            or not boundary.contains(float(lat), float(lon))
        ):
            continue

        seen_stop_ids.add(stop_id)
        candidates.append(
            CandidateStop(
                stop_id=stop_id,
                name=name.strip() if isinstance(name, str) else stop_id,
                lat=float(lat),
                lon=float(lon),
                modes=tuple(
                    mode for mode in modes if isinstance(mode, str)
                ),
            )
        )

    return tuple(candidates)


def _request_stop_departures(
    origin: ResolvedOrigin,
    travel_date: date,
    stop_id: str,
) -> dict:
    try:
        timezone = ZoneInfo(origin.timezone)
    except ZoneInfoNotFoundError as error:
        raise TransitousError(
            f"Unknown origin timezone: {origin.timezone}."
        ) from error

    local_start = datetime.combine(
        travel_date,
        DEPARTURE_START,
        tzinfo=timezone,
    )
    query = urlencode(
        {
            "stopId": stop_id,
            "time": local_start.isoformat(),
            "arriveBy": "false",
            "direction": "LATER",
            "window": DEPARTURE_WINDOW_SECONDS,
            "mode": ",".join(MOTIS_MODES),
            "fetchStops": "true",
            "withAlerts": "false",
            "language": "en",
        }
    )
    payload = _request_json(
        f"{TRANSITOUS_API_URL}?{query}",
        "Transitous stoptimes",
    )
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("stopTimes"), list)
    ):
        raise TransitousError(
            "Transitous stoptimes returned an unreadable response."
        )

    return payload


def _request_json(url: str, service_name: str):
    request = Request(
        url,
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
            f"{service_name} returned HTTP {error.code}. "
            "Please try again later."
        ) from error
    except (URLError, TimeoutError) as error:
        raise TransitousError(
            f"Could not reach {service_name}. Please try again later."
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise TransitousError(
            f"{service_name} returned an unreadable response."
        ) from error


def _boundary_bounds(
    boundary: OriginBoundary,
) -> tuple[float, float, float, float]:
    positions = [
        position
        for polygon in boundary.geometry.polygons
        for ring in polygon
        for position in ring
    ]
    longitudes = [position[0] for position in positions]
    latitudes = [position[1] for position in positions]
    return (
        min(latitudes),
        min(longitudes),
        max(latitudes),
        max(longitudes),
    )


def _stop_is_inside(boundary: OriginBoundary, stop: dict) -> bool:
    lat = stop.get("lat")
    lon = stop.get("lon")
    return (
        _valid_coordinate(lat, lon)
        and boundary.contains(float(lat), float(lon))
    )


def _valid_coordinate(lat, lon) -> bool:
    return (
        not isinstance(lat, bool)
        and not isinstance(lon, bool)
        and isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )
