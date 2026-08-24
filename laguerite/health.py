"""Optional tiny health endpoint.

Hosts like Render's Web Service type expect something listening on $PORT. If
PORT (or HEALTH_PORT) is set we serve a small JSON status page; otherwise the
monitor runs as a pure background worker.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


def start_health_server(port: int, snapshot) -> threading.Thread:
    """`snapshot` is a zero-arg callable returning the current MonitorState."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            try:
                body = json.dumps(
                    {"status": "ok", "monitor": asdict(snapshot())}, indent=2
                ).encode()
            except Exception as exc:  # pragma: no cover - defensive
                body = json.dumps({"status": "error", "detail": str(exc)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence per-request stderr spam
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    thread.start()
    logger.info("Health endpoint listening on port %s", port)
    return thread
