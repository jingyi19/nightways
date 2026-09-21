"""Explicit, standalone installer for the pinned Nightways GB BUA artifact.

The release asset is verified before extraction, and the extracted GeoPackage
is verified before replacing the installed file. No geospatial package or
Nightways runtime locality module is imported by this installer.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import struct
import sys
import tempfile
import zipfile
import zlib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.build_gb_bua_artifact import (  # noqa: E402
    DEFAULT_MANIFEST_PATH,
    GbBuaBuildError,
    load_manifest,
    validate_derived_geopackage,
)


GB_BUA_ASSET_URL = (
    "https://github.com/jingyi19/nightways/releases/download/"
    "data-os-bua-2026-04-v1/nightways-gb-bua-2026-04-v1.zip"
)
GB_BUA_ZIP_FILENAME = "nightways-gb-bua-2026-04-v1.zip"
GB_BUA_ZIP_SIZE_BYTES = 69_686_986
GB_BUA_ZIP_SHA256 = (
    "8758caf1b27e2178f8e643231a3bc950a4ac7c49e945b24b1cbc3a898ea3405e"
)
GB_BUA_GPKG_FILENAME = "nightways-gb-bua-2026-04-v1.gpkg"
GB_BUA_GPKG_SIZE_BYTES = 97_288_192
GB_BUA_GPKG_SHA256 = (
    "88abc8cde107d40045394c269b342922303ec0f33a3a5f9bd46bb9719a115212"
)
GB_BUA_NOTICE_FILENAME = "os-open-built-up-areas-2026.txt"
GB_BUA_NOTICE_SIZE_BYTES = 421
GB_BUA_NOTICE_SHA256 = (
    "1fe1f80e39cc9b5ccc92ca5cb2ddd7738128267de3c84914a0cd77bec5aaedde"
)
GB_BUA_PATH_ENV = "NIGHTWAYS_GB_BUA_PATH"
GB_BUA_DEFAULT_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "geo" / GB_BUA_GPKG_FILENAME
)
GB_BUA_SETUP_USER_AGENT = "Nightways/0.1 (GB BUA artifact setup)"
CHUNK_SIZE = 1024 * 1024


class GbBuaSetupError(RuntimeError):
    """Raised when the pinned artifact cannot be installed safely."""


@dataclass(frozen=True, slots=True)
class GbBuaInstallResult:
    path: Path
    size_bytes: int
    installed: bool


def _load_pinned_manifest() -> dict:
    """Require the tracked manifest to agree with independent release pins."""

    try:
        manifest = load_manifest(DEFAULT_MANIFEST_PATH)
        derived = manifest["derived"]
        notice = manifest["licence"]
        expected = (
            (derived, "zip_filename", GB_BUA_ZIP_FILENAME),
            (derived, "zip_size_bytes", GB_BUA_ZIP_SIZE_BYTES),
            (derived, "zip_sha256", GB_BUA_ZIP_SHA256),
            (derived, "geopackage_filename", GB_BUA_GPKG_FILENAME),
            (derived, "geopackage_size_bytes", GB_BUA_GPKG_SIZE_BYTES),
            (derived, "geopackage_sha256", GB_BUA_GPKG_SHA256),
            (notice, "notice_filename", GB_BUA_NOTICE_FILENAME),
            (notice, "notice_size_bytes", GB_BUA_NOTICE_SIZE_BYTES),
            (notice, "notice_sha256", GB_BUA_NOTICE_SHA256),
        )
        if any(section.get(key) != value for section, key, value in expected):
            raise GbBuaSetupError("Tracked GB BUA manifest differs from release pins.")
    except (GbBuaBuildError, KeyError, TypeError, OSError) as error:
        raise GbBuaSetupError("Pinned GB BUA manifest is invalid.") from error
    return manifest


def _check_file(
    path: Path, expected_size: int, expected_sha256: str, label: str
) -> int:
    try:
        if not path.is_file():
            raise GbBuaSetupError(f"{label} is not a file: {path}")
        size = path.stat().st_size
        if size != expected_size:
            raise GbBuaSetupError(
                f"{label} size is {size} bytes; expected {expected_size}."
            )
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise GbBuaSetupError(f"{label} SHA-256 does not match the pinned release.")
        return size
    except OSError as error:
        raise GbBuaSetupError(f"Could not read {label}: {path}") from error


def validate_gb_bua_artifact(path: str | Path) -> int:
    """Verify bytes and all GeoPackage invariants; return exact byte size."""

    artifact = Path(path).expanduser().resolve()
    manifest = _load_pinned_manifest()
    size = _check_file(
        artifact, GB_BUA_GPKG_SIZE_BYTES, GB_BUA_GPKG_SHA256, "GB BUA GeoPackage"
    )
    validation_failure = None
    try:
        validate_derived_geopackage(artifact, manifest)
        # SQLite's `NULL <> 'GB'` is NULL, not true. Check NULL explicitly as
        # well, independently of the builder's existing country-value query.
        with closing(
            sqlite3.connect(f"{artifact.as_uri()}?mode=ro", uri=True)
        ) as database:
            geometry_crs = database.execute(
                "SELECT srs_id FROM gpkg_geometry_columns "
                "WHERE table_name = 'nightways_gb_bua' AND column_name = 'geom'"
            ).fetchone()
            if geometry_crs is None or geometry_crs[0] != 4326:
                raise GbBuaSetupError("GB BUA geometry-column CRS is not EPSG:4326.")
            invalid = database.execute(
                "SELECT 1 FROM nightways_gb_bua "
                "WHERE CNTR_CODE IS NULL OR CNTR_CODE != 'GB' "
                "OR YEAR IS NULL OR YEAR != 2026 LIMIT 1"
            ).fetchone()
        if invalid is not None:
            raise GbBuaSetupError("GB BUA country or year values are invalid.")
    except GbBuaSetupError:
        raise
    except (
        GbBuaBuildError, sqlite3.Error, OSError, ValueError, TypeError, struct.error
    ) as error:
        # Do not retain the builder traceback here: on Windows it may keep an
        # unfinished SQLite cursor open while the caller cleans up the temp
        # GeoPackage, preventing the unlink.
        validation_failure = str(error)
    if validation_failure is not None:
        raise GbBuaSetupError(
            f"GB BUA GeoPackage validation failed: {validation_failure}"
        )
    return size


def _download_archive(descriptor: int, opener) -> None:
    request = Request(
        GB_BUA_ASSET_URL,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": GB_BUA_SETUP_USER_AGENT,
        },
    )
    open_url = urlopen if opener is None else opener
    try:
        with os.fdopen(descriptor, "wb") as destination:
            with open_url(request, timeout=120) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    digits = str(declared).strip()
                    if (
                        not digits
                        or len(digits) > 20
                        or any(char not in "0123456789" for char in digits)
                    ):
                        raise GbBuaSetupError(
                            "Release response has invalid Content-Length."
                        )
                    if int(digits) != GB_BUA_ZIP_SIZE_BYTES:
                        raise GbBuaSetupError(
                            "Release Content-Length is not the pinned ZIP size."
                        )
                size = 0
                digest = hashlib.sha256()
                while chunk := response.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > GB_BUA_ZIP_SIZE_BYTES:
                        raise GbBuaSetupError(
                            "Release download exceeds the pinned ZIP size."
                        )
                    destination.write(chunk)
                    digest.update(chunk)
            if size != GB_BUA_ZIP_SIZE_BYTES:
                raise GbBuaSetupError("Release download has the wrong ZIP size.")
            if digest.hexdigest() != GB_BUA_ZIP_SHA256:
                raise GbBuaSetupError("Release ZIP SHA-256 does not match the pin.")
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise GbBuaSetupError(
            "Could not download the pinned GB BUA Release asset."
        ) from error


def _extract_geopackage(archive_path: Path, target: Path, manifest: dict) -> Path:
    """Inspect the entire allowlist but write only the GeoPackage member."""

    temporary_path = None
    extracted = False
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = archive.infolist()
            if tuple(info.filename for info in members) != (
                GB_BUA_GPKG_FILENAME,
                GB_BUA_NOTICE_FILENAME,
            ) or any(info.is_dir() or info.flag_bits & 1 for info in members):
                raise GbBuaSetupError(
                    "Release ZIP members are not the pinned allowlist."
                )
            if (
                members[0].file_size != GB_BUA_GPKG_SIZE_BYTES
                or members[1].file_size != GB_BUA_NOTICE_SIZE_BYTES
            ):
                raise GbBuaSetupError("Release ZIP member sizes are invalid.")

            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.name}.gpkg.", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(name)
            size = 0
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as destination:
                with archive.open(members[0], "r") as source:
                    while chunk := source.read(CHUNK_SIZE):
                        size += len(chunk)
                        if size > GB_BUA_GPKG_SIZE_BYTES:
                            raise GbBuaSetupError(
                                "GeoPackage member exceeds its pinned size."
                            )
                        destination.write(chunk)
                        digest.update(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if (
                size != GB_BUA_GPKG_SIZE_BYTES
                or digest.hexdigest() != GB_BUA_GPKG_SHA256
            ):
                raise GbBuaSetupError(
                    "Extracted GeoPackage size or SHA-256 is invalid."
                )

            notice_size = 0
            notice_digest = hashlib.sha256()
            with archive.open(members[1], "r") as notice:
                while chunk := notice.read(CHUNK_SIZE):
                    notice_size += len(chunk)
                    if notice_size > GB_BUA_NOTICE_SIZE_BYTES:
                        raise GbBuaSetupError("Release licence notice is too large.")
                    notice_digest.update(chunk)
            if (
                notice_size != manifest["licence"]["notice_size_bytes"]
                or notice_digest.hexdigest() != manifest["licence"]["notice_sha256"]
            ):
                raise GbBuaSetupError("Release licence notice differs from its pin.")
        extracted = True
        return temporary_path
    except (
        zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError,
        EOFError, ValueError, zlib.error,
    ) as error:
        raise GbBuaSetupError("Release ZIP cannot be safely extracted.") from error
    finally:
        if temporary_path is not None and not extracted:
            temporary_path.unlink(missing_ok=True)


def _configured_target(target_path: str | Path | None) -> Path:
    if target_path is not None:
        return Path(target_path).expanduser().resolve()
    configured = os.environ.get(GB_BUA_PATH_ENV)
    if configured is not None:
        if not configured.strip():
            raise GbBuaSetupError(f"{GB_BUA_PATH_ENV} must not be empty.")
        return Path(configured).expanduser().resolve()
    return GB_BUA_DEFAULT_PATH


def install_gb_bua_artifact(
    target_path: str | Path | None = None,
    force: bool = False,
    opener=None,
) -> GbBuaInstallResult:
    """Reuse a valid file or atomically install the fixed verified Release."""

    target = _configured_target(target_path)
    manifest = _load_pinned_manifest()
    if target.exists() and not force:
        try:
            size = validate_gb_bua_artifact(target)
        except GbBuaSetupError as error:
            raise GbBuaSetupError(
                f"Existing GB BUA artifact is invalid and was not "
                f"overwritten: {target}. "
                "Use --force to replace it with a validated download."
            ) from error
        return GbBuaInstallResult(target, size, installed=False)

    target.parent.mkdir(parents=True, exist_ok=True)
    archive_path = None
    geopackage_path = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.zip.", suffix=".tmp", dir=target.parent
        )
        archive_path = Path(name)
        _download_archive(descriptor, opener)
        _check_file(
            archive_path, GB_BUA_ZIP_SIZE_BYTES, GB_BUA_ZIP_SHA256, "GB BUA ZIP"
        )
        geopackage_path = _extract_geopackage(archive_path, target, manifest)
        size = validate_gb_bua_artifact(geopackage_path)
        if target.exists() and not force:
            raise GbBuaSetupError(
                "GB BUA artifact appeared during download and was not "
                f"overwritten: {target}"
            )
        try:
            os.replace(geopackage_path, target)
        except OSError as error:
            raise GbBuaSetupError(
                f"Could not atomically install the validated GB BUA artifact: {target}"
            ) from error
        geopackage_path = None
        return GbBuaInstallResult(target, size, installed=True)
    finally:
        if geopackage_path is not None:
            geopackage_path.unlink(missing_ok=True)
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the pinned OS Open Built Up Areas 2026-04 GB artifact."
    )
    parser.add_argument(
        "--path", type=Path,
        help=f"installation path (default: {GB_BUA_PATH_ENV} or {GB_BUA_DEFAULT_PATH})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="replace an existing artifact only after the new download validates",
    )
    arguments = parser.parse_args(argv)
    try:
        result = install_gb_bua_artifact(arguments.path, force=arguments.force)
    except (GbBuaSetupError, OSError) as error:
        parser.exit(1, f"GB BUA setup failed: {error}\n")
    print(f"{'Installed' if result.installed else 'Already installed'}: {result.path}")
    print(f"Size: {result.size_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
