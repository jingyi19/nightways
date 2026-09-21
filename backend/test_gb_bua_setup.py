"""Offline installer tests against a small, real GeoPackage fixture.

The fixture preserves the 33 London members and all seven station polygons,
but substitutes a 39-feature count and test-only file digests. Production pins
remain unchanged outside these tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import struct
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from backend import build_gb_bua_artifact as builder
from backend import setup_gb_bua as installer


FIXTURE_FEATURE_COUNT = 39


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _square(x: float, y: float, radius: float = 0.0003):
    return (
        (x - radius, y - radius),
        (x + radius, y - radius),
        (x + radius, y + radius),
        (x - radius, y + radius),
        (x - radius, y - radius),
    )


def _multipolygon(squares) -> tuple[bytes, tuple[float, float, float, float]]:
    """Encode simple squares in the exact 2D GeoPackage/WKB representation."""

    coordinates = [point for square in squares for point in square]
    minx = min(point[0] for point in coordinates)
    maxx = max(point[0] for point in coordinates)
    miny = min(point[1] for point in coordinates)
    maxy = max(point[1] for point in coordinates)
    header = b"GP\x00\x03" + struct.pack("<i4d", 4326, minx, maxx, miny, maxy)
    multipolygon = struct.pack("<BI", 1, 6) + struct.pack("<I", len(squares))
    for square in squares:
        multipolygon += struct.pack("<BIII", 1, 3, 1, len(square))
        multipolygon += b"".join(struct.pack("<dd", *point) for point in square)
    return header + multipolygon, (minx, maxx, miny, maxy)


def _create_fixture(path: Path, manifest: dict) -> None:
    layer = builder.OUTPUT_LAYER
    rtree = f"rtree_{layer}_geom"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            f"""
            CREATE TABLE gpkg_contents (
                table_name TEXT PRIMARY KEY, data_type TEXT NOT NULL,
                identifier TEXT, description TEXT, last_change TEXT,
                min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER
            );
            CREATE TABLE gpkg_geometry_columns (
                table_name TEXT PRIMARY KEY, column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
                z INTEGER NOT NULL, m INTEGER NOT NULL
            );
            CREATE TABLE gpkg_extensions (
                table_name TEXT, column_name TEXT, extension_name TEXT,
                definition TEXT, scope TEXT
            );
            CREATE TABLE {layer} (
                fid INTEGER PRIMARY KEY,
                geom MULTIPOLYGON NOT NULL,
                GISCO_ID TEXT,
                CNTR_CODE TEXT,
                LAU_NAME TEXT,
                YEAR INTEGER,
                SOURCE_GSS_CODE TEXT,
                SOURCE_NAME1_TEXT TEXT,
                SOURCE_NAME1_LANGUAGE TEXT,
                SOURCE_NAME2_TEXT TEXT,
                SOURCE_NAME2_LANGUAGE TEXT,
                SOURCE_AREA_HECTARES REAL
            );
            CREATE VIRTUAL TABLE {rtree} USING rtree(
                id, minx, maxx, miny, maxy
            );
            CREATE TABLE {builder.AGGREGATE_TABLE} (
                aggregate_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                source_gss_code TEXT NOT NULL UNIQUE,
                PRIMARY KEY (aggregate_id, source_gss_code)
            );
            CREATE TABLE {builder.METADATA_TABLE} (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO gpkg_contents "
            "(table_name, data_type, identifier, last_change, srs_id) "
            "VALUES (?, 'features', ?, ?, 4326)",
            (layer, layer, builder.FIXED_GPKG_TIMESTAMP),
        )
        connection.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', "
            "'MULTIPOLYGON', 4326, 0, 0)",
            (layer,),
        )
        connection.execute(
            "INSERT INTO gpkg_extensions VALUES (?, 'geom', "
            "'gpkg_rtree_index', '', 'write-only')",
            (layer,),
        )
        for table in (builder.AGGREGATE_TABLE, builder.METADATA_TABLE):
            connection.execute(
                "INSERT INTO gpkg_contents "
                "(table_name, data_type, identifier, last_change) "
                "VALUES (?, 'attributes', ?, ?)",
                (table, table, builder.FIXED_GPKG_TIMESTAMP),
            )
        for suffix in (
            "insert", "update1", "update2", "update3", "update4", "delete"
        ):
            connection.execute(
                f'CREATE TRIGGER "{rtree}_{suffix}" AFTER INSERT ON {layer} '
                "BEGIN SELECT 1; END"
            )

        station_by_code = {
            point.source_gss_code: point for point in builder.VALIDATION_POINTS
        }
        features = []
        for index, code in enumerate(builder.LONDON_CODES):
            point = station_by_code.get(code)
            x, y = (
                (point.lon, point.lat)
                if point is not None
                else (-0.5 + index * 0.004, 51.8)
            )
            features.append((code, "GB_LONDON", "London", (_square(x, y),)))
        for point in builder.VALIDATION_POINTS[2:]:
            features.append(
                (
                    point.source_gss_code,
                    point.gisco_id,
                    point.display_name,
                    (_square(point.lon, point.lat),),
                )
            )
        # Four disjoint pieces in one feature give the realistic GB extent,
        # without accidentally containing any of the seven station points.
        features.append(
            (
                "E63099999", "E63099999", "Extent fixture",
                (
                    _square(-9, 51, 0.01), _square(2, 51, 0.01),
                    _square(0, 50, 0.01), _square(0, 61, 0.01),
                ),
            )
        )
        assert len(features) == FIXTURE_FEATURE_COUNT
        for fid, (code, identity, label, squares) in enumerate(features, 1):
            geom, bounds = _multipolygon(squares)
            connection.execute(
                f"INSERT INTO {layer} VALUES "
                "(?, ?, ?, 'GB', ?, 2026, ?, ?, NULL, NULL, NULL, 1.0)",
                (fid, geom, identity, label, code, label),
            )
            connection.execute(
                f"INSERT INTO {rtree} VALUES (?, ?, ?, ?, ?)",
                (fid, *bounds),
            )
        connection.executemany(
            f"INSERT INTO {builder.AGGREGATE_TABLE} VALUES "
            "('GB_LONDON', 'London', ?)",
            ((code,) for code in builder.LONDON_CODES),
        )
        connection.executemany(
            f"INSERT INTO {builder.METADATA_TABLE} VALUES (?, ?)",
            sorted(builder._metadata_values(manifest).items()),
        )
        connection.commit()


