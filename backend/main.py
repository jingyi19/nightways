import os
from datetime import date as calendar_date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.boundaries import (
    AmbiguousBoundaryError,
    BoundaryNotFoundError,
    BoundaryServiceError,
)
from backend.discovery import (
    CandidateStopLimitError,
    get_nightways_for_origin_by_locality,
)
from backend.localities import (
    LocalityDatasetError,
    LocalityDatasetUnavailableError,
    LocalityResolutionError,
)
from backend.transitous import (
    AmbiguousOriginError,
    OriginNotFoundError,
    OriginResolutionError,
    TransitousError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
INDEX_FILE = FRONTEND_ROOT / "index.html"
STYLE_FILE = FRONTEND_ROOT / "style.css"
SCRIPT_FILE = FRONTEND_ROOT / "app.js"
CORS_ORIGINS_ENV = "NIGHTWAYS_CORS_ORIGINS"
CORS_ORIGINS_ERROR = (
    f"{CORS_ORIGINS_ENV} must contain comma-separated HTTP(S) origins "
    "without wildcards, credentials, paths, queries, or fragments."
)


def _normalize_cors_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(CORS_ORIGINS_ERROR) from error

    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or "*" in hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(CORS_ORIGINS_ERROR)

    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"

    is_default_port = (
        parsed.scheme == "http" and port == 80
    ) or (
        parsed.scheme == "https" and port == 443
    )
    port_suffix = "" if port is None or is_default_port else f":{port}"
    return f"{parsed.scheme}://{hostname}{port_suffix}"


def configured_cors_origins(environ=None) -> list[str]:
    """Return the configured exact browser origins in first-seen order."""

    environment = os.environ if environ is None else environ
    configured = environment.get(CORS_ORIGINS_ENV, "")
    origins = []
    seen = set()
    for value in configured.split(","):
        value = value.strip()
        if not value:
            continue

        origin = _normalize_cors_origin(value)
        if origin and origin not in seen:
            origins.append(origin)
            seen.add(origin)
    return origins


def configure_cors(application: FastAPI, environ=None) -> None:
    application.add_middleware(
        CORSMiddleware,
        allow_origins=configured_cors_origins(environ),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=[],
    )


# English names for the countries present in GISCO LAU 2024 and supported by
# Nightways V1. Keep legacy API spellings stable at this public boundary.
GISCO_COUNTRY_NAMES = {
    "AL": "Albania",
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "CH": "Switzerland",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "EL": "Greece",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "HR": "Croatia",
    "HU": "Hungary",
    "IE": "Ireland",
    "IS": "Iceland",
    "IT": "Italy",
    "LI": "Liechtenstein",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MK": "North Macedonia",
    "MT": "Malta",
    "NL": "Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RS": "Serbia",
    "SE": "Sweden",
    "SI": "Slovenia",
    "SK": "Slovakia",
}


app = FastAPI(
    title="Nightways API",
    version="0.1.0"
)

configure_cors(app)


@app.get("/", include_in_schema=False)
def get_frontend():

    return FileResponse(INDEX_FILE)


@app.get("/style.css", include_in_schema=False)
def get_stylesheet():

    return FileResponse(STYLE_FILE)


@app.get("/app.js", include_in_schema=False)
def get_javascript():

    return FileResponse(SCRIPT_FILE)


@app.get("/api/nightways")
def get_nightways(origin: str, date: str):
    try:

        travel_date = calendar_date.fromisoformat(date)

        if travel_date.isoformat() != date:

            raise ValueError

    except ValueError as error:

        raise HTTPException(
            status_code=400,
            detail={
                "message": "Date must use the YYYY-MM-DD format."
            }
        ) from error


    try:
        response = get_nightways_for_origin_by_locality(
            origin,
            travel_date,
        )
        return _public_response(response)

    except OriginNotFoundError as error:
        raise _api_error(error, 400, "origin_not_found") from error

    except AmbiguousOriginError as error:
        raise _api_error(error, 409, "origin_ambiguous") from error

    except OriginResolutionError as error:
        raise _api_error(error, 400, "invalid_origin") from error

    except BoundaryNotFoundError as error:
        raise _api_error(error, 422, "origin_boundary_not_found") from error

    except AmbiguousBoundaryError as error:
        raise _api_error(error, 409, "origin_boundary_ambiguous") from error

    except BoundaryServiceError as error:
        raise _api_error(error, 502, "origin_boundary_service_failed") from error

    except CandidateStopLimitError as error:
        raise _api_error(
            error,
            503,
            "origin_candidate_limit_exceeded",
            candidate_count=error.candidate_count,
            limit=error.limit,
        ) from error

    except LocalityDatasetUnavailableError as error:
        raise _api_error(error, 503, "gisco_dataset_unavailable") from error

    except LocalityDatasetError as error:
        raise _api_error(error, 503, "gisco_dataset_invalid") from error

    except LocalityResolutionError as error:
        raise _api_error(error, 503, "locality_resolution_failed") from error

    except TransitousError as error:
        raise _api_error(error, 502, "transitous_failed") from error


def _api_error(error: Exception, status_code: int, code: str, **details):
    payload = {
        "message": str(error),
        "code": code,
    }
    payload.update(details)
    return HTTPException(status_code=status_code, detail=payload)


def _public_response(response: dict) -> dict:
    """Retain the legacy country field while exposing GISCO metadata."""

    public_response = dict(response)
    public_response["destinations"] = []
    for destination in response.get("destinations", []):
        public_destination = dict(destination)
        country_code = destination.get("country_code")
        try:
            public_destination["country"] = GISCO_COUNTRY_NAMES[country_code]
        except (KeyError, TypeError) as error:
            raise LocalityDatasetError(
                "No English country name is configured for GISCO country "
                f"code {country_code!r}."
            ) from error
        public_response["destinations"].append(public_destination)
    return public_response
