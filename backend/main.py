import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = PROJECT_ROOT / "index.html"
STYLE_FILE = PROJECT_ROOT / "style.css"
SCRIPT_FILE = PROJECT_ROOT / "app.js"
DATA_FILE = (
    PROJECT_ROOT
    / "data"
    / "nightways_dresden_2026-08-14.json"
)

SUPPORTED_ORIGIN = "Dresden"
SUPPORTED_DATE = "2026-08-14"


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

    is_supported_date = date == SUPPORTED_DATE


    if not is_supported_origin or not is_supported_date:

        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "This prototype currently supports only "
                    "Dresden on 2026-08-14."
                ),
                "supported_origin": SUPPORTED_ORIGIN,
                "supported_date": SUPPORTED_DATE
            }
        )


    with DATA_FILE.open(
        "r",
        encoding="utf-8"
    ) as data_file:

        return json.load(data_file)
