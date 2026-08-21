"""Conservative coordinate enrichment for a selected Transitous origin."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timezonefinder import TimezoneFinder

from backend.localities import (
    GiscoLauIndex,
    LocalityResolutionStatus,
)
from backend.transitous import (
    EUROPEAN_COUNTRY_CODES,
    OriginCandidate,
    OriginMetadata,
    OriginMetadataUnavailableError,
)


_GISCO_TO_ORIGIN_COUNTRY_CODE = {
    "EL": "GR",
}


class CoordinateOriginMetadataEnricher:
    """Fill only metadata missing from one selected Transitous identity."""

    def __init__(
        self,
        locality_index: GiscoLauIndex,
        timezone_finder_factory=TimezoneFinder,
    ):
        self.locality_index = locality_index
        self._timezone_finder_factory = timezone_finder_factory

    def __call__(self, candidate: OriginCandidate) -> OriginMetadata:
        country_code = None
        timezone = None

        if candidate.country_code is None:
            country_code = self._country_code_at(candidate)
        if candidate.timezone is None:
            timezone = self._timezone_at(candidate)

        return OriginMetadata(
            country_code=country_code,
            timezone=timezone,
        )

    def _country_code_at(self, candidate: OriginCandidate) -> str:
        resolution = self.locality_index.resolve(
            candidate.lat,
            candidate.lon,
        )
        if (
            resolution.status is not LocalityResolutionStatus.RESOLVED
            or resolution.locality is None
            or len(resolution.candidates) != 1
        ):
            raise OriginMetadataUnavailableError(
                "GISCO did not contain the origin in exactly one locality."
            )

        gisco_country_code = resolution.locality.country_code
        country_code = _GISCO_TO_ORIGIN_COUNTRY_CODE.get(
            gisco_country_code,
            gisco_country_code,
        )
        if country_code not in EUROPEAN_COUNTRY_CODES:
            raise OriginMetadataUnavailableError(
                "GISCO returned an unsupported origin country code."
            )
        return country_code

    def _timezone_at(self, candidate: OriginCandidate) -> str:
        try:
            with self._timezone_finder_factory(
                in_memory=False
            ) as timezone_finder:
                timezone = timezone_finder.timezone_at_land(
                    lng=candidate.lon,
                    lat=candidate.lat,
                )
        except (OSError, RuntimeError, ValueError) as error:
            raise OriginMetadataUnavailableError(
                "Could not determine origin timezone metadata."
            ) from error

        if not isinstance(timezone, str) or not timezone.strip():
            raise OriginMetadataUnavailableError(
                "No land timezone contains the origin coordinate."
            )

        timezone = timezone.strip()
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise OriginMetadataUnavailableError(
                "Origin timezone metadata is not a valid IANA timezone."
            ) from error
        return timezone
