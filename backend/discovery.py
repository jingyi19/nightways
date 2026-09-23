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

from backend.boundaries import (
    OriginBoundary,
    OriginBoundaryContainment,
    resolve_origin_boundary,
)
from backend.gb_localities import CompositeLocalityIndex
from backend.localities import LocalityIndex, build_locality_response
from backend.origin_metadata import CoordinateOriginMetadataEnricher
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
MAX_BATCH_CANDIDATES = 5
MAX_BATCH_RADIUS_METRES = 1500
MAX_FALLBACK_BATCH_RADIUS_METRES = 2000
BATCH_RADIUS_PADDING_METRES = 25
MAX_FALLBACK_MEMBER_RADIUS_METRES = (
    MAX_FALLBACK_BATCH_RADIUS_METRES - BATCH_RADIUS_PADDING_METRES
)
BATCH_ASSOCIATION_RADIUS_METRES = 250
MAX_DENSE_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_DENSE_AGGREGATE_BYTES = 50 * 1024 * 1024
MAX_DENSE_STOP_TIMES = 2000
MAX_DENSE_AGGREGATE_STOP_TIMES = 10000
EARTH_RADIUS_METRES = 6_371_008.8


class CandidateStopLimitError(TransitousError):
    """Raised before fan-out when candidate discovery exceeds its budget."""

    def __init__(
        self,
        candidate_count: int,
        limit: int,
        required_request_count: int | None = None,
    ):
        self.candidate_count = candidate_count
        self.limit = limit
        self.required_request_count = required_request_count
        if required_request_count is None:
            message = (
                f"Origin produced {candidate_count} candidate stops; "
                f"the safety limit is {limit}."
            )
        else:
            message = (
                f"Origin's {candidate_count} candidate stops require "
                f"{required_request_count} stoptimes requests; "
                f"the safety limit is {limit}."
            )
        super().__init__(message)


class DenseOriginResponseLimitError(TransitousError):
    """Raised when dense-origin Transitous responses exceed safe bounds."""

    def __init__(self, resource: str, observed: int, limit: int):
        self.resource = resource
        self.observed = observed
        self.limit = limit
        super().__init__(
            f"Dense-origin Transitous {resource} exceeded the safety limit "
            f"of {limit} (received {observed})."
        )


@dataclass(frozen=True, slots=True)
class CandidateStop:
    stop_id: str
    name: str
    lat: float
    lon: float
    modes: tuple[str, ...]
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateBatchCenter:
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    center: CandidateStop | CandidateBatchCenter
    members: tuple[CandidateStop, ...]
    radius_metres: int


def get_nightways_for_origin(
    city_name: str,
    travel_date: date,
    reference_file: Path,
    candidate_stop_limit: int = MAX_CANDIDATE_STOPS,
) -> dict:
    """Build a Nightways response without exposing arbitrary origins yet."""

    origin = resolve_origin(city_name)
    boundary = resolve_origin_boundary(origin)
    containment = OriginBoundaryContainment(boundary)
    response = request_origin_departures(
        origin,
        boundary,
        travel_date,
        candidate_stop_limit,
        containment=containment,
    )
    catalog = DestinationCatalog(reference_file)

    return _build_nightways_response(
        origin.name,
        travel_date,
        response,
        catalog,
        lambda stop: _stop_is_inside(containment, stop),
        origin.timezone,
    )


