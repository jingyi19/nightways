import sqlite3
import struct
import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.localities import (
    DEFAULT_GISCO_LAU_PATH,
    DestinationLocality,
    GISCO_LAU_PATH_ENV,
    GiscoLauIndex,
    LocalityDatasetUnavailableError,
    LocalityResolutionStatus,
    build_locality_response,
    configured_gisco_lau_path,
)


class GiscoPathConfigurationTests(unittest.TestCase):

    def test_default_path_is_relative_to_project_root(self):
        expected = (
            Path(__file__).resolve().parents[1] / DEFAULT_GISCO_LAU_PATH
        ).resolve()

        self.assertEqual(configured_gisco_lau_path({}), expected)

    def test_environment_path_overrides_default(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            override = Path(temporary_directory) / "custom.gpkg"

            result = configured_gisco_lau_path(
                {GISCO_LAU_PATH_ENV: str(override)}
            )

        self.assertEqual(result, override.resolve())

    def test_missing_configured_dataset_has_dedicated_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.gpkg"

            with self.assertRaisesRegex(
                LocalityDatasetUnavailableError,
                "python backend/setup_gisco.py",
            ):
                GiscoLauIndex.from_environment(
                    environ={GISCO_LAU_PATH_ENV: str(missing)}
                )


class GiscoLauIndexTests(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.fixture_path = (
            Path(self.temporary_directory.name) / "lau_fixture.gpkg"
        )
        _create_fixture(self.fixture_path)
        self.index = GiscoLauIndex(self.fixture_path)

    def tearDown(self):
        self.index.close()
        self.temporary_directory.cleanup()

    def test_polygon_uses_longitude_then_latitude(self):
        resolution = self.index.resolve(1.0, 3.0, "DE")

        self.assertEqual(resolution.status, LocalityResolutionStatus.RESOLVED)
        self.assertEqual(
            resolution.locality,
            DestinationLocality("DE_B", "DE", "Beta", 2024),
        )

    def test_multipolygon_resolves_second_component(self):
        resolution = self.index.resolve(10.5, 12.5, "DE")

        self.assertEqual(resolution.status, LocalityResolutionStatus.RESOLVED)
        self.assertEqual(resolution.locality.gisco_id, "DE_MULTI")

    def test_shared_boundary_is_ambiguous_and_no_match_is_explicit(self):
        boundary = self.index.resolve(1.0, 2.0, "DE")
        no_match = self.index.resolve(50.0, 50.0, "DE")

        self.assertEqual(
            boundary.status,
            LocalityResolutionStatus.MULTIPLE_MATCHES,
        )
        self.assertEqual(
            [item.gisco_id for item in boundary.candidates],
            ["DE_A", "DE_B"],
        )
        self.assertEqual(no_match.status, LocalityResolutionStatus.NO_MATCH)

    def test_unsupported_country_is_not_treated_as_no_match(self):
        resolution = self.index.resolve(51.5, -0.1, "GB")

        self.assertEqual(
            resolution.status,
            LocalityResolutionStatus.UNSUPPORTED_COUNTRY,
        )
        self.assertEqual(resolution.country_code, "GB")

    def test_representative_city_and_airport_containment(self):
        expected = {
            (48.185, 16.378): "AT_90001",  # Wien Hbf
            (48.174, 16.333): "AT_90001",  # Wien Meidling
            (48.191, 16.415): "AT_90001",  # Vienna Erdberg
            (48.120, 16.563): "AT_30740",  # Vienna Airport station
            (48.839, 2.383): "FR_PARIS",  # Paris Bercy
            (48.878, 2.282): "FR_PARIS",  # Porte Maillot
            (48.140, 11.560): "DE_MUNICH",  # Muenchen Hbf
            (48.212, 11.616): "DE_MUNICH",  # Froettmaning
            (52.379, 4.900): "NL_AMSTERDAM",  # Amsterdam Centraal
            (52.339, 4.872): "NL_AMSTERDAM",  # Amsterdam Zuid
            (52.310, 4.768): "NL_HAARLEMMERMEER",  # Schiphol
        }

        for (latitude, longitude), gisco_id in expected.items():
            with self.subTest(gisco_id=gisco_id, coordinate=(latitude, longitude)):
                resolution = self.index.resolve(latitude, longitude)
                self.assertEqual(
                    resolution.status,
                    LocalityResolutionStatus.RESOLVED,
                )
                self.assertEqual(resolution.locality.gisco_id, gisco_id)


class LocalityGroupingTests(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.fixture_path = (
            Path(self.temporary_directory.name) / "lau_fixture.gpkg"
        )
        _create_fixture(self.fixture_path)
        self.index = GiscoLauIndex(self.fixture_path)

    def tearDown(self):
        self.index.close()
        self.temporary_directory.cleanup()

    def test_stations_and_trips_group_by_gisco_id_without_losing_detail(self):
        trips = [
            _trip(
                "trip-one",
                [
                    _arrival("Wien Hbf", "2026-08-15T06:00:00+02:00", 48.185, 16.378, "AT"),
                    _arrival("Wien Meidling", "2026-08-15T06:10:00+02:00", 48.174, 16.333, "AT"),
                    _arrival("Vienna Erdberg", "2026-08-15T06:20:00+02:00", 48.191, 16.415, "AT"),
                    _arrival("Vienna Airport", "2026-08-15T06:40:00+02:00", 48.120, 16.563, "AT"),
                ],
            ),
            _trip(
                "trip-two",
                [
                    _arrival("Wien Hbf", "2026-08-15T07:00:00+02:00", 48.185, 16.378, "AT"),
                ],
            ),
        ]

        result = build_locality_response(
            "Berlin",
            date(2026, 8, 14),
            trips,
            self.index,
        )

        by_id = {
            destination["gisco_id"]: destination
            for destination in result["destinations"]
        }
        vienna = by_id["AT_90001"]
        airport = by_id["AT_30740"]
        self.assertEqual(vienna["city"], "Wien")
        self.assertEqual(vienna["dataset_year"], 2024)
        self.assertEqual(vienna["service_count"], 2)
        self.assertEqual(vienna["station_count"], 3)
        self.assertEqual(
            [service["trip_id"] for service in vienna["services"]],
            ["trip-one", "trip-two"],
        )
        self.assertEqual(
            [
                stop["station"]
                for stop in vienna["services"][0]["stops"]
            ],
            ["Wien Hbf", "Wien Meidling", "Vienna Erdberg"],
        )
        self.assertEqual(airport["city"], "Schwechat")
        self.assertEqual(airport["station_count"], 1)
        self.assertEqual(result["qualifying_trip_count"], 2)
        self.assertEqual(result["raw_arrival_event_count"], 5)

    def test_unresolved_ambiguous_and_unsupported_events_stay_explicit(self):
        trips = [
            _trip(
                "trip-one",
                [
                    _arrival("Shared boundary", "2026-08-15T06:00:00+02:00", 1.0, 2.0, "DE"),
                    _arrival("Outside", "2026-08-15T06:10:00+02:00", 50.0, 50.0, "DE"),
                    _arrival("London", "2026-08-15T06:20:00+01:00", 51.5, -0.1, "GB"),
                ],
            )
        ]

        result = build_locality_response(
            "Berlin",
            date(2026, 8, 14),
            trips,
            self.index,
        )

        self.assertEqual(result["destination_count"], 0)
        self.assertEqual(result["unresolved_arrival_count"], 2)
        self.assertEqual(
            {event["status"] for event in result["unresolved_arrivals"]},
            {"multiple_matches", "no_match"},
        )
        self.assertEqual(result["unsupported_country_arrival_count"], 1)
        self.assertEqual(
            result["unsupported_country_arrivals"][0]["status"],
            "unsupported_country",
        )


def _trip(trip_id, arrivals):
    return {
        "trip_id": trip_id,
        "service": f"Service {trip_id}",
        "operator": "Test operator",
        "mode": "train",
        "departure": "2026-08-14T20:00:00+02:00",
        "arrivals": arrivals,
    }


def _arrival(station, arrival, latitude, longitude, country_code):
    return {
        "station": station,
        "arrival": arrival,
        "lat": latitude,
        "lon": longitude,
        "country_code": country_code,
    }


def _create_fixture(path: Path) -> None:
    features = [
        ("DE_A", "DE", "Alpha", _polygon(0, 0, 2, 2)),
        ("DE_B", "DE", "Beta", _polygon(2, 0, 4, 2)),
        (
            "DE_MULTI",
            "DE",
            "Islands",
            _multipolygon(
                _polygon(10, 10, 11, 11),
                _polygon(12, 10, 13, 11),
            ),
        ),
        ("AT_90001", "AT", "Wien", _polygon(16.1, 48.1, 16.49, 48.35)),
        ("AT_30740", "AT", "Schwechat", _polygon(16.50, 48.0, 16.80, 48.20)),
        ("FR_PARIS", "FR", "Paris", _polygon(2.20, 48.75, 2.50, 48.95)),
        ("DE_MUNICH", "DE", "Muenchen", _polygon(11.35, 48.0, 11.75, 48.30)),
        ("NL_AMSTERDAM", "NL", "Amsterdam", _polygon(4.82, 52.30, 5.05, 52.50)),
        (
            "NL_HAARLEMMERMEER",
            "NL",
            "Haarlemmermeer",
            _polygon(4.65, 52.25, 4.80, 52.36),
        ),
    ]

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE gpkg_contents (
            table_name TEXT PRIMARY KEY,
            data_type TEXT NOT NULL,
            identifier TEXT,
            description TEXT,
            last_change TEXT,
            min_x DOUBLE,
            min_y DOUBLE,
            max_x DOUBLE,
            max_y DOUBLE,
            srs_id INTEGER
        );
        CREATE TABLE gpkg_geometry_columns (
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL,
            z TINYINT,
            m TINYINT
        );
        CREATE TABLE LAU_RG_01M_2024_4326 (
            fid INTEGER PRIMARY KEY,
            GISCO_ID TEXT NOT NULL,
            CNTR_CODE TEXT NOT NULL,
            LAU_NAME TEXT NOT NULL,
            YEAR INTEGER NOT NULL,
            geom BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE rtree_LAU_RG_01M_2024_4326_geom USING rtree(
            id, minx, maxx, miny, maxy
        );
        INSERT INTO gpkg_contents (
            table_name, data_type, identifier, srs_id
        ) VALUES (
            'LAU_RG_01M_2024_4326',
            'features',
            'LAU_RG_01M_2024_4326',
            4326
        );
        INSERT INTO gpkg_geometry_columns (
            table_name, column_name, geometry_type_name, srs_id, z, m
        ) VALUES (
            'LAU_RG_01M_2024_4326',
            'geom',
            'MULTIPOLYGON',
            4326,
            0,
            0
        );
        """
    )

    for feature_id, (gisco_id, country, name, geometry) in enumerate(features, 1):
        blob, bounds = geometry
        connection.execute(
            """
            INSERT INTO LAU_RG_01M_2024_4326 (
                fid, GISCO_ID, CNTR_CODE, LAU_NAME, YEAR, geom
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (feature_id, gisco_id, country, name, 2024, blob),
        )
        connection.execute(
            """
            INSERT INTO rtree_LAU_RG_01M_2024_4326_geom (
                id, minx, maxx, miny, maxy
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (feature_id, *bounds),
        )

    connection.commit()
    connection.close()


def _polygon(minimum_x, minimum_y, maximum_x, maximum_y):
    ring = (
        (minimum_x, minimum_y),
        (maximum_x, minimum_y),
        (maximum_x, maximum_y),
        (minimum_x, maximum_y),
        (minimum_x, minimum_y),
    )
    wkb = _polygon_wkb((ring,))
    blob = _geopackage_header() + wkb
    return blob, (minimum_x, maximum_x, minimum_y, maximum_y)


def _multipolygon(*polygon_values):
    polygon_wkbs = []
    bounds = []
    for blob, polygon_bounds in polygon_values:
        polygon_wkbs.append(blob[8:])
        bounds.append(polygon_bounds)
    wkb = (
        b"\x01"
        + struct.pack("<I", 6)
        + struct.pack("<I", len(polygon_wkbs))
        + b"".join(polygon_wkbs)
    )
    return (
        _geopackage_header() + wkb,
        (
            min(item[0] for item in bounds),
            max(item[1] for item in bounds),
            min(item[2] for item in bounds),
            max(item[3] for item in bounds),
        ),
    )


def _polygon_wkb(rings):
    value = b"\x01" + struct.pack("<I", 3) + struct.pack("<I", len(rings))
    for ring in rings:
        value += struct.pack("<I", len(ring))
        for longitude, latitude in ring:
            value += struct.pack("<dd", longitude, latitude)
    return value


def _geopackage_header():
    return b"GP" + bytes((0, 1)) + struct.pack("<i", 4326)


if __name__ == "__main__":
    unittest.main(verbosity=2)
