"""Mock of the opencode free-tier gateway, for offline testing.

Reproduces the behaviours the retry proxy has to survive, without touching
the real gateway, the running Clash Verge, or any live network:

    ok_sse      200 + SSE stream that ends with a finish_reason
    truncated   200 + SSE stream that stops before finish_reason
    empty       200 with an empty body (accepted, then dropped)
    ip_rep      403 FreeTierError: ... detected abusive traffic ...
    identity    403 FreeTierError: free tier can only be used from within OpenCode
    rate        429
    server      503
    slow        200 after a delay (timeout / patience tests)

The identity gate is enforced the way the real gateway enforces it, so the
tests also document those rules:

    User-Agent: opencode/<channel>/<version>/<client>   (three segments)
    x-opencode-client: cli
    body must contain "stream": true
    an x-opencode-session header of the form ses_...

Control plane (not part of the proxied surface):

    POST /__plan   {"responses": ["ip_rep", "ok_sse"], "default": "ok_sse"}
    GET  /__stats  {"counts": {...}, "plan": [...]}
    POST /__reset
"""
import json
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "plan": [],
    "default": "ok_sse",
    "counts": Counter(),
    "delay": 0.0,
}
LOCK = threading.Lock()

UA_RE = re.compile(r"^opencode/[^/]+/[^/]+/[^/]+$")

SSE_OK = (
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"hello"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)

SSE_TRUNCATED = (
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"half"},"finish_reason":null}]}\n\n'
)

BODY_IP_REP = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "FreeTierError",
            "message": "free tier detected abusive traffic from this IP address, please retry later",
        },
    }
).encode()

BODY_IDENTITY = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "FreeTierError",
            "message": "free tier can only be used from within OpenCode",
        },
    }
).encode()

BODY_RATE = json.dumps({"type": "error", "error": {"type": "RateLimitError", "message": "slow down"}}).encode()
BODY_SERVER = json.dumps({"type": "error", "error": {"type": "ServerError", "message": "upstream busy"}}).encode()


def next_behavior():
    with LOCK:
        if STATE["plan"]:
            return STATE["plan"].pop(0)
        return STATE["default"]


def count(name):
    with LOCK:
        STATE["counts"][name] += 1


def gate_ok(handler):
    """The request-shape gate the real gateway applies."""
    ua = handler.headers.get("User-Agent", "")
    if not UA_RE.match(ua):
        return False, "bad user-agent"
    if handler.headers.get("x-opencode-client") != "cli":
        return False, "missing x-opencode-client"
    if not any(k.lower().startswith("x-opencode-session") for k in handler.headers.keys()):
        return False, "missing x-opencode-session"
    return True, ""


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _raw(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/__stats"):
            with LOCK:
                self._json({"counts": dict(STATE["counts"]), "plan": list(STATE["plan"]), "default": STATE["default"]})
            return
        self._chat()

    def do_POST(self):
        if self.path.startswith("/__plan"):
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            with LOCK:
                STATE["plan"] = list(data.get("responses", []))
                STATE["default"] = data.get("default", STATE["default"])
                if "delay" in data:
                    STATE["delay"] = float(data["delay"])
                STATE["counts"] = Counter()
            self._json({"ok": True, "plan": STATE["plan"]})
            return
        if self.path.startswith("/__reset"):
            with LOCK:
                STATE["plan"] = []
                STATE["counts"] = Counter()
                STATE["delay"] = 0.0
            self._json({"ok": True})
            return
        self._chat()

    def _chat(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {}

        ok, why = gate_ok(self)
        if not ok:
            count("gate_reject")
            self._raw(403, BODY_IDENTITY)
            return
        if not parsed.get("stream"):
            count("gate_reject")
            self._raw(403, BODY_IDENTITY)
            return

        beh = next_behavior()
        count(beh)
        if STATE["delay"]:
            time.sleep(STATE["delay"])

        if beh == "ok_sse":
            self._raw(200, SSE_OK.encode(), "text/event-stream")
        elif beh == "truncated":
            self._raw(200, SSE_TRUNCATED.encode(), "text/event-stream")
        elif beh == "empty":
            self._raw(200, b"")
        elif beh == "ip_rep":
            self._raw(403, BODY_IP_REP)
        elif beh == "identity":
            self._raw(403, BODY_IDENTITY)
        elif beh == "rate":
            self._raw(429, BODY_RATE)
        elif beh == "server":
            self._raw(503, BODY_SERVER)
        else:
            self._raw(200, SSE_OK.encode(), "text/event-stream")


def start(host="127.0.0.1", port=0):
    """Start the mock in a background thread. Returns (server, thread, port)."""
    srv = ThreadingHTTPServer((host, port), MockHandler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


if __name__ == "__main__":
    import sys

    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    srv, _, port = start(port=p)
    print(f"mock gateway on http://127.0.0.1:{port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
