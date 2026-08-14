"""Explicit one-time installer for the local GISCO LAU 2024 dataset."""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.localities import (  # noqa: E402
    GISCO_LAU_YEAR,
    GISCO_LAU_PATH_ENV,
    GiscoLauIndex,
    LocalityDatasetError,
    LocalityDatasetUnavailableError,
    LocalityResolutionError,
    configured_gisco_lau_path,
)


GISCO_LAU_DOWNLOAD_URL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/lau/gpkg/"
    "LAU_RG_01M_2024_4326.gpkg"
)
GISCO_LAU_MINIMUM_BYTES = 10 * 1024 * 1024
GISCO_SETUP_USER_AGENT = "Nightways/0.1 (GISCO LAU setup)"
GISCO_REQUIRED_COLUMNS = ("GISCO_ID", "CNTR_CODE", "LAU_NAME", "YEAR")


class GiscoSetupError(RuntimeError):
    """Raised when the explicit dataset installation cannot complete."""


@dataclass(frozen=True, slots=True)
class GiscoInstallResult:
    path: Path
    size_bytes: int
    installed: bool


def validate_gisco_dataset(
    path: str | Path,
    minimum_size_bytes: int = GISCO_LAU_MINIMUM_BYTES,
) -> int:
    """Validate the expected LAU 2024 layer and return its file size."""

    dataset_path = Path(path).expanduser().resolve()
    if not dataset_path.is_file():
        raise LocalityDatasetUnavailableError(
            f"GISCO LAU {GISCO_LAU_YEAR} GeoPackage not found: "
            f"{dataset_path}"
        )
    if (
        isinstance(minimum_size_bytes, bool)
        or not isinstance(minimum_size_bytes, int)
        or minimum_size_bytes < 0
    ):
        raise ValueError("minimum_size_bytes must be a non-negative integer")

    size_bytes = dataset_path.stat().st_size
    if size_bytes < minimum_size_bytes:
        raise LocalityDatasetError(
            f"GISCO LAU GeoPackage is implausibly small: {size_bytes} bytes."
        )

    try:
        connection = sqlite3.connect(
            f"{dataset_path.as_uri()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        raise LocalityDatasetError(
            "GISCO LAU file is not a readable SQLite/GeoPackage database."
        ) from error

    try:
        table_name, columns_by_case = _discover_lau_layer(connection)
        year_column = columns_by_case["year"]
        years = {
            str(row["dataset_year"]).strip()
            for row in connection.execute(
                f"SELECT DISTINCT {_quote_identifier(year_column)} "
                f"AS dataset_year FROM {_quote_identifier(table_name)}"
            ).fetchall()
        }
        if years != {str(GISCO_LAU_YEAR)}:
            raise LocalityDatasetError(
                f"GISCO LAU layer must contain only YEAR={GISCO_LAU_YEAR}."
            )
    except sqlite3.Error as error:
        raise LocalityDatasetError(
            "GISCO LAU file is not a readable GeoPackage database."
        ) from error
    finally:
        connection.close()

    with GiscoLauIndex(
        dataset_path,
        dataset_year=GISCO_LAU_YEAR,
        table_name=table_name,
    ):
        pass

    return size_bytes


