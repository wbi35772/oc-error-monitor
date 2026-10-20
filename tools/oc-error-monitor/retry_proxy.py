"""Retry proxy for the OpenCode free tier.

Why this exists
---------------
The zen gateway answers a fraction of byte-identical requests with

    403 FreeTierError: ...detected abusive traffic from this IP address...

Measured on one captured real client request: 19/24 succeeded, and after a 403
the next attempt succeeded 3/4 times, with no failure streak longer than 2.
The 403 is therefore a per-request transient, not a state of the account, the
node or the session. Retrying the same bytes at the request layer is enough.

Design
------
- Sits between the opencode CLI and the gateway; the client request is replayed
  byte for byte, so the client-identity gate (UA + x-opencode-* + stream:true)
  keeps being satisfied by the real client's own headers.
- Retries only before any byte of the response body has been written to the
  client. A stream that breaks after content has started is passed through and
  left to the session layer.
- Retries 403 abusive-traffic, 429, 5xx, connection errors, and 200 responses
  whose stream ends before a single byte arrives.
- A 403 FreeTierError *without* the abusive-traffic marker is a malformed
  request; retrying it is pointless, so it is passed straight through.
- Successful responses stream through unbuffered (read1, not read).

Config keys read from the environment
-------------------------------------
    RETRY_PROXY_HOST           default 127.0.0.1
    RETRY_PROXY_PORT           default 8787
    RETRY_PROXY_UPSTREAM       default https://opencode.ai/inference/openai/v1
    RETRY_PROXY_UPSTREAM_PROXY default http://127.0.0.1:7899   (mihomo mixed)
    RETRY_PROXY_ATTEMPTS       default 4  (1 try + 3 retries)
    RETRY_PROXY_TIMEOUT        default 120 (seconds per socket operation)

Endpoints
---------
    POST/GET <anything>        proxied to RETRY_PROXY_UPSTREAM
    GET  /__stats              counters as JSON
    GET  /__health             {"ok": true}
"""
import http.server
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

# pythonw.exe has no console: sys.stdout/sys.stderr are None there.
for _s in (sys.stdout, sys.stderr):
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOST = os.environ.get("RETRY_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RETRY_PROXY_PORT", "8787"))
UPSTREAM = os.environ.get(
    "RETRY_PROXY_UPSTREAM", "https://opencode.ai/inference/openai/v1"
).rstrip("/")
UPSTREAM_PROXY = os.environ.get(
    "RETRY_PROXY_UPSTREAM_PROXY", "http://127.0.0.1:7899"
)
MAX_ATTEMPTS = max(1, int(os.environ.get("RETRY_PROXY_ATTEMPTS", "4")))
SOCK_TIMEOUT = float(os.environ.get("RETRY_PROXY_TIMEOUT", "120"))
BACKOFF = [0.4, 1.0, 1.8, 3.0, 4.5]

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "retry_proxy.log.jsonl")
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"retry_proxy-{PORT}.lock")

# "finish_reason":null is not a finish; only a non-null value ends the stream.
_FINISH_RE = re.compile(rb'"finish_reason"\s*:\s*"(?!null)\S')

HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
}

RETRY_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

# Two routes: through mihomo (normal) and direct (mihomo not running yet, or a
# network that reaches the gateway without it). A refused proxy connection
# switches the remaining attempts to direct instead of burning all of them.
# An empty RETRY_PROXY_UPSTREAM_PROXY means "direct only" (used by the tests).
if UPSTREAM_PROXY:
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": UPSTREAM_PROXY, "https": UPSTREAM_PROXY})
    )
else:
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_lock = threading.Lock()
_stats = {
    "requests": 0,
    "attempts": 0,
    "retried_requests": 0,
    "rescued_requests": 0,
    "exhausted_requests": 0,
    "failed_requests": 0,
    "truncated_streams": 0,
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
}
_classes = Counter()
_log_writes = 0


def bump(key, n=1):
    with _lock:
        _stats[key] = _stats.get(key, 0) + n


