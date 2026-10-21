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
    GET  /__pool               candidate members (mihomo group + DIRECT), L0
    GET  /__route              ledger, health and the member in use, L1-L3
    POST /__route              {"action": "pin|strategy|clear|switch"}
    GET  /__health             {"ok": true}

Durable state
-------------
    settings.json beside the running copy holds SETTINGS and ROUTE, written
    atomically on every /__control change and read at import, so a reboot no
    longer drops force_route back to auto. Unknown keys in that file survive
    a load/save round trip: the route block is meant to grow.
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
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# ---------------------------------------------------------------------------
# Pointing the client at this instance
#
# The client config carries one api override per model, and opencode honours
# no other form: a provider level `api` or `options.baseURL` on the built-in
# "opencode" provider is ignored, so a request without a per-model override
# goes straight to opencode.ai and never reaches the proxy. Measured on the
# live box (E-013).
#
# So the port cannot live in the config as a constant. It lives in exactly one
# place - a state file next to the client config - and the running instance
# writes it. Every other copy in the client config is rewritten from that one
# value, so switching instances is a write, not an edit of 44 lines.
# ---------------------------------------------------------------------------
CLIENT_CONFIG_DIR = os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".config", "opencode"
)
CLIENT_TARGET_FILE = os.environ.get(
    "RETRY_PROXY_CLIENT_TARGET", os.path.join(CLIENT_CONFIG_DIR, "retry-proxy-target.json")
)
CLIENT_CONFIG_FILE = os.environ.get(
    "RETRY_PROXY_CLIENT_CONFIG", os.path.join(CLIENT_CONFIG_DIR, "opencode.json")
)
# The api override shape this rewrites. Only these are touched, so a config
# that also points at some other service is left alone.
_CLIENT_API_RE = re.compile(
    r'("api"\s*:\s*")http://127\.0\.0\.1:\d+/v1(")'
)
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

# ---------------------------------------------------------------------------
# Durable settings (L0 of the route rework)
#
# SETTINGS used to live in memory only, so a reboot silently dropped the route
# back to auto and the 429s that direct mode had been avoiding came back with
# no visible cause. SETTINGS and ROUTE are now mirrored into settings.json
# beside the running copy, written atomically and read at import.
#
# The route block is deliberately a superset of what this build enforces:
# pool and select.pin are read, strategy / rotate_on / probe / persist are
# carried untouched for the layers that come later. Unknown keys from a newer
# or hand-edited file are preserved verbatim, so a downgrade never eats a
# field it does not understand.
# ---------------------------------------------------------------------------
SETTINGS_PATH = os.path.join(HERE, "settings.json")
SETTINGS_SCHEMA = 1

DEFAULT_ROUTE = {
    "pool": {
        # None = discover the selector that actually carries the upstream,
        # instead of pinning a group name in code: the subscription is
        # replaced often and a second group may appear.
        "mihomo_group": None,
        "extra_members": ["DIRECT"],
        "refresh_s": 30,
    },
    "select": {
        "strategy": "least_fail",
        "pin": None,
        # 1, not 3: controller.PERSISTENT_MIN_FAILURES is 1, and the backtest
        # found that waiting for a burst of three only ever caught sessions
        # that had already recovered.
        "threshold": 1,
        "window_s": 600,
        "cooldown_s": 120,
        "decay_half_life_s": 3600,
        # Whole families die together. Measured on the live box: 96 of the 216
        # testable members never answer in any sweep, and they are not spread
        # out but clustered (66 优选域名 + 30 IPv6优选). Cooling one member at a
        # time walks straight through the family, so after
        # family_threshold distinct members of one family fail inside window_s,
        # the family is skipped as a unit. This is also what keeps a fresh
        # batch of cfnew Workers from costing three dead attempts each.
        "family_threshold": 3,
        "family_cooldown_s": 900,
    },
    # rotate_on mirrors controller.SWITCH_CLASSES, the backtested set that may
    # move an exit IP. It is deliberately small: AUTH_IP_REPUTATION belongs to
    # the per-session layer (a model switch measured 8/8 against 33-83% for a
    # node switch), RATE_LIMIT means back off rather than rotate, and NET_TLS /
    # LOCAL_RELAY are not node problems. test_l0_settings.py pins the two sets
    # together so they cannot drift apart.
    "rotate_on": ["NET_CONNECT", "NET_DNS", "NET_STREAM", "NET_TIMEOUT"],
    "fallback": ["proxy", "direct"],
    "probe": {
        "enabled": True,
        # auto=false: a 218 node sweep is two hundred delay tests, so it stays
        # a deliberate action (the panel button / __net/probe?start=1) instead
        # of a minute-by-minute habit. Whenever a run does happen, the router
        # uses its verdicts until they are stale_s old.
        "auto": False,
        "stale_s": 300,
        "interval_s": 60,
        "target": "http://cp.cloudflare.com/generate_204",
        "timeout_s": 5,
    },
    "persist": {"path": "route_state.json", "fsync_s": 30},
}

ROUTE = json.loads(json.dumps(DEFAULT_ROUTE))  # deep copy, no shared lists


