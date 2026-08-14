import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.localities import GiscoLauIndex, LocalityDatasetError
from backend.setup_gisco import (
    GISCO_SETUP_USER_AGENT,
    GiscoSetupError,
    install_gisco_dataset,
    validate_gisco_dataset,
)
from backend.test_localities import GISCO_FIXTURE_TABLE, _create_fixture


class GiscoDatasetValidationTests(unittest.TestCase):

    def test_invalid_dataset_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "invalid.gpkg"
            path.write_bytes(b"not a GeoPackage")

            with self.assertRaises(LocalityDatasetError):
                validate_gisco_dataset(path, minimum_size_bytes=0)

    def test_valid_small_fixture_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "fixture.gpkg"
            _create_fixture(path)

            size_bytes = validate_gisco_dataset(
                path,
                minimum_size_bytes=0,
            )

        self.assertGreater(size_bytes, 0)

    def test_current_gisco_internal_table_name_is_discovered(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "current-name.gpkg"
            _create_fixture(path)

            validate_gisco_dataset(path, minimum_size_bytes=0)
            with GiscoLauIndex(path) as index:
                discovered_table = index.table_name

        self.assertEqual(discovered_table, GISCO_FIXTURE_TABLE)

    def test_fixture_without_year_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "missing-year.gpkg"
            _create_fixture(path)
            connection = sqlite3.connect(path)
            connection.execute(
                f'ALTER TABLE "{GISCO_FIXTURE_TABLE}" DROP COLUMN YEAR'
            )
            connection.close()

            with self.assertRaisesRegex(LocalityDatasetError, "YEAR"):
                validate_gisco_dataset(path, minimum_size_bytes=0)

    def test_multiple_appropriate_feature_layers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "multiple.gpkg"
            _create_fixture(path)
            _add_second_feature_layer(path)

            with self.assertRaisesRegex(
                LocalityDatasetError,
                "exactly one appropriate feature layer; found 2",
            ):
                validate_gisco_dataset(path, minimum_size_bytes=0)


class GiscoDatasetInstallationTests(unittest.TestCase):

    def test_existing_valid_dataset_is_not_overwritten_or_downloaded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "lau-2024.gpkg"
            _create_fixture(target)
            original = target.read_bytes()

            def unexpected_download(_request, timeout):
                self.fail(f"unexpected download with timeout {timeout}")

            result = install_gisco_dataset(
                target,
                opener=unexpected_download,
                minimum_size_bytes=0,
            )

            self.assertFalse(result.installed)
            self.assertEqual(target.read_bytes(), original)

    def test_mock_download_is_validated_and_installed_atomically(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.gpkg"
            target = root / "nested" / "lau-2024.gpkg"
            _create_fixture(source)
            payload = source.read_bytes()
            requests = []

            def open_fixture(request, timeout):
                requests.append((request, timeout))
                return _MockResponse(payload)

            result = install_gisco_dataset(
                target,
                opener=open_fixture,
                minimum_size_bytes=0,
            )

            self.assertTrue(result.installed)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(result.size_bytes, len(payload))
            self.assertEqual(requests[0][1], 120)
            self.assertEqual(
                requests[0][0].get_header("User-agent"),
                GISCO_SETUP_USER_AGENT,
            )
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.*.tmp")),
                [],
            )

    def test_incomplete_mock_download_never_replaces_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.gpkg"
            target = root / "lau-2024.gpkg"
            _create_fixture(source)
            payload = source.read_bytes()

            def open_incomplete(_request, timeout):
                return _MockResponse(payload, declared_size=len(payload) + 1)

            with self.assertRaises(GiscoSetupError):
                install_gisco_dataset(
                    target,
                    opener=open_incomplete,
                    minimum_size_bytes=0,
                )

            self.assertFalse(target.exists())
            self.assertEqual(
                list(root.glob(f".{target.name}.*.tmp")),
                [],
            )


class _MockResponse(io.BytesIO):

    def __init__(self, payload: bytes, declared_size: int | None = None):
        super().__init__(payload)
        self.headers = {
            "Content-Length": str(
                len(payload) if declared_size is None else declared_size
            )
        }


def _add_second_feature_layer(path: Path) -> None:
    table_name = "second_lau_layer"
    rtree_name = f"rtree_{table_name}_geom"
    source_rtree = f"rtree_{GISCO_FIXTURE_TABLE}_geom"
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE "{table_name}" AS
            SELECT * FROM "{GISCO_FIXTURE_TABLE}";
        CREATE VIRTUAL TABLE "{rtree_name}" USING rtree(
            id, minx, maxx, miny, maxy
        );
        INSERT INTO "{rtree_name}"
            SELECT * FROM "{source_rtree}";
        INSERT INTO gpkg_contents (
            table_name, data_type, identifier, srs_id
        ) VALUES (
            '{table_name}', 'features', '{table_name}', 4326
        );
        INSERT INTO gpkg_geometry_columns (
            table_name, column_name, geometry_type_name, srs_id, z, m
        ) VALUES (
            '{table_name}', 'geom', 'MULTIPOLYGON', 4326, 0, 0
        );
        """
    )
    connection.commit()
    connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
