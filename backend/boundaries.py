"""Resolve Nominatim city boundaries and test coordinates locally.

This module is deliberately independent of the current Dresden request path.
Boundary selection failures and Nominatim availability failures have their own
exception hierarchy, separate from origin resolution and Transitous failures.
"""

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from threading import Lock
from time import monotonic, sleep
from unicodedata import normalize as unicode_normalize
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.transitous import ResolvedOrigin


NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"
NOMINATIM_ACCEPT_LANGUAGE = "en"
NOMINATIM_USER_AGENT = "Nightways/0.1"
ORIGIN_BOUNDARY_CACHE_SIZE = 32
NOMINATIM_RETRY_BACKOFF_SECONDS = (0.5, 1.0)
NOMINATIM_MAX_RETRY_AFTER_SECONDS = 2.0
NOMINATIM_REQUEST_INTERVAL_SECONDS = 1.0
GB_COUNTRY_CODE = "gb"
ISO_LEVEL_6_ADDRESS_KEY = "ISO3166-2-lvl6"


_nominatim_request_lock = Lock()
_nominatim_last_request_started_at: float | None = None
_nominatim_retry_not_before: float | None = None


class BoundaryResolutionError(RuntimeError):
    """Base class for failures after an origin has already been resolved."""


class BoundaryNotFoundError(BoundaryResolutionError):
    """Raised when Nominatim provides no suitable polygonal city boundary."""


class AmbiguousBoundaryError(BoundaryResolutionError):
    """Raised when a fallback has multiple plausible municipality polygons."""

    def __init__(self, origin: ResolvedOrigin, candidates):
        self.candidates = tuple(candidates)
        super().__init__(
            f"Multiple municipality boundaries match {origin.name!r}."
        )


class BoundaryServiceError(BoundaryResolutionError):
    """Raised when Nominatim cannot supply a readable search response."""


Position = tuple[float, float]
LinearRing = tuple[Position, ...]
Polygon = tuple[LinearRing, ...]
MultiPolygon = tuple[Polygon, ...]


@dataclass(frozen=True, slots=True)
class BoundaryGeometry:
    """Immutable Polygon or MultiPolygon coordinates in GeoJSON order."""

    geojson_type: str
    polygons: MultiPolygon

    @classmethod
    def from_geojson(cls, geojson: dict) -> "BoundaryGeometry":
        if not isinstance(geojson, dict):
            raise ValueError("Boundary geometry must be a GeoJSON object.")

        geometry_type = geojson.get("type")
        coordinates = geojson.get("coordinates")

        if geometry_type == "Polygon":
            polygons = (_parse_polygon(coordinates),)
        elif geometry_type == "MultiPolygon":
            if not isinstance(coordinates, (list, tuple)) or not coordinates:
                raise ValueError("MultiPolygon coordinates cannot be empty.")
            polygons = tuple(
                _parse_polygon(polygon) for polygon in coordinates
            )
        else:
            raise ValueError("Only Polygon and MultiPolygon are supported.")

        return cls(geojson_type=geometry_type, polygons=polygons)

    def contains(self, lat: float, lon: float) -> bool:
        """Return whether latitude/longitude lies in this geometry."""

        latitude = _coordinate(lat, "latitude", -90, 90)
        longitude = _coordinate(lon, "longitude", -180, 180)
        return any(
            _polygon_contains(polygon, longitude, latitude)
            for polygon in self.polygons
        )


@dataclass(frozen=True, slots=True)
class OriginBoundary:
    """Stable OSM identity and local geometry for one origin city."""

    osm_type: str
    osm_id: int
    category: str
    display_name: str
    country_code: str
    geometry: BoundaryGeometry
    wikidata: str | None = None

    def contains(self, lat: float, lon: float) -> bool:
        """Return whether a coordinate belongs to the origin boundary."""

        return self.geometry.contains(lat, lon)


