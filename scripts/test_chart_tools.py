"""Verify download diagnostics and checksum validation without network access."""

import hashlib
from http.client import IncompleteRead
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
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
