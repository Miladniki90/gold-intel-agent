import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for sub in ("bridge", "dashboard", "scripts"):
    sys.path.insert(0, str(ROOT / sub))

import claude_bridge  # noqa: E402
import fetch_fundamentals as ff  # noqa: E402
import server  # noqa: E402


logging.disable(logging.CRITICAL)  # keep expected error-path logs out of test output


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    stop_serving = httpd.shutdown

    def stop():  # also release the listening socket so tests leave no ResourceWarning
        stop_serving()
        httpd.server_close()

    httpd.shutdown = stop
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def request(url, method="GET", body=None, headers=None):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class TestExtractJson(unittest.TestCase):
    def test_plain_fenced_and_invalid(self):
        self.assertEqual(claude_bridge.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(claude_bridge.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(claude_bridge.extract_json('```\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(claude_bridge.extract_json("not json at all"))


class TestBridge(unittest.TestCase):
    def start(self, token=None, runner=None):
        httpd, base = serve(claude_bridge.make_handler(token, runner or (lambda p: '{"echo": "%s"}' % p)))
        self.addCleanup(httpd.shutdown)
        return base

    def test_happy_path_and_non_json_output(self):
        base = self.start()
        status, body = request(base + "/interpret", "POST", {"prompt": "hi"})
        self.assertEqual((status, json.loads(body)), (200, {"echo": "hi"}))
        base2 = self.start(runner=lambda p: "plain text")
        status, body = request(base2 + "/interpret", "POST", {"prompt": "x"})
        self.assertEqual(json.loads(body)["raw_output"], "plain text")

    def test_auth_required_when_token_set(self):
        base = self.start(token="s3cret")
        self.assertEqual(request(base + "/interpret", "POST", {"prompt": "x"})[0], 401)
        self.assertEqual(request(base + "/interpret", "POST", {"prompt": "x"},
                                 {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(request(base + "/interpret", "POST", {"prompt": "x"},
                                 {"Authorization": "Bearer s3cret"})[0], 200)

    def test_input_validation(self):
        base = self.start()
        self.assertEqual(request(base + "/interpret", "POST", b"{bad")[0], 400)
        self.assertEqual(request(base + "/interpret", "POST", {"prompt": ""})[0], 400)
        self.assertEqual(request(base + "/interpret", "POST", {"prompt": 123})[0], 400)
        self.assertEqual(request(base + "/interpret", "POST", [1, 2])[0], 400)
        self.assertEqual(request(base + "/nope", "POST", {"prompt": "x"})[0], 404)
        big = json.dumps({"prompt": "x" * (claude_bridge.MAX_BODY_BYTES + 10)}).encode()
        self.assertEqual(request(base + "/interpret", "POST", big)[0], 413)

    def test_errors_do_not_leak_details(self):
        def boom(_):
            raise RuntimeError("secret internal path C:\\Users\\x")
        status, body = request(self.start(runner=boom) + "/interpret", "POST", {"prompt": "x"})
        self.assertEqual(status, 502)
        self.assertNotIn("secret", body)

        def timeout(_):
            raise subprocess.TimeoutExpired("claude", 1)
        self.assertEqual(request(self.start(runner=timeout) + "/interpret", "POST", {"prompt": "x"})[0], 504)

        def crash(_):
            raise ValueError("oops")
        status, body = request(self.start(runner=crash) + "/interpret", "POST", {"prompt": "x"})
        self.assertEqual(status, 500)
        self.assertNotIn("oops", body)

    def test_refuses_public_bind_without_token(self):
        with mock.patch.dict(os.environ, {"BRIDGE_HOST": "0.0.0.0"}, clear=False):
            os.environ.pop("BRIDGE_TOKEN", None)
            with self.assertRaises(SystemExit):
                claude_bridge.main()


class TestDashboardServer(unittest.TestCase):
    def setUp(self):
        self.logs = Path(tempfile.mkdtemp())

    def start(self):
        httpd, base = serve(server.make_handler(self.logs, today=lambda: "2026-01-01"))
        self.addCleanup(httpd.shutdown)
        return base

    def test_demo_fallback_when_no_logs(self):
        status, body = request(self.start() + "/api/today")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["demo"])
        self.assertEqual(data["synthesis"]["unit"], "synthesis")

    def test_real_logs_take_priority(self):
        (self.logs / "synthesis").mkdir()
        (self.logs / "synthesis" / "2026-01-01.json").write_text('{"unit": "synthesis", "bias": "bullish"}')
        data = json.loads(request(self.start() + "/api/today")[1])
        self.assertFalse(data["demo"])
        self.assertEqual(data["synthesis"]["bias"], "bullish")
        self.assertIsNone(data["news"])

    def test_corrupt_file_reported_not_crashing(self):
        (self.logs / "news").mkdir()
        (self.logs / "news" / "2026-01-01.json").write_text("{broken")
        data = json.loads(request(self.start() + "/api/today")[1])
        self.assertIn("error", data["news"])

    def test_pages_and_404(self):
        base = self.start()
        status, html = request(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("GOLD DAILY INTELLIGENCE", html)
        self.assertEqual(request(base + "/etc/passwd")[0], 404)
        self.assertEqual(request(base + "/../README.md")[0], 404)

    def test_example_files_follow_the_contract(self):
        for unit in server.UNITS:
            sample = json.loads((server.EXAMPLES_DIR / unit / "sample.json").read_text(encoding="utf-8"))
            for key in ("unit", "timestamp", "bias", "impact", "confidence", "reasoning", "raw_events"):
                self.assertIn(key, sample, f"{unit} sample missing {key}")
            self.assertIn(sample["bias"], ("bullish", "bearish", "neutral"))
            self.assertTrue(0 <= sample["confidence"] <= 1)


class TestFundamentals(unittest.TestCase):
    def test_summarize_changes(self):
        out = ff.summarize([{"date": "2026-02", "value": "110"}, {"date": "2026-01", "value": "100"}])
        self.assertEqual(out["change"], 10.0)
        self.assertEqual(out["change_pct"], 10.0)

    def test_summarize_skips_missing_values_and_handles_empty(self):
        out = ff.summarize([{"date": "d3", "value": "."}, {"date": "d2", "value": "5"}, {"date": "d1", "value": "4"}])
        self.assertEqual(out["latest_date"], "d2")
        self.assertEqual(ff.summarize([])["latest"], None)
        self.assertIsNone(ff.summarize([{"date": "d2", "value": "5"}, {"date": "d1", "value": "0"}])["change_pct"])

    def test_missing_api_key_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                ff.get_api_key()
        self.assertIn("FRED_API_KEY", str(ctx.exception))

    def test_fetch_series_builds_url_without_hardcoded_key(self):
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"observations": [{"date": "d", "value": "1"}]}).encode()

        def fake_urlopen(url, timeout=0):
            captured["url"] = url
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            ff.fetch_series("DFII10", api_key="TESTKEY")
        self.assertIn("series_id=DFII10", captured["url"])
        self.assertIn("api_key=TESTKEY", captured["url"])

    def test_cot_network_failure_returns_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            self.assertIn("error", ff.fetch_cot())

    def test_repo_is_english_only(self):
        # Code-point ranges: Hebrew/Arabic/Persian/Syriac/Thaana blocks and Arabic presentation forms.
        blocks = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))
        banned_words = re.compile("|".join(["rt" + "l", "fa" + "-ir", "fa" + "_ir", "pers" + "ian", "far" + "si", "teh" + "ran"]))
        for path in ROOT.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore")
                offenders = {c for c in text if any(lo <= ord(c) <= hi for lo, hi in blocks)}
                self.assertFalse(offenders, f"non-English script found in {path}")
                if path.name != "test_all.py":
                    self.assertIsNone(banned_words.search(text.lower()), f"language-specific leftovers in {path}")

    def test_repo_contains_no_hardcoded_secrets(self):
        import re
        pattern = re.compile(r"""(api[_-]?key|secret|token|password)\s*[=:]\s*["'][A-Za-z0-9]{20,}["']""", re.I)
        for path in ROOT.rglob("*"):
            if path.suffix in (".py", ".md", ".html", ".json", ".yml", ".txt") and ".git" not in path.parts \
                    and path.name != "test_all.py":
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8", errors="ignore")), str(path))


if __name__ == "__main__":
    unittest.main()