class OriginBoundaryContainment:
    """Request-local exact containment with a conservative coarse reject."""

    __slots__ = ("boundary", "_bounds", "_exact_results")

    def __init__(self, boundary: OriginBoundary):
        if not isinstance(boundary, OriginBoundary):
            raise TypeError("boundary must be an OriginBoundary")

        positions = (
            position
            for polygon in boundary.geometry.polygons
            for ring in polygon
            for position in ring
        )
        coordinates = tuple(positions)
        longitudes = tuple(position[0] for position in coordinates)
        latitudes = tuple(position[1] for position in coordinates)

        self.boundary = boundary
        self._bounds = (
            min(latitudes),
            min(longitudes),
            max(latitudes),
            max(longitudes),
        )
        self._exact_results: dict[tuple[float, float], bool] = {}

    def contains(self, lat: float, lon: float) -> bool:
        """Return exact containment, reusing results only within this object."""

        latitude = _coordinate(lat, "latitude", -90, 90)
        longitude = _coordinate(lon, "longitude", -180, 180)
        south, west, north, east = self._bounds
        if (
            latitude < south
            or latitude > north
            or longitude < west
            or longitude > east
        ):
            return False

        coordinate = (latitude, longitude)
        if coordinate not in self._exact_results:
            self._exact_results[coordinate] = self.boundary.contains(
                latitude,
                longitude,
            )
        return self._exact_results[coordinate]


def resolve_origin_boundary(origin: ResolvedOrigin) -> OriginBoundary:
    """Resolve one conservative polygonal boundary for a resolved origin.

    Raises BoundaryNotFoundError or AmbiguousBoundaryError for selection
    failures, and BoundaryServiceError for Nominatim/HTTP failures. Existing
    OriginResolutionError and TransitousError exceptions are not reused here.
    """

    if not isinstance(origin, ResolvedOrigin):
        raise TypeError("origin must be a ResolvedOrigin")

    country_code = origin.country_code.strip().lower()
    if not origin.name.strip() or len(country_code) != 2:
        raise ValueError("Resolved origin must have a name and country code.")

    return _resolve_origin_boundary_cached(origin)


@lru_cache(maxsize=ORIGIN_BOUNDARY_CACHE_SIZE)
def _resolve_origin_boundary_cached(
    origin: ResolvedOrigin,
) -> OriginBoundary:
    country_code = origin.country_code.strip().lower()
    matches = _request_nominatim_matches(origin)
    direct_boundary = _select_fallback_boundary(
        matches,
        origin,
        country_code,
    )
    if direct_boundary is not None:
        return direct_boundary

    if not matches or not _is_leading_city_point(matches[0], country_code):
        raise BoundaryNotFoundError(
            f"No city boundary found for {origin.name!r}."
        )

    excluded_reference = _osm_reference(matches[0])
    if excluded_reference is None:
        raise BoundaryNotFoundError(
            f"No city boundary found for {origin.name!r}."
        )

    fallback_matches = _request_nominatim_matches(
        origin,
        excluded_osm_reference=excluded_reference,
    )
    fallback_boundary = _select_fallback_boundary(
        fallback_matches,
        origin,
        country_code,
    )
    if fallback_boundary is not None:
        return fallback_boundary

    local_name = _local_city_name(matches[0], origin.name)
    if local_name is not None:
        local_matches = _request_nominatim_matches(
            replace(origin, name=local_name),
            excluded_osm_reference=excluded_reference,
        )
        local_boundary = _select_fallback_boundary(
            local_matches,
            origin,
            country_code,
        )
        if local_boundary is not None:
            return local_boundary

    county_identity = _gb_county_identity(matches[0], origin, country_code)
    if county_identity is not None:
        county_name, level_6_code = county_identity
        county_matches = _request_nominatim_matches(
            origin,
            county_name=county_name,
        )
        county_boundary = _select_gb_county_boundary(
            county_matches,
            origin,
            country_code,
            county_name,
            level_6_code,
        )
        if county_boundary is not None:
            return county_boundary

    raise BoundaryNotFoundError(
        f"No city boundary found for {origin.name!r}."
    )


def _select_fallback_boundary(
    matches: list[dict],
    origin: ResolvedOrigin,
    country_code: str,
) -> OriginBoundary | None:
    fallback_city = _first_boundary(
        matches,
        country_code,
        address_type="city",
    )
    if fallback_city is not None:
        return fallback_city

    municipalities = [
        boundary
        for match in matches
        if (
            boundary := _boundary_from_match(
                match,
                country_code,
                address_type="municipality",
            )
        )
        is not None
    ]

    if len(municipalities) == 1:
        return municipalities[0]
    if len(municipalities) > 1:
        raise AmbiguousBoundaryError(origin, municipalities)

    return None


