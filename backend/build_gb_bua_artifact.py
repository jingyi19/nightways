"""Build and validate the pinned Nightways GB built-up-area artifact.

This is a maintainer-only build tool. It uses an external GDAL/PROJ toolchain
to reproject the approved OS Open Built Up Areas release, then uses only the
Python standard library to add Nightways metadata and validate the result.
It is not imported by the Nightways runtime and adds no production dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = (
    PROJECT_ROOT / "data" / "manifests" / "gb-bua-2026-04-v1.json"
)
DEFAULT_LICENCE_PATH = (
    PROJECT_ROOT / "data" / "licenses" / "os-open-built-up-areas-2026.txt"
)

SOURCE_PRODUCT = "OS Open Built Up Areas"
SOURCE_RELEASE = "2026-04"
SOURCE_LAYER = "os_open_built_up_areas"
SOURCE_CRS_ID = 27700
SOURCE_GEOMETRY_TYPE = "MULTIPOLYGON"
SOURCE_FEATURE_COUNT = 8716
SOURCE_ARCHIVE_SIZE = 43_627_925
SOURCE_ARCHIVE_MD5 = "d3620702aa841625b3b557c676e295a5"
SOURCE_ARCHIVE_SHA256 = (
    "87750aa939151a003dec2dd0760daaeb342b3e1586168562d3dc16a6911946f0"
)
SOURCE_GPKG_MEMBER = "os_open_built_up_areas.gpkg"
SOURCE_GPKG_SIZE = 286_314_496
SOURCE_GPKG_SHA256 = (
    "dbbe4b19b3881c5eb2532c9cc12dcd8c427b578eef2497c25ad7be88a1a65759"
)
SOURCE_LICENCE_MEMBER = "licence.txt"
SOURCE_LICENCE_SHA256 = (
    "63f326b33f686abfc3985ecd29d7752b00b3a18e78543a41d707500cd92543a3"
)

OUTPUT_LAYER = "nightways_gb_bua"
OUTPUT_GEOMETRY_COLUMN = "geom"
OUTPUT_CRS_ID = 4326
OUTPUT_GEOMETRY_TYPE = "MULTIPOLYGON"
OUTPUT_YEAR = 2026
OUTPUT_COUNTRY = "GB"
AGGREGATE_TABLE = "nightways_locality_aggregate_members"
METADATA_TABLE = "nightways_dataset_metadata"
FIXED_GPKG_TIMESTAMP = "2026-04-01T00:00:00.000Z"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

ATTRIBUTION = (
    "Contains Ordnance Survey data © Crown copyright and database right 2026."
)
OSTN15_FILENAME = "uk_os_OSTN15_NTv2_OSGBtoETRS.tif"
OSTN15_SIZE = 3_035_814
OSTN15_SHA256 = (
    "5d6ed64d2119952c4c559fa1fccbc594b6520fc3ec3ef2fc10be13202c4384fa"
)
PROJ_DATABASE_SIZE = 8_712_192
PROJ_DATABASE_SHA256 = (
    "f58ae79e02d25e54ee962dc189d5771fe728f611c3f9feca8d2dc9633e76d114"
)
EXPECTED_PROJECTION_OPERATION = (
    "Inverse of British National Grid + OSGB36 to WGS 84 (9)"
)

SOURCE_FIELDS = (
    "gsscode",
    "name1_text",
    "name1_language",
    "name2_text",
    "name2_language",
    "areahectares",
)

DERIVED_FIELDS = (
    ("fid", "INTEGER"),
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
    ("geom", "MULTIPOLYGON"),
)

LONDON_CODES = (
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
LONDON_CODES_SHA256 = (
    "752011a9908115c58a0b211c2a149472113b5651976b60093af7450cad1ad4ba"
)


@dataclass(frozen=True)
class ValidationPoint:
    name: str
    transitous_stop_id: str
    lat: float
    lon: float
    source_gss_code: str
    gisco_id: str
    display_name: str


VALIDATION_POINTS = (
    ValidationPoint(
        "London Victoria",
        "gb-great-britain_910GVICTRIA",
        51.49473,
        -0.1445802,
        "E63019250",
        "GB_LONDON",
        "London",
    ),
    ValidationPoint(
        "London St Pancras",
        "gb-great-britain_910GSTPX",
        51.53272,
        -0.1270027,
        "E63019201",
        "GB_LONDON",
        "London",
    ),
    ValidationPoint(
        "Manchester Piccadilly",
        "gb-great-britain_910GMNCRPIC",
        53.47722,
        -2.2301402,
        "E63015571",
        "E63015571",
        "Manchester",
    ),
    ValidationPoint(
        "Birmingham New Street",
        "gb-great-britain_910GBHAMNWS",
        52.477646,
        -1.898694,
        "E63017231",
        "E63017231",
        "Birmingham",
    ),
    ValidationPoint(
        "Edinburgh Haymarket",
        "gb-great-britain_910GHAYMRKT",
        55.945183,
        -3.2193737,
        "S45001942",
        "S45001942",
        "Edinburgh",
    ),
    ValidationPoint(
        "Glasgow Central",
        "gb-great-britain_910GGLGC",
        55.858315,
        -4.258436,
        "S45001999",
        "S45001999",
        "Glasgow",
    ),
    ValidationPoint(
        "Cardiff Central",
        "gb-great-britain_910GCRDFCEN",
        51.475548,
        -3.1797056,
        "W45001843",
        "W45001843",
        "Cardiff",
    ),
)


class GbBuaBuildError(RuntimeError):
    """Raised when an artifact build or validation fails closed."""


@dataclass(frozen=True)
class BuildResult:
    geopackage_path: Path
    zip_path: Path
    geopackage_size_bytes: int
    zip_size_bytes: int
    geopackage_sha256: str
    zip_sha256: str
    validation_results: tuple[dict, ...]


def _duplicate_rejecting_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GbBuaBuildError(f"Manifest contains duplicate key {key!r}.")
        result[key] = value
    return result


def load_manifest(path: Path) -> dict:
    """Load and verify the tracked release manifest."""

    try:
        text = path.read_text(encoding="utf-8")
        manifest = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GbBuaBuildError(f"Could not read manifest: {path}") from error
    if not isinstance(manifest, dict):
        raise GbBuaBuildError("Manifest root must be an object.")
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: Mapping) -> None:
    expected_top_level = {
        "manifest_schema_version",
        "artifact_version",
        "source",
        "reprojection",
        "derived",
        "london_aggregation",
        "licence",
        "validation_points",
        "builder",
    }
    if set(manifest) != expected_top_level:
        raise GbBuaBuildError("Manifest top-level keys do not match schema v1.")
    if manifest["manifest_schema_version"] != 1:
        raise GbBuaBuildError("Unsupported manifest schema version.")
    if manifest["artifact_version"] != "gb-bua-2026-04-v1":
        raise GbBuaBuildError("Unexpected artifact version.")

    source = manifest["source"]
    expected_source = {
        "product": SOURCE_PRODUCT,
        "release": SOURCE_RELEASE,
        "archive_size_bytes": SOURCE_ARCHIVE_SIZE,
        "archive_md5": SOURCE_ARCHIVE_MD5,
        "archive_sha256": SOURCE_ARCHIVE_SHA256,
        "geopackage_member": SOURCE_GPKG_MEMBER,
        "geopackage_size_bytes": SOURCE_GPKG_SIZE,
        "geopackage_sha256": SOURCE_GPKG_SHA256,
        "licence_member": SOURCE_LICENCE_MEMBER,
        "licence_sha256": SOURCE_LICENCE_SHA256,
        "layer": SOURCE_LAYER,
        "crs": f"EPSG:{SOURCE_CRS_ID}",
        "geometry_type": SOURCE_GEOMETRY_TYPE,
        "feature_count": SOURCE_FEATURE_COUNT,
    }
    for key, expected in expected_source.items():
        if source.get(key) != expected:
            raise GbBuaBuildError(f"Manifest source {key!r} is not pinned.")
    if tuple(source.get("required_fields", ())) != SOURCE_FIELDS:
        raise GbBuaBuildError("Manifest source field contract is invalid.")

    reprojection = manifest["reprojection"]
    expected_reprojection = {
        "target_crs": f"EPSG:{OUTPUT_CRS_ID}",
        "operation": EXPECTED_PROJECTION_OPERATION,
        "accuracy_metres": 1,
        "proj_network": False,
        "grid_filename": OSTN15_FILENAME,
        "grid_size_bytes": OSTN15_SIZE,
        "grid_sha256": OSTN15_SHA256,
        "proj_database_filename": "proj.db",
        "proj_database_size_bytes": PROJ_DATABASE_SIZE,
        "proj_database_sha256": PROJ_DATABASE_SHA256,
        "proj_version": "9.3.1",
        "proj_data_version": "1.16",
    }
    for key, expected in expected_reprojection.items():
        if reprojection.get(key) != expected:
            raise GbBuaBuildError(
                f"Manifest reprojection {key!r} is not pinned."
            )

    derived = manifest["derived"]
    expected_derived = {
        "layer": OUTPUT_LAYER,
        "crs": f"EPSG:{OUTPUT_CRS_ID}",
        "geometry_type": OUTPUT_GEOMETRY_TYPE,
        "feature_count": SOURCE_FEATURE_COUNT,
        "geometry_column": OUTPUT_GEOMETRY_COLUMN,
        "geopackage_filename": "nightways-gb-bua-2026-04-v1.gpkg",
        "zip_filename": "nightways-gb-bua-2026-04-v1.zip",
    }
    for key, expected in expected_derived.items():
        if derived.get(key) != expected:
            raise GbBuaBuildError(f"Manifest derived {key!r} is invalid.")
    manifest_fields = tuple(
        (field.get("name"), field.get("type"))
        for field in derived.get("fields", ())
        if isinstance(field, dict)
    )
    if manifest_fields != DERIVED_FIELDS:
        raise GbBuaBuildError("Manifest derived field contract is invalid.")
    for key in (
        "geopackage_size_bytes",
        "zip_size_bytes",
        "geopackage_sha256",
        "zip_sha256",
    ):
        value = derived.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, str))
            or (isinstance(value, str) and len(value) != 64)
        ):
            raise GbBuaBuildError(f"Manifest derived {key!r} is invalid.")

    london = manifest["london_aggregation"]
    if london.get("aggregate_id") != "GB_LONDON":
        raise GbBuaBuildError("Manifest London aggregate id is invalid.")
    if london.get("display_name") != "London":
        raise GbBuaBuildError("Manifest London display name is invalid.")
    if london.get("member_count") != len(LONDON_CODES):
        raise GbBuaBuildError("Manifest London member count is invalid.")
    if tuple(london.get("member_codes", ())) != LONDON_CODES:
        raise GbBuaBuildError("Manifest London membership is invalid.")
    if london.get("codes_sha256") != LONDON_CODES_SHA256:
        raise GbBuaBuildError("Manifest London membership digest is invalid.")
    calculated_london_digest = hashlib.sha256(
        ("\n".join(LONDON_CODES) + "\n").encode("ascii")
    ).hexdigest()
    if calculated_london_digest != LONDON_CODES_SHA256:
        raise GbBuaBuildError("Compiled London membership digest is invalid.")

    licence = manifest["licence"]
    if licence.get("id") != "OGL-UK-3.0":
        raise GbBuaBuildError("Manifest licence id is invalid.")
    if licence.get("attribution") != ATTRIBUTION:
        raise GbBuaBuildError("Manifest attribution is invalid.")

    manifest_points = manifest.get("validation_points")
    expected_points = [point.__dict__ for point in VALIDATION_POINTS]
    if manifest_points != expected_points:
        raise GbBuaBuildError("Manifest validation-point contract is invalid.")


def _hashes(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_file(
    path: Path,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> None:
    if not path.is_file():
        raise GbBuaBuildError(f"{label} not found: {path}")
    size = path.stat().st_size
    if size != expected_size:
        raise GbBuaBuildError(
            f"{label} has {size} bytes; expected {expected_size}."
        )
    digest = _sha256(path)
    if digest != expected_sha256:
        raise GbBuaBuildError(
            f"{label} SHA-256 {digest} does not match the pinned value."
        )


def _safe_archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise GbBuaBuildError("Source archive contains duplicate member names.")
    expected = {SOURCE_GPKG_MEMBER, SOURCE_LICENCE_MEMBER}
    if set(names) != expected:
        raise GbBuaBuildError(
            "Source archive members differ from the pinned two-file contract."
        )
    result = {}
    for info in infos:
        member = PurePosixPath(info.filename)
        mode = info.external_attr >> 16
        if (
            info.is_dir()
            or member.is_absolute()
            or member.name != info.filename
            or ".." in member.parts
            or info.flag_bits & 0x1
            or stat.S_ISLNK(mode)
        ):
            raise GbBuaBuildError(
                f"Unsafe source archive member: {info.filename!r}."
            )
        result[info.filename] = info
    if result[SOURCE_GPKG_MEMBER].file_size != SOURCE_GPKG_SIZE:
        raise GbBuaBuildError("Source GeoPackage member size is invalid.")
    if result[SOURCE_LICENCE_MEMBER].file_size > 64 * 1024:
        raise GbBuaBuildError("Source licence member is unexpectedly large.")
    return result


def validate_and_extract_source(source_zip: Path, work_dir: Path) -> Path:
    """Validate the pinned OS archive, safely extract it, and return the GPKG."""

    if not source_zip.is_file():
        raise GbBuaBuildError(f"Source archive not found: {source_zip}")
    if source_zip.stat().st_size != SOURCE_ARCHIVE_SIZE:
        raise GbBuaBuildError("Source archive size does not match release pin.")
    md5, sha256 = _hashes(source_zip)
    if md5 != SOURCE_ARCHIVE_MD5:
        raise GbBuaBuildError(
            f"Source MD5 {md5} does not match {SOURCE_ARCHIVE_MD5}."
        )
    if sha256 != SOURCE_ARCHIVE_SHA256:
        raise GbBuaBuildError("Source archive SHA-256 does not match release pin.")

    try:
        with zipfile.ZipFile(source_zip, "r") as archive:
            members = _safe_archive_members(archive)
            bad_member = archive.testzip()
            if bad_member is not None:
                raise GbBuaBuildError(
                    f"Source archive CRC failed for {bad_member!r}."
                )
            licence_bytes = archive.read(members[SOURCE_LICENCE_MEMBER])
            if hashlib.sha256(licence_bytes).hexdigest() != SOURCE_LICENCE_SHA256:
                raise GbBuaBuildError("Source licence checksum is invalid.")
            try:
                licence_text = licence_bytes.decode("utf-8-sig")
            except UnicodeError as error:
                raise GbBuaBuildError("Source licence is not readable UTF-8.") from error
            if ATTRIBUTION not in licence_text:
                raise GbBuaBuildError("Source licence lacks required attribution.")

            extracted = work_dir / SOURCE_GPKG_MEMBER
            with archive.open(members[SOURCE_GPKG_MEMBER], "r") as source:
                with extracted.open("wb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
    except (OSError, zipfile.BadZipFile) as error:
        raise GbBuaBuildError("Source archive is unreadable.") from error

    _validate_file(
        extracted,
        SOURCE_GPKG_SIZE,
        SOURCE_GPKG_SHA256,
        "Extracted source GeoPackage",
    )
    return extracted


def _open_read_only(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise GbBuaBuildError(f"Could not open GeoPackage: {path}") from error
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    escaped = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    return tuple(row["name"] for row in rows)


def _selected_english_name(row: Mapping) -> str:
    name1 = str(row["name1_text"] or "").strip()
    language1 = str(row["name1_language"] or "").strip().casefold()
    name2 = str(row["name2_text"] or "").strip()
    language2 = str(row["name2_language"] or "").strip().casefold()
    if language1 in ("", "eng") and name1:
        return name1
    if language2 == "eng" and name2:
        return name2
    raise GbBuaBuildError(
        f"No English/default display name for {row['gsscode']!r}."
    )


def validate_source_geopackage(path: Path) -> None:
    """Fail unless the extracted file is exactly the approved source shape."""

    with closing(_open_read_only(path)) as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise GbBuaBuildError(f"Source GeoPackage quick_check: {integrity}")

        layer = connection.execute(
            """
            SELECT c.table_name, c.data_type, c.srs_id,
                   g.column_name, g.geometry_type_name, g.z, g.m
            FROM gpkg_contents AS c
            JOIN gpkg_geometry_columns AS g USING (table_name)
            WHERE c.table_name = ?
            """,
            (SOURCE_LAYER,),
        ).fetchone()
        if layer is None:
            raise GbBuaBuildError(f"Source layer {SOURCE_LAYER!r} is missing.")
        if (
            layer["data_type"] != "features"
            or layer["srs_id"] != SOURCE_CRS_ID
            or layer["geometry_type_name"].upper() != SOURCE_GEOMETRY_TYPE
            or layer["z"] != 0
            or layer["m"] != 0
        ):
            raise GbBuaBuildError("Source layer CRS or geometry contract is invalid.")

        columns = set(_column_names(connection, SOURCE_LAYER))
        required = {"fid", layer["column_name"], *SOURCE_FIELDS}
        if not required.issubset(columns):
            raise GbBuaBuildError("Source layer is missing required fields.")

        escaped_geometry = layer["column_name"].replace('"', '""')
        counts = connection.execute(
            f"""
            SELECT COUNT(*) AS feature_count,
                   COUNT(DISTINCT trim(gsscode)) AS distinct_codes,
                   SUM(CASE WHEN gsscode IS NULL OR trim(gsscode) = ''
                            THEN 1 ELSE 0 END) AS missing_codes,
                   SUM(CASE WHEN "{escaped_geometry}" IS NULL
                            THEN 1 ELSE 0 END) AS null_geometries,
                   SUM(CASE WHEN areahectares IS NULL
                            THEN 1 ELSE 0 END) AS null_areas
            FROM "{SOURCE_LAYER}"
            """
        ).fetchone()
        if counts["feature_count"] != SOURCE_FEATURE_COUNT:
            raise GbBuaBuildError("Source feature count is invalid.")
        if counts["distinct_codes"] != SOURCE_FEATURE_COUNT:
            raise GbBuaBuildError("Source GSS codes are duplicated.")
        if counts["missing_codes"] or counts["null_geometries"]:
            raise GbBuaBuildError("Source has missing GSS codes or geometries.")
        if counts["null_areas"]:
            raise GbBuaBuildError("Source has missing area values.")

        rows = connection.execute(
            f"""
            SELECT gsscode, name1_text, name1_language,
                   name2_text, name2_language
            FROM "{SOURCE_LAYER}"
            ORDER BY trim(gsscode)
            """
        ).fetchall()
        codes = []
        for row in rows:
            code = str(row["gsscode"] or "").strip()
            if not code:
                raise GbBuaBuildError("Source has an empty GSS code.")
            codes.append(code)
            _selected_english_name(row)
        if len(codes) != len(set(codes)):
            raise GbBuaBuildError("Source has duplicate normalized GSS codes.")
        if not set(LONDON_CODES).issubset(codes):
            raise GbBuaBuildError("Source is missing a London aggregation member.")

        cardiff = connection.execute(
            f"""
            SELECT gsscode, name1_text, name1_language,
                   name2_text, name2_language
            FROM "{SOURCE_LAYER}" WHERE trim(gsscode) = 'W45001843'
            """
        ).fetchone()
        if cardiff is None or (
            str(cardiff["name1_text"] or "").strip() != "Caerdydd"
            or str(cardiff["name1_language"] or "").strip().casefold()
            != "cym"
            or str(cardiff["name2_text"] or "").strip() != "Cardiff"
            or str(cardiff["name2_language"] or "").strip().casefold()
            != "eng"
            or _selected_english_name(cardiff) != "Cardiff"
        ):
            raise GbBuaBuildError("Cardiff English-name source contract changed.")

        _validate_rtree(
            connection,
            SOURCE_LAYER,
            layer["column_name"],
            SOURCE_FEATURE_COUNT,
        )


def _validate_rtree(
    connection: sqlite3.Connection,
    feature_table: str,
    geometry_column: str,
    expected_count: int,
) -> None:
    rtree = f"rtree_{feature_table}_{geometry_column}"
    if not _table_exists(connection, rtree):
        raise GbBuaBuildError(f"Required RTree {rtree!r} is missing.")
    rtree_count = connection.execute(
        f'SELECT COUNT(*) FROM "{rtree}"'
    ).fetchone()[0]
    if rtree_count != expected_count:
        raise GbBuaBuildError(f"RTree {rtree!r} has incomplete coverage.")
    unmatched = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM "{feature_table}" AS feature
        LEFT JOIN "{rtree}" AS bounds ON bounds.id = feature.rowid
        WHERE bounds.id IS NULL
        """
    ).fetchone()[0]
    orphans = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM "{rtree}" AS bounds
        LEFT JOIN "{feature_table}" AS feature ON feature.rowid = bounds.id
        WHERE feature.rowid IS NULL
        """
    ).fetchone()[0]
    invalid_bounds = connection.execute(
        f"""
        SELECT COUNT(*) FROM "{rtree}"
        WHERE minx > maxx OR miny > maxy
           OR minx IS NULL OR maxx IS NULL OR miny IS NULL OR maxy IS NULL
        """
    ).fetchone()[0]
    if unmatched or orphans or invalid_bounds:
        raise GbBuaBuildError(f"RTree {rtree!r} is inconsistent.")

    extension = connection.execute(
        """
        SELECT extension_name
        FROM gpkg_extensions
        WHERE table_name = ? AND column_name = ?
        """,
        (feature_table, geometry_column),
    ).fetchone()
    if extension is None or extension[0] != "gpkg_rtree_index":
        raise GbBuaBuildError("GeoPackage RTree extension metadata is invalid.")

    expected_triggers = {
        f"{rtree}_insert",
        f"{rtree}_update1",
        f"{rtree}_update2",
        f"{rtree}_update3",
        f"{rtree}_update4",
        f"{rtree}_delete",
    }
    actual_triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
        if row[0].startswith(f"{rtree}_")
    }
    if actual_triggers != expected_triggers:
        raise GbBuaBuildError("GeoPackage RTree triggers are invalid.")


def _resolve_executable(
    explicit: Optional[Path],
    name: str,
    candidates: Sequence[Path],
) -> Path:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise GbBuaBuildError(f"{name} executable not found: {path}")
        return path
    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise GbBuaBuildError(f"Could not find required {name} executable.")


def _resolve_directory(
    explicit: Optional[Path],
    environment_names: Sequence[str],
    candidates: Sequence[Path],
    marker: str,
    label: str,
) -> Path:
    choices = []
    if explicit is not None:
        choices.append(explicit)
    for name in environment_names:
        value = os.environ.get(name)
        if value:
            choices.extend(Path(part) for part in value.split(os.pathsep) if part)
    choices.extend(candidates)
    for choice in choices:
        resolved = choice.expanduser().resolve()
        if resolved.is_dir() and (resolved / marker).is_file():
            return resolved
    raise GbBuaBuildError(f"Could not find {label} containing {marker!r}.")


def _run_tool(
    command: Sequence[str],
    environment: Mapping[str, str],
    label: str,
    timeout: int = 1800,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(command),
            env=dict(environment),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GbBuaBuildError(f"Could not run {label}.") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GbBuaBuildError(
            f"{label} failed with exit {result.returncode}: {detail}"
        )
    return result


def _build_environment(
    gdal_data: Path,
    proj_data: Path,
    grid_path: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    proj_paths = [str(proj_data)]
    if grid_path.parent.resolve() != proj_data.resolve():
        proj_paths.append(str(grid_path.parent.resolve()))
    proj_search_path = os.pathsep.join(proj_paths)
    environment.update(
        {
            "GDAL_DATA": str(gdal_data),
            "PROJ_DATA": proj_search_path,
            "PROJ_LIB": proj_search_path,
            "PROJ_NETWORK": "OFF",
            "PROJ_ONLY_BEST_DEFAULT": "YES",
            "OGR_CURRENT_DATE": FIXED_GPKG_TIMESTAMP,
            "TZ": "UTC",
        }
    )
    plugin_path = gdal_data.parents[2] / "lib" / "gdalplugins"
    if plugin_path.is_dir():
        environment["GDAL_DRIVER_PATH"] = str(plugin_path)
    return environment


def validate_toolchain(
    ogr2ogr: Path,
    projinfo: Path,
    gdal_data: Path,
    proj_data: Path,
    grid_path: Path,
    manifest: Mapping,
) -> dict[str, str]:
    """Validate the canonical build stack and authoritative grid operation."""

    builder = manifest["builder"]
    expected_python = builder["canonical_python_version"]
    expected_sqlite = builder["canonical_sqlite_version"]
    expected_zlib = builder["canonical_zlib_version"]
    if sys.version.split()[0] != expected_python:
        raise GbBuaBuildError(
            f"Builder Python is {sys.version.split()[0]}; expected {expected_python}."
        )
    if sqlite3.sqlite_version != expected_sqlite:
        raise GbBuaBuildError(
            f"Builder SQLite is {sqlite3.sqlite_version}; expected {expected_sqlite}."
        )
    if zlib.ZLIB_VERSION != expected_zlib:
        raise GbBuaBuildError(
            f"Builder zlib is {zlib.ZLIB_VERSION}; expected {expected_zlib}."
        )

    _validate_file(grid_path, OSTN15_SIZE, OSTN15_SHA256, "OSTN15 grid")
    proj_database = proj_data / "proj.db"
    _validate_file(
        proj_database,
        PROJ_DATABASE_SIZE,
        PROJ_DATABASE_SHA256,
        "PROJ database",
    )
    try:
        with closing(_open_read_only(proj_database)) as connection:
            proj_metadata = dict(
                connection.execute("SELECT key, value FROM metadata").fetchall()
            )
    except sqlite3.Error as error:
        raise GbBuaBuildError("Could not inspect the pinned PROJ database.") from error
    if proj_metadata.get("PROJ.VERSION") != builder["canonical_proj_version"]:
        raise GbBuaBuildError("PROJ database/runtime version pin is invalid.")
    if proj_metadata.get("PROJ_DATA.VERSION") != manifest["reprojection"][
        "proj_data_version"
    ]:
        raise GbBuaBuildError("PROJ data version pin is invalid.")
    environment = _build_environment(gdal_data, proj_data, grid_path)
    version = _run_tool(
        (str(ogr2ogr), "--version"),
        environment,
        "ogr2ogr version check",
        timeout=60,
    ).stdout.strip()
    expected_gdal = builder["canonical_gdal_version"]
    if not version.startswith(f"GDAL {expected_gdal},"):
        raise GbBuaBuildError(
            f"GDAL version {version!r} does not match {expected_gdal}."
        )

    projection = _run_tool(
        (
            str(projinfo),
            "-s",
            f"EPSG:{SOURCE_CRS_ID}",
            "-t",
            f"EPSG:{OUTPUT_CRS_ID}",
            "--bbox",
            "-9,49,2,61",
            "--spatial-test",
            "intersects",
            "--grid-check",
            "known_available",
            "--summary",
        ),
        environment,
        "PROJ operation check",
        timeout=60,
    ).stdout
    operation_lines = [
        line.strip()
        for line in projection.splitlines()
        if line.strip().startswith("unknown id,")
    ]
    if not operation_lines or EXPECTED_PROJECTION_OPERATION not in operation_lines[0]:
        raise GbBuaBuildError(
            "PROJ did not select the pinned OSTN15-backed operation first."
        )
    if "1 m," not in operation_lines[0]:
        raise GbBuaBuildError("Pinned PROJ operation accuracy changed.")
    return environment


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _transformation_sql() -> str:
    london_values = ", ".join(_sql_literal(code) for code in LONDON_CODES)
    english_name = """
        CASE
            WHEN lower(trim(coalesce(name1_language, ''))) IN ('', 'eng')
                 AND trim(coalesce(name1_text, '')) <> ''
                THEN trim(name1_text)
            WHEN lower(trim(coalesce(name2_language, ''))) = 'eng'
                 AND trim(coalesce(name2_text, '')) <> ''
                THEN trim(name2_text)
            ELSE NULL
        END
    """.strip()
    return f"""
        SELECT
            CASE WHEN trim(gsscode) IN ({london_values})
                 THEN 'GB_LONDON' ELSE trim(gsscode) END AS GISCO_ID,
            'GB' AS CNTR_CODE,
            CASE WHEN trim(gsscode) IN ({london_values})
                 THEN 'London' ELSE ({english_name}) END AS LAU_NAME,
            CAST({OUTPUT_YEAR} AS INTEGER) AS YEAR,
            trim(gsscode) AS SOURCE_GSS_CODE,
            trim(name1_text) AS SOURCE_NAME1_TEXT,
            nullif(lower(trim(coalesce(name1_language, ''))), '')
                AS SOURCE_NAME1_LANGUAGE,
            nullif(trim(coalesce(name2_text, '')), '') AS SOURCE_NAME2_TEXT,
            nullif(lower(trim(coalesce(name2_language, ''))), '')
                AS SOURCE_NAME2_LANGUAGE,
            CAST(areahectares AS REAL) AS SOURCE_AREA_HECTARES,
            geometry
        FROM {SOURCE_LAYER}
        ORDER BY trim(gsscode)
    """.strip() + "\n"


def _run_reprojection(
    source_gpkg: Path,
    output_gpkg: Path,
    sql_path: Path,
    ogr2ogr: Path,
    environment: Mapping[str, str],
) -> None:
    sql_path.write_text(_transformation_sql(), encoding="utf-8", newline="\n")
    command = (
        str(ogr2ogr),
        "--config",
        "PROJ_NETWORK",
        "OFF",
        "--config",
        "PROJ_ONLY_BEST_DEFAULT",
        "YES",
        "--config",
        "OGR_CURRENT_DATE",
        FIXED_GPKG_TIMESTAMP,
        "-f",
        "GPKG",
        str(output_gpkg),
        str(source_gpkg),
        "-sql",
        f"@{sql_path}",
        "-dialect",
        "SQLite",
        "-s_srs",
        f"EPSG:{SOURCE_CRS_ID}",
        "-t_srs",
        f"EPSG:{OUTPUT_CRS_ID}",
        "-nln",
        OUTPUT_LAYER,
        "-nlt",
        OUTPUT_GEOMETRY_TYPE,
        "-dim",
        "XY",
        "-unsetFid",
        "-mapFieldType",
        "Integer=Integer64",
        "-dsco",
        "VERSION=1.2",
        "-lco",
        "FID=fid",
        "-lco",
        f"GEOMETRY_NAME={OUTPUT_GEOMETRY_COLUMN}",
        "-lco",
        "GEOMETRY_NULLABLE=NO",
        "-lco",
        "SPATIAL_INDEX=YES",
        "-gt",
        "65536",
    )
    _run_tool(command, environment, "GB BUA reprojection")
    if not output_gpkg.is_file() or output_gpkg.stat().st_size < 10 * 1024 * 1024:
        raise GbBuaBuildError("Reprojection did not produce a plausible GeoPackage.")


def _metadata_values(manifest: Mapping) -> dict[str, str]:
    source = manifest["source"]
    reprojection = manifest["reprojection"]
    london = manifest["london_aggregation"]
    licence = manifest["licence"]
    return {
        "artifact_version": manifest["artifact_version"],
        "attribution": licence["attribution"],
        "builder_version": str(manifest["builder"]["version"]),
        "derived_crs": manifest["derived"]["crs"],
        "derived_feature_count": str(manifest["derived"]["feature_count"]),
        "derived_geometry_type": manifest["derived"]["geometry_type"],
        "derived_layer": manifest["derived"]["layer"],
        "licence_id": licence["id"],
        "licence_notice_sha256": licence["notice_sha256"],
        "licence_url": licence["url"],
        "london_aggregate_id": london["aggregate_id"],
        "london_aggregate_name": london["display_name"],
        "london_codes_sha256": london["codes_sha256"],
        "london_member_count": str(london["member_count"]),
        "provider": "os_open_built_up_areas",
        "reprojection_accuracy_metres": str(reprojection["accuracy_metres"]),
        "reprojection_grid_sha256": reprojection["grid_sha256"],
        "reprojection_operation": reprojection["operation"],
        "schema_version": str(manifest["manifest_schema_version"]),
        "source_archive_md5": source["archive_md5"],
        "source_archive_sha256": source["archive_sha256"],
        "source_crs": source["crs"],
        "source_geopackage_sha256": source["geopackage_sha256"],
        "source_layer": source["layer"],
        "source_product": source["product"],
        "source_product_id": source["product_id"],
        "source_release": source["release"],
    }


def _add_metadata(path: Path, manifest: Mapping) -> None:
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                f"""
                CREATE UNIQUE INDEX nightways_gb_bua_source_gss_code_uq
                ON {OUTPUT_LAYER}(SOURCE_GSS_CODE)
                """
            )
            connection.execute(
                f"""
                CREATE INDEX nightways_gb_bua_gisco_id_idx
                ON {OUTPUT_LAYER}(GISCO_ID)
                """
            )
            connection.execute(
                f"""
                CREATE TABLE {AGGREGATE_TABLE} (
                    aggregate_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    source_gss_code TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (aggregate_id, source_gss_code),
                    FOREIGN KEY (source_gss_code)
                        REFERENCES {OUTPUT_LAYER}(SOURCE_GSS_CODE)
                )
                """
            )
            connection.executemany(
                f"""
                INSERT INTO {AGGREGATE_TABLE}
                    (aggregate_id, display_name, source_gss_code)
                VALUES (?, ?, ?)
                """,
                (("GB_LONDON", "London", code) for code in LONDON_CODES),
            )
            connection.execute(
                f"""
                CREATE TABLE {METADATA_TABLE} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.executemany(
                f"INSERT INTO {METADATA_TABLE} (key, value) VALUES (?, ?)",
                sorted(_metadata_values(manifest).items()),
            )
            for table, description in (
                (AGGREGATE_TABLE, "Nightways synthetic locality membership"),
                (METADATA_TABLE, "Nightways GB BUA release metadata"),
            ):
                connection.execute(
                    """
                    INSERT INTO gpkg_contents
                        (table_name, data_type, identifier, description,
                         last_change, srs_id)
                    VALUES (?, 'attributes', ?, ?, ?, NULL)
                    """,
                    (table, table, description, FIXED_GPKG_TIMESTAMP),
                )
            connection.execute(
                "UPDATE gpkg_contents SET last_change = ?",
                (FIXED_GPKG_TIMESTAMP,),
            )
            connection.commit()
            connection.execute("VACUUM")
    except sqlite3.Error as error:
        raise GbBuaBuildError("Could not add deterministic artifact metadata.") from error


