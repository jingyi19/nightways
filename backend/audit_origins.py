"""Audit representative European origins through the public Nightways API."""

import argparse
import csv
import json
import math
import socket
import time
from collections import Counter
from datetime import date as calendar_date
from http.client import HTTPException as HttpClientError
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_ORIGINS = (
    # Germany
    "Berlin",
    "Hamburg",
    "Munich",
    "Frankfurt",
    "Cologne",
    "Düsseldorf",
    "Stuttgart",
    "Dresden",
    "Leipzig",
    "Nuremberg",
    # Austria
    "Vienna",
    "Salzburg",
    "Innsbruck",
    "Graz",
    "Linz",
    # Switzerland
    "Zurich",
    "Basel",
    "Bern",
    "Geneva",
    # Czechia
    "Prague",
    "Brno",
    "Ostrava",
    # Poland
    "Warsaw",
    "Krakow",
    "Wroclaw",
    "Poznan",
    "Gdansk",
    "Katowice",
    # Slovakia
    "Bratislava",
    "Kosice",
    # Hungary
    "Budapest",
    # Slovenia
    "Ljubljana",
    # Croatia
    "Zagreb",
    # Italy
    "Milan",
    "Rome",
    "Venice",
    "Bologna",
    "Florence",
    "Turin",
    "Verona",
    # France
    "Paris",
    "Strasbourg",
    "Lyon",
    "Marseille",
    "Nice",
    # Belgium
    "Brussels",
    "Antwerp",
    "Liege",
    # Netherlands
    "Amsterdam",
    "Rotterdam",
    "Utrecht",
    "The Hague",
    # Denmark
    "Copenhagen",
    "Aarhus",
    # Sweden
    "Stockholm",
    "Gothenburg",
    "Malmo",
    # Norway
    "Oslo",
    "Bergen",
    "Trondheim",
    # Finland
    "Helsinki",
    "Tampere",
    "Turku",
    # Baltics
    "Tallinn",
    "Riga",
    "Vilnius",
    # Romania
    "Bucharest",
    "Cluj-Napoca",
    # Bulgaria
    "Sofia",
    # Spain
    "Madrid",
    "Barcelona",
    # Portugal
    "Lisbon",
    "Porto",
)

CSV_FIELDS = (
    "input_origin",
    "status",
    "http_status",
    "resolved_city",
    "resolved_country",
    "destination_count",
    "error_code",
    "error_message",
    "elapsed_seconds",
)

SUCCESS_STATUSES = {"success", "success_empty"}
DEFAULT_TIMEOUT_SECONDS = 120.0


class InvalidResponseError(ValueError):
    """Raised when a successful HTTP response is not a Nightways payload."""


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Audit representative European origins through an existing "
            "Nightways API deployment."
        )
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Nightways site URL (default: %(default)s)",
    )
    parser.add_argument(
        "--date",
        required=True,
        type=valid_date,
        help="Travel date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("origin_audit.csv"),
        help="CSV output path (default: %(default)s)",
    )
    parser.add_argument(
        "--delay",
        type=nonnegative_float,
        default=1.0,
        help="Seconds to wait between requests (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-request timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--city",
        dest="cities",
        action="append",
        metavar="CITY",
        help=(
            "Audit only this city; repeat the option for multiple cities. "
            "By default the built-in representative city set is used."
        ),
    )
    args = parser.parse_args()
    args.base_url = valid_base_url(args.base_url, parser)
    if args.cities:
        args.cities = [city.strip() for city in args.cities if city.strip()]
        if not args.cities:
            parser.error("at least one --city value must be non-empty")
    else:
        args.cities = list(DEFAULT_ORIGINS)
    return args


def valid_date(value):
    try:
        parsed = calendar_date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "date must use the YYYY-MM-DD format"
        ) from error
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError(
            "date must use the YYYY-MM-DD format"
        )
    return value


def nonnegative_float(value):
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a number") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            "value must be a finite number that is zero or greater"
        )
    return parsed


