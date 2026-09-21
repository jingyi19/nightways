"""Local GISCO LAU destination containment and grouping.

The production data file is deliberately external to the repository. Searches
query an installed GISCO LAU 2024 GeoPackage in read-only mode and never make a
geographic network request.
"""

import math
import os
import sqlite3
import struct
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Protocol

from backend.boundaries import BoundaryGeometry


GISCO_LAU_YEAR = 2024
GISCO_LAU_PATH_ENV = "NIGHTWAYS_GISCO_LAU_PATH"
DEFAULT_GISCO_LAU_PATH = Path("data") / "geo" / "lau-2024.gpkg"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_REQUIRED_COLUMNS = ("GISCO_ID", "CNTR_CODE", "LAU_NAME")


def configured_gisco_lau_path(environ=None) -> Path:
    """Return the absolute environment override or project-default path."""

    environment = os.environ if environ is None else environ
    configured = environment.get(GISCO_LAU_PATH_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
    else:
        path = _PROJECT_ROOT / DEFAULT_GISCO_LAU_PATH
    return path.resolve()


class LocalityResolutionError(RuntimeError):
    """Base class for destination-locality configuration/data failures."""


class LocalityDatasetUnavailableError(LocalityResolutionError):
    """Raised when no configured GISCO LAU GeoPackage can be opened."""


class LocalityDatasetError(LocalityResolutionError):
    """Raised when the configured GeoPackage is incompatible or corrupt."""


class LocalityResolutionStatus(str, Enum):
    """Conservative outcomes for one arrival coordinate."""

    RESOLVED = "resolved"
    NO_MATCH = "no_match"
    MULTIPLE_MATCHES = "multiple_matches"
    UNSUPPORTED_COUNTRY = "unsupported_country"
    INVALID_COORDINATE = "invalid_coordinate"


@dataclass(frozen=True, slots=True)
class DestinationLocality:
    """Stable identity and display metadata within one GISCO release."""

    gisco_id: str
    country_code: str
    lau_name: str
    dataset_year: int

    @property
    def grouping_key(self) -> str:
        """Return the V1 grouping key for this specific annual release."""

        return self.gisco_id


@dataclass(frozen=True, slots=True)
class LocalityResolution:
    """Result of resolving one arrival coordinate against the local index."""

    status: LocalityResolutionStatus
    locality: DestinationLocality | None = None
    candidates: tuple[DestinationLocality, ...] = ()
    country_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status is LocalityResolutionStatus.RESOLVED


class LocalityIndex(Protocol):
    """Provider contract used by locality grouping and origin enrichment."""

    def resolve(
        self,
        lat: float,
        lon: float,
        country_code: str | None = None,
    ) -> LocalityResolution:
        """Resolve one coordinate without changing the response contract."""

        ...


class GiscoLauIndex:
    """Read-only, RTree-backed access to a GISCO LAU GeoPackage."""

    def __init__(
        self,
        path: str | Path,
        dataset_year: int = GISCO_LAU_YEAR,
        table_name: str | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise LocalityDatasetUnavailableError(
                f"GISCO LAU {dataset_year} GeoPackage not found: "
                f"{self.path}. Run 'python backend/setup_gisco.py' or set "
                f"{GISCO_LAU_PATH_ENV}."
            )
        if isinstance(dataset_year, bool) or not isinstance(dataset_year, int):
            raise ValueError("dataset_year must be an integer")

        self.dataset_year = dataset_year
        self._connection = self._open_read_only(self.path)
        self._connection.row_factory = sqlite3.Row

        try:
            layer = self._select_layer(table_name)
            self.table_name = layer["table_name"]
            self.geometry_column = layer["column_name"]
            self._columns = layer["columns"]
            self.rtree_table = (
                f"rtree_{self.table_name}_{self.geometry_column}"
            )
            self._supported_country_codes = self._load_country_codes()
        except Exception:
            self._connection.close()
            raise

    @classmethod
    def from_environment(
        cls,
        dataset_year: int = GISCO_LAU_YEAR,
        table_name: str | None = None,
        environ=None,
    ) -> "GiscoLauIndex":
        """Open the environment override or project-default GeoPackage."""

        return cls(
            configured_gisco_lau_path(environ),
            dataset_year,
            table_name,
        )

    @property
    def supported_country_codes(self) -> frozenset[str]:
        return self._supported_country_codes

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "GiscoLauIndex":
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.close()

    def resolve(
        self,
        lat: float,
        lon: float,
        country_code: str | None = None,
    ) -> LocalityResolution:
        """Resolve latitude/longitude to exactly one containing LAU.

        A country hint is accepted only as explicit upstream metadata. If exact
        containment finds nothing, it distinguishes a country absent from the
        installed release from an unresolved coordinate. Polygon containment
        remains authoritative, and no country is inferred when the hint is
        absent.
        """

        normalized_country = _country_code(country_code)
        if country_code is not None and normalized_country is None:
            normalized_country = None

        if not _valid_coordinate(lat, lon):
            return LocalityResolution(
                LocalityResolutionStatus.INVALID_COORDINATE,
                country_code=normalized_country,
            )

        matches = {}
        for row in self._candidate_rows(float(lon), float(lat)):
            locality = self._locality_from_row(row)
            try:
                geometry = _geometry_from_geopackage(row["geometry"])
            except (TypeError, ValueError, struct.error) as error:
                raise LocalityDatasetError(
                    f"Unreadable geometry for {locality.gisco_id}."
                ) from error

            if geometry.contains(float(lat), float(lon)):
                previous = matches.setdefault(locality.gisco_id, locality)
                if previous != locality:
                    raise LocalityDatasetError(
                        f"Inconsistent attributes for {locality.gisco_id}."
                    )

        candidates = tuple(
            sorted(matches.values(), key=lambda item: item.gisco_id)
        )
        if not candidates:
            if (
                normalized_country is not None
                and normalized_country not in self.supported_country_codes
            ):
                return LocalityResolution(
                    LocalityResolutionStatus.UNSUPPORTED_COUNTRY,
                    country_code=normalized_country,
                )
            return LocalityResolution(
                LocalityResolutionStatus.NO_MATCH,
                country_code=normalized_country,
            )
        if len(candidates) > 1:
            return LocalityResolution(
                LocalityResolutionStatus.MULTIPLE_MATCHES,
                candidates=candidates,
                country_code=normalized_country,
            )
        return LocalityResolution(
            LocalityResolutionStatus.RESOLVED,
            locality=candidates[0],
            candidates=candidates,
            country_code=normalized_country,
        )

    @staticmethod
    def _open_read_only(path: Path) -> sqlite3.Connection:
        try:
            return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise LocalityDatasetUnavailableError(
                f"Could not open GISCO LAU GeoPackage: {path}"
            ) from error

    def _select_layer(self, requested_table: str | None) -> dict:
        try:
            rows = self._connection.execute(
                """
                SELECT contents.table_name, geometry.column_name,
                       geometry.geometry_type_name, geometry.srs_id
                FROM gpkg_contents AS contents
                JOIN gpkg_geometry_columns AS geometry
                  ON geometry.table_name = contents.table_name
                WHERE contents.data_type = 'features'
                """
            ).fetchall()
        except sqlite3.Error as error:
            raise LocalityDatasetError(
                "File is not a readable GeoPackage feature database."
            ) from error

        compatible = []
        for row in rows:
            if requested_table is not None and row["table_name"] != requested_table:
                continue
            geometry_type = str(row["geometry_type_name"]).upper()
            if geometry_type not in {"POLYGON", "MULTIPOLYGON"}:
                continue
            if row["srs_id"] != 4326:
                continue

            columns = self._layer_columns(row["table_name"])
            by_case = {column.casefold(): column for column in columns}
            if not all(column.casefold() in by_case for column in _REQUIRED_COLUMNS):
                continue

            rtree_name = f"rtree_{row['table_name']}_{row['column_name']}"
            if not self._table_exists(rtree_name):
                continue

            compatible.append(
                {
                    "table_name": row["table_name"],
                    "column_name": row["column_name"],
                    "columns": {
                        column: by_case[column.casefold()]
                        for column in _REQUIRED_COLUMNS
                    },
                }
            )

        if len(compatible) == 1:
            return compatible[0]
        if requested_table is not None:
            raise LocalityDatasetError(
                f"GeoPackage layer {requested_table!r} is not a GISCO LAU "
                "Polygon/MultiPolygon layer in EPSG:4326 with an RTree."
            )
        if not compatible:
            raise LocalityDatasetError(
                "GeoPackage has no GISCO LAU Polygon/MultiPolygon layer in "
                "EPSG:4326 with the required fields and an RTree."
            )
        raise LocalityDatasetError(
            "GeoPackage has multiple compatible LAU layers; configure "
            "table_name explicitly."
        )

    def _layer_columns(self, table_name: str) -> tuple[str, ...]:
        try:
            rows = self._connection.execute(
                f"PRAGMA table_info({_quote_identifier(table_name)})"
            ).fetchall()
        except sqlite3.Error as error:
            raise LocalityDatasetError(
                f"Could not inspect GeoPackage layer {table_name!r}."
            ) from error
        return tuple(row["name"] for row in rows)

    def _table_exists(self, table_name: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type IN ('table', 'view') AND name = ?
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    def _load_country_codes(self) -> frozenset[str]:
        country_column = _quote_identifier(self._columns["CNTR_CODE"])
        table = _quote_identifier(self.table_name)
        try:
            rows = self._connection.execute(
                f"SELECT DISTINCT {country_column} AS country_code "
                f"FROM {table}"
            ).fetchall()
        except sqlite3.Error as error:
            raise LocalityDatasetError(
                "Could not read GISCO LAU country coverage."
            ) from error

        countries = {
            country
            for row in rows
            if (country := _country_code(row["country_code"])) is not None
        }
        if not countries:
            raise LocalityDatasetError(
                "GISCO LAU layer contains no valid CNTR_CODE values."
            )
        return frozenset(countries)

    def _candidate_rows(self, longitude: float, latitude: float):
        table = _quote_identifier(self.table_name)
        rtree = _quote_identifier(self.rtree_table)
        geometry = _quote_identifier(self.geometry_column)
        gisco_id = _quote_identifier(self._columns["GISCO_ID"])
        country = _quote_identifier(self._columns["CNTR_CODE"])
        name = _quote_identifier(self._columns["LAU_NAME"])
        try:
            return self._connection.execute(
                f"""
                SELECT feature.{gisco_id} AS gisco_id,
                       feature.{country} AS country_code,
                       feature.{name} AS lau_name,
                       feature.{geometry} AS geometry
                FROM {table} AS feature
                JOIN {rtree} AS bounds ON bounds.id = feature.rowid
                WHERE bounds.minx <= ? AND bounds.maxx >= ?
                  AND bounds.miny <= ? AND bounds.maxy >= ?
                """,
                (longitude, longitude, latitude, latitude),
            ).fetchall()
        except sqlite3.Error as error:
            raise LocalityDatasetError(
                "Could not query the GISCO LAU spatial index."
            ) from error

    def _locality_from_row(self, row: sqlite3.Row) -> DestinationLocality:
        values = tuple(
            str(row[key]).strip()
            if row[key] is not None
            else ""
            for key in ("gisco_id", "country_code", "lau_name")
        )
        gisco_id, country_code, lau_name = values
        normalized_country = _country_code(country_code)
        if not gisco_id or normalized_country is None or not lau_name:
            raise LocalityDatasetError(
                "GISCO LAU feature has missing or invalid identity fields."
            )
        return DestinationLocality(
            gisco_id=gisco_id,
            country_code=normalized_country,
            lau_name=lau_name,
            dataset_year=self.dataset_year,
        )


def build_locality_response(
    origin_name: str,
    travel_date: date,
    qualified_trips,
    locality_index: LocalityIndex,
) -> dict:
    """Group already-qualified arrival events by GISCO_ID."""

    trips = tuple(qualified_trips)
    localities = {}
    services_by_locality = defaultdict(dict)
    unresolved = []
    unsupported = []
    raw_arrival_event_count = 0

    for trip in trips:
        for arrival in trip.get("arrivals", []):
            raw_arrival_event_count += 1
            resolution = locality_index.resolve(
                arrival.get("lat"),
                arrival.get("lon"),
                arrival.get("country_code"),
            )
            if not resolution.resolved:
                event = _unresolved_event(trip, arrival, resolution)
                if (
                    resolution.status
                    is LocalityResolutionStatus.UNSUPPORTED_COUNTRY
                ):
                    unsupported.append(event)
                else:
                    unresolved.append(event)
                continue

            locality = resolution.locality
            if locality is None:
                raise LocalityDatasetError(
                    "Resolved locality result has no locality metadata."
                )
            previous = localities.setdefault(locality.grouping_key, locality)
            if previous != locality:
                raise LocalityDatasetError(
                    f"Inconsistent attributes for {locality.grouping_key}."
                )

            services = services_by_locality[locality.grouping_key]
            service = services.setdefault(
                trip["trip_id"],
                {
                    "trip_id": trip["trip_id"],
                    "service": trip["service"],
                    "operator": trip["operator"],
                    "mode": trip["mode"],
                    "departure": trip["departure"],
                    "stops": [],
                },
            )
            stop = {
                "station": arrival["station"],
                "arrival": arrival["arrival"],
                "lat": arrival.get("lat"),
                "lon": arrival.get("lon"),
            }
            if stop not in service["stops"]:
                service["stops"].append(stop)

    destinations = []
    for gisco_id, locality in localities.items():
        services = list(services_by_locality[gisco_id].values())
        for service in services:
            service["stops"].sort(key=lambda stop: stop["arrival"])
        services.sort(key=lambda service: service["departure"])
        all_stops = [
            stop
            for service in services
            for stop in service["stops"]
        ]
        destinations.append(
            {
                "gisco_id": locality.gisco_id,
                "country_code": locality.country_code,
                "lau_name": locality.lau_name,
                "dataset_year": locality.dataset_year,
                "city": locality.lau_name,
                "earliest_arrival": min(
                    stop["arrival"] for stop in all_stops
                ),
                "service_count": len(services),
                "station_count": len(
                    {
                        (
                            stop["station"],
                            stop.get("lat"),
                            stop.get("lon"),
                        )
                        for stop in all_stops
                    }
                ),
                "services": services,
            }
        )

    destinations.sort(
        key=lambda destination: (
            destination["earliest_arrival"],
            destination["lau_name"].casefold(),
            destination["gisco_id"],
        )
    )
    unresolved.sort(key=_event_sort_key)
    unsupported.sort(key=_event_sort_key)

    return {
        "origin": origin_name,
        "date": travel_date.isoformat(),
        "qualifying_trip_count": len(trips),
        "raw_arrival_event_count": raw_arrival_event_count,
        "destination_count": len(destinations),
        "destinations": destinations,
        "unresolved_arrival_count": len(unresolved),
        "unresolved_arrivals": unresolved,
        "unsupported_country_arrival_count": len(unsupported),
        "unsupported_country_arrivals": unsupported,
    }


def _unresolved_event(
    trip: dict,
    arrival: dict,
    resolution: LocalityResolution,
) -> dict:
    event = {
        "status": resolution.status.value,
        "trip_id": trip.get("trip_id"),
        "station": arrival.get("station"),
        "arrival": arrival.get("arrival"),
        "lat": arrival.get("lat"),
        "lon": arrival.get("lon"),
        "country_code": resolution.country_code,
    }
    if resolution.candidates:
        event["candidate_gisco_ids"] = [
            candidate.gisco_id for candidate in resolution.candidates
        ]
    return event


def _event_sort_key(event: dict):
    return (
        str(event.get("trip_id") or ""),
        str(event.get("arrival") or ""),
        str(event.get("station") or ""),
    )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _country_code(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if len(normalized) != 2 or not normalized.isalpha():
        return None
    return normalized


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


def _geometry_from_geopackage(value) -> BoundaryGeometry:
    data = bytes(value)
    if len(data) < 9 or data[:2] != b"GP":
        raise ValueError("Invalid GeoPackage geometry header.")

    flags = data[3]
    if flags & 0b00010000:
        raise ValueError("Empty GeoPackage geometry.")
    envelope_indicator = (flags >> 1) & 0b111
    envelope_lengths = {0: 0, 1: 4, 2: 6, 3: 6, 4: 8}
    if envelope_indicator not in envelope_lengths:
        raise ValueError("Invalid GeoPackage envelope indicator.")
    offset = 8 + envelope_lengths[envelope_indicator] * 8
    if offset >= len(data):
        raise ValueError("Missing WKB geometry.")

    geometry_type, polygons, final_offset = _read_wkb(data, offset)
    if final_offset > len(data):
        raise ValueError("Truncated WKB geometry.")
    if geometry_type == "Polygon":
        return BoundaryGeometry("Polygon", polygons)
    if geometry_type == "MultiPolygon":
        return BoundaryGeometry("MultiPolygon", polygons)
    raise ValueError("Only Polygon and MultiPolygon are supported.")


def _read_wkb(data: bytes, offset: int):
    byte_order = data[offset]
    if byte_order not in (0, 1):
        raise ValueError("Invalid WKB byte order.")
    endian = "<" if byte_order == 1 else ">"
    offset += 1
    raw_type = struct.unpack_from(f"{endian}I", data, offset)[0]
    offset += 4
    base_type, dimensions, has_srid = _wkb_type(raw_type)
    if has_srid:
        offset += 4

    if base_type == 3:
        ring_count = struct.unpack_from(f"{endian}I", data, offset)[0]
        offset += 4
        rings = []
        for _ in range(ring_count):
            point_count = struct.unpack_from(f"{endian}I", data, offset)[0]
            offset += 4
            ring = []
            for _ in range(point_count):
                coordinates = struct.unpack_from(
                    f"{endian}{'d' * dimensions}", data, offset
                )
                offset += dimensions * 8
                ring.append((coordinates[0], coordinates[1]))
            rings.append(tuple(ring))
        return "Polygon", (tuple(rings),), offset

    if base_type == 6:
        polygon_count = struct.unpack_from(f"{endian}I", data, offset)[0]
        offset += 4
        polygons = []
        for _ in range(polygon_count):
            geometry_type, geometry_polygons, offset = _read_wkb(data, offset)
            if geometry_type != "Polygon":
                raise ValueError("MultiPolygon contains a non-Polygon.")
            polygons.extend(geometry_polygons)
        return "MultiPolygon", tuple(polygons), offset

    raise ValueError("Only Polygon and MultiPolygon WKB are supported.")


def _wkb_type(raw_type: int) -> tuple[int, int, bool]:
    has_z = bool(raw_type & 0x80000000)
    has_m = bool(raw_type & 0x40000000)
    has_srid = bool(raw_type & 0x20000000)
    if raw_type & 0xE0000000:
        base_type = raw_type & 0x0000FFFF
        dimensions = 2 + int(has_z) + int(has_m)
        return base_type, dimensions, has_srid

    dimension_code, base_type = divmod(raw_type, 1000)
    dimensions = {0: 2, 1: 3, 2: 3, 3: 4}.get(dimension_code)
    if dimensions is None:
        raise ValueError("Unsupported WKB dimensionality.")
    return base_type, dimensions, False