def _geometry_header(value: bytes) -> tuple[int, tuple[float, float, float, float]]:
    data = bytes(value)
    if len(data) < 8 or data[:2] != b"GP":
        raise GbBuaBuildError("Invalid GeoPackage geometry header.")
    flags = data[3]
    if flags & 0b00010000:
        raise GbBuaBuildError("GeoPackage contains an empty geometry.")
    endian = "<" if flags & 1 else ">"
    srs_id = struct.unpack_from(f"{endian}i", data, 4)[0]
    envelope_indicator = (flags >> 1) & 0b111
    envelope_lengths = {0: 0, 1: 4, 2: 6, 3: 6, 4: 8}
    envelope_length = envelope_lengths.get(envelope_indicator)
    if envelope_length is None:
        raise GbBuaBuildError("Invalid GeoPackage geometry envelope type.")
    if envelope_length < 4:
        raise GbBuaBuildError("GeoPackage geometry lacks an XY envelope.")
    envelope = struct.unpack_from(f"{endian}{'d' * envelope_length}", data, 8)
    return srs_id, (envelope[0], envelope[1], envelope[2], envelope[3])


def _geometry_polygons(value: bytes):
    data = bytes(value)
    _srs_id, _envelope = _geometry_header(data)
    flags = data[3]
    envelope_indicator = (flags >> 1) & 0b111
    envelope_lengths = {0: 0, 1: 4, 2: 6, 3: 6, 4: 8}
    offset = 8 + envelope_lengths[envelope_indicator] * 8
    geometry_type, polygons, final_offset = _read_wkb(data, offset)
    if geometry_type != "MultiPolygon" or final_offset != len(data):
        raise GbBuaBuildError("Expected one complete MultiPolygon geometry.")
    return polygons