def _discover_lau_layer(
    connection: sqlite3.Connection,
) -> tuple[str, dict[str, str]]:
    """Discover exactly one indexed LAU feature layer from GeoPackage metadata."""

    rows = connection.execute(
        """
        SELECT contents.table_name,
               contents.srs_id AS contents_srs_id,
               geometry.column_name,
               geometry.geometry_type_name,
               geometry.srs_id AS geometry_srs_id
        FROM gpkg_contents AS contents
        LEFT JOIN gpkg_geometry_columns AS geometry
          ON geometry.table_name = contents.table_name
        WHERE contents.data_type = 'features'
        ORDER BY contents.table_name
        """
    ).fetchall()

    appropriate = []
    rejected = []
    for row in rows:
        table_name = str(row["table_name"])
        issues = []
        geometry_column = row["column_name"]
        geometry_type = str(row["geometry_type_name"] or "").upper()

        if geometry_column is None:
            issues.append("missing gpkg_geometry_columns metadata")
        if row["contents_srs_id"] != 4326 or row["geometry_srs_id"] != 4326:
            issues.append("not EPSG:4326")
        if geometry_type not in {"POLYGON", "MULTIPOLYGON"}:
            issues.append("geometry is not Polygon/MultiPolygon")

        columns = connection.execute(
            f"PRAGMA table_info({_quote_identifier(table_name)})"
        ).fetchall()
        columns_by_case = {
            str(column["name"]).casefold(): str(column["name"])
            for column in columns
        }
        missing = [
            column
            for column in GISCO_REQUIRED_COLUMNS
            if column.casefold() not in columns_by_case
        ]
        if missing:
            issues.append("missing required fields: " + ", ".join(missing))

        if geometry_column is not None:
            rtree_table = f"rtree_{table_name}_{geometry_column}"
            if not _table_exists(connection, rtree_table):
                issues.append("missing RTree spatial index")

        if issues:
            rejected.append(f"{table_name!r}: " + ", ".join(issues))
        else:
            appropriate.append((table_name, columns_by_case))

    if len(appropriate) != 1:
        names = ", ".join(repr(item[0]) for item in appropriate) or "none"
        details = "; ".join(rejected)
        message = (
            "GISCO LAU GeoPackage must contain exactly one appropriate "
            f"feature layer; found {len(appropriate)} ({names})."
        )
        if details:
            message += " Rejected feature layers: " + details
        raise LocalityDatasetError(message)

    return appropriate[0]


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type IN ('table', 'view') AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def install_gisco_dataset(
    target_path: str | Path | None = None,
    force: bool = False,
    opener=None,
    minimum_size_bytes: int = GISCO_LAU_MINIMUM_BYTES,
) -> GiscoInstallResult:
    """Install the official file atomically, or reuse a valid existing file."""

    target = (
        configured_gisco_lau_path()
        if target_path is None
        else Path(target_path).expanduser().resolve()
    )

    if target.exists() and not force:
        try:
            size_bytes = validate_gisco_dataset(
                target,
                minimum_size_bytes,
            )
        except LocalityResolutionError as error:
            raise GiscoSetupError(
                f"Existing dataset is invalid and was not overwritten: "
                f"{target}. Re-run with --force to replace it."
            ) from error
        return GiscoInstallResult(target, size_bytes, installed=False)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        request = Request(
            GISCO_LAU_DOWNLOAD_URL,
            headers={
                "Accept": "application/geopackage+sqlite3, application/octet-stream",
                "User-Agent": GISCO_SETUP_USER_AGENT,
            },
        )
        open_url = urlopen if opener is None else opener

        try:
            with os.fdopen(descriptor, "wb") as destination:
                with open_url(request, timeout=120) as response:
                    shutil.copyfileobj(response, destination, 1024 * 1024)
                    declared_size = response.headers.get("Content-Length")
                downloaded_size = destination.tell()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise GiscoSetupError(
                "Could not download the official GISCO LAU 2024 GeoPackage."
            ) from error

        if declared_size:
            try:
                expected_size = int(declared_size)
            except ValueError as error:
                raise GiscoSetupError(
                    "GISCO download returned an invalid Content-Length."
                ) from error
            if downloaded_size != expected_size:
                raise GiscoSetupError(
                    "GISCO download ended before the declared file size."
                )

        size_bytes = validate_gisco_dataset(
            temporary_path,
            minimum_size_bytes,
        )
        if target.exists() and not force:
            raise GiscoSetupError(
                f"Dataset appeared during download and was not overwritten: "
                f"{target}"
            )
        os.replace(temporary_path, target)
        temporary_path = None
        return GiscoInstallResult(target, size_bytes, installed=True)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the local GISCO LAU 2024 GeoPackage for Nightways."
    )
    parser.add_argument(
        "--path",
        type=Path,
        help=(
            f"installation path (default: {GISCO_LAU_PATH_ENV} or "
            "data/geo/lau-2024.gpkg)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing dataset after the new download validates",
    )
    arguments = parser.parse_args(argv)

    try:
        result = install_gisco_dataset(
            target_path=arguments.path,
            force=arguments.force,
        )
    except (GiscoSetupError, LocalityResolutionError, OSError) as error:
        parser.exit(1, f"GISCO setup failed: {error}\n")

    action = "Installed" if result.installed else "Already installed"
    print(f"{action}: {result.path}")
    print(f"Size: {result.size_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
