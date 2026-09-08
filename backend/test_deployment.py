import asyncio
import unittest
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.main import (
    CORS_ORIGINS_ERROR,
    CORS_ORIGINS_ENV,
    FRONTEND_ROOT,
    INDEX_FILE,
    PROJECT_ROOT,
    SCRIPT_FILE,
    STYLE_FILE,
    app,
    configure_cors,
    configured_cors_origins,
    get_frontend,
    get_javascript,
    get_stylesheet,
)


async def _request(application, method, origin, requested_method=None):
    request_headers = [(b"origin", origin.encode("ascii"))]
    if requested_method:
        request_headers.append(
            (
                b"access-control-request-method",
                requested_method.encode("ascii"),
            )
        )

    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": b"",
            "headers": request_headers,
            "client": ("test-client", 50000),
            "server": ("test-server", 80),
            "root_path": "",
        },
        receive,
        send,
    )

    response_start = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_headers = {}
    for name, value in response_start["headers"]:
        response_headers.setdefault(name.decode("ascii"), []).append(
            value.decode("ascii")
        )
    return response_start["status"], response_headers


class _ApiBaseUrlMetaParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if (
            tag == "meta"
            and attributes.get("name") == "nightways-api-base-url"
        ):
            self.values.append(attributes.get("content"))


class CorsConfigurationTests(unittest.TestCase):

    def test_unset_cors_origins_are_empty(self):
        self.assertEqual(configured_cors_origins({}), [])

    def test_cors_origins_are_trimmed_and_deduplicated(self):
        environ = {
            CORS_ORIGINS_ENV: (
                " HTTPS://Nightways.Pages.Dev/, ,"
                "https://travel.example:443,https://nightways.pages.dev "
            )
        }

        self.assertEqual(
            configured_cors_origins(environ),
            [
                "https://nightways.pages.dev",
                "https://travel.example",
            ],
        )

    def test_invalid_or_wildcard_cors_origins_are_rejected(self):
        invalid_origins = (
            "*",
            "https://*.pages.dev",
            "https://user:secret@example.com",
            "https://example.com/path",
            "https://example.com?query=yes",
            "https://example.com#fragment",
            "ftp://example.com",
            "example.com",
            "https://example.com:not-a-port",
        )

        for origin in invalid_origins:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError) as caught:
                    configured_cors_origins(
                        {CORS_ORIGINS_ENV: origin}
                    )
                self.assertEqual(
                    str(caught.exception),
                    CORS_ORIGINS_ERROR,
                )

    def test_app_uses_exact_get_only_cors_configuration(self):
        middleware = next(
            item
            for item in app.user_middleware
            if item.cls is CORSMiddleware
        )

        self.assertEqual(
            middleware.kwargs["allow_origins"],
            configured_cors_origins(),
        )
        self.assertEqual(middleware.kwargs["allow_methods"], ["GET"])
        self.assertEqual(middleware.kwargs["allow_headers"], [])
        self.assertFalse(middleware.kwargs["allow_credentials"])
        self.assertNotIn("allow_origin_regex", middleware.kwargs)

    def test_cors_headers_allow_only_a_configured_origin(self):
        test_app = FastAPI()

        @test_app.get("/probe")
        def probe():
            return {"ok": True}

        allowed_origin = "https://nightways.pages.dev"
        configure_cors(
            test_app,
            {CORS_ORIGINS_ENV: allowed_origin},
        )

        allowed_status, allowed_headers = asyncio.run(
            _request(test_app, "GET", allowed_origin)
        )
        denied_status, denied_headers = asyncio.run(
            _request(
                test_app,
                "GET",
                "https://not-allowed.example",
            )
        )
        preflight_status, preflight_headers = asyncio.run(
            _request(
                test_app,
                "OPTIONS",
                allowed_origin,
                requested_method="GET",
            )
        )

        self.assertEqual(allowed_status, 200)
        self.assertEqual(
            allowed_headers["access-control-allow-origin"],
            [allowed_origin],
        )
        self.assertEqual(denied_status, 200)
        self.assertNotIn(
            "access-control-allow-origin",
            denied_headers,
        )
        self.assertEqual(preflight_status, 200)
        self.assertEqual(
            preflight_headers["access-control-allow-origin"],
            [allowed_origin],
        )
        self.assertEqual(
            preflight_headers["access-control-allow-methods"],
            ["GET"],
        )


class FrontendFileServingTests(unittest.TestCase):

    def test_frontend_files_are_scoped_to_frontend_directory(self):
        self.assertEqual(FRONTEND_ROOT, PROJECT_ROOT / "frontend")
        self.assertEqual(INDEX_FILE, FRONTEND_ROOT / "index.html")
        self.assertEqual(STYLE_FILE, FRONTEND_ROOT / "style.css")
        self.assertEqual(SCRIPT_FILE, FRONTEND_ROOT / "app.js")

    def test_static_routes_return_the_configured_frontend_files(self):
        responses = (
            (get_frontend(), INDEX_FILE),
            (get_stylesheet(), STYLE_FILE),
            (get_javascript(), SCRIPT_FILE),
        )

        for response, expected_path in responses:
            with self.subTest(path=expected_path):
                self.assertIsInstance(response, FileResponse)
                self.assertEqual(Path(response.path), expected_path)
                self.assertTrue(expected_path.is_file())

    def test_static_frontend_targets_the_render_backend(self):
        parser = _ApiBaseUrlMetaParser()
        parser.feed(INDEX_FILE.read_text(encoding="utf-8"))

        self.assertEqual(
            parser.values,
            ["https://nightways.onrender.com"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