def _read_wkb(data: bytes, offset: int):
    if offset + 5 > len(data):
        raise GbBuaBuildError("Truncated WKB geometry.")
    byte_order = data[offset]
    if byte_order not in (0, 1):
        raise GbBuaBuildError("Invalid WKB byte order.")
    endian = "<" if byte_order == 1 else ">"
    offset += 1
    raw_type = struct.unpack_from(f"{endian}I", data, offset)[0]
    offset += 4
    base_type, dimensions, has_srid = _wkb_type(raw_type)
    if has_srid:
        offset += 4

    if base_type == 3:
        if offset + 4 > len(data):
            raise GbBuaBuildError("Truncated Polygon WKB.")
        ring_count = struct.unpack_from(f"{endian}I", data, offset)[0]
        offset += 4
        rings = []
        for _ in range(ring_count):
            if offset + 4 > len(data):
                raise GbBuaBuildError("Truncated Polygon ring WKB.")
            point_count = struct.unpack_from(f"{endian}I", data, offset)[0]
            offset += 4
            ring = []
            for _ in range(point_count):
                size = dimensions * 8
                if offset + size > len(data):
                    raise GbBuaBuildError("Truncated Polygon coordinate WKB.")
                coordinates = struct.unpack_from(
                    f"{endian}{'d' * dimensions}", data, offset
                )
                offset += size
                ring.append((coordinates[0], coordinates[1]))
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise GbBuaBuildError("Invalid Polygon linear ring.")
            rings.append(tuple(ring))
        return "Polygon", (tuple(rings),), offset

    if base_type == 6:
        if offset + 4 > len(data):
            raise GbBuaBuildError("Truncated MultiPolygon WKB.")
        polygon_count = struct.unpack_from(f"{endian}I", data, offset)[0]
        offset += 4
        polygons = []
        for _ in range(polygon_count):
            geometry_type, geometry_polygons, offset = _read_wkb(data, offset)
            if geometry_type != "Polygon":
                raise GbBuaBuildError("MultiPolygon contains a non-Polygon.")
            polygons.extend(geometry_polygons)
        return "MultiPolygon", tuple(polygons), offset

    raise GbBuaBuildError("Artifact contains a non-polygon WKB geometry.")


