"""Runtime Great Britain locality index and GISCO-first composite routing."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Callable

from backend.localities import (
    GiscoLauIndex,
    LocalityDatasetError,
    LocalityDatasetUnavailableError,
    LocalityIndex,
    LocalityResolution,
    LocalityResolutionError,
    LocalityResolutionStatus,
)


GB_BUA_YEAR = 2026
GB_BUA_LAYER = "nightways_gb_bua"
GB_BUA_GEOMETRY_COLUMN = "geom"
GB_BUA_FEATURE_COUNT = 8_716
GB_BUA_PATH_ENV = "NIGHTWAYS_GB_BUA_PATH"
DEFAULT_GB_BUA_PATH = (
    Path("data") / "geo" / "nightways-gb-bua-2026-04-v1.gpkg"
)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_AGGREGATE_TABLE = "nightways_locality_aggregate_members"
_METADATA_TABLE = "nightways_dataset_metadata"
_LONDON_CODES_SHA256 = (
    "752011a9908115c58a0b211c2a149472113b5651976b60093af7450cad1ad4ba"
)
_LONDON_CODES = (
    "E63019020",
    "E63019085",
    "E63019110",
    "E63019112",
    "E63019113",
    "E63019125",
    "E63019145",
    "E63019182",
    "E63019186",
    "E63019196",
    "E63019198",
    "E63019201",
    "E63019218",
    "E63019221",
    "E63019237",
    "E63019239",
    "E63019243",
    "E63019250",
    "E63019272",
    "E63019295",
    "E63019322",
    "E63019323",
    "E63019345",
    "E63019349",
    "E63019363",
    "E63019369",
    "E63019370",
    "E63019385",
    "E63019458",
    "E63019496",
    "E63019512",
    "E63019573",
    "E63019581",
)
_EXPECTED_FIELDS = (
    ("fid", "INTEGER"),
    ("geom", "MULTIPOLYGON"),
    ("GISCO_ID", "TEXT"),
    ("CNTR_CODE", "TEXT"),
    ("LAU_NAME", "TEXT"),
    ("YEAR", "INTEGER"),
    ("SOURCE_GSS_CODE", "TEXT"),
    ("SOURCE_NAME1_TEXT", "TEXT"),
    ("SOURCE_NAME1_LANGUAGE", "TEXT"),
    ("SOURCE_NAME2_TEXT", "TEXT"),
    ("SOURCE_NAME2_LANGUAGE", "TEXT"),
    ("SOURCE_AREA_HECTARES", "REAL"),
)


def configured_gb_bua_path(environ=None) -> Path:
    """Return the installer-compatible GB artifact path."""

    environment = os.environ if environ is None else environ
    configured = environment.get(GB_BUA_PATH_ENV)
    if configured is None:
        return (_PROJECT_ROOT / DEFAULT_GB_BUA_PATH).resolve()
    if not isinstance(configured, str) or not configured.strip():
        raise LocalityDatasetUnavailableError(
            f"{GB_BUA_PATH_ENV} must name a GeoPackage file."
        )
    return Path(configured).expanduser().resolve()


def _expected_metadata() -> dict[str, str]:
    return {
        "artifact_version": "gb-bua-2026-04-v1",
        "attribution": (
            "Contains Ordnance Survey data © Crown copyright and database "
            "right 2026."
        ),
        "builder_version": "1",
        "derived_crs": "EPSG:4326",
        "derived_feature_count": str(GB_BUA_FEATURE_COUNT),
        "derived_geometry_type": "MULTIPOLYGON",
        "derived_layer": GB_BUA_LAYER,
        "licence_id": "OGL-UK-3.0",
        "licence_notice_sha256": (
            "1fe1f80e39cc9b5ccc92ca5cb2ddd7738128267de3c84914a0cd77bec5aaedde"
        ),
        "licence_url": (
            "https://www.nationalarchives.gov.uk/doc/"
            "open-government-licence/version/3/"
        ),
        "london_aggregate_id": "GB_LONDON",
        "london_aggregate_name": "London",
        "london_codes_sha256": _LONDON_CODES_SHA256,
        "london_member_count": str(len(_LONDON_CODES)),
        "provider": "os_open_built_up_areas",
        "reprojection_accuracy_metres": "1",
        "reprojection_grid_sha256": (
            "5d6ed64d2119952c4c559fa1fccbc594b6520fc3ec3ef2fc10be13202c4384fa"
        ),
        "reprojection_operation": (
            "Inverse of British National Grid + OSGB36 to WGS 84 (9)"
        ),
        "schema_version": "1",
        "source_archive_md5": "d3620702aa841625b3b557c676e295a5",
        "source_archive_sha256": (
            "87750aa939151a003dec2dd0760daaeb342b3e1586168562d3dc16a6911946f0"
        ),
        "source_crs": "EPSG:27700",
        "source_geopackage_sha256": (
            "dbbe4b19b3881c5eb2532c9cc12dcd8c427b578eef2497c25ad7be88a1a65759"
        ),
        "source_layer": "os_open_built_up_areas",
        "source_product": "OS Open Built Up Areas",
        "source_product_id": "BuiltUpAreas",
        "source_release": "2026-04",
    }


class GbBuiltUpAreaIndex(GiscoLauIndex):
    """Read-only, RTree-backed access to the pinned GB BUA artifact."""

    def __init__(self, path: str | Path):
        artifact = Path(path).expanduser().resolve()
        if not artifact.is_file():
            raise LocalityDatasetUnavailableError(
                f"GB BUA {GB_BUA_YEAR} GeoPackage not found: {artifact}. "
                f"Run 'python backend/setup_gb_bua.py' or set {GB_BUA_PATH_ENV}."
            )

        try:
            super().__init__(
                artifact,
                dataset_year=GB_BUA_YEAR,
                table_name=GB_BUA_LAYER,
            )
        except LocalityDatasetUnavailableError as error:
            raise LocalityDatasetUnavailableError(
                f"Could not open GB BUA GeoPackage: {artifact}. Run "
                f"'python backend/setup_gb_bua.py' or set {GB_BUA_PATH_ENV}."
            ) from error
        except LocalityDatasetError as error:
            raise LocalityDatasetError(
                f"GB BUA GeoPackage is incompatible or corrupt: {artifact}."
            ) from error

        try:
            self._validate_runtime_contract()
        except LocalityDatasetError:
            self.close()
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            self.close()
            raise LocalityDatasetError(
                f"GB BUA GeoPackage is incompatible or corrupt: {artifact}."
            ) from error

    @classmethod
    def from_environment(cls, environ=None) -> "GbBuiltUpAreaIndex":
        return cls(configured_gb_bua_path(environ))

    def __enter__(self) -> "GbBuiltUpAreaIndex":
        return self

    @staticmethod
    def _open_read_only(path: Path) -> sqlite3.Connection:
        try:
            return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise LocalityDatasetUnavailableError(
                f"Could not open GB BUA GeoPackage: {path}"
            ) from error

    def _validate_runtime_contract(self) -> None:
        layer = self._connection.execute(
            """
            SELECT contents.data_type,
                   contents.srs_id AS contents_srs_id,
                   geometry.column_name,
                   geometry.geometry_type_name,
                   geometry.srs_id AS geometry_srs_id,
                   geometry.z,
                   geometry.m
            FROM gpkg_contents AS contents
            JOIN gpkg_geometry_columns AS geometry USING (table_name)
            WHERE contents.table_name = ?
            """,
            (GB_BUA_LAYER,),
        ).fetchone()
        if layer is None or tuple(layer) != (
            "features",
            4326,
            GB_BUA_GEOMETRY_COLUMN,
            "MULTIPOLYGON",
            4326,
            0,
            0,
        ):
            raise LocalityDatasetError(
                "GB BUA layer CRS or geometry metadata is invalid."
            )

        columns = self._connection.execute(
            f"PRAGMA table_info({GB_BUA_LAYER})"
        ).fetchall()
        actual_fields = tuple(
            (str(row["name"]), str(row["type"]).upper()) for row in columns
        )
        if actual_fields != _EXPECTED_FIELDS:
            raise LocalityDatasetError("GB BUA feature schema is invalid.")
        if columns[0]["pk"] != 1 or columns[1]["notnull"] != 1:
            raise LocalityDatasetError(
                "GB BUA primary key or geometry nullability is invalid."
            )

        counts = self._connection.execute(
            f"""
            SELECT COUNT(*) AS feature_count,
                   COUNT(DISTINCT SOURCE_GSS_CODE) AS distinct_codes,
                   SUM(CASE WHEN SOURCE_GSS_CODE IS NULL
                                 OR trim(SOURCE_GSS_CODE) = ''
                            THEN 1 ELSE 0 END) AS missing_codes,
                   SUM(CASE WHEN CNTR_CODE IS NULL OR CNTR_CODE != 'GB'
                            THEN 1 ELSE 0 END) AS wrong_country,
                   SUM(CASE WHEN YEAR IS NULL OR YEAR != {GB_BUA_YEAR}
                            THEN 1 ELSE 0 END) AS wrong_year,
                   SUM(CASE WHEN geom IS NULL THEN 1 ELSE 0 END)
                       AS null_geometries
            FROM {GB_BUA_LAYER}
            """
        ).fetchone()
        if (
            counts["feature_count"] != GB_BUA_FEATURE_COUNT
            or counts["distinct_codes"] != GB_BUA_FEATURE_COUNT
            or any(
                counts[key]
                for key in (
                    "missing_codes",
                    "wrong_country",
                    "wrong_year",
                    "null_geometries",
                )
            )
            or self.supported_country_codes != frozenset({"GB"})
        ):
            raise LocalityDatasetError("GB BUA feature values are invalid.")

        metadata = dict(
            self._connection.execute(
                f"SELECT key, value FROM {_METADATA_TABLE} ORDER BY key"
            ).fetchall()
        )
        if metadata != _expected_metadata():
            raise LocalityDatasetError("GB BUA embedded metadata is invalid.")

        aggregate_rows = self._connection.execute(
            f"""
            SELECT aggregate_id, display_name, source_gss_code
            FROM {_AGGREGATE_TABLE}
            ORDER BY source_gss_code
            """
        ).fetchall()
        expected_aggregates = tuple(
            ("GB_LONDON", "London", code) for code in _LONDON_CODES
        )
        if tuple(tuple(row) for row in aggregate_rows) != expected_aggregates:
            raise LocalityDatasetError("GB BUA London metadata is invalid.")

        placeholders = ", ".join("?" for _code in _LONDON_CODES)
        london_rows = self._connection.execute(
            f"""
            SELECT SOURCE_GSS_CODE, GISCO_ID, LAU_NAME
            FROM {GB_BUA_LAYER}
            WHERE SOURCE_GSS_CODE IN ({placeholders})
               OR GISCO_ID = 'GB_LONDON'
               OR LAU_NAME = 'London'
            ORDER BY SOURCE_GSS_CODE
            """,
            _LONDON_CODES,
        ).fetchall()
        expected_london = tuple(
            (code, "GB_LONDON", "London") for code in _LONDON_CODES
        )
        if tuple(tuple(row) for row in london_rows) != expected_london:
            raise LocalityDatasetError("GB BUA London membership is invalid.")

        rtree = self.rtree_table
        rtree_count = self._connection.execute(
            f'SELECT COUNT(*) FROM "{rtree}"'
        ).fetchone()[0]
        unmatched = self._connection.execute(
            f"""
            SELECT COUNT(*)
            FROM {GB_BUA_LAYER} AS feature
            LEFT JOIN "{rtree}" AS bounds ON bounds.id = feature.rowid
            WHERE bounds.id IS NULL
            """
        ).fetchone()[0]
        orphans = self._connection.execute(
            f"""
            SELECT COUNT(*)
            FROM "{rtree}" AS bounds
            LEFT JOIN {GB_BUA_LAYER} AS feature ON feature.rowid = bounds.id
            WHERE feature.rowid IS NULL
            """
        ).fetchone()[0]
        invalid_bounds = self._connection.execute(
            f"""
            SELECT COUNT(*) FROM "{rtree}"
            WHERE minx IS NULL OR maxx IS NULL OR miny IS NULL OR maxy IS NULL
               OR minx > maxx OR miny > maxy
            """
        ).fetchone()[0]
        extension = self._connection.execute(
            """
            SELECT extension_name
            FROM gpkg_extensions
            WHERE table_name = ? AND column_name = ?
            """,
            (GB_BUA_LAYER, GB_BUA_GEOMETRY_COLUMN),
        ).fetchall()
        if (
            rtree_count != GB_BUA_FEATURE_COUNT
            or unmatched
            or orphans
            or invalid_bounds
            or tuple(row[0] for row in extension) != ("gpkg_rtree_index",)
        ):
            raise LocalityDatasetError("GB BUA RTree is invalid.")


class CompositeLocalityIndex:
    """Route GB to BUA while preserving GISCO results and errors exactly."""

    def __init__(
        self,
        gisco_index: LocalityIndex,
        gb_index: LocalityIndex | None = None,
        *,
        gb_index_factory: Callable[[], LocalityIndex] | None = None,
    ):
        if gb_index is not None and gb_index_factory is not None:
            raise ValueError("Provide either gb_index or gb_index_factory, not both.")
        self.gisco_index = gisco_index
        self._gb_index = gb_index
        self._gb_index_factory = (
            GbBuiltUpAreaIndex.from_environment
            if gb_index is None and gb_index_factory is None
            else gb_index_factory
        )
        self._closed = False

    @classmethod
    def from_environment(cls, environ=None) -> "CompositeLocalityIndex":
        environment = os.environ if environ is None else environ
        gb_environment = {}
        if GB_BUA_PATH_ENV in environment:
            gb_environment[GB_BUA_PATH_ENV] = environment[GB_BUA_PATH_ENV]
        gisco_index = GiscoLauIndex.from_environment(environ=environ)
        return cls(
            gisco_index,
            gb_index_factory=(
                lambda: GbBuiltUpAreaIndex.from_environment(gb_environment)
            ),
        )

    def resolve(
        self,
        lat: float,
        lon: float,
        country_code: str | None = None,
    ) -> LocalityResolution:
        if self._closed:
            raise LocalityResolutionError("Composite locality index is closed.")

        is_blank = country_code is None or (
            isinstance(country_code, str) and not country_code.strip()
        )
        is_gb = (
            isinstance(country_code, str)
            and country_code.strip().upper() == "GB"
        )
        if is_gb:
            return self._get_gb_index().resolve(lat, lon, country_code)
        if not is_blank:
            return self.gisco_index.resolve(lat, lon, country_code)

        gisco_result = self.gisco_index.resolve(lat, lon, country_code)
        if gisco_result.status is not LocalityResolutionStatus.NO_MATCH:
            return gisco_result
        return self._get_gb_index().resolve(lat, lon, country_code)

    def _get_gb_index(self) -> LocalityIndex:
        if self._closed:
            raise LocalityResolutionError("Composite locality index is closed.")
        if self._gb_index is not None:
            return self._gb_index
        if self._gb_index_factory is None:
            raise LocalityDatasetUnavailableError(
                "No GB BUA locality provider is configured."
            )
        try:
            index = self._gb_index_factory()
        except LocalityResolutionError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise LocalityDatasetUnavailableError(
                "Could not initialize the GB BUA locality provider."
            ) from error
        if not callable(getattr(index, "resolve", None)):
            close = getattr(index, "close", None)
            if callable(close):
                close()
            raise LocalityDatasetError(
                "Configured GB BUA provider does not implement locality resolution."
            )
        self._gb_index = index
        return index

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        gb_error = None
        if self._gb_index is not None:
            try:
                self._gb_index.close()
            except Exception as error:  # pragma: no cover - defensive cleanup
                gb_error = error
        try:
            self.gisco_index.close()
        except Exception:
            if gb_error is None:
                raise
        if gb_error is not None:
            raise gb_error

    def __enter__(self) -> "CompositeLocalityIndex":
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.close()
