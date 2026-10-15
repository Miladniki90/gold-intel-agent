"""HTTP bridge between a containerised workflow tool (e.g. n8n in Docker) and the
`claude -p` CLI that runs on the host machine.

    POST /interpret   {"prompt": "..."}   ->   parsed JSON (or {"raw_output": ...})

Security model
--------------
* Binds to 127.0.0.1 by default.
* If you bind to a non-loopback address (BRIDGE_HOST=0.0.0.0, needed on some
  Docker setups) you MUST also set BRIDGE_TOKEN; the bridge refuses to start
  otherwise. Clients then send `Authorization: Bearer <token>`.
* Request bodies are capped (256 KB), the CLI is invoked without a shell, and
  internal error details are logged, not returned to the caller.

Configuration (environment variables)
-------------------------------------
BRIDGE_HOST (127.0.0.1), BRIDGE_PORT (8787), BRIDGE_TOKEN (unset),
CLAUDE_TIMEOUT_SECONDS (120)
"""
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 256 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

log = logging.getLogger("bridge")


def run_claude(prompt: str, timeout: int = None) -> str:
    """Pipe the prompt to `claude -p` and return stdout. No shell is involved."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    timeout = timeout or int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "120"))
    result = subprocess.run(
        [exe, "-p"], input=prompt, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout,
    )
    if result.returncode != 0:
        log.error("claude exited with %s: %s", result.returncode, result.stderr[:500])
        raise RuntimeError("claude CLI failed")
    return result.stdout.strip()


def extract_json(text: str):
    """Parse model output as JSON, tolerating a ```json fenced block. None if not JSON."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def make_handler(token=None, runner=run_claude):
    """Build a request handler class. `runner` is injectable so tests need no CLI."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not token:
                return True
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def _read_body(self, length: int):
            """Consume the request body (bounded). Returns bytes, or None if it is too large.

            The body is always read *before* any response is sent. Replying while the client
            is still uploading makes some clients (notably on Windows) see a reset connection
            instead of the 401/404/413 we meant to send.
            """
            if length <= MAX_BODY_BYTES:
                return self.rfile.read(length) if length else b""
            remaining = min(length, MAX_BODY_BYTES * 4)  # discard a bounded amount, then give up
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return None

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return self._send(400, {"error": "invalid Content-Length"})
            if length < 0:
                return self._send(400, {"error": "invalid Content-Length"})
            raw_body = self._read_body(length)

            if self.path != "/interpret":
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if raw_body is None:
                return self._send(413, {"error": f"body exceeds {MAX_BODY_BYTES} bytes"})

            try:
                data = json.loads(raw_body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send(400, {"error": "invalid JSON body"})

            prompt = data.get("prompt") if isinstance(data, dict) else None
            if not isinstance(prompt, str) or not prompt.strip():
                return self._send(400, {"error": "missing 'prompt' string"})

            try:
                raw_output = runner(prompt)
            except subprocess.TimeoutExpired:
                return self._send(504, {"error": "claude timed out"})
            except RuntimeError as exc:
                log.error("runner failed: %s", exc)
                return self._send(502, {"error": "upstream CLI failed"})
            except Exception:
                log.exception("unexpected error")
                return self._send(500, {"error": "internal error"})

            parsed = extract_json(raw_output)
            if parsed is not None:
                self._send(200, parsed)
            else:
                self._send(200, {"raw_output": raw_output, "warning": "output was not valid JSON"})

        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "8787"))
    token = os.environ.get("BRIDGE_TOKEN") or None
    if host not in LOOPBACK_HOSTS and not token:
        sys.exit("Refusing to listen on a non-loopback address without BRIDGE_TOKEN set.")
    server = ThreadingHTTPServer((host, port), make_handler(token))
    log.info("bridge listening on http://%s:%s (auth %s)", host, port, "on" if token else "off")
    server.serve_forever()


if __name__ == "__main__":
    main()
