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
    RETRY_PROXY_PORT           default 8899
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
import socket
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque

# pythonw.exe has no console: sys.stdout/sys.stderr are None there.
for _s in (sys.stdout, sys.stderr):
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOST = os.environ.get("RETRY_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RETRY_PROXY_PORT", "8899"))
# If the port is taken by something that is not us (playwright-mcp's internal
# server once grabbed 8787), the proxy moves to the next one and publishes it.
PORT_CANDIDATES = [
    int(x)
    for x in os.environ.get("RETRY_PROXY_PORTS", "8899,8900,8901,8902").split(",")
    if x.strip().isdigit()
] or [PORT]
UPSTREAM = os.environ.get(
    "RETRY_PROXY_UPSTREAM", "https://opencode.ai/inference/openai/v1"
).rstrip("/")
UPSTREAM_PROXY = os.environ.get(
    "RETRY_PROXY_UPSTREAM_PROXY", "http://127.0.0.1:7899"
)
MAX_ATTEMPTS = max(1, int(os.environ.get("RETRY_PROXY_ATTEMPTS", "4")))
SOCK_TIMEOUT = float(os.environ.get("RETRY_PROXY_TIMEOUT", "120"))
BACKOFF = [0.4, 1.0, 1.8, 3.0, 4.5]
# A rate limit needs more patience than a transient 403: the gateway is asking
# us to slow down, not to try again in a fraction of a second.
RATE_BACKOFF = [2.0, 5.0, 9.0, 15.0, 25.0]

HERE = os.path.dirname(os.path.abspath(__file__))


def _read_version():
    """The version sits in VERSION at the repository root, and the staging
    step copies it next to the running copy, so a live instance can answer
    "which build am I" over HTTP instead of by directory name.

    Two layouts have to work: the repository (VERSION two levels up from
    oc-error-monitor/v1.1.0-beta.1) and the staged runtime directory (VERSION beside
    the script). So the lookup walks up instead of assuming one shape."""
    here = HERE
    for _ in range(5):
        try:
            with open(os.path.join(here, "VERSION"), "r", encoding="utf-8") as f:
                first = f.read().strip().splitlines()[0].strip()
                if first:
                    return first
        except (OSError, IndexError):
            pass
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return "0.0.0-dev"


VERSION = _read_version()

LOG_PATH = os.path.join(HERE, "retry_proxy.log.jsonl")
ACTIVE_PORT_FILE = os.path.join(HERE, "active-port.txt")
HANDOVER_FILE = os.path.join(HERE, "handover-request.json")
# A user stop has to outlive the watchdog: the marker is written here and
# both the proxy and the supervisor refuse to run while it exists. Only an
# explicit start (the other instance, or switch-instance.bat) removes it.
STOP_MARKER = os.path.join(HERE, "stopped.by-user")
# Repo root, written by start-retry-proxy.bat when it stages this copy.
REPO_FILE = os.path.join(HERE, "repo-path.txt")
PEER_PORTS = [
    int(x)
    for x in os.environ.get(
        "RETRY_PROXY_PEER_PORTS", "8788,8899,8900,8901,8902"
    ).split(",")
    if x.strip().isdigit()
]
LOCK_PATH = os.path.join(HERE, f"retry_proxy-{PORT}.lock")

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

# Runtime-tunable knobs. The dashboard writes these through /__control, so a
# change of patience or route does not need a restart (and a restart is what
# used to drop the CLI's connection).
SETTINGS = {
    "attempts": MAX_ATTEMPTS,
    "backoff": list(BACKOFF),
    "timeout": SOCK_TIMEOUT,
    "paused": False,      # paused = one attempt, no retry: pure pass-through
    "force_route": None,  # None | "proxy" | "direct"
}
STARTED_TS = time.time()


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
# Requests currently in flight. A handover drains this counter before the old
# instance exits, so a running stream is never cut in half.
_active = 0