def _wkb_type(raw_type: int) -> tuple[int, int, bool]:
    has_z = bool(raw_type & 0x80000000)
    has_m = bool(raw_type & 0x40000000)
    has_srid = bool(raw_type & 0x20000000)
    if raw_type & 0xE0000000:
        return raw_type & 0x0000FFFF, 2 + int(has_z) + int(has_m), has_srid
    dimension_code, base_type = divmod(raw_type, 1000)
    dimensions = {0: 2, 1: 3, 2: 3, 3: 4}.get(dimension_code)
    if dimensions is None:
        raise GbBuaBuildError("Unsupported WKB dimensionality.")
    return base_type, dimensions, False


def _point_on_segment(x, y, start, end) -> bool:
    start_x, start_y = start
    end_x, end_y = end
    cross_product = (
        (x - start_x) * (end_y - start_y)
        - (y - start_y) * (end_x - start_x)
    )
    tolerance = 1e-12 * max(
        1.0,
        abs(end_x - start_x),
        abs(end_y - start_y),
    )
    return (
        abs(cross_product) <= tolerance
        and min(start_x, end_x) - tolerance <= x <= max(start_x, end_x) + tolerance
        and min(start_y, end_y) - tolerance <= y <= max(start_y, end_y) + tolerance
    )


def _ring_location(ring, x: float, y: float) -> tuple[bool, bool]:
    inside = False
    for start, end in zip(ring, ring[1:]):
        if _point_on_segment(x, y, start, end):
            return False, True
        start_x, start_y = start
        end_x, end_y = end
        if (start_y > y) == (end_y > y):
            continue
        crossing_x = (end_x - start_x) * (y - start_y) / (
            end_y - start_y
        ) + start_x
        if x < crossing_x:
            inside = not inside
    return inside, False


