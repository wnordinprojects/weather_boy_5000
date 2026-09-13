"""Dashboard: JSON API + a self-contained polling page. Served on $PORT, no dependencies."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import config
from .api import Api

PAGE = open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8").read()


def serve(agent):
    api = Api(agent)
    routes = {
        "/api/summary": api.summary,
        "/api/positions": api.positions,
        "/api/cities": api.cities,
        "/api/scorecard": api.scorecard,
        "/api/activity": api.activity,
    }

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ct, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path in routes:
                    self._send(json.dumps(routes[path](), default=str).encode(), "application/json")
                elif path == "/logs":
                    from .agent import RING
                    self._send("\n".join(RING.lines).encode(), "text/plain; charset=utf-8")
                elif path == "/status.json":
                    self._send(json.dumps(dict(agent.last_status, threshold=agent.threshold,
                                               halted=agent.halted)).encode(), "application/json")
                else:
                    self._send(PAGE.encode(), "text/html; charset=utf-8")
            except Exception as e:  # never let the dashboard take the agent down
                self._send(json.dumps(dict(error=str(e))).encode(), "application/json", 500)

    ThreadingHTTPServer(("0.0.0.0", config.PORT), H).serve_forever()
