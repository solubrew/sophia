"""Security-headers middleware tests (2026-09-11, web_security_headers fix).

Mirrors ``shieldface/tests/unit/shieldface/test_server.py::TestSendJsonSecurityHeaders``
so the SB stack has one testing pattern for env-gated response
headers. Default OFF for LAN/JSON-only deployment; operators opt in
per-header via ``SOPHIA_SECURITY_HEADER_<NAME>=1``.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sophia.security_headers import SecurityHeadersMiddleware


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _build_app() -> FastAPI:
    """A tiny app with the middleware + a single /probe endpoint."""
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/probe")
    def probe():
        return {"ok": True}

    return app


class TestSecurityHeaders(unittest.TestCase):
    def setUp(self) -> None:
        # Clear all SOPHIA_SECURITY_HEADER_* vars before each test.
        for k in list(os.environ):
            if k.startswith("SOPHIA_SECURITY_HEADER_"):
                del os.environ[k]

    def tearDown(self) -> None:
        for k in list(os.environ):
            if k.startswith("SOPHIA_SECURITY_HEADER_"):
                del os.environ[k]

    def test_default_no_headers(self):
        """All flags unset → no security headers attached (LAN default)."""
        client = _client(_build_app())
        r = client.get("/probe")
        self.assertEqual(r.status_code, 200)
        for name in (
            "Content-Security-Policy", "X-Frame-Options",
            "Strict-Transport-Security", "X-Content-Type-Options",
            "Referrer-Policy",
        ):
            self.assertNotIn(name, r.headers)

    def test_per_header_opt_in(self):
        """Set one flag at a time → exactly that header is attached."""
        client = _client(_build_app())
        os.environ["SOPHIA_SECURITY_HEADER_X-Frame-Options"] = "1"
        # Middleware reads env at construction — rebuild.
        app = _build_app()
        client = _client(app)
        r = client.get("/probe")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        # The other 4 must still be absent.
        for name in (
            "Content-Security-Policy", "Strict-Transport-Security",
            "X-Content-Type-Options", "Referrer-Policy",
        ):
            self.assertNotIn(name, r.headers)

    def test_truthy_spellings_accepted(self):
        """1 / true / TRUE / yes / YES all enable the header."""
        for spelling in ("1", "true", "TRUE", "yes", "YES"):
            for k in list(os.environ):
                if k.startswith("SOPHIA_SECURITY_HEADER_"):
                    del os.environ[k]
            os.environ["SOPHIA_SECURITY_HEADER_X-Content-Type-Options"] = spelling
            client = _client(_build_app())
            r = client.get("/probe")
            self.assertEqual(
                r.headers.get("X-Content-Type-Options"), "nosniff",
                f"spelling {spelling!r} did not enable the header",
            )

    def test_unset_value_disables(self):
        """Empty string is NOT truthy → header stays off."""
        os.environ["SOPHIA_SECURITY_HEADER_X-Frame-Options"] = ""
        client = _client(_build_app())
        r = client.get("/probe")
        self.assertNotIn("X-Frame-Options", r.headers)

    def test_all_headers_attached_when_all_on(self):
        """All five flags set → all five headers in the response."""
        os.environ.update({
            "SOPHIA_SECURITY_HEADER_Content-Security-Policy": "1",
            "SOPHIA_SECURITY_HEADER_X-Frame-Options": "1",
            "SOPHIA_SECURITY_HEADER_Strict-Transport-Security": "1",
            "SOPHIA_SECURITY_HEADER_X-Content-Type-Options": "1",
            "SOPHIA_SECURITY_HEADER_Referrer-Policy": "1",
        })
        client = _client(_build_app())
        r = client.get("/probe")
        self.assertEqual(
            r.headers.get("Content-Security-Policy"),
            "default-src 'none'; frame-ancestors 'none'",
        )
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(
            r.headers.get("Strict-Transport-Security"),
            "max-age=31536000; includeSubDomains",
        )
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("Referrer-Policy"), "no-referrer")


if __name__ == "__main__":
    unittest.main()