def _deep_merge(base, over):
    """over wins, but a dict in over merges into the dict in base."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_settings_lock = threading.Lock()
_settings_raw = {}


def _coerce_settings(src):
    """Apply the same clamps /__control applies, so a hand-edited file cannot
    put the proxy into a state the dashboard could never set."""
    if not isinstance(src, dict):
        return
    try:
        if "attempts" in src:
            SETTINGS["attempts"] = max(1, min(10, int(src["attempts"])))
        if "timeout" in src:
            SETTINGS["timeout"] = max(5.0, min(900.0, float(src["timeout"])))
        if isinstance(src.get("backoff"), list) and src["backoff"]:
            SETTINGS["backoff"] = [
                max(0.0, min(30.0, float(x))) for x in src["backoff"]
            ][:6]
        if "paused" in src:
            SETTINGS["paused"] = bool(src["paused"])
        if "force_route" in src:
            v = src["force_route"]
            SETTINGS["force_route"] = v if v in ("proxy", "direct") else None
    except (TypeError, ValueError):
        pass


def _load_settings():
    global _settings_raw
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    with _settings_lock:
        _settings_raw = raw
    _coerce_settings(raw.get("settings") or {})
    route = raw.get("route")
    if isinstance(route, dict):
        ROUTE.clear()
        ROUTE.update(_deep_merge(DEFAULT_ROUTE, route))


def _route_diff(node, base):
    """Only what differs from the defaults.

    A full dump would freeze today's defaults into the file, so a later
    corrected default (say, which failure classes may move an exit IP) would
    never reach an instance that already had a settings file. Storing the diff
    keeps a changed default live while still honouring an explicit choice.
    """
    out = {}
    for k, v in node.items():
        if k not in base:
            out[k] = v
        elif isinstance(v, dict) and isinstance(base[k], dict):
            sub = _route_diff(v, base[k])
            if sub:
                out[k] = sub
        elif v != base[k]:
            out[k] = v
    return out


def save_settings(reason="control"):
    """Write SETTINGS + ROUTE atomically. Returns (ok, error)."""
    with _settings_lock:
        raw = dict(_settings_raw)
        raw["schema"] = SETTINGS_SCHEMA
        # "paused" is a session switch, not a preference: a forgotten pause
        # that survived a reboot would look exactly like the bug this layer
        # fixes, the proxy quietly not retrying.
        persisted = dict(SETTINGS)
        persisted.pop("paused", None)
        persisted["backoff"] = list(SETTINGS["backoff"])
        raw["settings"] = persisted
        raw["route"] = _route_diff(ROUTE, DEFAULT_ROUTE)
        raw["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        raw["saved_by"] = reason
        tmp = SETTINGS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_PATH)
            _settings_raw.clear()
            _settings_raw.update(raw)
            return True, None
        except OSError as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)


_load_settings()

STARTED_TS = time.time()


# ---------------------------------------------------------------------------
# Client pointing
# ---------------------------------------------------------------------------
def client_base_url(port=None):
    return "http://%s:%d/v1" % (HOST, int(port or PORT))


def _client_config_files():
    """Every opencode config that may carry an api override.

    The global one is the usual place. OpenChamber runs opencode with its own
    managed config, and a project can carry one too, so all of them are
    offered for rewriting.
    """
    out = []
    for path in (CLIENT_CONFIG_FILE, os.environ.get("OPENCODE_CONFIG") or ""):
        if path and path not in out:
            out.append(path)
    cwd = os.getcwd()
    for _ in range(3):
        path = os.path.join(cwd, "opencode.json")
        if path not in out:
            out.append(path)
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
    return [p for p in out if p and os.path.isfile(p)]


def read_client_target():
    """What the client is currently pointed at, according to the state file."""
    try:
        with open(CLIENT_TARGET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _rewrite_client_config(path, base_url):
    """Point every local api override in one config at base_url.

    Returns (changed, count, error). The file is only written when the
    rewrite leaves valid JSON behind, so a config the proxy cannot understand
    is never damaged.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        return False, 0, "%s: %s" % (type(exc).__name__, exc)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return False, 0, "not utf-8: %s" % exc

    def sub(m):
        return m.group(1) + base_url + m.group(2)

    new_text, count = _CLIENT_API_RE.subn(sub, text)
    if not count:
        return False, 0, None
    try:
        json.loads(new_text)
    except ValueError as exc:
        return False, 0, "rewrite would not be valid json: %s" % exc
    # Keep the original byte order mark and line endings: the file is the
    # client's, not ours.
    prefix = raw[:3] if raw[:3] == b"\xef\xbb\xbf" else b""
    body = new_text.encode("utf-8")
    if b"\r\n" in raw and b"\r\n" not in body:
        body = body.replace(b"\n", b"\r\n")
    try:
        with open(path, "wb") as f:
            f.write(prefix + body)
    except OSError as exc:
        return False, 0, "%s: %s" % (type(exc).__name__, exc)
    return True, count, None


def point_client(port=None, reason="control"):
    """Tell the client to talk to this instance.

    The instance reports its own port; nothing here knows a port number.
    """
    base_url = client_base_url(port)
    files = _client_config_files()
    changed, total, errors = [], 0, []
    for path in files:
        ok, count, err = _rewrite_client_config(path, base_url)
        if err:
            errors.append({"file": path, "error": err})
        if ok:
            changed.append({"file": path, "overrides": count})
            total += count
    state = {
        "base_url": base_url,
        "port": int(port or PORT),
        "version": VERSION,
        "pid": os.getpid(),
        "reason": reason,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "configs": changed,
    }
    try:
        tmp = CLIENT_TARGET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CLIENT_TARGET_FILE)
    except OSError as exc:
        errors.append({"file": CLIENT_TARGET_FILE,
                       "error": "%s: %s" % (type(exc).__name__, exc)})
    return {
        "ok": not errors,
        "base_url": base_url,
        "port": int(port or PORT),
        "overrides": total,
        "configs": changed,
        "errors": errors,
        "target_file": CLIENT_TARGET_FILE,
    }


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
        out["route"] = json.loads(json.dumps(ROUTE))
        out["settings_path"] = SETTINGS_PATH
        out["settings_schema"] = SETTINGS_SCHEMA
        # Where the client is pointed right now, and where it would be pointed
        # if it followed this instance. A mismatch is the one thing that makes
        # "the router is up but nothing happens" obvious.
        target = read_client_target()
        out["client_target"] = target
        out["client_base_url"] = client_base_url()
        out["client_matches"] = bool(
            target and target.get("base_url") == out["client_base_url"]
        )
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


def _mihomo_send(path, secret, method="PUT", payload=None, timeout=6.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        MIHOMO_API.rstrip("/") + path, data=data, method=method,
        headers={"Accept": "application/json"},
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("Authorization", "Bearer " + secret)
    with _direct_opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}


_PROBE_WORKERS = 16
_probe_lock = threading.Lock()
_probe_state = {
    "running": False,
    "group": None,
    "timeout_ms": 5000,
    "total": 0,
    "done": 0,
    "alive": 0,
    "dead": 0,
    "started_at": 0.0,
    "finished_at": 0.0,
    "nodes": {},
}


def _probe_run(names, timeout_ms):
    """Measure every candidate on its own, and give every one a verdict.

    mihomo's group delay endpoint answers only for the members that replied:
    a node that times out is left out of the response instead of being
    reported as a timeout. On this machine the same group answered 48, 52, 58,
    60 and 62 entries across five runs, so "missing" and "dead" were
    indistinguishable, and 160 broken candidates looked untested. Asking each
    node separately is the only way to give every candidate a verdict.
    """
    secret = _clash_secret()
    url = urllib.parse.quote("https://opencode.ai", safe="")

    def one(name):
        path = "/proxies/%s/delay?url=%s&timeout=%d" % (
            urllib.parse.quote(name, safe=""), url, timeout_ms,
        )
        try:
            raw = _mihomo(path, secret, timeout=(timeout_ms / 1000.0) + 15.0)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:120]
            except Exception:
                pass
            return name, {
                "delay": None,
                "dead": True,
                "error": "timeout" if exc.code == 504 else (detail or "HTTP %d" % exc.code),
            }
        except Exception as exc:
            return name, {"delay": None, "dead": True,
                          "error": "%s: %s" % (type(exc).__name__, exc)}
        delay = raw.get("delay") if isinstance(raw, dict) else None
        if not isinstance(delay, int):
            return name, {"delay": None, "dead": True, "error": "no delay in response"}
        # mihomo reports the timeout value itself when the probe failed.
        return name, {"delay": delay, "dead": delay >= timeout_ms, "error": None}

    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
        futures = {pool.submit(one, n): n for n in names}
        for fut in as_completed(futures):
            try:
                name, row = fut.result()
            except Exception as exc:
                name = futures[fut]
                row = {"delay": None, "dead": True, "error": str(exc)}
            with _probe_lock:
                _probe_state["nodes"][name] = row
                _probe_state["done"] += 1
                if row["dead"]:
                    _probe_state["dead"] += 1
                else:
                    _probe_state["alive"] += 1
    with _probe_lock:
        _probe_state["running"] = False
        _probe_state["finished_at"] = time.time()