def log(rec):
    """Append one JSONL record, rotating the file at 5 MB."""
    global _log_writes
    try:
        _log_writes += 1
        if _log_writes % 200 == 0 and os.path.exists(LOG_PATH):
            if os.path.getsize(LOG_PATH) > 5 * 1024 * 1024:
                try:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
                except OSError:
                    pass
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def classify(status, body):
    text = body.decode("utf-8", "replace") if body else ""
    if status == 200:
        return "OK"
    if "abusive traffic" in text:
        return "IP_REP"
    if "FreeTierError" in text:
        return "IDENTITY"
    if "1010" in text:
        return "CF_UA"
    if status == 429:
        return "RATE"
    if status >= 500:
        return "5XX"
    return f"HTTP{status}"


def should_retry(cls, status):
    if cls in ("IP_REP", "RATE", "5XX", "EMPTY_STREAM", "ERR"):
        return True
    return status in RETRY_STATUS


def backoff(attempt):
    delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
    return delay * (0.85 + random.random() * 0.3)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle(self):
        # The CLI drops connections aggressively (keep-alive reuse, aborted
        # streams). Those resets are normal, not a proxy fault.
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, OSError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            raise

    # ---- plumbing -------------------------------------------------------
    def _send_json(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _target(self):
        p = self.path
        if p.startswith("/__"):
            return None
        if p.startswith("/v1"):
            return UPSTREAM + p[3:]
        if p.startswith("http://") or p.startswith("https://"):
            return p
        return UPSTREAM + p

    def _open_upstream(self, method, target, hdrs, body, direct=False):
        req = urllib.request.Request(
            target, data=body if body else None, method=method
        )
        for k, v in hdrs.items():
            req.add_header(k, v)
        opener = _direct_opener if direct else _opener
        return opener.open(req, timeout=SOCK_TIMEOUT)

    def _handle(self, method):
        target = self._target()
        if target is None:
            if self.path.startswith("/__stats"):
                with _lock:
                    out = dict(_stats)
                    out["classes"] = dict(_classes)
                self._send_json(out)
            elif self.path.startswith("/__health"):
                self._send_json({"ok": True, "upstream": UPSTREAM})
            else:
                self._send_json({"error": "not found"}, 404)
            return

        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""

        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        hdrs["Accept-Encoding"] = "identity"

        bump("requests")
        trail = []
        t_start = time.time()

        resp = None
        first = b""
        ctype = "application/json"
        last_status = None
        last_body = b""
        streamed = False
        route_direct = False

        for attempt in range(MAX_ATTEMPTS):
            bump("attempts")
            try:
                resp = self._open_upstream(method, target, hdrs, body, route_direct)
                status = resp.status
                if status == 200:
                    ctype = resp.headers.get("Content-Type", "application/json")
                    is_sse = "event-stream" in ctype
                    if is_sse:
                        first = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                    else:
                        first = resp.read()
                    if not first:
                        # 200 with an empty body: the request was accepted and
                        # then dropped. Nothing reached the client, so retry.
                        trail.append("EMPTY_STREAM")
                        try:
                            resp.close()
                        except Exception:
                            pass
                        if attempt + 1 < MAX_ATTEMPTS:
                            time.sleep(backoff(attempt))
                            continue
                        break
                    trail.append("OK")
                    streamed = True
                    break

                last_body = resp.read()
                cls = classify(status, last_body)
                trail.append(cls)
                last_status = status
                if attempt + 1 < MAX_ATTEMPTS and should_retry(cls, status):
                    time.sleep(backoff(attempt))
                    continue
                break
            except urllib.error.HTTPError as e:
                last_status = e.code
                try:
                    last_body = e.read()
                except Exception:
                    last_body = b""
                cls = classify(last_status, last_body)
                trail.append(cls)
                if attempt + 1 < MAX_ATTEMPTS and should_retry(cls, last_status):
                    time.sleep(backoff(attempt))
                    continue
                break
            except Exception as e:
                trail.append(f"ERR:{type(e).__name__}")
                last_status = None
                last_body = b""
                if not route_direct and _proxy_refused(e):
                    # mihomo is down or its port moved: stop paying for it.
                    route_direct = True
                    trail.append("ROUTE:direct")
                    time.sleep(backoff(attempt))
                    continue
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(backoff(attempt))
                    continue
                break

        # Every attempt is counted, not only the last one: IP_REP:5 alongside
        # OK:16 is the ratio that shows how hard the gateway is pushing back.
        with _lock:
            for entry in trail:
                _classes[entry] += 1

        if streamed:
            if len(trail) > 1:
                bump("retried_requests")
                bump("rescued_requests")
            if self._stream_success(resp, ctype, first) is False:
                # Upstream closed the SSE before any finish_reason, so the
                # client will report "stream ended without finish_reason".
                # Bytes are already on the wire: observe it, do not retry.
                bump("truncated_streams")
        else:
            bump("failed_requests")
            if len(trail) > 1:
                # We retried and still failed: the request is exhausted.
                bump("retried_requests")
                bump("exhausted_requests")
            self._send_failure(last_status, last_body)

        log(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "method": method,
                "path": self.path,
                "attempts": trail,
                "ms": int((time.time() - t_start) * 1000),
                "body_len": len(body),
                "model": _peek_model(body),
                "route": "direct" if route_direct else "proxy",
            }
        )

    def _stream_success(self, resp, ctype, first):
        """Forward a 200. Returns None for non-SSE, else whether finish_reason arrived."""
        saw_finish = False
        is_sse = "event-stream" in ctype
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if is_sse:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                if not saw_finish and _FINISH_RE.search(first):
                    saw_finish = True
                self.wfile.write(first)
                self.wfile.flush()
                while True:
                    # read1 returns as soon as any bytes are available; read(n)
                    # would block until n bytes accumulate and stall tokens.
                    chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                    if not chunk:
                        break
                    if not saw_finish and _FINISH_RE.search(chunk):
                        saw_finish = True
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                self.send_header("Content-Length", str(len(first)))
                self.end_headers()
                self.wfile.write(first)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return None
        finally:
            self.close_connection = True
        return saw_finish if is_sse else None

    def _send_failure(self, status, body):
        if status is None:
            raw = json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "ProxyUpstreamError",
                        "message": "retry proxy could not reach the upstream after all attempts",
                    },
                }
            ).encode()
            code = 502
        else:
            raw = body or b"{}"
            code = status
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True

    # ---- verbs ----------------------------------------------------------
    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")


