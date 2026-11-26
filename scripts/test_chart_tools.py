"""Verify download diagnostics and checksum validation without network access."""

import hashlib
from http.client import IncompleteRead
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

SPEC = importlib.util.spec_from_file_location(
    "chart_tools", Path(__file__).with_name("chart-tools.py")
)
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)

URL = "https://get.helm.sh/helm-v4.3.0-linux-amd64.tar.gz"


class ChartToolsTest(unittest.TestCase):
    def test_fetch_error_includes_requested_url_and_original_error(self):
        errors = (
            HTTPError(
                "https://redirect.example/asset", 504, "Gateway Time-out", {}, None
            ),
            URLError("connection refused"),
            TimeoutError("timed out"),
        )
        for error in errors:
            with (
                self.subTest(error=error),
                patch.object(TOOLS, "urlopen", side_effect=error),
                self.assertRaises(RuntimeError) as raised,
            ):
                TOOLS.fetch(URL, "unused")
            self.assertIn(URL, str(raised.exception))
            self.assertIn(str(error), str(raised.exception))
            self.assertIs(raised.exception.__cause__, error)

    def test_fetch_read_error_includes_requested_url(self):
        error = IncompleteRead(b"partial", 100)
        with patch.object(TOOLS, "urlopen") as request:
            request.return_value.__enter__.return_value.read.side_effect = error
            with self.assertRaises(RuntimeError) as raised:
                TOOLS.fetch(URL, "unused")
        self.assertIn(URL, str(raised.exception))
        self.assertIs(raised.exception.__cause__, error)

    def test_fetch_preserves_checksum_validation(self):
        data = b"downloaded archive"
        with patch.object(TOOLS, "urlopen", return_value=io.BytesIO(data)):
            self.assertEqual(TOOLS.fetch(URL, hashlib.sha256(data).hexdigest()), data)
        with (
            patch.object(TOOLS, "urlopen", return_value=io.BytesIO(data)),
            self.assertRaisesRegex(RuntimeError, "checksum mismatch"),
        ):
            TOOLS.fetch(URL, "wrong checksum")

    def test_apparmor_amd64_retries_forbidden_download_with_same_checksum(self):
        path = (
            f"/ubuntu/pool/main/a/apparmor/apparmor_{TOOLS.APPARMOR_VERSION}_amd64.deb"
        )
        primary = f"https://archive.ubuntu.com{path}"
        mirror = f"https://security.ubuntu.com{path}"
        forbidden = HTTPError(primary, 403, "Forbidden", {}, None)
        data = b"pinned apparmor archive"
        digest = hashlib.sha256(data).hexdigest()
        with (
            patch.object(
                TOOLS, "urlopen", side_effect=[forbidden, io.BytesIO(data)]
            ) as request,
            patch("builtins.print"),
        ):
            self.assertEqual(TOOLS.fetch_apparmor("amd64", digest), data)
            self.assertEqual(
                request.call_args_list,
                [call(primary, timeout=60), call(mirror, timeout=60)],
            )
        with (
            patch.object(TOOLS, "urlopen", side_effect=[forbidden, io.BytesIO(data)]),
            patch("builtins.print"),
            self.assertRaisesRegex(RuntimeError, "checksum mismatch"),
        ):
            TOOLS.fetch_apparmor("amd64", "wrong checksum")