def _polygon_contains(polygon, x: float, y: float) -> bool:
    outer_inside, outer_boundary = _ring_location(polygon[0], x, y)
    if not outer_inside and not outer_boundary:
        return False
    for hole in polygon[1:]:
        hole_inside, hole_boundary = _ring_location(hole, x, y)
        if hole_inside or hole_boundary:
            return False
    return True


def _geometry_contains(value: bytes, lon: float, lat: float) -> bool:
    return any(
        _polygon_contains(polygon, lon, lat)
        for polygon in _geometry_polygons(value)
    )


def _validate_points(connection: sqlite3.Connection) -> tuple[dict, ...]:
    rtree = f"rtree_{OUTPUT_LAYER}_{OUTPUT_GEOMETRY_COLUMN}"
    results = []
    for point in VALIDATION_POINTS:
        rows = connection.execute(
            f"""
            SELECT feature.SOURCE_GSS_CODE,
                   feature.GISCO_ID,
                   feature.LAU_NAME,
                   feature.{OUTPUT_GEOMETRY_COLUMN} AS geometry
            FROM {OUTPUT_LAYER} AS feature
            JOIN {rtree} AS bounds ON bounds.id = feature.rowid
            WHERE bounds.minx <= ? AND bounds.maxx >= ?
              AND bounds.miny <= ? AND bounds.maxy >= ?
            ORDER BY feature.SOURCE_GSS_CODE
            """,
            (point.lon, point.lon, point.lat, point.lat),
        ).fetchall()
        contained = [
            row
            for row in rows
            if _geometry_contains(row["geometry"], point.lon, point.lat)
        ]
        if len(contained) != 1:
            raise GbBuaBuildError(
                f"{point.name} matched {len(contained)} polygons; expected one."
            )
        row = contained[0]
        actual = (
            row["SOURCE_GSS_CODE"],
            row["GISCO_ID"],
            row["LAU_NAME"],
        )
        expected = (point.source_gss_code, point.gisco_id, point.display_name)
        if actual != expected:
            raise GbBuaBuildError(
                f"{point.name} resolved to {actual!r}; expected {expected!r}."
            )
        results.append(
            {
                "name": point.name,
                "source_gss_code": row["SOURCE_GSS_CODE"],
                "gisco_id": row["GISCO_ID"],
                "display_name": row["LAU_NAME"],
            }
        )
    return tuple(results)


