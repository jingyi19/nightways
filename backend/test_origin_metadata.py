import unittest
from unittest.mock import MagicMock, Mock

from backend.localities import (
    DestinationLocality,
    LocalityResolution,
    LocalityResolutionStatus,
)
from backend.origin_metadata import CoordinateOriginMetadataEnricher
from backend.transitous import (
    OriginCandidate,
    OriginMetadata,
    OriginMetadataUnavailableError,
)


class CoordinateOriginMetadataEnricherTests(unittest.TestCase):

    def test_missing_both_uses_unique_gisco_and_land_timezone(self):
        index = Mock()
        index.resolve.return_value = _resolved("PL")
        timezone_context, timezone_finder = _timezone_context(
            "Europe/Warsaw"
        )
        factory = Mock(return_value=timezone_context)
        enricher = CoordinateOriginMetadataEnricher(index, factory)

        result = enricher(_candidate())

        self.assertEqual(result, OriginMetadata("PL", "Europe/Warsaw"))
        index.resolve.assert_called_once_with(50.0619474, 19.9368564)
        factory.assert_called_once_with(in_memory=False)
        timezone_finder.timezone_at_land.assert_called_once_with(
            lng=19.9368564,
            lat=50.0619474,
        )
        timezone_context.__exit__.assert_called_once()

    def test_missing_country_does_not_construct_timezonefinder(self):
        index = Mock()
        index.resolve.return_value = _resolved("PL")
        factory = Mock()
        enricher = CoordinateOriginMetadataEnricher(index, factory)

        result = enricher(_candidate(timezone="Europe/Warsaw"))

        self.assertEqual(result, OriginMetadata(country_code="PL"))
        factory.assert_not_called()

    def test_missing_timezone_does_not_query_gisco(self):
        index = Mock()
        timezone_context, _timezone_finder = _timezone_context(
            "Europe/Warsaw"
        )
        enricher = CoordinateOriginMetadataEnricher(
            index,
            Mock(return_value=timezone_context),
        )

        result = enricher(_candidate(country_code="PL"))

        self.assertEqual(result, OriginMetadata(timezone="Europe/Warsaw"))
        index.resolve.assert_not_called()

    def test_complete_candidate_performs_no_lookups(self):
        index = Mock()
        factory = Mock()
        enricher = CoordinateOriginMetadataEnricher(index, factory)

        result = enricher(
            _candidate(country_code="PL", timezone="Europe/Warsaw")
        )

        self.assertEqual(result, OriginMetadata())
        index.resolve.assert_not_called()
        factory.assert_not_called()

    def test_gisco_no_match_fails_before_timezone_lookup(self):
        index = Mock()
        index.resolve.return_value = LocalityResolution(
            LocalityResolutionStatus.NO_MATCH
        )
        factory = Mock()
        enricher = CoordinateOriginMetadataEnricher(index, factory)

        with self.assertRaises(OriginMetadataUnavailableError):
            enricher(_candidate())

        factory.assert_not_called()

    def test_gisco_multiple_containment_is_not_selected(self):
        first = DestinationLocality("PL_A", "PL", "A", 2024)
        second = DestinationLocality("PL_B", "PL", "B", 2024)
        index = Mock()
        index.resolve.return_value = LocalityResolution(
            LocalityResolutionStatus.MULTIPLE_MATCHES,
            candidates=(first, second),
        )
        enricher = CoordinateOriginMetadataEnricher(index, Mock())

        with self.assertRaises(OriginMetadataUnavailableError):
            enricher(_candidate())

    def test_gisco_el_is_normalized_only_for_origin(self):
        index = Mock()
        index.resolve.return_value = _resolved("EL")
        enricher = CoordinateOriginMetadataEnricher(index, Mock())

        result = enricher(_candidate(timezone="Europe/Athens"))

        self.assertEqual(result.country_code, "GR")
        self.assertEqual(
            index.resolve.return_value.locality.country_code,
            "EL",
        )

    def test_invalid_timezone_is_rejected_after_context_cleanup(self):
        index = Mock()
        timezone_context, _timezone_finder = _timezone_context(
            "Invalid/Nightways"
        )
        enricher = CoordinateOriginMetadataEnricher(
            index,
            Mock(return_value=timezone_context),
        )

        with self.assertRaises(OriginMetadataUnavailableError):
            enricher(_candidate(country_code="PL"))

        timezone_context.__exit__.assert_called_once()


def _candidate(
    country_code: str | None = None,
    timezone: str | None = None,
) -> OriginCandidate:
    return OriginCandidate(
        name="Kraków",
        lat=50.0619474,
        lon=19.9368564,
        country_code=country_code,
        timezone=timezone,
    )


def _resolved(country_code: str) -> LocalityResolution:
    locality = DestinationLocality(
        f"{country_code}_TEST",
        country_code,
        "Test locality",
        2024,
    )
    return LocalityResolution(
        LocalityResolutionStatus.RESOLVED,
        locality=locality,
        candidates=(locality,),
    )


def _timezone_context(timezone: str):
    context = MagicMock()
    finder = context.__enter__.return_value
    finder.timezone_at_land.return_value = timezone
    return context, finder


if __name__ == "__main__":
    unittest.main(verbosity=2)