def _local_city_name(match: dict, canonical_name: str) -> str | None:
    if not isinstance(match, dict):
        return None

    namedetails = match.get("namedetails") or {}
    local_name = namedetails.get("name")
    if not isinstance(local_name, str) or not local_name.strip():
        return None

    local_name = local_name.strip()
    if _normalized_boundary_name(local_name) == _normalized_boundary_name(
        canonical_name
    ):
        return None

    return local_name


def _gb_county_identity(
    match: dict,
    origin: ResolvedOrigin,
    country_code: str,
) -> tuple[str, str] | None:
    """Return a city-linked GB level-6 authority from a leading Point."""

    if (
        country_code != GB_COUNTRY_CODE
        or not _is_leading_city_point(match, country_code)
    ):
        return None

    address = match.get("address") or {}
    city_name = address.get("city")
    county_name = address.get("county")
    level_6_code = address.get(ISO_LEVEL_6_ADDRESS_KEY)
    if not all(
        isinstance(value, str) and value.strip()
        for value in (city_name, county_name, level_6_code)
    ):
        return None

    city_name = city_name.strip()
    county_name = county_name.strip()
    level_6_code = level_6_code.strip()
    city_tokens = _normalized_boundary_name(city_name).split()
    county_tokens = _normalized_boundary_name(county_name).split()
    if (
        _normalized_boundary_name(origin.name)
        != _normalized_boundary_name(city_name)
        or county_tokens not in (city_tokens, [*city_tokens, "city"])
        or not level_6_code.startswith("GB-")
    ):
        return None

    return county_name, level_6_code


def _select_gb_county_boundary(
    matches: list[dict],
    origin: ResolvedOrigin,
    country_code: str,
    county_name: str,
    level_6_code: str,
) -> OriginBoundary | None:
    """Select one strictly linked, enclosing GB level-6 county relation."""

    expected_county = _normalized_boundary_name(county_name)
    candidates = []
    for match in matches:
        if not isinstance(match, dict):
            continue

        address = match.get("address") or {}
        extra_tags = match.get("extratags") or {}
        match_name = match.get("name")
        match_county = address.get("county")
        if (
            country_code != GB_COUNTRY_CODE
            or not isinstance(match_name, str)
            or _normalized_boundary_name(match_name) != expected_county
            or not isinstance(match_county, str)
            or _normalized_boundary_name(match_county) != expected_county
            or address.get(ISO_LEVEL_6_ADDRESS_KEY) != level_6_code
            or match.get("osm_type") != "relation"
            or extra_tags.get("admin_level") != "6"
        ):
            continue

        boundary = _boundary_from_match(
            match,
            country_code,
            address_type="county",
        )
        if boundary is None:
            continue
        try:
            contains_origin = boundary.contains(origin.lat, origin.lon)
        except (TypeError, ValueError):
            contains_origin = False
        if contains_origin:
            candidates.append(boundary)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise AmbiguousBoundaryError(origin, candidates)
    return None


def _normalized_boundary_name(name: str) -> str:
    normalized = unicode_normalize("NFKC", name)
    return " ".join(normalized.split()).casefold()


