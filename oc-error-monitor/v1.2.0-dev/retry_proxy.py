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
    RETRY_PROXY_PORT           default 8899 (production sets 8788)
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
import urllib.parse
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
# explicit start (start-retry-proxy.bat --force) removes it.
STOP_MARKER = os.path.join(HERE, "stopped.by-user")
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


def snippet(raw, limit=240):
    """The first bytes of an error body, flattened to one line.

    A class such as RATE says what we called it; the body says what the
    gateway called it. Without this the log records that we were refused
    without recording why, and every later question needs a fresh experiment.
    """
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return ""
    return " ".join(text.split())[:limit]


def retry_after_seconds(value):
    """Seconds to wait from a Retry-After header, either form of it."""
    if not value:
        return None
    value = str(value).strip()
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        delta = when.timestamp() - time.time()
        return delta if delta > 0 else None
    except Exception:
        return None


def pause(attempt, cls, wait_hint=None):
    """Sleep before the next attempt, honouring the upstream's own hint."""
    delay = backoff(attempt, cls)
    if wait_hint:
        # Backing off 0.4s when the server asked for 5 is how a request burns
        # every attempt and still ends up exhausted.
        delay = max(delay, min(wait_hint, 30.0))
    time.sleep(delay)


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


# ---------------------------------------------------------------------------
# Which node is carrying the traffic
# ---------------------------------------------------------------------------
#
# A 429 says the gateway refused; it does not say who was carrying the request.
# When the refusal rate climbs, the first question is always the same: which
# node, in which group, and how healthy is it. mihomo knows, but only through
# its controller API, so this reads that API and reports what is actually on
# the wire rather than what the configuration says should be.

CLASH_DIRS = [
    os.path.join(os.environ.get("APPDATA") or "", "io.github.clash-verge-rev.clash-verge-rev"),
    os.path.join(os.environ.get("LOCALAPPDATA") or "", "io.github.clash-verge-rev.clash-verge-rev"),
]
MIHOMO_API = os.environ.get("RETRY_PROXY_MIHOMO_API", "http://127.0.0.1:9097")
_NET_TTL = 3.0
_net_cache = {"at": 0.0, "data": None}
_net_lock = threading.Lock()


