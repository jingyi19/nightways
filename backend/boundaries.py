"""Resolve Nominatim city boundaries and test coordinates locally.

This module is deliberately independent of the current Dresden request path.
Boundary selection failures and Nominatim availability failures have their own
exception hierarchy, separate from origin resolution and Transitous failures.
"""

import json
import math
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.transitous import ResolvedOrigin


NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"
NOMINATIM_ACCEPT_LANGUAGE = "en"
NOMINATIM_USER_AGENT = "Nightways/0.1"


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

    matches = _request_nominatim_matches(origin)
    direct_boundary = _first_boundary(
        matches,
        country_code,
        address_type="city",
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
    fallback_city = _first_boundary(
        fallback_matches,
        country_code,
        address_type="city",
    )
    if fallback_city is not None:
        return fallback_city

    municipalities = [
        boundary
        for match in fallback_matches
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

    raise BoundaryNotFoundError(
        f"No city boundary found for {origin.name!r}."
    )


def _request_nominatim_matches(
    origin: ResolvedOrigin,
    excluded_osm_reference: str | None = None,
) -> list[dict]:
    parameters = {
        "city": origin.name,
        "countrycodes": origin.country_code.lower(),
        "featureType": "city",
        "format": "jsonv2",
        "addressdetails": 1,
        "extratags": 1,
        "namedetails": 1,
        "polygon_geojson": 1,
        "accept-language": NOMINATIM_ACCEPT_LANGUAGE,
        "dedupe": 0,
        "limit": 10,
    }
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

    try:
        with urlopen(request, timeout=60) as response:
            matches = json.load(response)
    except HTTPError as error:
        raise BoundaryServiceError(
            f"Nominatim returned HTTP {error.code}. Please try again later."
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
