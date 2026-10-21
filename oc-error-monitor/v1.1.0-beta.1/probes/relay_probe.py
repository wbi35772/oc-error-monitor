"""Plaintext relay in front of the opencode (zen) provider.

Purpose: capture exactly what the real CLI sends so the free-tier identity
gate can be reproduced, and later serve as the retry proxy.

Listens on http://127.0.0.1:8788/v1/...  ->  forwards to https://opencode.ai/zen/v1/...
Each request is appended to relay_log.jsonl (headers + body preview, no secrets
other than what the client itself sends).
"""
import http.server
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LISTEN = ("127.0.0.1", 8788)
UPSTREAM = os.environ.get(
    "RELAY_UPSTREAM", "https://opencode.ai/inference/openai/v1"
)
PROXY = os.environ.get("RELAY_UPSTREAM_PROXY", "http://127.0.0.1:7899")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay_log.jsonl")

HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
)


def log(rec):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _target(self):
        path = self.path
        if path.startswith("/v1"):
            return UPSTREAM + path[3:]
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return UPSTREAM + path

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        target = self._target()

        hdrs = {}
        for k, v in self.headers.items():
            if k.lower() in HOP:
                continue
            hdrs[k] = v

        log(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "method": method,
                "path": self.path,
                "target": target,
                "headers": dict(self.headers.items()),
                "body_len": len(body),
                "body": body[:4000].decode("utf-8", "replace"),
            }
        )

        req = urllib.request.Request(target, data=body if body else None, method=method)
        for k, v in hdrs.items():
            req.add_header(k, v)

        try:
            resp = OPENER.open(req, timeout=600)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            msg = f"relay upstream error: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        try:
            status = resp.status if hasattr(resp, "status") else resp.code
        except Exception:
            status = resp.code

        self.send_response(status)
        ctype = resp.headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", ctype)
        is_sse = "event-stream" in ctype
        if is_sse:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.flush()
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            return

        payload = resp.read()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def do_DELETE(self):
        self._handle("DELETE")


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(LISTEN, Handler)
    print(f"relay listening on http://{LISTEN[0]}:{LISTEN[1]}  ->  {UPSTREAM}")
    print(f"log: {LOG}")
    srv.serve_forever()