def get_nightways_for_origin_by_locality(
    city_name: str,
    travel_date: date,
    locality_index: LocalityIndex | None = None,
    candidate_stop_limit: int = MAX_CANDIDATE_STOPS,
) -> dict:
    """Build the internal locality-grouped response for an arbitrary origin.

    A caller can reuse an already-open index. Otherwise a GISCO-first composite
    index is opened for this operation.
    """

    if locality_index is None:
        with CompositeLocalityIndex.from_environment() as configured_index:
            return get_nightways_for_origin_by_locality(
                city_name,
                travel_date,
                configured_index,
                candidate_stop_limit,
            )

    origin = resolve_origin(
        city_name,
        CoordinateOriginMetadataEnricher(locality_index),
    )
    boundary = resolve_origin_boundary(origin)
    containment = OriginBoundaryContainment(boundary)
    response = request_origin_departures(
        origin,
        boundary,
        travel_date,
        candidate_stop_limit,
        containment=containment,
    )
    trips = _collect_qualified_trips(
        response,
        travel_date,
        lambda stop: _stop_is_inside(containment, stop),
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
    containment: OriginBoundaryContainment | None = None,
) -> dict:
    """Request detailed departures for bounded, core-mode candidate stops."""

    if (
        isinstance(candidate_stop_limit, bool)
        or not isinstance(candidate_stop_limit, int)
        or candidate_stop_limit < 1
    ):
        raise ValueError("candidate_stop_limit must be a positive integer")

    containment = _containment_for_boundary(boundary, containment)
    candidates = _request_candidate_stops(boundary, containment)
    request_limit = min(candidate_stop_limit, MAX_CANDIDATE_STOPS)

    if len(candidates) <= MAX_CANDIDATE_STOPS:
        if len(candidates) > request_limit:
            raise CandidateStopLimitError(
                len(candidates),
                request_limit,
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

    batches = _plan_candidate_batches(candidates, request_limit)
    stop_times = []
    aggregate_bytes = 0
    aggregate_stop_times = 0

    for batch in batches:
        response, response_bytes = _request_dense_stop_departures(
            origin,
            travel_date,
            batch,
        )
        batch_stop_times = response["stopTimes"]

        if len(batch_stop_times) > MAX_DENSE_STOP_TIMES:
            raise DenseOriginResponseLimitError(
                "stoptimes in one response",
                len(batch_stop_times),
                MAX_DENSE_STOP_TIMES,
            )

        aggregate_bytes += response_bytes
        if aggregate_bytes > MAX_DENSE_AGGREGATE_BYTES:
            raise DenseOriginResponseLimitError(
                "aggregate response bytes",
                aggregate_bytes,
                MAX_DENSE_AGGREGATE_BYTES,
            )

        aggregate_stop_times += len(batch_stop_times)
        if aggregate_stop_times > MAX_DENSE_AGGREGATE_STOP_TIMES:
            raise DenseOriginResponseLimitError(
                "aggregate stoptimes",
                aggregate_stop_times,
                MAX_DENSE_AGGREGATE_STOP_TIMES,
            )

        stop_times.extend(
            stop_time
            for stop_time in batch_stop_times
            if isinstance(stop_time, dict)
            and _dense_boarding_stop_is_eligible(
                stop_time.get("place"),
                boundary,
                batch,
                containment,
            )
        )

    return {"stopTimes": stop_times}


def _plan_candidate_batches(
    candidates: tuple[CandidateStop, ...],
    request_limit: int = MAX_CANDIDATE_STOPS,
) -> tuple[CandidateBatch, ...]:
    """Plan complete deterministic spatial coverage before request fan-out."""

    if (
        isinstance(request_limit, bool)
        or not isinstance(request_limit, int)
        or request_limit < 1
        or request_limit > MAX_CANDIDATE_STOPS
    ):
        raise ValueError(
            f"request_limit must be between 1 and {MAX_CANDIDATE_STOPS}"
        )

    candidate_ids = [candidate.stop_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise TransitousError(
            "Dense-origin candidate batch plan contains duplicate stop IDs."
        )

    admission_request_count = math.ceil(
        len(candidates) / MAX_BATCH_CANDIDATES
    )
    if len(candidates) > request_limit * MAX_BATCH_CANDIDATES:
        raise CandidateStopLimitError(
            len(candidates),
            request_limit,
            admission_request_count,
        )

    legacy_batches = _plan_legacy_candidate_batches(candidates)
    _validate_candidate_batches(candidates, legacy_batches)
    if len(legacy_batches) <= request_limit:
        return legacy_batches

    fallback_batches = _plan_fallback_candidate_batches(candidates)
    _validate_candidate_batches(candidates, fallback_batches)
    if len(fallback_batches) > request_limit:
        raise CandidateStopLimitError(
            len(candidates),
            request_limit,
            len(fallback_batches),
        )

    return fallback_batches


def _plan_legacy_candidate_batches(
    candidates: tuple[CandidateStop, ...],
) -> tuple[CandidateBatch, ...]:
    """Preserve the original bounded planner for already-supported cities."""

    remaining = {
        candidate.stop_id: candidate
        for candidate in candidates
    }
    batches = []
    eligible_distance = (
        MAX_BATCH_RADIUS_METRES - BATCH_RADIUS_PADDING_METRES
    )

    while remaining:
        choices = []
        for center in sorted(
            remaining.values(),
            key=lambda candidate: candidate.stop_id,
        ):
            neighbours = sorted(
                (
                    (_candidate_distance_metres(center, candidate), candidate)
                    for candidate in remaining.values()
                    if _candidate_distance_metres(center, candidate)
                    <= eligible_distance
                ),
                key=lambda item: (item[0], item[1].stop_id),
            )
            choices.append(
                (
                    -len(neighbours),
                    center.stop_id,
                    center,
                    [
                        (0.0, center),
                        *(
                            item
                            for item in neighbours
                            if item[1].stop_id != center.stop_id
                        ),
                    ][:MAX_BATCH_CANDIDATES],
                )
            )

        _, _, center, selected = min(choices)
        farthest_distance = max(distance for distance, _ in selected)
        radius_metres = math.ceil(
            farthest_distance + BATCH_RADIUS_PADDING_METRES
        )
        batch = CandidateBatch(
            center=center,
            members=tuple(candidate for _, candidate in selected),
            radius_metres=radius_metres,
        )
        batches.append(batch)

        for member in batch.members:
            del remaining[member.stop_id]

    return tuple(batches)


def _plan_fallback_candidate_batches(
    candidates: tuple[CandidateStop, ...],
) -> tuple[CandidateBatch, ...]:
    """Cover a failed dense plan with deterministic, bounded spatial disks."""

    ordered_candidates = tuple(
        sorted(candidates, key=lambda candidate: candidate.stop_id)
    )
    if not ordered_candidates:
        return ()

    effective_radius = MAX_FALLBACK_MEMBER_RADIUS_METRES
    projection = _candidate_projection(ordered_candidates)
    projected_candidates = tuple(
        (
            candidate,
            *_project_candidate(candidate, projection),
        )
        for candidate in ordered_candidates
    )

    proposed_centers = [
        (x, y)
        for _, x, y in projected_candidates
    ]
    for first_index, (_, first_x, first_y) in enumerate(
        projected_candidates
    ):
        for _, second_x, second_y in projected_candidates[
            first_index + 1 :
        ]:
            proposed_centers.extend(
                _pair_circle_centers(
                    first_x,
                    first_y,
                    second_x,
                    second_y,
                    effective_radius,
                )
            )

    options_by_members = {}
    for x, y in proposed_centers:
        center = _unproject_batch_center(x, y, projection)
        if not _valid_coordinate(center.lat, center.lon):
            continue
        members = tuple(
            index
            for index, candidate in enumerate(ordered_candidates)
            if _coordinate_distance_metres(
                center.lat,
                center.lon,
                candidate.lat,
                candidate.lon,
            )
            <= effective_radius
        )
        if not members:
            continue

        member_mask = sum(1 << index for index in members)
        center_key = (
            round(x, 6),
            round(y, 6),
            x,
            y,
        )
        previous = options_by_members.get(member_mask)
        if previous is None or center_key < previous[0]:
            options_by_members[member_mask] = (center_key, center)

    options = _remove_dominated_batch_options(options_by_members)
    remaining_mask = (1 << len(ordered_candidates)) - 1
    batches = []

    while remaining_mask:
        choices = []
        for member_mask, center_key, center in options:
            uncovered_mask = member_mask & remaining_mask
            uncovered_count = uncovered_mask.bit_count()
            if uncovered_count:
                choices.append(
                    (
                        -uncovered_count,
                        center_key,
                        member_mask,
                        center,
                        uncovered_mask,
                    )
                )

        if not choices:
            raise TransitousError(
                "Dense-origin fallback batch plan cannot cover every stop."
            )

        _, _, _, center, selected_mask = min(choices)
        selected_members = tuple(
            candidate
            for index, candidate in enumerate(ordered_candidates)
            if selected_mask & (1 << index)
        )
        farthest_distance = max(
            _coordinate_distance_metres(
                center.lat,
                center.lon,
                candidate.lat,
                candidate.lon,
            )
            for candidate in selected_members
        )
        radius_metres = math.ceil(
            farthest_distance + BATCH_RADIUS_PADDING_METRES
        )
        if radius_metres > MAX_FALLBACK_BATCH_RADIUS_METRES:
            raise TransitousError(
                "Dense-origin fallback batch radius is invalid."
            )

        batches.append(
            CandidateBatch(
                center=center,
                members=selected_members,
                radius_metres=radius_metres,
            )
        )
        remaining_mask &= ~selected_mask

    return tuple(batches)


def _candidate_projection(
    candidates: tuple[CandidateStop, ...],
) -> tuple[float, float, float]:
    reference_lat = sum(candidate.lat for candidate in candidates) / len(
        candidates
    )
    longitude_x = sum(
        math.cos(math.radians(candidate.lon)) for candidate in candidates
    )
    longitude_y = sum(
        math.sin(math.radians(candidate.lon)) for candidate in candidates
    )
    if longitude_x == 0 and longitude_y == 0:
        reference_lon = candidates[0].lon
    else:
        reference_lon = math.degrees(math.atan2(longitude_y, longitude_x))
    longitude_scale = max(
        math.cos(math.radians(reference_lat)),
        1e-12,
    )
    return reference_lat, reference_lon, longitude_scale


def _project_candidate(
    candidate: CandidateStop,
    projection: tuple[float, float, float],
) -> tuple[float, float]:
    reference_lat, reference_lon, longitude_scale = projection
    longitude_delta = (
        (candidate.lon - reference_lon + 180) % 360
    ) - 180
    return (
        EARTH_RADIUS_METRES
        * longitude_scale
        * math.radians(longitude_delta),
        EARTH_RADIUS_METRES
        * math.radians(candidate.lat - reference_lat),
    )


def _unproject_batch_center(
    x: float,
    y: float,
    projection: tuple[float, float, float],
) -> CandidateBatchCenter:
    reference_lat, reference_lon, longitude_scale = projection
    lat = reference_lat + math.degrees(y / EARTH_RADIUS_METRES)
    lon = reference_lon + math.degrees(
        x / (EARTH_RADIUS_METRES * longitude_scale)
    )
    lon = ((lon + 180) % 360) - 180
    return CandidateBatchCenter(lat=lat, lon=lon)


def _pair_circle_centers(
    first_x: float,
    first_y: float,
    second_x: float,
    second_y: float,
    radius_metres: float,
) -> tuple[tuple[float, float], ...]:
    x_delta = second_x - first_x
    y_delta = second_y - first_y
    distance = math.hypot(x_delta, y_delta)
    if distance == 0 or distance > 2 * radius_metres:
        return ()

    midpoint_x = (first_x + second_x) / 2
    midpoint_y = (first_y + second_y) / 2
    offset = math.sqrt(
        max(0.0, radius_metres**2 - (distance / 2) ** 2)
    )
    perpendicular_x = -y_delta / distance
    perpendicular_y = x_delta / distance
    return (
        (
            midpoint_x + perpendicular_x * offset,
            midpoint_y + perpendicular_y * offset,
        ),
        (
            midpoint_x - perpendicular_x * offset,
            midpoint_y - perpendicular_y * offset,
        ),
    )


def _remove_dominated_batch_options(
    options_by_members: dict[
        int,
        tuple[tuple[float, float, float, float], CandidateBatchCenter],
    ],
) -> tuple[
    tuple[
        int,
        tuple[float, float, float, float],
        CandidateBatchCenter,
    ],
    ...,
]:
    ordered_options = sorted(
        (
            (member_mask, center_key, center)
            for member_mask, (center_key, center) in (
                options_by_members.items()
            )
        ),
        key=lambda option: (
            -option[0].bit_count(),
            option[1],
        ),
    )
    kept = []
    member_count = max(
        (
            member_mask.bit_length()
            for member_mask, _, _ in ordered_options
        ),
        default=0,
    )
    kept_by_member = [[] for _ in range(max(1, member_count))]

    for member_mask, center_key, center in ordered_options:
        member_indices = tuple(
            index
            for index in range(member_mask.bit_length())
            if member_mask & (1 << index)
        )
        possible_supersets = min(
            (kept_by_member[index] for index in member_indices),
            key=len,
        )
        if any(
            (member_mask & ~kept[option_index][0]) == 0
            for option_index in possible_supersets
        ):
            continue

        kept_index = len(kept)
        kept.append((member_mask, center_key, center))
        for index in member_indices:
            kept_by_member[index].append(kept_index)

    return tuple(kept)


def _validate_candidate_batches(
    candidates: tuple[CandidateStop, ...],
    batches: tuple[CandidateBatch, ...],
) -> None:
    candidate_ids = {candidate.stop_id for candidate in candidates}
    assigned_ids = []

    for batch in batches:
        member_ids = {member.stop_id for member in batch.members}
        if isinstance(batch.center, CandidateStop):
            center_is_valid = (
                1 <= len(batch.members) <= MAX_BATCH_CANDIDATES
                and batch.center.stop_id in candidate_ids
                and batch.center.stop_id in member_ids
            )
            maximum_radius = MAX_BATCH_RADIUS_METRES
            maximum_member_distance = batch.radius_metres
        elif isinstance(batch.center, CandidateBatchCenter):
            center_is_valid = (
                1
                <= len(batch.members)
                <= MAX_CANDIDATE_STOPS * MAX_BATCH_CANDIDATES
                and _valid_coordinate(batch.center.lat, batch.center.lon)
            )
            maximum_radius = MAX_FALLBACK_BATCH_RADIUS_METRES
            maximum_member_distance = min(
                batch.radius_metres,
                MAX_FALLBACK_MEMBER_RADIUS_METRES,
            )
        else:
            center_is_valid = False
            maximum_radius = 0
            maximum_member_distance = 0

        if (
            not center_is_valid
            or not BATCH_RADIUS_PADDING_METRES
            <= batch.radius_metres
            <= maximum_radius
        ):
            raise TransitousError(
                "Dense-origin candidate batch plan is invalid."
            )

        for member in batch.members:
            if (
                member.stop_id not in candidate_ids
                or _candidate_distance_metres(batch.center, member)
                > maximum_member_distance
            ):
                raise TransitousError(
                    "Dense-origin candidate batch plan is invalid."
                )
            assigned_ids.append(member.stop_id)

    if (
        len(assigned_ids) != len(candidate_ids)
        or set(assigned_ids) != candidate_ids
    ):
        raise TransitousError(
            "Dense-origin candidate batch plan does not cover every stop."
        )


def _candidate_distance_metres(
    first: CandidateStop | CandidateBatchCenter,
    second: CandidateStop,
) -> float:
    return _coordinate_distance_metres(
        first.lat,
        first.lon,
        second.lat,
        second.lon,
    )


def _coordinate_distance_metres(
    first_lat: float,
    first_lon: float,
    second_lat: float,
    second_lon: float,
) -> float:
    first_lat_radians = math.radians(first_lat)
    second_lat_radians = math.radians(second_lat)
    latitude_delta = math.radians(second_lat - first_lat)
    longitude_delta = math.radians(second_lon - first_lon)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_lat_radians)
        * math.cos(second_lat_radians)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METRES * math.asin(math.sqrt(haversine))


def _dense_boarding_stop_is_eligible(
    stop,
    boundary: OriginBoundary,
    batch: CandidateBatch,
    containment: OriginBoundaryContainment | None = None,
) -> bool:
    if not isinstance(stop, dict):
        return False

    containment = _containment_for_boundary(boundary, containment)
    lat = stop.get("lat")
    lon = stop.get("lon")
    if (
        not _valid_coordinate(lat, lon)
        or not containment.contains(float(lat), float(lon))
        or _coordinate_distance_metres(
            batch.center.lat,
            batch.center.lon,
            float(lat),
            float(lon),
        )
        > batch.radius_metres
    ):
        return False

    stop_id = stop.get("stopId")
    parent_id = _parent_stop_id(stop)
    candidate_ids = {candidate.stop_id for candidate in batch.members}
    candidate_parent_ids = {
        candidate.parent_id
        for candidate in batch.members
        if candidate.parent_id is not None
    }

    if (
        isinstance(stop_id, str)
        and (
            stop_id in candidate_ids
            or stop_id in candidate_parent_ids
        )
    ):
        return True

    if (
        parent_id is not None
        and (
            parent_id in candidate_ids
            or parent_id in candidate_parent_ids
        )
    ):
        return True

    return any(
        _coordinate_distance_metres(
            candidate.lat,
            candidate.lon,
            float(lat),
            float(lon),
        )
        <= BATCH_ASSOCIATION_RADIUS_METRES
        for candidate in batch.members
    )


def _parent_stop_id(stop: dict) -> str | None:
    for key in ("parentId", "parentStopId"):
        value = stop.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    parent = stop.get("parent")
    if isinstance(parent, str) and parent.strip():
        return parent.strip()
    if isinstance(parent, dict):
        for key in ("stopId", "id"):
            value = parent.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


def _request_candidate_stops(
    boundary: OriginBoundary,
    containment: OriginBoundaryContainment | None = None,
) -> tuple[CandidateStop, ...]:
    containment = _containment_for_boundary(boundary, containment)
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
        parent_id = _parent_stop_id(stop)

        if (
            not isinstance(stop_id, str)
            or not stop_id.strip()
            or stop_id in seen_stop_ids
            or not isinstance(modes, list)
            or not core_modes.intersection(modes)
            or not _valid_coordinate(lat, lon)
            or not containment.contains(float(lat), float(lon))
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
                parent_id=parent_id,
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


def _request_dense_stop_departures(
    origin: ResolvedOrigin,
    travel_date: date,
    batch: CandidateBatch,
) -> tuple[dict, int]:
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
            "center": f"{batch.center.lat},{batch.center.lon}",
            "radius": batch.radius_metres,
            "exactRadius": "true",
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
    payload, response_bytes = _request_bounded_json(
        f"{TRANSITOUS_API_URL}?{query}",
        "Transitous stoptimes",
        MAX_DENSE_RESPONSE_BYTES,
    )
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("stopTimes"), list)
    ):
        raise TransitousError(
            "Transitous stoptimes returned an unreadable response."
        )

    return payload, response_bytes


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