def _request_nominatim_matches(
    origin: ResolvedOrigin,
    excluded_osm_reference: str | None = None,
    *,
    county_name: str | None = None,
) -> list[dict]:
    if county_name is not None and excluded_osm_reference is not None:
        raise ValueError(
            "A county search cannot exclude a city OSM reference."
        )

    parameters = {
        "countrycodes": origin.country_code.lower(),
        "format": "jsonv2",
        "addressdetails": 1,
        "extratags": 1,
        "namedetails": 1,
        "polygon_geojson": 1,
        "accept-language": NOMINATIM_ACCEPT_LANGUAGE,
        "dedupe": 0,
        "limit": 10,
    }
    if county_name is None:
        parameters["city"] = origin.name
        parameters["featureType"] = "city"
    else:
        parameters["county"] = county_name

    if excluded_osm_reference is not None:
        parameters["exclude_place_ids"] = excluded_osm_reference

    request = Request(
        f"{NOMINATIM_BASE_URL}/search?{urlencode(parameters)}",
        headers={
            "Accept": "application/json",
            "Accept-Language": NOMINATIM_ACCEPT_LANGUAGE,
            "User-Agent": NOMINATIM_USER_AGENT,
        },
    )

    retry_not_before = None
    for retry_index in range(len(NOMINATIM_RETRY_BACKOFF_SECONDS) + 1):
        try:
            matches = _load_nominatim_response(
                request,
                retry_not_before,
            )
        except HTTPError as error:
            if (
                error.code == 429
                and retry_index < len(NOMINATIM_RETRY_BACKOFF_SECONDS)
            ):
                retry_delay = _nominatim_retry_delay(error, retry_index)
                if retry_delay is not None:
                    retry_not_before = monotonic() + retry_delay
                    continue

            raise BoundaryServiceError(
                f"Nominatim returned HTTP {error.code}. "
                "Please try again later."
            ) from error
        except (URLError, TimeoutError) as error:
            raise BoundaryServiceError(
                "Could not reach Nominatim. Please try again later."
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise BoundaryServiceError(
                "Nominatim returned an unreadable response."
            ) from error

        if not isinstance(matches, list):
            raise BoundaryServiceError(
                "Nominatim returned an unreadable response."
            )

        return matches

    raise AssertionError("Nominatim retry loop ended unexpectedly.")


def _load_nominatim_response(
    request: Request,
    not_before: float | None,
):
    global _nominatim_last_request_started_at
    global _nominatim_retry_not_before

    with _nominatim_request_lock:
        while True:
            now = monotonic()
            next_start = now if not_before is None else not_before
            if _nominatim_retry_not_before is not None:
                next_start = max(
                    next_start,
                    _nominatim_retry_not_before,
                )
            if _nominatim_last_request_started_at is not None:
                next_start = max(
                    next_start,
                    _nominatim_last_request_started_at
                    + NOMINATIM_REQUEST_INTERVAL_SECONDS,
                )

            delay = next_start - now
            if delay <= 0:
                break
            if delay > NOMINATIM_MAX_RETRY_AFTER_SECONDS:
                raise BoundaryServiceError(
                    "Nominatim asked Nightways to retry later. "
                    "Please try again later."
                )
            sleep(delay)

        _nominatim_last_request_started_at = monotonic()
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 429:
                retry_after = _nominatim_retry_after(error)
                if retry_after is not None:
                    retry_at = monotonic() + retry_after
                    if _nominatim_retry_not_before is None:
                        _nominatim_retry_not_before = retry_at
                    else:
                        _nominatim_retry_not_before = max(
                            _nominatim_retry_not_before,
                            retry_at,
                        )
            raise


def _nominatim_retry_delay(
    error: HTTPError,
    retry_index: int,
) -> float | None:
    parsed_retry_after = _nominatim_retry_after(error)
    if parsed_retry_after is None:
        return NOMINATIM_RETRY_BACKOFF_SECONDS[retry_index]
    if parsed_retry_after > NOMINATIM_MAX_RETRY_AFTER_SECONDS:
        return None
    return parsed_retry_after


def _nominatim_retry_after(error: HTTPError) -> float | None:
    retry_after = None
    if error.headers is not None:
        retry_after = error.headers.get("Retry-After")
    return _parse_retry_after(retry_after)


def _parse_retry_after(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None

    value = value.strip()
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (
            retry_at - datetime.now(timezone.utc)
        ).total_seconds()

    if not math.isfinite(delay):
        return None
    return max(0.0, delay)


def _first_boundary(
    matches: list[dict],
    country_code: str,
    address_type: str,
) -> OriginBoundary | None:
    for match in matches:
        boundary = _boundary_from_match(
            match,
            country_code,
            address_type,
        )
        if boundary is not None:
            return boundary
    return None


def _boundary_from_match(
    match: dict,
    country_code: str,
    address_type: str,
) -> OriginBoundary | None:
    if not isinstance(match, dict):
        return None

    address = match.get("address") or {}
    match_country = address.get("country_code")
    category = match.get("category", match.get("class"))
    display_name = match.get("display_name")
    osm_type = match.get("osm_type")
    osm_id = match.get("osm_id")

    if (
        not isinstance(match_country, str)
        or match_country.casefold() != country_code.casefold()
        or category != "boundary"
        or match.get("type") != "administrative"
        or match.get("addresstype") != address_type
        or not isinstance(display_name, str)
        or not display_name.strip()
        or not isinstance(osm_type, str)
        or osm_type.casefold() not in {"node", "way", "relation"}
        or isinstance(osm_id, bool)
        or not isinstance(osm_id, (int, str))
    ):
        return None

    try:
        numeric_osm_id = int(osm_id)
        geometry = BoundaryGeometry.from_geojson(match.get("geojson"))
    except (TypeError, ValueError):
        return None

    extra_tags = match.get("extratags") or {}
    wikidata = extra_tags.get("wikidata")
    if not isinstance(wikidata, str) or not wikidata.strip():
        wikidata = None

    return OriginBoundary(
        osm_type=osm_type.casefold(),
        osm_id=numeric_osm_id,
        category=category,
        display_name=display_name.strip(),
        country_code=match_country.upper(),
        geometry=geometry,
        wikidata=wikidata,
    )


def _is_leading_city_point(match: dict, country_code: str) -> bool:
    if not isinstance(match, dict):
        return False

    address = match.get("address") or {}
    match_country = address.get("country_code")
    geometry = match.get("geojson") or {}
    category = match.get("category", match.get("class"))
    return (
        isinstance(match_country, str)
        and match_country.casefold() == country_code.casefold()
        and category == "place"
        and match.get("type") == "city"
        and match.get("addresstype") == "city"
        and geometry.get("type") == "Point"
    )


def _osm_reference(match: dict) -> str | None:
    prefixes = {"node": "N", "way": "W", "relation": "R"}
    osm_type = match.get("osm_type")
    osm_id = match.get("osm_id")

    if (
        not isinstance(osm_type, str)
        or osm_type.casefold() not in prefixes
        or isinstance(osm_id, bool)
        or not isinstance(osm_id, (int, str))
    ):
        return None

    try:
        numeric_osm_id = int(osm_id)
    except ValueError:
        return None

    return f"{prefixes[osm_type.casefold()]}{numeric_osm_id}"


def _parse_polygon(value) -> Polygon:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("Polygon coordinates cannot be empty.")
    return tuple(_parse_ring(ring) for ring in value)


def _parse_ring(value) -> LinearRing:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        raise ValueError("A linear ring needs at least four positions.")

    ring = tuple(_parse_position(position) for position in value)
    if ring[0] != ring[-1]:
        raise ValueError("A linear ring must be closed.")
    return ring


def _parse_position(value) -> Position:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("A position needs longitude and latitude.")
    longitude = _coordinate(value[0], "longitude", -180, 180)
    latitude = _coordinate(value[1], "latitude", -90, 90)
    return longitude, latitude


def _coordinate(value, label: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"Invalid {label}.")
    return float(value)


def _polygon_contains(
    polygon: Polygon,
    longitude: float,
    latitude: float,
) -> bool:
    outer_inside, outer_boundary = _ring_location(
        polygon[0],
        longitude,
        latitude,
    )
    if not outer_inside and not outer_boundary:
        return False

    for hole in polygon[1:]:
        hole_inside, hole_boundary = _ring_location(
            hole,
            longitude,
            latitude,
        )
        if hole_inside or hole_boundary:
            return False

    return True


def _ring_location(
    ring: LinearRing,
    longitude: float,
    latitude: float,
) -> tuple[bool, bool]:
    inside = False

    for start, end in zip(ring, ring[1:]):
        if _point_on_segment(longitude, latitude, start, end):
            return False, True

        start_lon, start_lat = start
        end_lon, end_lat = end
        crosses_latitude = (start_lat > latitude) != (end_lat > latitude)
        if not crosses_latitude:
            continue

        crossing_longitude = (
            (end_lon - start_lon)
            * (latitude - start_lat)
            / (end_lat - start_lat)
            + start_lon
        )
        if longitude < crossing_longitude:
            inside = not inside

    return inside, False


def _point_on_segment(
    longitude: float,
    latitude: float,
    start: Position,
    end: Position,
) -> bool:
    start_lon, start_lat = start
    end_lon, end_lat = end
    cross_product = (
        (longitude - start_lon) * (end_lat - start_lat)
        - (latitude - start_lat) * (end_lon - start_lon)
    )
    tolerance = 1e-12 * max(
        1.0,
        abs(end_lon - start_lon),
        abs(end_lat - start_lat),
    )
    return (
        abs(cross_product) <= tolerance
        and min(start_lon, end_lon) - tolerance
        <= longitude
        <= max(start_lon, end_lon) + tolerance
        and min(start_lat, end_lat) - tolerance
        <= latitude
        <= max(start_lat, end_lat) + tolerance
    )