def net_probe(timeout_ms=5000, start=True):
    """Measure every candidate in the live group against the upstream itself.

    `/proxies` only carries whatever latency the client happened to record, and
    for a node that is currently selected and slow that history lags badly.
    This asks mihomo to test each candidate against opencode.ai right now,
    which is the only number that answers "which line should this be using".

    A group this size takes tens of seconds to measure, so the run happens in
    the background: start=True kicks it off, start=False reports progress.
    """
    try:
        timeout_ms = max(1000, min(int(timeout_ms), 15000))
    except (TypeError, ValueError):
        timeout_ms = 5000
    snap = net_snapshot() or {}
    group = snap.get("group")
    if not group:
        return {"ok": False, "reason": snap.get("reason") or "no group resolved",
                "group": None, "running": False, "nodes": []}

    members = snap.get("members") or []
    # Only a leaf proxy can be measured on its own; a nested group and DIRECT
    # answer 504 to a per-node delay test and would look broken.
    leaves = [m["name"] for m in members
              if m.get("type") not in GROUPS and m.get("type") != "Direct"]

    with _probe_lock:
        busy = _probe_state["running"] and _probe_state["group"] == group
    if start and not busy:
        with _probe_lock:
            _probe_state.update({
                "running": True, "group": group, "timeout_ms": timeout_ms,
                "total": len(leaves), "done": 0, "alive": 0, "dead": 0,
                "started_at": time.time(), "finished_at": 0.0, "nodes": {},
            })
        threading.Thread(target=_probe_run, args=(leaves, timeout_ms), daemon=True).start()

    with _probe_lock:
        st = dict(_probe_state)
        measured = dict(_probe_state["nodes"])
    rows = []
    for name, row in measured.items():
        rows.append({
            "name": name,
            "delay": row.get("delay"),
            "dead": bool(row.get("dead")),
            "error": row.get("error"),
            "current": name == snap.get("node"),
        })
    rows.sort(key=lambda r: (r["dead"], r["delay"] if r["delay"] is not None else 10 ** 9))
    st.update({
        "ok": True,
        "group": group,
        "node": snap.get("node"),
        "tested": st.get("done"),
        "candidates": len(members),
        "nodes": rows,
    })
    return st


def net_switch(payload, group=None):
    """Point the group at one node, then report the chain that results."""
    try:
        data = json.loads(payload or b"{}")
    except Exception:
        return {"ok": False, "reason": "bad json"}
    node = (data.get("node") or "").strip()
    group = (data.get("group") or group or "").strip()
    if not node:
        return {"ok": False, "reason": "node is required"}
    snap = net_snapshot() or {}
    group = group or snap.get("group")
    if not group:
        return {"ok": False, "reason": snap.get("reason") or "group unknown"}
    try:
        _mihomo_send(
            "/proxies/%s" % urllib.parse.quote(group, safe=""),
            _clash_secret(), "PUT", {"name": node},
        )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return {"ok": False, "group": group, "node": node,
                "reason": "controller returned %s %s" % (exc.code, body)}
    except Exception as exc:
        return {"ok": False, "group": group, "node": node,
                "reason": "%s: %s" % (type(exc).__name__, exc)}
    _net_cache.update({"at": 0.0, "data": None})
    return {"ok": True, "group": group, "node": node, "net": net_snapshot()}


GROUPS = ("Selector", "URLTest", "Fallback", "Relay", "LoadBalance")


def _node_family(name):
    """The family a node name belongs to: 优选域名 / 多线 / 电信 / 移动 / 联通.

    The subscriber's own Clash script sorts on a "preferred" regex
    (家宽/住宅/ISP/原生/...) that matches nothing in this subscription, so the
    name family is the only signal left that separates a Cloudflare-fronted
    优选域名 node from a plain carrier 电信 node. Everything before the
    trailing -NN is the family.
    """
    base = re.sub(r"-\d+$", "", name or "").strip()
    # Drop leading emoji and other decoration, keep the readable label.
    base = re.sub(r"^[^\w\u4e00-\u9fff]+", "", base).strip()
    return base or (name or "")


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


# ---------------------------------------------------------------------------
# Pool discovery (L0)
#
# The pool is the mihomo selector that carries the upstream, plus a DIRECT
# pseudo member, so "direct vs proxy" stops being a special case and becomes
# one candidate among many. Members are found at runtime: a new subscription,
# or twenty new cfnew Workers added to the group, appear here with no code
# change. ROUTE["pool"]["mihomo_group"] overrides discovery for the odd setup
# where the selector has no OpenCode rule and no recognisable name.
# ---------------------------------------------------------------------------
_pool_cache = {"at": 0.0, "data": None}
_pool_lock = threading.Lock()


def _pool_group(proxies, secret, host):
    """(group_name, basis) for the selector that carries the upstream."""
    wanted = ROUTE["pool"].get("mihomo_group")
    if isinstance(wanted, str) and wanted and wanted in proxies:
        return wanted, "configured"

    # A live connection is the honest answer: rules, groups and their defaults
    # can disagree, the connection list cannot.
    try:
        conns = _mihomo("/connections", secret).get("connections") or []
    except Exception:
        conns = []
    for c in conns:
        meta = c.get("metadata") or {}
        if host and host not in (meta.get("host") or ""):
            continue
        chains = c.get("chains") or []
        if chains:
            return chains[-1], "live connection"

    try:
        mode = (_mihomo("/configs", secret).get("mode") or "rule").lower()
    except Exception:
        mode = "rule"
    if mode == "global":
        return "GLOBAL", "mode global"
    if mode == "direct":
        return "DIRECT", "mode direct"

    try:
        rules = _mihomo("/rules", secret).get("rules") or []
    except Exception:
        rules = []
    entry, _ = _rule_target(rules, host)
    if entry and entry in proxies:
        return entry, "rule"

    for name, obj in proxies.items():
        if (
            isinstance(obj, dict)
            and obj.get("type") == "Selector"
            and "opencode" in name.lower()
        ):
            return name, "group name"
    return None, "not found"