def _clash_secret():
    """The controller secret Clash Verge generated for this profile."""
    pattern = re.compile(r"(?m)^\s*secret\s*:\s*(.+?)\s*$")
    for base in CLASH_DIRS:
        for name in ("clash-verge.yaml", "config.yaml"):
            path = os.path.join(base, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    match = pattern.search(f.read())
            except OSError:
                continue
            if match:
                return match.group(1).strip().strip('"').strip("'")
    return ""


def _mihomo(path, secret, timeout=1.8):
    req = urllib.request.Request(
        MIHOMO_API.rstrip("/") + path, headers={"Accept": "application/json"}
    )
    if secret:
        req.add_header("Authorization", "Bearer " + secret)
    # Loopback: the system proxy must never be consulted for the controller,
    # or a broken proxy makes the controller itself look unreachable.
    with _direct_opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


GROUPS = ("Selector", "URLTest", "Fallback", "Relay", "LoadBalance")


def _resolve_chain(proxies, name, limit=8):
    """Follow a group down to the node that actually leaves the machine."""
    chain, cur = [], name
    while cur and len(chain) < limit:
        chain.append(cur)
        obj = proxies.get(cur) or {}
        if obj.get("type") not in GROUPS:
            break
        nxt = obj.get("now")
        if not nxt or nxt == cur:
            break
        cur = nxt
    return chain


def _rule_target(rules, host):
    """The rule that would carry this host, and what it points at.

    Domain-shaped rules are matched here. GeoSite and GeoIP rules need the
    database mihomo holds, so they are skipped rather than guessed at; the
    basis field says which rule answered, so a wrong guess stays visible.
    """
    h = (host or "").lower()
    for rule in rules:
        kind = (rule.get("type") or "").lower()
        payload = (rule.get("payload") or "").strip()
        low = payload.lower()
        if kind == "match":
            return payload or "MATCH", "MATCH"
        if kind == "domain" and h == low:
            return rule.get("proxy"), "DOMAIN " + payload
        if kind == "domainsuffix" and (h == low or h.endswith("." + low)):
            return rule.get("proxy"), "DOMAIN-SUFFIX " + payload
        if kind == "domainkeyword" and low and low in h:
            return rule.get("proxy"), "DOMAIN-KEYWORD " + payload
        if kind == "domainregex":
            try:
                if re.search(payload, host or "", re.I):
                    return rule.get("proxy"), "DOMAIN-REGEX " + payload
            except re.error:
                pass
    return None, None


def net_snapshot():
    """Node, group and live connections for the upstream, cached briefly."""
    with _net_lock:
        if _net_cache["data"] and time.time() - _net_cache["at"] < _NET_TTL:
            return _net_cache["data"]

    base = {
        "controller": MIHOMO_API,
        "proxy": UPSTREAM_PROXY or "-",
        "upstream": UPSTREAM,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    secret = _clash_secret()
    try:
        proxies = _mihomo("/proxies", secret).get("proxies", {})
    except Exception as exc:
        out = dict(
            base,
            ok=False,
            reason=("controller unreachable: %s: %s" % (type(exc).__name__, exc))[:220],
        )
        with _net_lock:
            _net_cache["at"] = time.time()
            _net_cache["data"] = out
        return out

    host = urllib.parse.urlsplit(UPSTREAM).hostname or ""

    try:
        mode = (_mihomo("/configs", secret).get("mode") or "rule").lower()
    except Exception:
        mode = "rule"
    try:
        rules = _mihomo("/rules", secret).get("rules") or []
    except Exception:
        rules = []

    # A live connection is the honest answer to "what is on the wire": rules,
    # groups and their defaults can disagree, the connection list cannot. It is
    # also the only place the real chain of nested groups shows up.
    live, live_chain = [], []
    try:
        conns = _mihomo("/connections", secret).get("connections") or []
    except Exception:
        conns = []
    for c in conns:
        meta = c.get("metadata") or {}
        if host and host not in (meta.get("host") or ""):
            continue
        chains = c.get("chains") or []
        live.append(
            {
                "host": meta.get("host") or meta.get("destinationIP") or "?",
                "chains": chains,
                "upload": c.get("upload"),
                "download": c.get("download"),
                "start": c.get("start"),
            }
        )
        if not live_chain and chains:
            live_chain = list(chains)

    matched_rule = None
    if live_chain:
        chain = list(reversed(live_chain))  # group .. node
        basis = "live connection"
    else:
        entry = None
        if mode == "global":
            entry, basis = "GLOBAL", "mode global"
        elif mode == "direct":
            entry, basis = "DIRECT", "mode direct"
        else:
            entry, matched_rule = _rule_target(rules, host)
            basis = matched_rule or "no rule matched"
        if not entry:
            # Nothing matched: fall back to the group that looks meant for it.
            for name, obj in proxies.items():
                if (
                    isinstance(obj, dict)
                    and obj.get("type") == "Selector"
                    and "opencode" in name.lower()
                ):
                    entry, basis = name, "group name"
                    break
        chain = _resolve_chain(proxies, entry) if entry else []

    node = chain[-1] if chain else None
    group = chain[0] if len(chain) > 1 else None
    node_obj = proxies.get(node) or {}
    group_obj = proxies.get(group) or {}
    history = node_obj.get("history") or []

    # The same classification the Clash Verge extension script uses to put
    # residential and preferred nodes first: a node called 家宽 / 住宅 / 原生 is
    # not the same bet as one called 多线-10, and the subscriber cannot tell
    # them apart from the name alone.
    preferred_re = re.compile(r"家宽|住宅|ISP|原生|WARP|MASQUE|Plus|US|美国|SG|新加坡|JP|日本", re.I)
    members = []
    for name in (group_obj.get("all") or [])[:80]:
        obj = proxies.get(name)
        if not isinstance(obj, dict):
            continue
        h = obj.get("history") or []
        members.append(
            {
                "name": name,
                "type": obj.get("type"),
                "delay": h[-1].get("delay") if h else None,
                "preferred": bool(preferred_re.search(name)),
            }
        )

    out = dict(
        base,
        ok=True,
        mode=mode,
        basis=basis,
        rule=matched_rule,
        route=SETTINGS["force_route"] or "auto",
        group=group,
        node=node,
        node_type=node_obj.get("type"),
        node_preferred=bool(preferred_re.search(node or "")),
        chain=chain,
        candidates=len(group_obj.get("all") or []),
        delay_ms=history[-1].get("delay") if history else None,
        history=[{"time": h.get("time"), "delay": h.get("delay")} for h in history[-6:]],
        live=live[:5],
        members=members,
        groups=[
            {"name": n, "now": o.get("now"), "count": len(o.get("all") or [])}
            for n, o in proxies.items()
            if isinstance(o, dict) and o.get("type") in GROUPS
        ][:12],
    )
    with _net_lock:
        _net_cache["at"] = time.time()
        _net_cache["data"] = out
    return out

def tail_log(limit=200, cls=None, q=None):
    """The newest records from the JSONL log, newest first.

    The live SSE feed only has what arrived while the page was open; a refusal
    that happened before the tab was loaded is exactly the one worth reading,
    so the file is the source for history.
    """
    try:
        limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        limit = 200
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=limit * 6)
    except FileNotFoundError:
        # A freshly started instance has never logged anything. That is an empty
        # history, not a broken dashboard: the page should say so and keep polling.
        return {
            "ok": True,
            "path": LOG_PATH,
            "count": 0,
            "records": [],
            "note": "no requests logged yet",
        }
    except OSError as exc:
        return {"ok": False, "reason": str(exc) or "log file not readable", "path": LOG_PATH, "records": []}

    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        trail = rec.get("attempts") or []
        if cls == "!OK":
            if trail and trail[-1] == "OK":
                continue
        elif cls and cls not in trail:
            continue
        if q and q.lower() not in json.dumps(rec, ensure_ascii=False).lower():
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return {"ok": True, "path": LOG_PATH, "count": len(out), "records": out}


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
            # A handover that fails silently is worse than one that fails: the
            # button says it started and nothing happens. Keep its output.
            log_path = os.path.join(here, "restart.log")
            ps = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
            )
            if not os.path.exists(ps):
                ps = "powershell"
            try:
                out = open(log_path, "ab")
                out.write(
                    ("\n=== %s restart requested by dashboard (pid %d) ===\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid())).encode()
                )
                out.flush()
                subprocess.Popen(
                    [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Handover"],
                    cwd=here, creationflags=0x08000000, close_fds=True,
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                )
            except Exception as exc:
                return self._send_json(
                    {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500
                )
            return self._send_json(
                {"ok": True, "action": "restart", "log": log_path,
                 "note": "zero-downtime handover started"}
            )
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
                    "note": "run start-retry-proxy.bat --force to start it again",
                }
            )
            threading.Thread(
                target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True
            ).start()
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
            if path == "/__net":
                return self._send_json(net_snapshot())
            if path == "/__logs":
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                return self._send_json(
                    tail_log(
                        (qs.get("limit") or ["200"])[0],
                        (qs.get("cls") or [None])[0],
                        (qs.get("q") or [None])[0],
                    )
                )
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
            if path == "/__control":
                n = int(self.headers.get("Content-Length") or 0)
                return self._control(self.rfile.read(n) if n else b"")
            if path == "/":
                return self._send_json({
                    "service": "opencode retry proxy",
                    "version": VERSION,
                    "dashboard": "/__ui",
                    "stats": "/__stats",
                    "net": "/__net",
                    "logs": "/__logs",
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
        detail = []
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
                        detail.append({"n": attempt + 1, "cls": "EMPTY_STREAM", "status": 200})
                        try:
                            resp.close()
                        except Exception:
                            pass
                        if attempt + 1 < max_attempts:
                            time.sleep(backoff(attempt, "EMPTY_STREAM"))
                            continue
                        break
                    trail.append("OK")
                    detail.append({"n": attempt + 1, "cls": "OK", "status": 200})
                    streamed = True
                    break

                last_body = resp.read()
                cls = classify(status, last_body)
                trail.append(cls)
                last_status = status
                hint = retry_after_seconds(resp.headers.get("Retry-After"))
                detail.append(
                    {
                        "n": attempt + 1,
                        "cls": cls,
                        "status": status,
                        "retry_after": hint,
                        "snippet": snippet(last_body),
                    }
                )
                if attempt + 1 < max_attempts and should_retry(cls, status):
                    pause(attempt, cls, hint)
                    continue
                break
            except urllib.error.HTTPError as e:
                last_status = e.code
                try:
                    last_body = e.read()
                except Exception:
                    last_body = b""
                if not last_body:
                    # A refusal sometimes carries its reason on e.fp rather than
                    # through read(): an empty snippet turns a diagnosable 404 or
                    # 401 into a bare status code in the log.
                    try:
                        last_body = e.fp.read() or b""
                    except Exception:
                        last_body = b""
                cls = classify(last_status, last_body)
                trail.append(cls)
                hint = retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
                detail.append(
                    {
                        "n": attempt + 1,
                        "cls": cls,
                        "status": last_status,
                        "retry_after": hint,
                        "snippet": snippet(last_body),
                    }
                )
                if attempt + 1 < max_attempts and should_retry(cls, last_status):
                    pause(attempt, cls, hint)
                    continue
                break
            except Exception as e:
                trail.append(f"ERR:{type(e).__name__}")
                detail.append(
                    {"n": attempt + 1, "cls": f"ERR:{type(e).__name__}", "err": str(e)[:200]}
                )
                last_status = None
                last_body = b""
                if not route_direct and _proxy_refused(e):
                    # mihomo is down or its port moved: stop paying for it.
                    route_direct = True
                    trail.append("ROUTE:direct")
                    detail.append({"n": attempt + 1, "cls": "ROUTE:direct"})
                    time.sleep(backoff(attempt, "ERR"))
                    continue
                if attempt + 1 < max_attempts:
                    pause(attempt, "ERR")
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
                # The last status is what the client got; the detail list is what
                # the upstream said on the way there. Four attempts is enough to
                # recognise a pattern without turning the log into a transcript.
                "status": last_status,
                "detail": detail[-4:],
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


def _port_is_healthy():
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
    if _port_is_healthy():
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
        if e.errno in (48, 98, 10048) and _port_is_healthy():
            print("retry proxy already running; exiting")
            raise SystemExit(0)
        _record_fatal(e)
        raise
    except Exception:
        import traceback

        _record_fatal(traceback.format_exc())
        raise