def _distribution_zip(gpkg: bytes, licence: bytes, extra=None) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(
        payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.writestr(
            builder._zip_info("nightways-gb-bua-2026-04-v1.gpkg"), gpkg
        )
        archive.writestr(
            builder._zip_info("os-open-built-up-areas-2026.txt"), licence
        )
        for name, data in extra or ():
            archive.writestr(builder._zip_info(name), data)
    return payload.getvalue()


class _MockResponse(io.BytesIO):

    def __init__(self, payload: bytes, content_length=None):
        super().__init__(payload)
        self.headers = {
            "Content-Length": (
                str(len(payload)) if content_length is None else content_length
            )
        }


class GbBuaInstallerTests(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(builder, "SOURCE_FEATURE_COUNT", FIXTURE_FEATURE_COUNT)
        )
        self.manifest = json.loads(
            builder.DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        self.manifest["source"]["feature_count"] = FIXTURE_FEATURE_COUNT
        self.manifest["derived"]["feature_count"] = FIXTURE_FEATURE_COUNT
        self.fixture = self.root / "source.gpkg"
        _create_fixture(self.fixture, self.manifest)
        self.gpkg = self.fixture.read_bytes()
        self.licence = builder.DEFAULT_LICENCE_PATH.read_bytes()
        self.zip_bytes = _distribution_zip(self.gpkg, self.licence)
        self.manifest_path = self.root / "manifest.json"
        self.stack.enter_context(
            patch.object(installer, "DEFAULT_MANIFEST_PATH", self.manifest_path)
        )
        self._pin(self.gpkg, self.zip_bytes)
        self.target = self.root / "installed" / "gb-bua.gpkg"

    def _pin(self, gpkg: bytes, zip_bytes: bytes) -> None:
        derived = self.manifest["derived"]
        derived["geopackage_size_bytes"] = len(gpkg)
        derived["geopackage_sha256"] = _sha256(gpkg)
        derived["zip_size_bytes"] = len(zip_bytes)
        derived["zip_sha256"] = _sha256(zip_bytes)
        self.manifest_path.write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        for name, value in (
            ("GB_BUA_GPKG_SIZE_BYTES", len(gpkg)),
            ("GB_BUA_GPKG_SHA256", _sha256(gpkg)),
            ("GB_BUA_ZIP_SIZE_BYTES", len(zip_bytes)),
            ("GB_BUA_ZIP_SHA256", _sha256(zip_bytes)),
        ):
            self.stack.enter_context(patch.object(installer, name, value))

    def _opener(self, payload: bytes, content_length=None):
        def open_fixture(request, timeout):
            self.assertEqual(request.full_url, installer.GB_BUA_ASSET_URL)
            self.assertGreater(timeout, 0)
            return _MockResponse(payload, content_length)

        return open_fixture

    def _repack_modified_database(self, change) -> bytes:
        path = self.root / "modified.gpkg"
        path.write_bytes(self.gpkg)
        with closing(sqlite3.connect(path)) as connection:
            if isinstance(change, str):
                connection.executescript(change)
            else:
                change(connection)
            connection.commit()
        gpkg = path.read_bytes()
        archive = _distribution_zip(gpkg, self.licence)
        self._pin(gpkg, archive)
        return archive

    def test_valid_install_and_seven_station_checks(self):
        result = installer.install_gb_bua_artifact(
            self.target, opener=self._opener(self.zip_bytes)
        )
        self.assertTrue(result.installed)
        self.assertEqual(result.path, self.target)
        self.assertEqual(result.size_bytes, len(self.gpkg))
        self.assertEqual(self.target.read_bytes(), self.gpkg)
        self.assertEqual(installer.validate_gb_bua_artifact(self.target), len(self.gpkg))
        self.assertEqual(list(self.target.parent.glob("*.tmp")), [])

    def test_valid_existing_artifact_reused_without_download(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.gpkg)

        def forbidden_download(_request, timeout):
            self.fail(f"unexpected download (timeout={timeout})")

        result = installer.install_gb_bua_artifact(
            self.target, opener=forbidden_download
        )
        self.assertFalse(result.installed)
        self.assertEqual(result.size_bytes, len(self.gpkg))
        self.assertEqual(self.target.read_bytes(), self.gpkg)

    def test_environment_path_override(self):
        path = self.root / "override" / "custom.gpkg"
        with patch.dict(os.environ, {installer.GB_BUA_PATH_ENV: str(path)}):
            result = installer.install_gb_bua_artifact(
                opener=self._opener(self.zip_bytes)
            )
        self.assertEqual(result.path, path)
        self.assertEqual(path.read_bytes(), self.gpkg)

    def test_incorrect_content_length_rejected(self):
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target,
                opener=self._opener(
                    self.zip_bytes, content_length=str(len(self.zip_bytes) + 1)
                ),
            )
        self.assertFalse(self.target.exists())

    def test_bad_zip_checksum_rejected(self):
        altered = self.zip_bytes[:-1] + bytes([self.zip_bytes[-1] ^ 1])
        self.assertEqual(len(altered), len(self.zip_bytes))
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(altered)
            )
        self.assertFalse(self.target.exists())

    def test_bad_geopackage_checksum_rejected(self):
        altered_gpkg = self.gpkg[:-1] + bytes([self.gpkg[-1] ^ 1])
        archive = _distribution_zip(altered_gpkg, self.licence)
        self._pin(self.gpkg, archive)  # Valid ZIP, but pinned GPKG differs.
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )
        self.assertFalse(self.target.exists())

    def test_unexpected_zip_member_rejected(self):
        archive = _distribution_zip(
            self.gpkg, self.licence, extra=[("raw-os-source.gpkg", b"not allowed")]
        )
        self._pin(self.gpkg, archive)
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )
        self.assertFalse(self.target.exists())

    def test_corrupt_sqlite_rejected_after_hash_validation(self):
        corrupt = b"not a SQLite database"
        archive = _distribution_zip(corrupt, self.licence)
        self._pin(corrupt, archive)
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )
        self.assertFalse(self.target.exists())

    def test_wrong_layer_schema_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            f"ALTER TABLE {builder.OUTPUT_LAYER} DROP COLUMN LAU_NAME;"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_wrong_crs_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            "UPDATE gpkg_geometry_columns SET srs_id=27700 "
            "WHERE table_name='nightways_gb_bua';"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_wrong_feature_count_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            "DELETE FROM nightways_gb_bua WHERE SOURCE_GSS_CODE='E63099999'; "
            "DELETE FROM rtree_nightways_gb_bua_geom WHERE id=39;"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_wrong_london_membership_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            "DELETE FROM nightways_locality_aggregate_members "
            "WHERE source_gss_code='E63019250';"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_wrong_embedded_metadata_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            "DELETE FROM nightways_dataset_metadata "
            "WHERE key='source_release';"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_null_country_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            "UPDATE nightways_gb_bua SET CNTR_CODE=NULL "
            "WHERE SOURCE_GSS_CODE='E63015571';"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_invalid_geometry_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            "UPDATE nightways_gb_bua SET geom=zeroblob(length(geom)) "
            "WHERE SOURCE_GSS_CODE='E63015571';"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_station_point_mismatch_rejected_after_hash_validation(self):
        manchester = builder.VALIDATION_POINTS[2]
        geom, bounds = _multipolygon(
            (_square(manchester.lon, manchester.lat + 0.02),)
        )

        def move_polygon(connection):
            rowid = connection.execute(
                "SELECT fid FROM nightways_gb_bua WHERE SOURCE_GSS_CODE=?",
                (manchester.source_gss_code,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE nightways_gb_bua SET geom=? WHERE fid=?",
                (geom, rowid),
            )
            connection.execute(
                "UPDATE rtree_nightways_gb_bua_geom "
                "SET minx=?, maxx=?, miny=?, maxy=? WHERE id=?",
                (*bounds, rowid),
            )

        archive = self._repack_modified_database(move_polygon)
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_missing_rtree_rejected_after_hash_validation(self):
        archive = self._repack_modified_database(
            "DROP TABLE rtree_nightways_gb_bua_geom;"
        )
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, opener=self._opener(archive)
            )

    def test_failed_forced_install_keeps_valid_existing_artifact(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.gpkg)
        corrupt = b"not a SQLite database"
        archive = _distribution_zip(corrupt, self.licence)
        self._pin(corrupt, archive)
        with self.assertRaises(installer.GbBuaSetupError):
            installer.install_gb_bua_artifact(
                self.target, force=True, opener=self._opener(archive)
            )
        self.assertEqual(self.target.read_bytes(), self.gpkg)

    def test_failed_atomic_replace_keeps_valid_existing_artifact(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.gpkg)
        with patch.object(installer.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(installer.GbBuaSetupError):
                installer.install_gb_bua_artifact(
                    self.target,
                    force=True,
                    opener=self._opener(self.zip_bytes),
                )
        self.assertEqual(self.target.read_bytes(), self.gpkg)
        self.assertEqual(list(self.target.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
