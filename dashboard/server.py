"""Local dashboard server.

Serves dashboard/index.html and one JSON endpoint, GET /api/today, which merges
today's output of the four units (calendar / fundamental / news / synthesis)
from the logs directory into a single response.

If today's files are missing, the endpoint falls back to the synthetic files in
examples/logs/ and sets "demo": true so the UI shows a banner. That makes
`python dashboard/server.py` useful on a fresh clone.

Usage:
    python dashboard/server.py        # then open http://127.0.0.1:8790

Environment: DASHBOARD_HOST (127.0.0.1), DASHBOARD_PORT (8790), GOLD_LOGS_DIR (./logs)
"""
import datetime
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UNITS = ["calendar", "fundamental", "news", "synthesis"]
DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
EXAMPLES_DIR = PROJECT_ROOT / "examples" / "logs"


def today_str():
    return datetime.date.today().isoformat()


def load_unit(logs_dir: Path, unit: str, name: str):
    """Read <logs_dir>/<unit>/<name>.json; None if missing, {"error": ...} if unreadable."""
    path = Path(logs_dir) / unit / f"{name}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


def build_today(logs_dir: Path, examples_dir: Path, date_str: str) -> dict:
    data = {"date": date_str, "demo": False}
    for unit in UNITS:
        data[unit] = load_unit(logs_dir, unit, date_str)
    if all(data[u] is None for u in UNITS):
        data = {"date": date_str, "demo": True}
        for unit in UNITS:
            data[unit] = load_unit(examples_dir, unit, "sample")
    return data


def make_handler(logs_dir: Path, examples_dir: Path = EXAMPLES_DIR, today=today_str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/today":
                payload = build_today(logs_dir, examples_dir, today())
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                return self._send(200, body, "application/json; charset=utf-8")
            if path in ("/", "/index.html"):
                try:
                    body = (DASHBOARD_DIR / "index.html").read_bytes()
                except FileNotFoundError:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, body, "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def log_message(self, fmt, *args):
            print("[dashboard]", self.address_string(), "-", fmt % args)

    return Handler


def main():
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8790"))
    logs_dir = Path(os.environ.get("GOLD_LOGS_DIR", PROJECT_ROOT / "logs"))
    server = ThreadingHTTPServer((host, port), make_handler(logs_dir))
    print(f"Dashboard running at http://{host}:{port}  (logs: {logs_dir})")
    server.serve_forever()


if __name__ == "__main__":
    main()
