from datetime import date as calendar_date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from backend.transitous import TransitousError, get_nightways_for_dresden


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = PROJECT_ROOT / "index.html"
STYLE_FILE = PROJECT_ROOT / "style.css"
SCRIPT_FILE = PROJECT_ROOT / "app.js"
REFERENCE_DATA_FILE = (
    PROJECT_ROOT
    / "data"
    / "nightways_dresden_2026-08-14.json"
)

SUPPORTED_ORIGIN = "Dresden"


app = FastAPI(
    title="Nightways API",
    version="0.1.0"
)


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

    is_supported_origin = (
        origin.strip().casefold()
        == SUPPORTED_ORIGIN.casefold()
    )

    if not is_supported_origin:

        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "This prototype currently supports only "
                    "Dresden as the origin."
                ),
                "supported_origin": SUPPORTED_ORIGIN
            }
        )


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

        return get_nightways_for_dresden(
            travel_date,
            REFERENCE_DATA_FILE
        )

    except TransitousError as error:

        raise HTTPException(
            status_code=502,
            detail={
                "message": str(error)
            }
        ) from error