def _mark_stopped(by="dashboard"):
    """Leave a marker the watchdog honours, so a stop stays stopped."""
    try:
        with open(STOP_MARKER, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "by": by,
                    "pid": os.getpid(),
                    "port": PORT,
                    "version": VERSION,
                },
                f,
            )
    except OSError:
        pass


def _repo_root():
    # cmd's echo writes in the console code page, not UTF-8, and the path can
    # hold non-ASCII: read bytes and try the encodings that actually occur.
    try:
        with open(REPO_FILE, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    p = ""
    for enc in ("utf-8-sig", "gbk", "mbcs"):
        try:
            p = raw.decode(enc).strip()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    return p if p and os.path.isdir(p) else None


def _probe_peer():
    """The other build answers on a different port and reports a different
    version, so the peer is whoever answers there. Probing beats asking the
    user which port the other instance happened to take."""
    for port in PEER_PORTS:
        if port == PORT:
            continue
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:%d/__health" % port, timeout=0.4
            ) as r:
                h = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        if h.get("ok") and h.get("version") != VERSION:
            return {
                "port": port,
                "version": h.get("version"),
                "pid": h.get("pid"),
                "paused": h.get("paused"),
                "uptime_s": h.get("uptime_s"),
            }
    return None


def _peer_dirs():
    repo = _repo_root()
    if not repo:
        return []
    base = os.path.join(repo, "oc-error-monitor")
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    for name in names:
        d = os.path.join(base, name)
        bat = os.path.join(d, "start-retry-proxy.bat")
        if not os.path.isfile(bat):
            continue
        try:
            with open(os.path.join(d, "VERSION"), "r", encoding="utf-8") as f:
                ver = f.read().strip()
        except OSError:
            ver = "?"
        out.append({"version": ver, "dir": d, "bat": bat})
    return out


def _vkey(v):
    # "1.1.0-beta.1" must outrank "1.0.1": compare the numeric core first, and
    # a pre-release only wins against the same core with nothing after it.
    core, _, tail = (v or "").partition("-")
    nums = []
    for part in core.split("."):
        nums.append(int(part) if part.isdigit() else 0)
    return (tuple(nums), 0 if tail else 1)


def _start_peer():
    dirs = [d for d in _peer_dirs() if d["version"] != VERSION]
    if not dirs:
        return {"ok": False, "error": "no peer build under oc-error-monitor/"}
    # The newest sibling is the one worth testing against; an older build can
    # even want the port this instance owns.
    peer = max(dirs, key=lambda d: _vkey(d["version"]))
    p = _probe_peer()
    if p and p.get("version") == peer["version"]:
        return {"ok": True, "action": "start_peer", "version": peer["version"], "already_running": True}
    rundir = os.path.join(
        os.path.expanduser("~"), ".config", "opencode", "oc-error-monitor-v" + peer["version"]
    )
    marker = os.path.join(rundir, "stopped.by-user")
    cleared = False
    if os.path.exists(marker):
        try:
            os.remove(marker)
            cleared = True
        except OSError:
            pass
    subprocess.Popen(
        ["cmd", "/c", peer["bat"]],
        cwd=peer["dir"],
        creationflags=0x00000008,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "ok": True,
        "action": "start_peer",
        "version": peer["version"],
        "script": peer["bat"],
        "cleared_marker": cleared,
    }


