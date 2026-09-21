"""Focused runtime tests for the pinned Great Britain BUA provider."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from backend import build_gb_bua_artifact as builder
from backend import gb_localities as gb
from backend.localities import (
    DestinationLocality,
    GiscoLauIndex,
    LocalityDatasetError,
    LocalityDatasetUnavailableError,
    LocalityResolution,
    LocalityResolutionStatus,
    build_locality_response,
)
from backend.test_gb_bua_setup import FIXTURE_FEATURE_COUNT, _create_fixture
from backend.test_localities import _create_fixture as _create_gisco_fixture


class GbBuaPathConfigurationTests(unittest.TestCase):

    def test_default_path_matches_the_installer_default(self):
        expected = Path(gb.DEFAULT_GB_BUA_PATH)
        if not expected.is_absolute():
            expected = Path(__file__).resolve().parents[1] / expected

        self.assertEqual(gb.configured_gb_bua_path({}), expected.resolve())

    def test_environment_path_overrides_default(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            override = Path(temporary_directory) / "custom.gpkg"

            result = gb.configured_gb_bua_path(
                {gb.GB_BUA_PATH_ENV: str(override)}
            )

        self.assertEqual(result, override.resolve())

    def test_missing_dataset_raises_a_controlled_backend_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.gpkg"

            with self.assertRaisesRegex(
                LocalityDatasetUnavailableError,
                "setup_gb_bua.py",
            ):
                gb.GbBuiltUpAreaIndex.from_environment(
                    environ={gb.GB_BUA_PATH_ENV: str(missing)}
                )


class GbBuiltUpAreaIndexTests(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(gb, "GB_BUA_FEATURE_COUNT", FIXTURE_FEATURE_COUNT)
        )
        self.manifest = json.loads(
            builder.DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        self.manifest["source"]["feature_count"] = FIXTURE_FEATURE_COUNT
        self.manifest["derived"]["feature_count"] = FIXTURE_FEATURE_COUNT
        self.fixture = self.root / "gb-bua.gpkg"
        _create_fixture(self.fixture, self.manifest)
        self.index = gb.GbBuiltUpAreaIndex(self.fixture)
        self.addCleanup(self.index.close)

    def test_all_seven_station_points_resolve_to_approved_localities(self):
        for point in builder.VALIDATION_POINTS:
            with self.subTest(station=point.name):
                result = self.index.resolve(point.lat, point.lon, "GB")

                self.assertEqual(
                    result.status,
                    LocalityResolutionStatus.RESOLVED,
                )
                self.assertEqual(
                    result.locality,
                    DestinationLocality(
                        point.gisco_id,
                        "GB",
                        point.display_name,
                        2026,
                    ),
                )
                self.assertEqual(result.candidates, (result.locality,))

    def test_cardiff_uses_the_english_display_name(self):
        cardiff = builder.VALIDATION_POINTS[-1]

        result = self.index.resolve(cardiff.lat, cardiff.lon, "GB")

        self.assertEqual(result.locality.lau_name, "Cardiff")
        self.assertEqual(result.locality.gisco_id, "W45001843")

    def test_invalid_embedded_metadata_is_rejected(self):
        self.index.close()
        with closing(sqlite3.connect(self.fixture)) as connection:
            connection.execute(
                f"DELETE FROM {builder.METADATA_TABLE} "
                "WHERE key = 'source_release'"
            )
            connection.commit()

        with self.assertRaisesRegex(LocalityDatasetError, "metadata"):
            gb.GbBuiltUpAreaIndex(self.fixture)

    def test_invalid_london_membership_is_rejected(self):
        self.index.close()
        with closing(sqlite3.connect(self.fixture)) as connection:
            connection.execute(
                f"DELETE FROM {builder.AGGREGATE_TABLE} "
                "WHERE source_gss_code = ?",
                (builder.LONDON_CODES[0],),
            )
            connection.commit()

        with self.assertRaisesRegex(LocalityDatasetError, "London"):
            gb.GbBuiltUpAreaIndex(self.fixture)


class CompositeLocalityIndexTests(unittest.TestCase):

    @patch.object(gb.GbBuiltUpAreaIndex, "from_environment")
    @patch.object(gb.GiscoLauIndex, "from_environment")
    def test_environment_composite_keeps_gb_lazy_for_gisco_only_request(
        self,
        gisco_from_environment,
        gb_from_environment,
    ):
        gisco = Mock()
        expected = _resolved("DE_BERLIN", "DE", "Berlin", 2024)
        gisco.resolve.return_value = expected
        gisco_from_environment.return_value = gisco
        environment = {
            "NIGHTWAYS_GISCO_LAU_PATH": "gisco.gpkg",
            gb.GB_BUA_PATH_ENV: "missing-gb.gpkg",
        }

        with gb.CompositeLocalityIndex.from_environment(environment) as composite:
            result = composite.resolve(52.52, 13.405, "DE")

        self.assertIs(result, expected)
        gisco_from_environment.assert_called_once_with(environ=environment)
        gb_from_environment.assert_not_called()
        gisco.close.assert_called_once_with()

    def test_real_gisco_results_are_unchanged_and_never_open_gb(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Path(temporary_directory) / "gisco.gpkg"
            _create_gisco_fixture(fixture)
            gisco = GiscoLauIndex(fixture)
            factory = Mock()
            composite = gb.CompositeLocalityIndex(
                gisco, gb_index_factory=factory
            )
            try:
                explicit_direct = gisco.resolve(1.0, 3.0, "DE")
                explicit_composite = composite.resolve(1.0, 3.0, "DE")
                hintless_direct = gisco.resolve(48.839, 2.383)
                hintless_composite = composite.resolve(48.839, 2.383)

                self.assertEqual(explicit_composite, explicit_direct)
                self.assertEqual(hintless_composite, hintless_direct)
                self.assertEqual(
                    explicit_composite.status,
                    LocalityResolutionStatus.RESOLVED,
                )
                self.assertEqual(
                    hintless_composite.status,
                    LocalityResolutionStatus.RESOLVED,
                )
                factory.assert_not_called()
            finally:
                composite.close()

    def test_explicit_gb_provider_failures_are_controlled_and_skip_gisco(self):
        cases = (
            (OSError("missing artifact"), LocalityDatasetUnavailableError),
            (
                sqlite3.DatabaseError("unreadable artifact"),
                LocalityDatasetUnavailableError,
            ),
            (LocalityDatasetError("invalid artifact"), LocalityDatasetError),
        )
        for failure, expected_error in cases:
            with self.subTest(failure=type(failure).__name__):
                gisco = Mock()
                factory = Mock(side_effect=failure)
                composite = gb.CompositeLocalityIndex(
                    gisco, gb_index_factory=factory
                )

                with self.assertRaises(expected_error):
                    composite.resolve(51.49473, -0.1445802, "GB")

                factory.assert_called_once_with()
                gisco.resolve.assert_not_called()

    def test_explicit_gb_uses_only_the_lazy_gb_provider(self):
        gisco = Mock()
        gb_index = Mock()
        expected = _resolved("GB_LONDON", "GB", "London", 2026)
        gb_index.resolve.return_value = expected
        factory = Mock(return_value=gb_index)
        composite = gb.CompositeLocalityIndex(
            gisco, gb_index_factory=factory
        )

        result = composite.resolve(51.49473, -0.1445802, "GB")

        self.assertIs(result, expected)
        factory.assert_called_once_with()
        gb_index.resolve.assert_called_once_with(51.49473, -0.1445802, "GB")
        gisco.resolve.assert_not_called()

    def test_explicit_non_gb_uses_only_gisco(self):
        gisco = Mock()
        expected = _resolved("DE_BERLIN", "DE", "Berlin", 2024)
        gisco.resolve.return_value = expected
        factory = Mock()
        composite = gb.CompositeLocalityIndex(
            gisco, gb_index_factory=factory
        )

        result = composite.resolve(52.52, 13.405, "DE")

        self.assertIs(result, expected)
        gisco.resolve.assert_called_once_with(52.52, 13.405, "DE")
        factory.assert_not_called()

    def test_hintless_gisco_match_never_falls_through_to_gb(self):
        gisco = Mock()
        expected = _resolved("FR_PARIS", "FR", "Paris", 2024)
        gisco.resolve.return_value = expected
        factory = Mock()
        composite = gb.CompositeLocalityIndex(
            gisco, gb_index_factory=factory
        )

        result = composite.resolve(48.8566, 2.3522)

        self.assertIs(result, expected)
        gisco.resolve.assert_called_once_with(48.8566, 2.3522, None)
        factory.assert_not_called()

    def test_hintless_gisco_no_match_falls_through_to_gb(self):
        gisco = Mock()
        gisco.resolve.return_value = LocalityResolution(
            LocalityResolutionStatus.NO_MATCH
        )
        gb_index = Mock()
        expected = _resolved(
            "E63015571", "GB", "Manchester", 2026
        )
        gb_index.resolve.return_value = expected
        factory = Mock(return_value=gb_index)
        composite = gb.CompositeLocalityIndex(
            gisco, gb_index_factory=factory
        )

        result = composite.resolve(53.47722, -2.2301402)

        self.assertIs(result, expected)
        gisco.resolve.assert_called_once_with(53.47722, -2.2301402, None)
        factory.assert_called_once_with()
        gb_index.resolve.assert_called_once_with(53.47722, -2.2301402, None)

    def test_blank_country_hint_is_treated_as_hintless(self):
        gisco = Mock()
        gisco.resolve.return_value = LocalityResolution(
            LocalityResolutionStatus.NO_MATCH
        )
        gb_index = Mock()
        expected = _resolved("GB_LONDON", "GB", "London", 2026)
        gb_index.resolve.return_value = expected
        composite = gb.CompositeLocalityIndex(gisco, gb_index)

        result = composite.resolve(51.53272, -0.1270027, "  ")

        self.assertIs(result, expected)
        gisco.resolve.assert_called_once_with(51.53272, -0.1270027, "  ")
        gb_index.resolve.assert_called_once_with(51.53272, -0.1270027, "  ")

    def test_gisco_non_no_match_results_never_fall_through(self):
        for status in (
            LocalityResolutionStatus.INVALID_COORDINATE,
            LocalityResolutionStatus.MULTIPLE_MATCHES,
            LocalityResolutionStatus.UNSUPPORTED_COUNTRY,
        ):
            with self.subTest(status=status):
                gisco = Mock()
                expected = LocalityResolution(status)
                gisco.resolve.return_value = expected
                factory = Mock()
                composite = gb.CompositeLocalityIndex(
                    gisco, gb_index_factory=factory
                )

                result = composite.resolve(1.0, 2.0)

                self.assertIs(result, expected)
                factory.assert_not_called()

    def test_gisco_error_is_not_masked_by_gb_fallback(self):
        gisco = Mock()
        error = LocalityDatasetError("GISCO failed")
        gisco.resolve.side_effect = error
        factory = Mock()
        composite = gb.CompositeLocalityIndex(
            gisco, gb_index_factory=factory
        )

        with self.assertRaisesRegex(LocalityDatasetError, "GISCO failed"):
            composite.resolve(51.5, -0.1)

        factory.assert_not_called()

    def test_close_is_safe_when_lazy_gb_provider_was_never_opened(self):
        gisco = Mock()
        factory = Mock()
        composite = gb.CompositeLocalityIndex(
            gisco, gb_index_factory=factory
        )

        composite.close()

        gisco.close.assert_called_once_with()
        factory.assert_not_called()

    def test_context_manager_closes_both_opened_providers(self):
        gisco = Mock()
        gb_index = Mock()
        gb_index.resolve.return_value = _resolved(
            "GB_LONDON", "GB", "London", 2026
        )
        with gb.CompositeLocalityIndex(
            gisco, gb_index_factory=Mock(return_value=gb_index)
        ) as composite:
            composite.resolve(51.5, -0.1, "GB")

        gb_index.close.assert_called_once_with()
        gisco.close.assert_called_once_with()


class GbCompositeGroupingTests(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(gb, "GB_BUA_FEATURE_COUNT", FIXTURE_FEATURE_COUNT)
        )
        manifest = json.loads(
            builder.DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        manifest["source"]["feature_count"] = FIXTURE_FEATURE_COUNT
        manifest["derived"]["feature_count"] = FIXTURE_FEATURE_COUNT
        fixture = self.root / "gb-bua.gpkg"
        _create_fixture(fixture, manifest)
        self.gb_index = gb.GbBuiltUpAreaIndex(fixture)
        self.addCleanup(self.gb_index.close)

    def test_missing_country_british_arrival_falls_through_to_gb(self):
        gisco = Mock()
        gisco.resolve.return_value = LocalityResolution(
            LocalityResolutionStatus.NO_MATCH
        )
        composite = gb.CompositeLocalityIndex(gisco, self.gb_index)
        manchester = builder.VALIDATION_POINTS[2]
        trips = [
            {
                "trip_id": "gb-trip",
                "service": "Night train",
                "operator": "Test operator",
                "mode": "train",
                "departure": "2026-08-14T20:00:00+01:00",
                "arrivals": [
                    {
                        "station": manchester.name,
                        "arrival": "2026-08-15T05:00:00+01:00",
                        "lat": manchester.lat,
                        "lon": manchester.lon,
                        "country_code": None,
                    }
                ],
            }
        ]

        result = build_locality_response(
            "London",
            date(2026, 8, 14),
            trips,
            composite,
        )

        self.assertEqual(result["destination_count"], 1)
        self.assertEqual(result["unresolved_arrival_count"], 0)
        self.assertEqual(result["unsupported_country_arrival_count"], 0)
        self.assertEqual(
            result["destinations"][0]["gisco_id"], "E63015571"
        )
        self.assertEqual(result["destinations"][0]["city"], "Manchester")
        self.assertEqual(result["destinations"][0]["country_code"], "GB")
        self.assertEqual(result["destinations"][0]["dataset_year"], 2026)
        gisco.resolve.assert_called_once_with(
            manchester.lat, manchester.lon, None
        )


def _resolved(
    gisco_id: str,
    country_code: str,
    name: str,
    year: int,
) -> LocalityResolution:
    locality = DestinationLocality(gisco_id, country_code, name, year)
    return LocalityResolution(
        LocalityResolutionStatus.RESOLVED,
        locality=locality,
        candidates=(locality,),
        country_code=country_code,
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