def discover_pool(force=False):
    """Candidate members, with the DIRECT pseudo member included."""
    with _pool_lock:
        ttl = max(5, int(ROUTE["pool"].get("refresh_s") or 30))
        if (
            not force
            and _pool_cache["data"]
            and time.time() - _pool_cache["at"] < ttl
        ):
            return _pool_cache["data"]

    host = urllib.parse.urlsplit(UPSTREAM).hostname or ""
    secret = _clash_secret()
    out = {
        "ok": False,
        "group": None,
        "basis": None,
        "members": [],
        "count": 0,
        "controller": MIHOMO_API,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        proxies = _mihomo("/proxies", secret).get("proxies", {})
    except Exception as exc:
        out["reason"] = (
            "controller unreachable: %s: %s" % (type(exc).__name__, exc)
        )[:220]
        with _pool_lock:
            _pool_cache["at"] = time.time()
            _pool_cache["data"] = out
        return out

    group, basis = _pool_group(proxies, secret, host)
    out["group"], out["basis"] = group, basis
    names = []
    if group and isinstance(proxies.get(group), dict):
        names = list(proxies[group].get("all") or [])
    for extra in ROUTE["pool"].get("extra_members") or []:
        if extra not in names:
            names.append(extra)

    members = []
    for name in names:
        obj = proxies.get(name)
        if obj is None and name == "DIRECT":
            # DIRECT is a pseudo member of the pool. mihomo always lists it,
            # but the pool must not lose its "bypass everything" candidate
            # because a controller response omitted it.
            obj = {"type": "Direct"}
        if not isinstance(obj, dict):
            continue
        history = obj.get("history") or []
        members.append(
            {
                "name": name,
                "type": obj.get("type"),
                "family": _node_family(name),
                # A nested group and DIRECT cannot be delay tested on their
                # own: mihomo answers 504 for both.
                "probeable": (
                    obj.get("type") not in GROUPS and obj.get("type") != "Direct"
                ),
                "history_delay": history[-1].get("delay") if history else None,
            }
        )
    out["members"] = members
    out["count"] = len(members)
    # `now` rides along on the same cached response: the router needs to know
    # which member is actually in use to account for it, and a second call per
    # attempt would be pure overhead.
    if group:
        obj = proxies.get(group) or {}
        out["group_type"] = obj.get("type")
        out["now"] = obj.get("now")
    out["ok"] = bool(group and members)
    with _pool_lock:
        _pool_cache["at"] = time.time()
        _pool_cache["data"] = out
    return out


# ---------------------------------------------------------------------------
# Route ledger, health, strategies (L1-L3)
#
# L0 gave the pool an address: a member can be named. This part decides which
# member a request leaves through, remembers how each one behaved, and moves
# off one that stops working.
#
# Three sources of truth, in order of authority:
#
#   1. select.pin        an explicit member, including DIRECT. Wins over all.
#   2. SETTINGS.force_route  the older two-value switch, still honoured:
#      "direct" means the DIRECT member, "proxy" means "let mihomo decide".
#   3. select.strategy   pick from the healthy candidates and stay there until
#      the member is cooled down, then pick again.
#
# Health has two layers and they are deliberately not merged:
#
#   L1 probe verdict   a member the delay test could not reach is skipped
#                      until the verdict goes stale (probe.stale_s). This
#                      catches a node that is simply gone, cheaply.
#   L2 real outcomes   a request that failed with a rotate_on class cools the
#                      member down for cooldown_s. This catches a node that
#                      answers but is rejected upstream, which no delay test
#                      can see.
#
# Weights decay with a half life, so a member that was bad an hour ago is not
# still being punished, and a node added five minutes ago (a fresh cfnew
# Worker, say) starts from a neutral score rather than from the pool average.
# ---------------------------------------------------------------------------
DIRECT = "DIRECT"

ROUTE_STATE_PATH = os.path.join(HERE, ROUTE["persist"].get("path") or "route_state.json")

_route_state = {
    "nodes": {},        # member name -> ledger entry
    "families": {},     # node family -> whole-batch health, see family_threshold
    "current": None,    # the member in use, None after a cooldown
    "switches": 0,
    "rr": 0,
    "last_switch": None,
}
_route_lock = threading.Lock()
_route_dirty = [False]
_route_saved_at = [0.0]


def _decay(entry, now=None):
    """Halve the accumulated failures once per decay_half_life_s."""
    now = now if now is not None else time.time()
    half_life = max(60.0, float(ROUTE["select"].get("decay_half_life_s") or 3600))
    last = entry.get("decay_ts")
    if not isinstance(last, (int, float)):
        # A brand new entry starts its clock now rather than inheriting age.
        entry["decay_ts"] = now
        return entry
    dt = now - last
    if dt <= 0:
        return entry
    factor = 0.5 ** (dt / half_life)
    entry["fail"] = round(float(entry.get("fail") or 0.0) * factor, 6)
    by_class = entry.get("fail_by_class") or {}
    for cls in list(by_class):
        by_class[cls] = round(float(by_class[cls]) * factor, 6)
    entry["decay_ts"] = now
    return entry


def _entry(name):
    """The ledger row for a member, created neutral on first sight."""
    entry = _route_state["nodes"].get(name)
    if entry is None:
        entry = {
            "ok": 0,
            "fail": 0.0,
            "fail_by_class": {},
            "last_ok_ts": None,
            "last_fail_ts": None,
            "ewma_ms": None,
            "cooldown_until": 0.0,
            "decay_ts": time.time(),
        }
        _route_state["nodes"][name] = entry
    return entry


def _probe_verdict(name):
    """(dead, delay) from the last probe run, or (None, None) if it is stale."""
    stale = max(30.0, float(ROUTE["probe"].get("stale_s") or 300))
    with _probe_lock:
        state = dict(_probe_state)
    finished = state.get("finished_at") or 0.0
    if not finished or time.time() - finished > stale:
        return None, None
    row = (state.get("nodes") or {}).get(name) or {}
    return bool(row.get("dead")), row.get("delay")


def member_health(name, now=None, families=None):
    now = now if now is not None else time.time()
    family = _node_family(name)
    with _route_lock:
        # Read only. Routing status walks every member of a 218 node pool, and
        # creating a ledger row for each one turned "show me the ledger" into
        # "write 218 rows", which also made /__route's clear action look like
        # it had not worked.
        entry = _route_state["nodes"].get(name)
        if entry is not None:
            _decay(entry, now)
        else:
            entry = {}
        cooling = float(entry.get("cooldown_until") or 0.0) > now
        if families is None:
            # Single lookup: fine for one member, but a caller walking the
            # whole pool should pass a _family_snapshot() instead.
            fam = _family_row(family, now)
        else:
            fam = families.get(family) or {"cooling": False, "recent": 0}
        row = {
            "ok": entry.get("ok", 0),
            "fail": entry.get("fail", 0.0),
            "fail_by_class": dict(entry.get("fail_by_class") or {}),
            "last_ok_ts": entry.get("last_ok_ts"),
            "last_fail_ts": entry.get("last_fail_ts"),
            "ewma_ms": entry.get("ewma_ms"),
            "last_raw_class": entry.get("last_raw_class"),
            "cooldown_until": entry.get("cooldown_until") or 0.0,
        }
    dead, delay = _probe_verdict(name)
    row.update({
        "name": name,
        "family": family,
        "cooling": cooling,
        "cooldown_s": max(0, int(row["cooldown_until"] - now)),
        # A family of one (a lone cfnew Worker, a node named without a -NN
        # suffix) has no siblings to condemn it, so it is never family-cooled.
        "family_cooling": bool(fam["cooling"]) and family != name,
        "family_recent_fails": fam["recent"],
        "probe_dead": bool(dead),
        "delay": delay,
        "ok_rate": round((row["ok"] + 1) / (row["ok"] + row["fail"] + 2), 4),
    })
    return row


def _family_row(family, now=None):
    now = now if now is not None else time.time()
    fam = (_route_state.get("families") or {}).get(family) or {}
    window = max(60.0, float(ROUTE["select"].get("window_s") or 600))
    recent = 0
    for name, entry in (_route_state.get("nodes") or {}).items():
        if _node_family(name) != family:
            continue
        last = entry.get("last_fail_ts")
        if isinstance(last, (int, float)) and now - last <= window:
            recent += 1
    return {
        "cooling": float(fam.get("cooldown_until") or 0.0) > now,
        "cooldown_until": fam.get("cooldown_until") or 0.0,
        "cooldown_s": max(0, int((fam.get("cooldown_until") or 0.0) - now)),
        "fail": fam.get("fail") or 0.0,
        "recent": recent,
    }


def _family_snapshot(now=None):
    """`_family_row` for every family in one pass over the ledger.

    Called on the request path, where a per-member scan would be 218 full
    ledger walks per pick.
    """
    now = now if now is not None else time.time()
    window = max(60.0, float(ROUTE["select"].get("window_s") or 600))
    recent = {}
    for name, entry in (_route_state.get("nodes") or {}).items():
        last = entry.get("last_fail_ts")
        if isinstance(last, (int, float)) and now - last <= window:
            fam = _node_family(name)
            recent[fam] = recent.get(fam, 0) + 1
    out = {}
    for fam_name in set(recent) | set(_route_state.get("families") or {}):
        row = (_route_state.get("families") or {}).get(fam_name) or {}
        until = row.get("cooldown_until") or 0.0
        out[fam_name] = {
            "cooling": float(until) > now,
            "cooldown_until": until,
            "cooldown_s": max(0, int(float(until) - now)),
            "fail": row.get("fail") or 0.0,
            "recent": recent.get(fam_name, 0),
        }
    return out


def _delay_key(member):
    """A sort key for latency: probe first, then what mihomo recorded, then
    anything unknown, which must never win by looking like zero."""
    health = member.get("health") or {}
    for value in (health.get("delay"), member.get("delay"), member.get("history_delay")):
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return float(10 ** 9)


def _strat_least_fail(cands, current):
    # `current` is accepted by every strategy so the registry stays uniform.
    return sorted(cands, key=lambda m: (m["health"]["fail"], _delay_key(m), m["name"]))


def _strat_ewma_ok(cands, current):
    return sorted(cands, key=lambda m: (-m["health"]["ok_rate"], _delay_key(m), m["name"]))


def _strat_lowest_latency(cands, current):
    return sorted(cands, key=lambda m: (_delay_key(m), m["health"]["fail"], m["name"]))


def _strat_round_robin(cands, current):
    if not cands:
        return cands
    start = int(_route_state.get("rr") or 0) % len(cands)
    return cands[start:] + cands[:start]


def _strat_weighted_random(cands, current):
    """Draw without replacement, weight = recent success rate."""
    remaining = list(cands)
    out = []
    while remaining:
        weights = [max(0.05, m["health"]["ok_rate"]) for m in remaining]
        total = sum(weights)
        point = random.random() * total
        acc = 0.0
        for i, weight in enumerate(weights):
            acc += weight
            if point <= acc:
                out.append(remaining.pop(i))
                break
        else:
            out.append(remaining.pop())
    return out


def _rank(order_fn):
    """DIRECT is the safety net, not a preference.

    Every strategy otherwise sorts by health alone, and with no data yet a
    neutral DIRECT ('D' sorts before the emoji names) would win every tie and
    quietly bypass the whole pool. A stable re-sort pushes it behind equals
    without disturbing the strategy's own order.
    """
    def run(cands, current):
        return sorted(order_fn(cands, current), key=lambda m: bool(m.get("direct")))
    return run


STRATEGIES = {
    "least_fail": _rank(_strat_least_fail),
    "ewma_ok": _rank(_strat_ewma_ok),
    "lowest_latency": _rank(_strat_lowest_latency),
    "round_robin": _rank(_strat_round_robin),
    "weighted_random": _rank(_strat_weighted_random),
}
PASSIVE_STRATEGIES = ("off", "none", "mihomo", "manual")


def policy_class(cls, err=None):
    """Log vocabulary -> policy vocabulary.

    classify() says what the log needed to say (IP_REP, RATE, 5XX);
    controller.py says what may move an exit IP (NET_*). The ledger rotates on
    the second set, and the two do not line up on their own: a probe that
    found the error early read "ERR:URLError", which is in neither set, so the
    four dead IPv6 members it hit were never cooled down and never skipped.

    Unknown classes come back unchanged: recorded, never rotated on. That is
    the safe default, because a wrong rotation moves everyone's exit IP.
    """
    if not cls:
        return "UNKNOWN"
    if cls == "OK":
        return "OK"
    if cls == "EMPTY_STREAM":
        # 200 and then nothing: the gateway accepted the request and the
        # stream died. Observed as a retryable stream failure, not a health
        # signal about the client.
        return "NET_STREAM"
    if cls.startswith("ERR:"):
        # Normalise the exception name and the message into one token stream:
        # "ERR:ConnectionResetError" and "... connection reset ..." are the
        # same failure and must not need two rules. The class name is camel
        # case ("connectionreseterror") and the message is prose ("connection
        # reset"), so compare with punctuation and spaces removed.
        text = re.sub(r"[^a-z0-9]", "", ("%s %s" % (cls[4:], err or "")).lower())
        if ("getaddrinfo" in text or "nameorservicenotknown" in text
                or "nodenameorservname" in text):
            return "NET_DNS"
        if "timeout" in text or "timedout" in text:
            return "NET_TIMEOUT"
        if "certificate" in text or "selfsigned" in text or "certverify" in text:
            return "NET_TLS"
        if ("connectionrefused" in text or "connectionreset" in text
                or "brokenpipe" in text or "connectionaborted" in text):
            return "NET_CONNECT"
        if "ssl" in text or "eof" in text or "handshake" in text:
            # A handshake that dies mid-flight is the node, not our trust
            # store: measured on the dead IPv6 members as
            # UNEXPECTED_EOF_WHILE_READING.
            return "NET_STREAM"
        return cls
    if cls == "IP_REP":
        return "AUTH_IP_REPUTATION"
    if cls == "IDENTITY":
        return "AUTH_FREE_TIER"
    if cls == "RATE":
        return "RATE_LIMIT"
    if cls == "5XX":
        return "UPSTREAM_5XX"
    if cls == "CF_UA":
        return "CLIENT_REQUEST"
    return cls


def _usable(health):
    """A member is usable unless it, or the family it belongs to, is out."""
    return not (health["cooling"] or health["probe_dead"] or health["family_cooling"])


def _healthy_members(pool, now=None):
    """Pool members that are neither cooled down nor declared dead by a probe."""
    now = now if now is not None else time.time()
    families = _family_snapshot(now)
    out = []
    for member in pool.get("members") or []:
        health = member_health(member["name"], now, families=families)
        if not _usable(health):
            continue
        row = dict(member)
        row["health"] = health
        row["direct"] = member["name"] == DIRECT
        out.append(row)
    return out


def _as_route(name, via, pool=None):
    return {
        "name": name,
        "direct": name == DIRECT,
        "via": via,
        "group": (pool or {}).get("group"),
        "group_type": (pool or {}).get("group_type"),
        "now": (pool or {}).get("now"),
    }


def pick_route(force_direct=False):
    """The member this attempt should leave through.

    Called once per attempt, so it must stay cheap: the pool is discovered
    through a cached controller response, and nothing here writes to mihomo.
    """
    pool = discover_pool()
    if force_direct:
        _route_state["current"] = DIRECT
        return _as_route(DIRECT, "local fallback", pool)

    pin = ROUTE["select"].get("pin")
    if isinstance(pin, str) and pin.strip():
        name = pin.strip()
        _route_state["current"] = name
        return _as_route(name, "pin", pool)

    forced = SETTINGS.get("force_route")
    if forced == "direct":
        _route_state["current"] = DIRECT
        return _as_route(DIRECT, "force_route", pool)
    if forced == "proxy":
        # Not a member choice at all: mihomo's own selection stands. The
        # ledger still accounts against whatever it currently points at.
        now_member = pool.get("now") or DIRECT
        _route_state["current"] = now_member
        return _as_route(now_member, "force_route:proxy", pool)

    strategy = str(ROUTE["select"].get("strategy") or "least_fail").lower()
    if strategy in PASSIVE_STRATEGIES:
        now_member = pool.get("now") or DIRECT
        _route_state["current"] = now_member
        return _as_route(now_member, "mihomo", pool)

    if not pool.get("ok"):
        _route_state["current"] = DIRECT
        return _as_route(DIRECT, "no pool: " + str(pool.get("reason") or pool.get("basis")), pool)

    current = _route_state.get("current")
    if not current:
        # Adopt whatever mihomo already uses, when it is healthy. Moving the
        # exit IP with no reason behind it costs every in-flight session, so
        # the first decision of a fresh run should be "stay".
        now_member = pool.get("now")
        known = {m["name"] for m in pool.get("members") or []}
        if now_member and now_member in known:
            health = member_health(now_member)
            if _usable(health):
                _route_state["current"] = now_member
                return _as_route(now_member, "adopted", pool)
    if current:
        health = member_health(current)
        if _usable(health):
            # Sticky on purpose. Switching exit IPs costs every in-flight
            # session, so a member that is working keeps the job.
            return _as_route(current, "sticky", pool)

    candidates = _healthy_members(pool)
    if not candidates:
        _route_state["current"] = DIRECT
        return _as_route(DIRECT, "every candidate is cooling", pool)

    order = STRATEGIES.get(strategy) or _strat_least_fail
    chosen = order(candidates, current)
    name = chosen[0]["name"] if chosen else DIRECT
    # Without this the decision was recomputed on every attempt and the
    # strategy became an implicit round robin: each failure pushed the failed
    # member down the ranking, so the next attempt moved to a fresh node
    # whether or not that was wanted.
    _route_state["current"] = name
    if strategy == "round_robin":
        _route_state["rr"] = int(_route_state.get("rr") or 0) + 1
    return _as_route(name, "strategy:" + strategy, pool)


def apply_route(route):
    """Point the mihomo group at the chosen member, if that is a no-op away.

    A Selector can be pinned, which is the real case here. An automatic group
    (URLTest, Fallback, LoadBalance) re-decides on its own, and fighting it
    would produce a switch every few seconds, so there the only lever the
    router has is the DIRECT member.
    """
    if not route or route.get("direct"):
        return route
    group = route.get("group")
    if not group:
        return route
    if route.get("group_type") not in (None, "Selector"):
        route["apply_error"] = (
            "group %r is a %s and re-selects on its own; only DIRECT can be pinned"
            % (group, route.get("group_type"))
        )
        return route
    if route.get("via") in ("force_route:proxy", "mihomo"):
        return route
    if route.get("via") == "sticky" and route.get("now") == route["name"]:
        # Already where we want to be. Re-applying would be a controller write
        # per request; *not* checking would let the ledger and mihomo drift
        # apart after a restart resets the group's selection.
        return route
    try:
        _mihomo_send(
            "/proxies/%s" % urllib.parse.quote(group, safe=""),
            _clash_secret(),
            "PUT",
            {"name": route["name"]},
        )
    except Exception as exc:
        route["apply_error"] = "%s: %s" % (type(exc).__name__, exc)
        return route
    route["applied"] = True
    _net_cache.update({"at": 0.0, "data": None})
    with _pool_lock:
        if _pool_cache.get("data"):
            _pool_cache["data"]["now"] = route["name"]
    return route


def report_route(route, cls, status=None, elapsed_ms=None, err=None):
    """Record what a member actually did, and cool it down if it failed badly.

    `cls` arrives in the log vocabulary and is translated before it reaches
    the ledger, so rotation and the log agree on what happened.
    """
    name = (route or {}).get("name")
    if not name:
        return
    now = time.time()
    rotate_on = set(ROUTE.get("rotate_on") or [])
    policy = policy_class(cls, err)
    with _route_lock:
        entry = _decay(_entry(name), now)
        if policy == "OK":
            entry["ok"] = int(entry.get("ok") or 0) + 1
            entry["last_ok_ts"] = now
            if elapsed_ms:
                prev = entry.get("ewma_ms")
                entry["ewma_ms"] = (
                    round(float(elapsed_ms), 1) if prev is None
                    else round(float(prev) * 0.7 + float(elapsed_ms) * 0.3, 1)
                )
        else:
            entry["fail"] = round(float(entry.get("fail") or 0.0) + 1.0, 6)
            entry["last_fail_ts"] = now
            entry["last_raw_class"] = cls
            by_class = entry.setdefault("fail_by_class", {})
            by_class[policy] = round(float(by_class.get(policy) or 0.0) + 1.0, 6)
            threshold = max(1, int(ROUTE["select"].get("threshold") or 1))
            if policy in rotate_on and by_class[policy] >= threshold:
                entry["cooldown_until"] = now + max(
                    5.0, float(ROUTE["select"].get("cooldown_s") or 120)
                )
                # Whole families answer or fail together, so count the family
                # too. Without this the router walks the family one member at a
                # time, three dead attempts per family, which is exactly what
                # the live run showed.
                family = _node_family(name)
                fam_cooled = False
                if family and family != name:
                    fam = (_route_state.setdefault("families", {})
                           .setdefault(family, {"fail": 0.0}))
                    fam["fail"] = round(float(fam.get("fail") or 0.0) + 1.0, 6)
                    fam["last_fail_ts"] = now
                    row = _family_row(family, now)
                    fam["cooled_members"] = row["recent"]
                    if row["recent"] >= max(2, int(
                            ROUTE["select"].get("family_threshold") or 3)):
                        fam["cooldown_until"] = now + max(
                            60.0,
                            float(ROUTE["select"].get("family_cooldown_s") or 900),
                        )
                        fam["cooled_at"] = now
                        fam_cooled = True
                if _route_state.get("current") == name:
                    _route_state["current"] = None
                    _route_state["switches"] = int(_route_state.get("switches") or 0) + 1
                _route_state["last_switch"] = {
                    "at": now,
                    "from": name,
                    "family": family,
                    "family_cooled": fam_cooled,
                    "class": policy,
                    "raw_class": cls,
                    "status": status,
                }
        _route_dirty[0] = True
    _route_maybe_save()


def route_for_attempt(force_direct=False):
    return apply_route(pick_route(force_direct=force_direct))


def route_status():
    pool = discover_pool()
    now = time.time()
    families = _family_snapshot(now)
    rows = []
    for member in pool.get("members") or []:
        health = member_health(member["name"], now, families=families)
        health["type"] = member.get("type")
        health["family"] = member.get("family")
        health["current"] = member["name"] == pool.get("now")
        rows.append(health)
    rows.sort(key=lambda r: (r["cooling"], r["family_cooling"], r["probe_dead"],
                             -r["ok_rate"], _delay_key(r)))
    with _route_lock:
        state = {
            "current": _route_state.get("current"),
            "switches": _route_state.get("switches") or 0,
            "last_switch": _route_state.get("last_switch"),
        }
        # Only families the router has an opinion about: a list of all 40-odd
        # families would be noise, and the panel needs the condemned ones.
        families = []
        for fam_name in sorted(_route_state.get("families") or {}):
            row = _family_row(fam_name, now)
            if row["fail"] <= 0 and not row["cooling"]:
                continue
            row["name"] = fam_name
            families.append(row)
    families.sort(key=lambda r: (not r["cooling"], -r["recent"]))
    return {
        "ok": bool(pool.get("ok")),
        "pool": {
            "group": pool.get("group"),
            "group_type": pool.get("group_type"),
            "basis": pool.get("basis"),
            "now": pool.get("now"),
            "count": pool.get("count"),
        },
        "strategy": ROUTE["select"].get("strategy"),
        "pin": ROUTE["select"].get("pin"),
        "rotate_on": list(ROUTE.get("rotate_on") or []),
        "threshold": ROUTE["select"].get("threshold"),
        "cooldown_s": ROUTE["select"].get("cooldown_s"),
        "family_threshold": ROUTE["select"].get("family_threshold"),
        "family_cooldown_s": ROUTE["select"].get("family_cooldown_s"),
        "strategies": sorted(STRATEGIES) + list(PASSIVE_STRATEGIES),
        "state": state,
        "state_path": ROUTE_STATE_PATH,
        # The settings file lives beside the route ledger, and the panel needs
        # both paths to say where the state survives a reboot.
        "settings_path": SETTINGS_PATH,
        "settings_schema": SETTINGS_SCHEMA,
        "families": families,
        "members": rows,
    }


def route_action(payload):
    """Router side of POST /__route: pin, strategy, clear, switch."""
    try:
        data = json.loads(payload or b"{}")
    except Exception:
        return {"ok": False, "reason": "bad json"}
    action = (data.get("action") or "").strip()

    if action == "pin":
        name = (data.get("name") or "").strip()
        ROUTE["select"]["pin"] = name or None
        save_settings("route:pin")
        return {"ok": True, "action": action, "pin": ROUTE["select"]["pin"], "route": route_status()}

    if action == "strategy":
        value = (data.get("value") or "").strip().lower()
        if value not in STRATEGIES and value not in PASSIVE_STRATEGIES:
            return {"ok": False, "reason": "unknown strategy %r" % value,
                    "known": sorted(STRATEGIES) + list(PASSIVE_STRATEGIES)}
        ROUTE["select"]["strategy"] = value
        _route_state["current"] = None
        save_settings("route:strategy")
        return {"ok": True, "action": action, "strategy": value, "route": route_status()}

    if action == "set":
        # The knobs the dashboard exposes. Clamped like /__control does, so a
        # hand-edited request cannot put the router into a state the panel
        # could never set.
        select = ROUTE["select"]
        changed = {}
        for key, lo, hi in (
            ("threshold", 1, 10),
            ("cooldown_s", 5, 3600),
            ("family_threshold", 2, 30),
            ("family_cooldown_s", 60, 7200),
            ("window_s", 60, 86400),
        ):
            if key in data:
                try:
                    value = max(lo, min(hi, int(data[key])))
                except (TypeError, ValueError):
                    continue
                if value != select.get(key):
                    select[key] = value
                    changed[key] = value
        if "decay_half_life_s" in data:
            try:
                value = max(60, min(86400, int(data["decay_half_life_s"])))
                if value != select.get("decay_half_life_s"):
                    select["decay_half_life_s"] = value
                    changed["decay_half_life_s"] = value
            except (TypeError, ValueError):
                pass
        if changed:
            save_settings("route:set")
        return {"ok": True, "action": action, "changed": changed, "route": route_status()}

    if action in ("clear", "reset"):
        with _route_lock:
            _route_state["nodes"] = {}
            _route_state["families"] = {}
            _route_state["current"] = None
            _route_state["switches"] = 0
            _route_state["last_switch"] = None
            _route_dirty[0] = True
        _route_save(force=True)
        return {"ok": True, "action": action, "route": route_status()}

    if action in ("switch", "reset_current"):
        name = (data.get("name") or "").strip()
        if name:
            with _route_lock:
                _route_state["current"] = name
                _entry(name)["cooldown_until"] = 0.0
        else:
            with _route_lock:
                _route_state["current"] = None
        _route_save(force=True)
        route = apply_route(pick_route()) if name else None
        return {"ok": True, "action": action, "applied": route, "route": route_status()}

    return {"ok": False, "reason": "unknown action %r" % action,
            "known": ["pin", "strategy", "set", "clear", "switch", "reset_current"]}


def _route_save(force=False):
    interval = max(1.0, float(ROUTE["persist"].get("fsync_s") or 30))
    now = time.time()
    with _route_lock:
        if not force and not _route_dirty[0]:
            return
        if not force and now - _route_saved_at[0] < interval:
            return
        snapshot = json.loads(json.dumps(_route_state))
        _route_dirty[0] = False
        _route_saved_at[0] = now
    tmp = ROUTE_STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ROUTE_STATE_PATH)
    except OSError:
        pass