def _proxy_refused(exc):
    """True when the failure is mihomo not accepting connections."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return True
    return "refused" in str(reason).lower()


def _peek_model(body):
    try:
        if not body or len(body) > 2_000_000:
            return None
        return json.loads(body).get("model")
    except Exception:
        return None


def _err_log_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "retry_proxy.err.log")


def _record_fatal(text):
    try:
        with open(_err_log_path(), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n" + str(text) + "\n")
    except Exception:
        pass


def _acquire_single_instance():
    """Take an exclusive lock so only one proxy owns the port.

    Windows sockets set SO_REUSEADDR by default, so a second bind on a busy
    port *succeeds* and the newcomer steals connections from the incumbent.
    A file lock is process scoped and released when the process dies, so it is
    the reliable guard here. Returns the open file (keep it alive) or False
    when another instance already holds the lock.
    """
    try:
        f = open(LOCK_PATH, "a+b")
    except OSError:
        return None
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            f.close()
        except OSError:
            pass
        return False
    return f


def _peer_is_healthy():
    # Direct opener on purpose: a system proxy must never be consulted for a
    # loopback health check, or the check fails while the proxy is fine.
    try:
        with _direct_opener.open(f"http://{HOST}:{PORT}/__health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    # Guard first: a healthy instance on the port means there is nothing to do.
    if _peer_is_healthy():
        print("retry proxy already running; exiting")
        raise SystemExit(0)
    _lock_handle = _acquire_single_instance()
    if _lock_handle is False:
        print("retry proxy already running; exiting")
        raise SystemExit(0)
    try:
        srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
        srv.daemon_threads = True
        print(f"retry proxy  http://{HOST}:{PORT}  ->  {UPSTREAM}")
        print(f"via proxy    {UPSTREAM_PROXY}")
        print(f"attempts     {MAX_ATTEMPTS}   log {LOG_PATH}")
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as e:
        # Single instance guard: if the port is already served by a healthy
        # proxy, exit quietly instead of crashing on every watchdog tick.
        if e.errno in (48, 98, 10048) and _peer_is_healthy():
            print("retry proxy already running; exiting")
            raise SystemExit(0)
        _record_fatal(e)
        raise
    except Exception:
        import traceback

        _record_fatal(traceback.format_exc())
        raise