def positive_float(value):
    parsed = nonnegative_float(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def valid_base_url(value, parser):
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        parser.error(
            "--base-url must be an HTTP(S) URL without a query or fragment"
        )
    return normalized


def audit_origin(base_url, travel_date, origin, timeout):
    query = urlencode({"origin": origin, "date": travel_date})
    url = f"{base_url}/api/nightways?{query}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Nightways origin coverage audit",
        },
    )
    started = time.perf_counter()

    try:
        with urlopen(request, timeout=timeout) as response:
            http_status = response.getcode()
            payload = read_json_object(response)
        result = success_result(origin, http_status, payload)
    except HTTPError as error:
        result = api_error_result(origin, error)
    except (socket.timeout, TimeoutError) as error:
        result = failure_result(origin, "timeout", error_message=str(error))
    except URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            result = failure_result(
                origin,
                "timeout",
                error_message=str(error.reason),
            )
        else:
            result = failure_result(
                origin,
                "network_error",
                error_message=str(error.reason),
            )
    except InvalidResponseError as error:
        result = failure_result(
            origin,
            "invalid_response",
            error_message=str(error),
        )
    except (HttpClientError, OSError) as error:
        result = failure_result(
            origin,
            "network_error",
            error_message=str(error),
        )

    result["elapsed_seconds"] = f"{time.perf_counter() - started:.3f}"
    return result


def read_json_object(response):
    try:
        body = response.read().decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidResponseError("response body is not valid UTF-8") from error
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise InvalidResponseError("response body is not valid JSON") from error
    if not isinstance(payload, dict):
        raise InvalidResponseError("response JSON is not an object")
    return payload


def success_result(origin, http_status, payload):
    destination_count = payload.get("destination_count")
    if (
        isinstance(destination_count, bool)
        or not isinstance(destination_count, int)
        or destination_count < 0
    ):
        raise InvalidResponseError(
            "successful response has no valid destination_count"
        )

    resolved_city = payload.get("origin", "")
    if not isinstance(resolved_city, str):
        raise InvalidResponseError("successful response has no valid origin")

    result = empty_result(origin)
    result.update(
        {
            "status": "success" if destination_count else "success_empty",
            "http_status": http_status,
            "resolved_city": resolved_city,
            # The current public API does not expose an origin country.
            "resolved_country": "",
            "destination_count": destination_count,
        }
    )
    return result


def api_error_result(origin, error):
    result = empty_result(origin)
    result["status"] = "api_error"
    result["http_status"] = error.code

    try:
        payload = read_json_object(error)
    except (InvalidResponseError, HttpClientError, OSError):
        return result
    finally:
        error.close()

    detail = payload.get("detail")
    if isinstance(detail, dict):
        if isinstance(detail.get("code"), str):
            result["error_code"] = detail["code"]
        if isinstance(detail.get("message"), str):
            result["error_message"] = detail["message"]
    elif isinstance(detail, str):
        result["error_message"] = detail
    return result


def failure_result(origin, status, error_message=""):
    result = empty_result(origin)
    result["status"] = status
    result["error_message"] = error_message
    return result


def empty_result(origin):
    return {field: "" for field in CSV_FIELDS} | {"input_origin": origin}


def print_summary(results):
    successful = [result for result in results if result["status"] == "success"]
    successful_empty = [
        result for result in results if result["status"] == "success_empty"
    ]
    failures = [
        result for result in results if result["status"] not in SUCCESS_STATUSES
    ]

    print("\nAudit summary")
    print(f"  Total origins: {len(results)}")
    print(f"  Successful origins: {len(successful)}")
    print(f"  Successful with zero destinations: {len(successful_empty)}")
    print(f"  Failed origins: {len(failures)}")

    error_codes = Counter(
        result["error_code"] or f"({result['status']}; no API error code)"
        for result in failures
    )
    print("  Failures by error code:")
    if error_codes:
        for code, count in sorted(error_codes.items()):
            print(f"    {code}: {count}")
    else:
        print("    none")

    slowest = sorted(
        successful + successful_empty,
        key=lambda result: float(result["elapsed_seconds"]),
        reverse=True,
    )[:5]
    print("  Slowest successful requests:")
    if slowest:
        for result in slowest:
            print(
                f"    {result['input_origin']}: "
                f"{result['elapsed_seconds']}s "
                f"({result['destination_count']} destinations)"
            )
    else:
        print("    none")


def main():
    args = parse_args()
    results = []

    try:
        output_file = args.output.open("w", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise SystemExit(f"Could not open {args.output}: {error}") from error

    with output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for index, origin in enumerate(args.cities, start=1):
            result = audit_origin(
                args.base_url,
                args.date,
                origin,
                args.timeout,
            )
            results.append(result)
            writer.writerow(result)
            output_file.flush()
            print(
                f"[{index}/{len(args.cities)}] {origin}: "
                f"{result['status']} ({result['elapsed_seconds']}s)"
            )

            if index < len(args.cities) and args.delay:
                time.sleep(args.delay)

    print_summary(results)
    print(f"\nCSV written to: {args.output.resolve()}")


if __name__ == "__main__":
    main()