def _request_bounded_json(
    url: str,
    service_name: str,
    max_response_bytes: int,
):
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": TRANSITOUS_USER_AGENT,
        },
    )

    try:
        with urlopen(request, timeout=60) as response:
            body = response.read(max_response_bytes + 1)
    except HTTPError as error:
        raise TransitousError(
            f"{service_name} returned HTTP {error.code}. "
            "Please try again later."
        ) from error
    except (URLError, TimeoutError) as error:
        raise TransitousError(
            f"Could not reach {service_name}. Please try again later."
        ) from error
    except OSError as error:
        raise TransitousError(
            f"{service_name} returned an unreadable response."
        ) from error

    if len(body) > max_response_bytes:
        raise DenseOriginResponseLimitError(
            "response body bytes",
            len(body),
            max_response_bytes,
        )

    try:
        return json.loads(body), len(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
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


def _stop_is_inside(
    containment: OriginBoundary | OriginBoundaryContainment,
    stop: dict,
) -> bool:
    lat = stop.get("lat")
    lon = stop.get("lon")
    return (
        _valid_coordinate(lat, lon)
        and containment.contains(float(lat), float(lon))
    )


def _containment_for_boundary(
    boundary: OriginBoundary,
    containment: OriginBoundaryContainment | None,
) -> OriginBoundaryContainment:
    if containment is None:
        return OriginBoundaryContainment(boundary)
    if containment.boundary is not boundary:
        raise ValueError("containment must belong to boundary")
    return containment


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