def validate_derived_geopackage(
    path: Path,
    manifest: Mapping,
) -> tuple[dict, ...]:
    """Validate every production invariant and return station results."""

    if not path.is_file():
        raise GbBuaBuildError(f"Derived GeoPackage not found: {path}")
    with closing(_open_read_only(path)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise GbBuaBuildError(f"Derived GeoPackage integrity_check: {integrity}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise GbBuaBuildError("Derived GeoPackage foreign keys are invalid.")

        layer = connection.execute(
            """
            SELECT c.data_type, c.srs_id, c.last_change,
                   g.column_name, g.geometry_type_name, g.z, g.m
            FROM gpkg_contents AS c
            JOIN gpkg_geometry_columns AS g USING (table_name)
            WHERE c.table_name = ?
            """,
            (OUTPUT_LAYER,),
        ).fetchone()
        if layer is None:
            raise GbBuaBuildError("Derived feature layer is missing.")
        if (
            layer["data_type"] != "features"
            or layer["srs_id"] != OUTPUT_CRS_ID
            or layer["last_change"] != FIXED_GPKG_TIMESTAMP
            or layer["column_name"] != OUTPUT_GEOMETRY_COLUMN
            or layer["geometry_type_name"].upper() != OUTPUT_GEOMETRY_TYPE
            or layer["z"] != 0
            or layer["m"] != 0
        ):
            raise GbBuaBuildError("Derived layer CRS or geometry contract is invalid.")

        columns = connection.execute(
            f"PRAGMA table_info({OUTPUT_LAYER})"
        ).fetchall()
        actual_fields = tuple((row["name"], row["type"].upper()) for row in columns)
        expected_fields = (
            ("fid", "INTEGER"),
            (OUTPUT_GEOMETRY_COLUMN, OUTPUT_GEOMETRY_TYPE),
            *DERIVED_FIELDS[1:-1],
        )
        if actual_fields != expected_fields:
            raise GbBuaBuildError(
                f"Derived fields {actual_fields!r} do not match the contract."
            )
        if columns[0]["pk"] != 1 or columns[1]["notnull"] != 1:
            raise GbBuaBuildError("Derived primary key/geometry nullability is invalid.")

        counts = connection.execute(
            f"""
            SELECT COUNT(*) AS feature_count,
                   COUNT(DISTINCT SOURCE_GSS_CODE) AS distinct_codes,
                   SUM(CASE WHEN SOURCE_GSS_CODE IS NULL
                                 OR trim(SOURCE_GSS_CODE) = ''
                            THEN 1 ELSE 0 END) AS missing_codes,
                   SUM(CASE WHEN {OUTPUT_GEOMETRY_COLUMN} IS NULL
                            THEN 1 ELSE 0 END) AS null_geometries,
                   SUM(CASE WHEN LAU_NAME IS NULL OR trim(LAU_NAME) = ''
                            THEN 1 ELSE 0 END) AS missing_names,
                   SUM(CASE WHEN CNTR_CODE <> 'GB' THEN 1 ELSE 0 END)
                       AS wrong_country,
                   SUM(CASE WHEN YEAR <> {OUTPUT_YEAR} THEN 1 ELSE 0 END)
                       AS wrong_year,
                   SUM(CASE WHEN SOURCE_AREA_HECTARES IS NULL
                            THEN 1 ELSE 0 END) AS missing_areas
            FROM {OUTPUT_LAYER}
            """
        ).fetchone()
        if counts["feature_count"] != SOURCE_FEATURE_COUNT:
            raise GbBuaBuildError("Derived feature count is invalid.")
        if counts["distinct_codes"] != SOURCE_FEATURE_COUNT:
            raise GbBuaBuildError("Derived GSS codes are duplicated.")
        if any(
            counts[key]
            for key in (
                "missing_codes",
                "null_geometries",
                "missing_names",
                "wrong_country",
                "wrong_year",
                "missing_areas",
            )
        ):
            raise GbBuaBuildError("Derived features contain invalid values.")

        rows = connection.execute(
            f"""
            SELECT SOURCE_GSS_CODE AS gsscode,
                   SOURCE_NAME1_TEXT AS name1_text,
                   SOURCE_NAME1_LANGUAGE AS name1_language,
                   SOURCE_NAME2_TEXT AS name2_text,
                   SOURCE_NAME2_LANGUAGE AS name2_language,
                   GISCO_ID, LAU_NAME
            FROM {OUTPUT_LAYER}
            ORDER BY SOURCE_GSS_CODE
            """
        ).fetchall()
        for row in rows:
            expected_name = _selected_english_name(row)
            is_london = row["gsscode"] in LONDON_CODES
            expected_id = "GB_LONDON" if is_london else row["gsscode"]
            if row["GISCO_ID"] != expected_id:
                raise GbBuaBuildError("Derived locality identity is invalid.")
            if row["LAU_NAME"] != ("London" if is_london else expected_name):
                raise GbBuaBuildError("Derived English display name is invalid.")

        london_rows = connection.execute(
            f"""
            SELECT SOURCE_GSS_CODE FROM {OUTPUT_LAYER}
            WHERE GISCO_ID = 'GB_LONDON' OR LAU_NAME = 'London'
            ORDER BY SOURCE_GSS_CODE
            """
        ).fetchall()
        if tuple(row[0] for row in london_rows) != LONDON_CODES:
            raise GbBuaBuildError("Derived London membership is invalid.")

        aggregate_rows = connection.execute(
            f"""
            SELECT aggregate_id, display_name, source_gss_code
            FROM {AGGREGATE_TABLE}
            ORDER BY source_gss_code
            """
        ).fetchall()
        expected_aggregates = tuple(
            ("GB_LONDON", "London", code) for code in LONDON_CODES
        )
        if tuple(tuple(row) for row in aggregate_rows) != expected_aggregates:
            raise GbBuaBuildError("London aggregate metadata is invalid.")

        metadata = dict(
            connection.execute(
                f"SELECT key, value FROM {METADATA_TABLE} ORDER BY key"
            ).fetchall()
        )
        if metadata != _metadata_values(manifest):
            raise GbBuaBuildError("Embedded dataset metadata is invalid.")

        _validate_rtree(
            connection,
            OUTPUT_LAYER,
            OUTPUT_GEOMETRY_COLUMN,
            SOURCE_FEATURE_COUNT,
        )
        rtree = f"rtree_{OUTPUT_LAYER}_{OUTPUT_GEOMETRY_COLUMN}"
        extent = connection.execute(
            f"SELECT min(minx), max(maxx), min(miny), max(maxy) FROM {rtree}"
        ).fetchone()
        if not (
            -10.0 < extent[0] < -5.0
            and 1.0 < extent[1] < 3.0
            and 49.0 < extent[2] < 51.0
            and 60.0 < extent[3] < 62.0
        ):
            raise GbBuaBuildError(f"Derived extent is implausible: {tuple(extent)!r}")

        geometry_rows = connection.execute(
            f"""
            SELECT feature.rowid, feature.{OUTPUT_GEOMETRY_COLUMN},
                   bounds.minx, bounds.maxx, bounds.miny, bounds.maxy
            FROM {OUTPUT_LAYER} AS feature
            JOIN {rtree} AS bounds ON bounds.id = feature.rowid
            ORDER BY feature.rowid
            """
        )
        for row in geometry_rows:
            srs_id, envelope = _geometry_header(row[1])
            if srs_id != OUTPUT_CRS_ID:
                raise GbBuaBuildError("A geometry header has the wrong CRS id.")
            if not all(math.isfinite(value) for value in envelope):
                raise GbBuaBuildError("A geometry envelope is non-finite.")
            rtree_envelope = (row[2], row[3], row[4], row[5])
            for actual, indexed in zip(envelope, rtree_envelope):
                tolerance = 1e-5 * max(1.0, abs(actual))
                if abs(actual - indexed) > tolerance:
                    raise GbBuaBuildError("RTree envelope disagrees with geometry.")

        return _validate_points(connection)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.flag_bits = 0x800
    info.extra = b""
    info.comment = b""
    return info


def create_distribution_zip(
    zip_path: Path,
    geopackage_path: Path,
    licence_path: Path,
    manifest: Mapping,
) -> None:
    members = (
        (manifest["derived"]["geopackage_filename"], geopackage_path),
        (manifest["licence"]["notice_filename"], licence_path),
    )
    if tuple(name for name, _path in members) != tuple(
        sorted(name for name, _path in members)
    ):
        raise GbBuaBuildError("Distribution ZIP members are not canonical.")
    try:
        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            for name, path in members:
                with path.open("rb") as source:
                    with archive.open(_zip_info(name), "w") as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)
    except (OSError, zipfile.BadZipFile) as error:
        raise GbBuaBuildError("Could not create distribution ZIP.") from error
    validate_distribution_zip(zip_path, geopackage_path, licence_path, manifest)


def validate_distribution_zip(
    zip_path: Path,
    geopackage_path: Path,
    licence_path: Path,
    manifest: Mapping,
) -> None:
    expected_members = (
        manifest["derived"]["geopackage_filename"],
        manifest["licence"]["notice_filename"],
    )
    with zipfile.ZipFile(zip_path, "r") as archive:
        if tuple(info.filename for info in archive.infolist()) != expected_members:
            raise GbBuaBuildError("Distribution ZIP member order is invalid.")
        if archive.comment:
            raise GbBuaBuildError("Distribution ZIP must not have a comment.")
        for info in archive.infolist():
            if (
                info.date_time != FIXED_ZIP_TIMESTAMP
                or info.compress_type != zipfile.ZIP_DEFLATED
                or info.extra
                or info.comment
                or info.create_system != 3
                or (info.external_attr >> 16) != (stat.S_IFREG | 0o644)
            ):
                raise GbBuaBuildError(
                    f"Distribution ZIP metadata is not deterministic for {info.filename}."
                )
        if archive.testzip() is not None:
            raise GbBuaBuildError("Distribution ZIP CRC validation failed.")
        gpkg_hash = hashlib.sha256()
        with archive.open(expected_members[0], "r") as member:
            for chunk in iter(lambda: member.read(1024 * 1024), b""):
                gpkg_hash.update(chunk)
        if gpkg_hash.hexdigest() != _sha256(geopackage_path):
            raise GbBuaBuildError("ZIP GeoPackage member checksum is invalid.")
        if archive.read(expected_members[1]) != licence_path.read_bytes():
            raise GbBuaBuildError("ZIP licence member differs from repository notice.")


def _compare_manifest_artifact_values(
    manifest: Mapping,
    gpkg_size: int,
    zip_size: int,
    gpkg_sha256: str,
    zip_sha256: str,
) -> None:
    derived = manifest["derived"]
    actual = {
        "geopackage_size_bytes": gpkg_size,
        "zip_size_bytes": zip_size,
        "geopackage_sha256": gpkg_sha256,
        "zip_sha256": zip_sha256,
    }
    for key, value in actual.items():
        pinned = derived.get(key)
        if pinned is not None and pinned != value:
            raise GbBuaBuildError(
                f"Built {key} {value!r} does not match manifest {pinned!r}."
            )


def build_artifact(
    source_zip: Path,
    output_gpkg: Path,
    output_zip: Path,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    licence_path: Path = DEFAULT_LICENCE_PATH,
    ogr2ogr_path: Optional[Path] = None,
    projinfo_path: Optional[Path] = None,
    proj_data_path: Optional[Path] = None,
    gdal_data_path: Optional[Path] = None,
    ostn15_grid_path: Optional[Path] = None,
    force: bool = False,
) -> BuildResult:
    """Build both outputs in a temporary directory, validate, then install."""

    source_zip = source_zip.expanduser().resolve()
    output_gpkg = output_gpkg.expanduser().resolve()
    output_zip = output_zip.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    licence_path = licence_path.expanduser().resolve()
    manifest = load_manifest(manifest_path)

    if output_gpkg == output_zip:
        raise GbBuaBuildError("GeoPackage and ZIP output paths must differ.")
    if output_gpkg.name != manifest["derived"]["geopackage_filename"]:
        raise GbBuaBuildError("GeoPackage output filename must match the manifest.")
    if output_zip.name != manifest["derived"]["zip_filename"]:
        raise GbBuaBuildError("ZIP output filename must match the manifest.")
    if not licence_path.is_file():
        raise GbBuaBuildError(f"Licence notice not found: {licence_path}")
    _validate_file(
        licence_path,
        manifest["licence"]["notice_size_bytes"],
        manifest["licence"]["notice_sha256"],
        "Repository licence notice",
    )
    if ATTRIBUTION not in licence_path.read_text(encoding="utf-8"):
        raise GbBuaBuildError("Repository licence notice lacks attribution.")
    existing = [path for path in (output_gpkg, output_zip) if path.exists()]
    if existing and not force:
        raise GbBuaBuildError(
            "Output already exists; pass --force only after reviewing the target paths."
        )

    ogr2ogr = _resolve_executable(
        ogr2ogr_path,
        "ogr2ogr",
        (Path("E:/bin/ogr2ogr.exe"),),
    )
    projinfo = _resolve_executable(
        projinfo_path,
        "projinfo",
        (ogr2ogr.parent / "projinfo.exe", ogr2ogr.parent / "projinfo"),
    )
    proj_data = _resolve_directory(
        proj_data_path,
        ("PROJ_DATA", "PROJ_LIB"),
        (Path("E:/share/proj"),),
        "proj.db",
        "PROJ data directory",
    )
    gdal_data = _resolve_directory(
        gdal_data_path,
        ("GDAL_DATA",),
        (Path("E:/apps/gdal/share/gdal"),),
        "gdalvrt.xsd",
        "GDAL data directory",
    )
    grid_path = (
        ostn15_grid_path.expanduser().resolve()
        if ostn15_grid_path is not None
        else proj_data / OSTN15_FILENAME
    )
    environment = validate_toolchain(
        ogr2ogr,
        projinfo,
        gdal_data,
        proj_data,
        grid_path,
        manifest,
    )

    output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_gpkg.parent != output_zip.parent:
        raise GbBuaBuildError("Both outputs must use the same parent directory.")

    with tempfile.TemporaryDirectory(
        prefix=".gb-bua-2026-04-v1.",
        dir=output_gpkg.parent,
    ) as temporary_name:
        work_dir = Path(temporary_name)
        source_gpkg = validate_and_extract_source(source_zip, work_dir)
        validate_source_geopackage(source_gpkg)
        temporary_gpkg = work_dir / manifest["derived"]["geopackage_filename"]
        temporary_zip = work_dir / manifest["derived"]["zip_filename"]
        sql_path = work_dir / "transform.sql"
        _run_reprojection(
            source_gpkg,
            temporary_gpkg,
            sql_path,
            ogr2ogr,
            environment,
        )
        _add_metadata(temporary_gpkg, manifest)
        validation_results = validate_derived_geopackage(
            temporary_gpkg,
            manifest,
        )
        create_distribution_zip(
            temporary_zip,
            temporary_gpkg,
            licence_path,
            manifest,
        )
        gpkg_size = temporary_gpkg.stat().st_size
        zip_size = temporary_zip.stat().st_size
        gpkg_sha256 = _sha256(temporary_gpkg)
        zip_sha256 = _sha256(temporary_zip)
        _compare_manifest_artifact_values(
            manifest,
            gpkg_size,
            zip_size,
            gpkg_sha256,
            zip_sha256,
        )

        os.replace(temporary_gpkg, output_gpkg)
        os.replace(temporary_zip, output_zip)

    return BuildResult(
        geopackage_path=output_gpkg,
        zip_path=output_zip,
        geopackage_size_bytes=gpkg_size,
        zip_size_bytes=zip_size,
        geopackage_sha256=gpkg_sha256,
        zip_sha256=zip_sha256,
        validation_results=validation_results,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the checksum-pinned Nightways GB BUA 2026-04 v1 artifact."
        )
    )
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--output-gpkg", type=Path, required=True)
    parser.add_argument("--output-zip", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--licence", type=Path, default=DEFAULT_LICENCE_PATH)
    parser.add_argument("--ogr2ogr", type=Path)
    parser.add_argument("--projinfo", type=Path)
    parser.add_argument("--proj-data", type=Path)
    parser.add_argument("--gdal-data", type=Path)
    parser.add_argument("--ostn15-grid", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_artifact(
            source_zip=args.source_zip,
            output_gpkg=args.output_gpkg,
            output_zip=args.output_zip,
            manifest_path=args.manifest,
            licence_path=args.licence,
            ogr2ogr_path=args.ogr2ogr,
            projinfo_path=args.projinfo,
            proj_data_path=args.proj_data,
            gdal_data_path=args.gdal_data,
            ostn15_grid_path=args.ostn15_grid,
            force=args.force,
        )
    except GbBuaBuildError as error:
        print(f"GB BUA artifact build failed: {error}", file=sys.stderr)
        return 1

    payload = {
        "geopackage": str(result.geopackage_path),
        "geopackage_size_bytes": result.geopackage_size_bytes,
        "geopackage_sha256": result.geopackage_sha256,
        "zip": str(result.zip_path),
        "zip_size_bytes": result.zip_size_bytes,
        "zip_sha256": result.zip_sha256,
        "validation_points": list(result.validation_results),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