def _route_maybe_save():
    _route_save(force=False)


def _route_load():
    try:
        with open(ROUTE_STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    nodes = raw.get("nodes")
    with _route_lock:
        if isinstance(nodes, dict):
            _route_state["nodes"] = {
                k: v for k, v in nodes.items() if isinstance(v, dict)
            }
        for key in ("switches", "rr", "last_switch"):
            if key in raw:
                _route_state[key] = raw[key]
        families = raw.get("families")
        if isinstance(families, dict):
            _route_state["families"] = {
                k: v for k, v in families.items() if isinstance(v, dict)
            }
        _route_state["current"] = raw.get("current")
        for entry in _route_state["nodes"].values():
            _decay(entry)


_route_load()


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
    # them apart from the name alone. On this subscription that regex matches
    # nothing, so the name family is carried as well: 优选域名 / 多线 / 电信 are
    # different bets even when the script calls them all "other".
    preferred_re = re.compile(r"家宽|住宅|ISP|原生|WARP|MASQUE|Plus|US|美国|SG|新加坡|JP|日本", re.I)
    with _probe_lock:
        measured = dict(_probe_state["nodes"])
    members = []
    for name in (group_obj.get("all") or []):
        obj = proxies.get(name)
        if not isinstance(obj, dict):
            continue
        h = obj.get("history") or []
        pr = measured.get(name)
        members.append(
            {
                "name": name,
                "type": obj.get("type"),
                "delay": h[-1].get("delay") if h else None,
                "preferred": bool(preferred_re.search(name)),
                "family": _node_family(name),
                "measured": bool(pr),
                "measured_delay": (pr or {}).get("delay"),
                "dead": bool((pr or {}).get("dead")),
                # A nested group and DIRECT cannot be delay tested on their own:
                # mihomo answers 504 for both. Saying so beats leaving them
                # looking like nodes that were never tried.
                "probeable": obj.get("type") not in GROUPS and obj.get("type") != "Direct",
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
        node_family=_node_family(node or ""),
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
        elif action == "point_client":
            # The client follows whichever instance is alive. The instance
            # reports its own port, so nothing here knows a port number.
            out = point_client(reason="control")
            out["action"] = action
            out["settings"] = dict(SETTINGS)
            return self._send_json(out)
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
        ok, err = save_settings(reason="control:" + action)
        payload = {
            "ok": True,
            "action": action,
            "settings": dict(SETTINGS),
            "settings_path": SETTINGS_PATH,
        }
        if not ok:
            # The change is live either way; saying it did not reach the disk
            # is the difference between "survives a reboot" and "does not".
            payload["persist_error"] = err
        return self._send_json(payload)

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
            if path == "/__net/probe":
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                # start=1 kicks a run off; without it this only reports progress,
                # so a polling client never restarts the run it is watching.
                start = (qs.get("start") or ["0"])[0] not in ("0", "false", "")
                return self._send_json(
                    net_probe((qs.get("timeout") or ["5000"])[0], start=start)
                )
            if path == "/__net/switch":
                n = int(self.headers.get("Content-Length") or 0)
                return self._send_json(net_switch(self.rfile.read(n) if n else b""))
            if path == "/__pool":
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                force = (qs.get("refresh") or ["0"])[0] not in ("0", "false", "")
                return self._send_json(discover_pool(force=force))
            if path == "/__route":
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    return self._send_json(route_action(self.rfile.read(n) if n else b""))
                return self._send_json(route_status())
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
                    "pool": "/__pool",
                    "route": "/__route",
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
        route = None
        local_direct = False
        for attempt in range(max_attempts):
            # One route decision per attempt: a failure that cools this member
            # down has to be visible to the next attempt, which is the entire
            # point of rotating.
            route = route_for_attempt(force_direct=local_direct)
            route_direct = route["direct"]
            bump("attempts")
            t_attempt = time.time()
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
                        report_route(route, "EMPTY_STREAM", 200,
                                     int((time.time() - t_attempt) * 1000))
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
                    report_route(route, "OK", 200,
                                 int((time.time() - t_attempt) * 1000))
                    streamed = True
                    break

                last_body = resp.read()
                cls = classify(status, last_body)
                trail.append(cls)
                last_status = status
                report_route(route, cls, status,
                             int((time.time() - t_attempt) * 1000))
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
                report_route(route, cls, last_status,
                             int((time.time() - t_attempt) * 1000))
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
                report_route(route, "ERR:" + type(e).__name__, None,
                             int((time.time() - t_attempt) * 1000), err=str(e))
                if not route_direct and _proxy_refused(e):
                    # mihomo is down or its port moved: stop paying for it.
                    route_direct = True
                    local_direct = True
                    report_route(route, "LOCAL_RELAY", None,
                                 int((time.time() - t_attempt) * 1000))
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
                # Which member carried it, and why it was chosen: without this
                # a rotation is invisible in the log, which is how the last
                # silent route change went unnoticed for a day.
                "member": (route or {}).get("name"),
                "via": (route or {}).get("via"),
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