def _stop_peer():
    p = _probe_peer()
    if not p:
        return {"ok": False, "error": "peer is not answering"}
    req = urllib.request.Request(
        "http://127.0.0.1:%d/__control" % p["port"],
        data=json.dumps({"action": "stop", "user": True}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return {"ok": True, "peer": p, "reply": json.loads(r.read().decode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Live feed for the dashboard: the last 400 records plus one queue per open
# /__events stream. Bounded so a forgotten tab cannot grow memory.
_events = deque(maxlen=400)
_subscribers = set()


def publish(ev):
    with _lock:
        _events.append(ev)
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass


def snapshot_stats():
    with _lock:
        out = dict(_stats)
        out["classes"] = dict(_classes)
        out["settings"] = dict(SETTINGS)
        out["settings"]["backoff"] = list(SETTINGS["backoff"])
        out["uptime_s"] = round(time.time() - STARTED_TS, 1)
        out["pid"] = os.getpid()
        out["port"] = PORT
        out["version"] = VERSION
        out["upstream"] = UPSTREAM
        out["upstream_proxy"] = UPSTREAM_PROXY
        return out


def bump(key, n=1):
    with _lock:
        _stats[key] = _stats.get(key, 0) + n


def log(rec):
    """Append one JSONL record, rotate at 5 MB, and push it to the live feed."""
    global _log_writes
    publish(rec)
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


def backoff(attempt, cls=None):
    if cls == "RATE":
        delay = RATE_BACKOFF[min(attempt, len(RATE_BACKOFF) - 1)]
    else:
        seq = SETTINGS["backoff"] or BACKOFF
        delay = seq[min(attempt, len(seq) - 1)]
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

    # ---- dashboard ------------------------------------------------------
    def _serve_ui(self):
        here = os.path.dirname(os.path.abspath(__file__))
        page = os.path.join(here, "ui.html")
        try:
            with open(page, "rb") as f:
                raw = f.read()
        except OSError:
            return self._send_json({"error": "ui.html is missing next to retry_proxy.py"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_events(self):
        q = queue.Queue(maxsize=200)
        with _lock:
            _subscribers.add(q)
            backlog = list(_events)[-50:]
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for ev in backlog:
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _lock:
                _subscribers.discard(q)
            self.close_connection = True

    def _control(self, body):
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return self._send_json({"ok": False, "error": "bad json"}, 400)
        action = str(data.get("action") or "")
        if action == "pause":
            SETTINGS["paused"] = True
        elif action == "resume":
            SETTINGS["paused"] = False
        elif action == "drain":
            want = data.get("pid")
            if want and int(want) != os.getpid():
                # Two instances share the port during a handover; the request
                # may land on the wrong one.
                return self._send_json(
                    {"ok": False, "error": "not the addressed instance", "pid": os.getpid()}, 409
                )
            self._send_json({"ok": True, "action": "drain", "pid": os.getpid(), "active": _active})
            threading.Thread(target=self._drain_and_exit, daemon=True).start()
            return
        elif action == "set":
            try:
                if "attempts" in data:
                    SETTINGS["attempts"] = max(1, min(10, int(data["attempts"])))
                if "timeout" in data:
                    SETTINGS["timeout"] = max(5.0, min(900.0, float(data["timeout"])))
                if isinstance(data.get("backoff"), list) and data["backoff"]:
                    SETTINGS["backoff"] = [max(0.0, min(30.0, float(x))) for x in data["backoff"]][:6]
            except (TypeError, ValueError):
                return self._send_json({"ok": False, "error": "bad number"}, 400)
            if "force_route" in data:
                v = data["force_route"]
                SETTINGS["force_route"] = v if v in ("proxy", "direct") else None
        elif action == "reset":
            with _lock:
                for k in ("requests", "attempts", "retried_requests", "rescued_requests",
                          "exhausted_requests", "failed_requests", "truncated_streams"):
                    _stats[k] = 0
                _classes.clear()
                _stats["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        elif action == "restart":
            here = os.path.dirname(os.path.abspath(__file__))
            script = os.path.join(here, "restart-retry-proxy.ps1")
            if not os.path.exists(script):
                return self._send_json({"ok": False, "error": "restart-retry-proxy.ps1 missing"}, 400)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Handover"],
                cwd=here, creationflags=0x00000008, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return self._send_json({"ok": True, "action": "restart", "note": "zero-downtime handover started"})
        elif action == "shutdown":
            self._send_json({"ok": True, "action": "shutdown"})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        elif action == "stop":
            # A plain shutdown gets revived by the supervisor; a stop leaves a
            # marker, so both the proxy and the supervisor exit and stay out.
            _mark_stopped(str(data.get("by") or "dashboard"))
            self._send_json(
                {
                    "ok": True,
                    "action": "stop",
                    "pid": os.getpid(),
                    "marker": STOP_MARKER,
                    "note": "start it from the other instance or switch-instance.bat",
                }
            )
            threading.Thread(
                target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True
            ).start()
            return
        elif action == "start_peer":
            res = _start_peer()
            return self._send_json(res, 200 if res.get("ok") else 400)
        elif action == "stop_peer":
            res = _stop_peer()
            return self._send_json(res, 200 if res.get("ok") else 400)
        elif action == "switch":
            started = _start_peer()
            if not started.get("ok"):
                return self._send_json(started, 400)

            def _finish_switch():
                # Never take this instance down before the peer is proven
                # healthy, or a switch leaves the client with nothing.
                for _ in range(50):
                    time.sleep(0.5)
                    if _probe_peer():
                        _mark_stopped("switch")
                        os._exit(0)
                # Peer never answered: stay up rather than leave a hole.

            self._send_json({"ok": True, "action": "switch", "peer": started})
            threading.Thread(target=_finish_switch, daemon=True).start()
            return
        else:
            return self._send_json({"ok": False, "error": "unknown action " + action}, 400)
        return self._send_json({"ok": True, "action": action, "settings": dict(SETTINGS)})

    def _drain_and_exit(self):
        """Stop accepting, let the running requests finish, then leave the port.

        Order matters: the listening socket is closed *after* the in-flight
        requests are done, so a stream that is half way through is never cut.
        """
        _drain_and_exit(self.server)

    def _target(self):
        p = self.path
        if p.startswith("/__"):
            return None
        if p in ("/", "/favicon.ico", "/index.html"):
            # Never forward a probe of our own root to the gateway: the
            # gateway answers 404 "not found" and the client blames the
            # provider. Serve local metadata instead.
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
        return opener.open(req, timeout=SETTINGS["timeout"])

    def _handle(self, method):
        target = self._target()
        if target is None:
            path = self.path.split("?", 1)[0]
            if path in ("/__ui", "/__ui/"):
                return self._serve_ui()
            if path == "/__events":
                return self._serve_events()
            if path == "/__stats":
                return self._send_json(snapshot_stats())
            if path == "/__config":
                return self._send_json(snapshot_stats())
            if path == "/__health":
                return self._send_json({
                    "ok": True,
                    "service": "opencode retry proxy",
                    "version": VERSION,
                    "pid": os.getpid(),
                    "uptime_s": round(time.time() - STARTED_TS, 1),
                    "upstream": UPSTREAM,
                    "paused": SETTINGS["paused"],
                })
            if path == "/__peer":
                return self._send_json(
                    {
                        "ok": True,
                        "me": {"version": VERSION, "port": PORT, "pid": os.getpid()},
                        "peer": _probe_peer(),
                        "stopped_marker": os.path.exists(STOP_MARKER),
                        "peers": [{"version": d["version"], "dir": d["dir"]} for d in _peer_dirs()],
                        "repo": _repo_root(),
                    }
                )
            if path == "/__control":
                n = int(self.headers.get("Content-Length") or 0)
                return self._control(self.rfile.read(n) if n else b"")
            if path == "/":
                return self._send_json({
                    "service": "opencode retry proxy",
                    "version": VERSION,
                    "dashboard": "/__ui",
                    "stats": "/__stats",
                    "events": "/__events",
                    "health": "/__health",
                    "upstream": UPSTREAM,
                    "pid": os.getpid(),
                })
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send_json({"error": "not found"}, 404)

        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""

        global _active
        with _lock:
            _active += 1
        try:
            return self._proxy_request(method, target, body)
        finally:
            with _lock:
                _active -= 1

    def _proxy_request(self, method, target, body):
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

        max_attempts = 1 if SETTINGS["paused"] else SETTINGS["attempts"]
        route_direct = SETTINGS["force_route"] == "direct"
        for attempt in range(max_attempts):
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
                        if attempt + 1 < max_attempts:
                            time.sleep(backoff(attempt, "EMPTY_STREAM"))
                            continue
                        break
                    trail.append("OK")
                    streamed = True
                    break

                last_body = resp.read()
                cls = classify(status, last_body)
                trail.append(cls)
                last_status = status
                if attempt + 1 < max_attempts and should_retry(cls, status):
                    time.sleep(backoff(attempt, cls))
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
                if attempt + 1 < max_attempts and should_retry(cls, last_status):
                    time.sleep(backoff(attempt, cls))
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
                    time.sleep(backoff(attempt, "ERR"))
                    continue
                if attempt + 1 < max_attempts:
                    time.sleep(backoff(attempt, "ERR"))
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


def _port_has_listener(port):
    """True when something already answers on the port.

    On Windows SO_REUSEADDR lets a process bind a port another process is
    listening on, so binding is no proof that the port is free. Connecting is.
    """
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((HOST, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _port_is_ours(port):
    """True when the port is served by a retry proxy (this one or another)."""
    try:
        with _direct_opener.open(f"http://127.0.0.1:{port}/__health", timeout=2) as r:
            if r.status != 200:
                return False
            body = r.read(400).decode("utf-8", "replace")
            return "opencode retry proxy" in body
    except Exception:
        return False


def _port_is_free(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _port_owners(port):
    """Who listens on the port (pid + executable), via the IP Helper API."""
    try:
        import port_owner

        return port_owner.owners(port)
    except Exception:
        return []


def _bind_server():
    """Bind the first port we can have to ourselves.

    Sharing a port with a foreign server is what produced the "HTTP 404: not
    found" failures: Windows SO_REUSEADDR lets both bind, and connections are
    then handed out at random, so some requests reached the wrong server.
    A bind attempt proves nothing on Windows, so the OS is asked who owns the
    port, and the connect test is the second opinion.
    """
    global PORT
    mine = os.getpid()
    for candidate in [PORT] + [p for p in PORT_CANDIDATES if p != PORT]:
        owners = _port_owners(candidate)
        foreign = [o for o in owners if o.get("pid") != mine]
        if foreign:
            for o in foreign:
                exe = (o.get("exe") or "?").split("\\")[-1]
                print(f"port {candidate} belongs to pid {o['pid']} ({exe}), skipping it")
            continue
        if _port_has_listener(candidate):
            if _port_is_ours(candidate):
                return None, candidate, "already-running"
            continue
        try:
            srv = http.server.ThreadingHTTPServer((HOST, candidate), Handler)
            srv.daemon_threads = True
            PORT = candidate
            return srv, candidate, "bound"
        except OSError:
            continue
    return None, PORT, "failed"


def _publish_port(port):
    try:
        os.makedirs(os.path.dirname(ACTIVE_PORT_FILE), exist_ok=True)
        with open(ACTIVE_PORT_FILE, "w", encoding="utf-8") as f:
            f.write(str(port))
    except OSError:
        pass


def _request_handover(target_pid):
    """Ask the incumbent instance to drain and exit.

    The request goes through a file, not through the port: during a handover
    both instances are bound, and Windows hands a new connection to one of
    them at random, so an HTTP request cannot be addressed reliably.
    """
    try:
        with open(HANDOVER_FILE, "w", encoding="utf-8") as f:
            json.dump({"target": int(target_pid), "from": os.getpid(),
                       "ts": time.time()}, f)
    except OSError:
        pass


def _watch_handover_requests(server):
    """The incumbent watches for a handover request and drains itself."""
    while True:
        time.sleep(0.4)
        try:
            with open(HANDOVER_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        try:
            target = int(data.get("target", 0))
        except (TypeError, ValueError):
            continue
        if target != os.getpid():
            continue
        try:
            os.remove(HANDOVER_FILE)
        except OSError:
            pass
        print(f"handover requested by pid {data.get('from')}, draining")
        _drain_and_exit(server)
        return


def _drain_and_exit(server):
    """Stop accepting, let the running requests finish, then leave the port."""
    try:
        server.shutdown()
    except Exception:
        pass
    _wait_for_idle()
    try:
        server.server_close()
    except Exception:
        pass
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": "SYSTEM", "path": "/__drain",
         "attempts": ["DRAIN"], "ms": 0, "body_len": 0, "model": None, "route": "local"})
    os._exit(0)


def _wait_for_idle(timeout=120):
    """Block until no request is in flight (or the timeout runs out)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            n = _active
        if n <= 0:
            return True
        time.sleep(0.2)
    return False


if __name__ == "__main__":
    # Never share the port with a foreign server. Windows SO_REUSEADDR lets
    # both bind, and connections are then handed out at random: some requests
    # reached the wrong server and failed with "HTTP 404: not found".
    if "--handover" in sys.argv[1:]:
        _publish_port(PORT)

    # --handover: the restart script starts this instance while the old one is
    # still serving. Windows SO_REUSEADDR lets both bind 8787, so the socket is
    # never closed. We serve immediately (the script waits for our /__health to
    # answer with a new pid, then stops the old one) and take the file lock in
    # the background once the incumbent releases it.
    if "--handover" in sys.argv[1:]:
        try:
            srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
        except OSError as e:
            _record_fatal(e)
            raise SystemExit(1)
        srv.daemon_threads = True

        def _late_lock():
            deadline = time.time() + 60
            while time.time() < deadline:
                got = _acquire_single_instance()
                if got is not False:
                    globals()["_handover_lock"] = got
                    return
                time.sleep(0.5)

        threading.Thread(target=_late_lock, daemon=True).start()
        # The incumbent watches for a handover request through a file, so it
        # drains itself even though the port now serves two instances.
        threading.Thread(target=_watch_handover_requests, args=(srv,), daemon=True).start()
        # Tell the incumbent to step down.
        if "--handover-from" in sys.argv:
            try:
                idx = sys.argv.index("--handover-from")
                _request_handover(int(sys.argv[idx + 1]))
            except (IndexError, ValueError):
                pass
        print(f"retry proxy (handover) http://{HOST}:{PORT} -> {UPSTREAM}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        # serve_forever returns as soon as a drain asks it to; the process
        # must not leave before the running requests are done.
        _wait_for_idle()
        raise SystemExit(0)

    # Guard first: a healthy instance on the port means there is nothing to do.
    if _peer_is_healthy():
        print("retry proxy already running; exiting")
        raise SystemExit(0)
    _lock_handle = _acquire_single_instance()
    if _lock_handle is False:
        print("retry proxy already running; exiting")
        raise SystemExit(0)
    try:
        srv, chosen, why = _bind_server()
        if why == "already-running":
            print("retry proxy already running; exiting")
            raise SystemExit(0)
        if why == "failed":
            _record_fatal(f"no free port among {PORT_CANDIDATES}")
            print(f"no free port among {PORT_CANDIDATES}")
            raise SystemExit(1)
        requested = int(os.environ.get("RETRY_PROXY_PORT", "8899"))
        if chosen != requested:
            print(f"port {requested} is taken by another service, moving to {chosen}")
        _publish_port(chosen)
        print(f"retry proxy  http://{HOST}:{PORT}  ->  {UPSTREAM}")
        print(f"via proxy    {UPSTREAM_PROXY}")
        print(f"attempts     {MAX_ATTEMPTS}   log {LOG_PATH}")
        # Every instance watches for a handover request, so a later upgrade
        # can make this one step down without an HTTP round trip.
        threading.Thread(target=_watch_handover_requests, args=(srv,), daemon=True).start()
        srv.serve_forever()
        # A drain makes serve_forever return; wait for the in-flight requests.
        _wait_for_idle()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
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
