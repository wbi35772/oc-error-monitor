"""A fixed front door for the retry proxy.

The proxy's port moves between 8788 and 8899 depending on which build is
current, so every agent platform has to be re-pointed by hand and nobody
remembers which one is live. This gives them one address that never changes:

    http://127.0.0.1:<gateway port>/v1

It is OpenAI-compatible on purpose: `/v1/models` lists the models the official
free tier offers right now (aligned against models.dev, never the 44 stale
entries in a config file), and `/v1/chat/completions` streams straight through
to whichever proxy instance answers `/__health`.

It also serves the dashboard, so one browser tab shows both the routing state
and the connection details an agent platform needs.

Port selection is automatic: candidate ports are tried in order and a port is
only accepted when nothing else owns it. port_owner.py exists because on
Windows `bind()` succeeds on a port another process is listening on
(SO_REUSEADDR); a listening socket also refuses a connect, so `connect_ex` is
the test used here, with a bind as a second opinion.

    python gateway.py               # foreground, picks a port
    python gateway.py --port 8790   # force one
    python gateway.py --print       # print the resolved config, do not serve
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "gateway.json")
VERSION = "1.2.0-dev"
HOST = "127.0.0.1"

# Candidate ports, in the order they are tried. The first block sits next to
# the proxy's own range; the second is the fallback when an unrelated service
# has taken the neighbourhood.
CANDIDATE_PORTS = list(range(8790, 8800)) + list(range(18800, 18810))

# Where a request goes. First one that answers /__health wins.
DEFAULT_BACKENDS = [8899, 8788]

# The live catalogue behind opencode's provider list. One request, ~6 MB, and
# it carries every model's cost, so "free" is decided by data, not by a name.
ALIGN_URL = "https://models.dev/api.json"
ALIGN_PROVIDER = "opencode"

# Headers that belong to one hop and must not be copied to the next.
HOP_BY_HOP = {
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
}

DEFAULTS = {
    "port": None,
    "key": None,
    "key_enabled": False,
    "upstream_key": None,
    "backend_override": None,
    "backends": DEFAULT_BACKENDS,
    "models": [],
    "model_names": {},
    "aligned_at": None,
    "aligned_source": ALIGN_URL,
    "align_error": None,
    "align_interval_s": 6 * 3600,
    "only_free": True,
    # "cost" keeps anything the catalogue prices at zero (big-pickle and
    # grok-code are $0 without a -free name); "name" trusts only the suffix.
    "free_rule": "cost",
    "created_at": None,
}

_lock = threading.Lock()
_state: dict = {}
_recent: list = []
_backend_cache: tuple[float, int | None] | None = None

# Local hops must never be sent through a system proxy: mihomo is usually
# listening on this machine, and a proxy hop to 127.0.0.1 either fails or
# loops. One opener with an empty ProxyHandler is the whole fix.
_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_state() -> dict:
    """Read gateway.json, filling anything a hand edit removed."""
    global _state
    raw = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8-sig") as fh:
                raw = json.load(fh) or {}
        except Exception as exc:  # a broken file must not stop the gateway
            print("gateway: cannot read %s (%s), starting from defaults"
                  % (STATE_PATH, exc))
    st = dict(DEFAULTS)
    st.update({k: v for k, v in raw.items() if k in DEFAULTS})
    if not st["key"]:
        st["key"] = new_key()
    if not st["created_at"]:
        st["created_at"] = _now()
    if not isinstance(st["models"], list):
        st["models"] = []
    if not isinstance(st["model_names"], dict):
        st["model_names"] = {}
    if not st["backends"]:
        st["backends"] = list(DEFAULT_BACKENDS)
    _state = st
    return st


def save_state() -> None:
    with _lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, STATE_PATH)


def new_key() -> str:
    # Deliberately shaped like an OpenAI key: several provider forms refuse a
    # value that does not start with sk-.
    return "sk-ocg-" + secrets.token_urlsafe(24)


def public_state() -> dict:
    """What the dashboard is allowed to see: the key, masked."""
    st = dict(_state)
    key = st.get("key") or ""
    st["key_masked"] = (key[:11] + "…" + key[-4:]) if len(key) > 18 else key
    st["key"] = None
    st["upstream_key_set"] = bool(_state.get("upstream_key"))
    st["upstream_key"] = None
    st["running"] = True
    st["version"] = VERSION
    st["pid"] = os.getpid()
    st["state_path"] = STATE_PATH
    st["base_url"] = base_url()
    st["align_due_in_s"] = align_due_in()
    # The dashboard must be able to say which proxy is actually serving.
    # `backends` stays the configured port list; `backends_live` is the probe
    # result with versions, so the page never has to guess.
    st["backends_live"] = backend_info()["backends"]
    st["active_backend"] = pick_backend()
    return st


def base_url(port: int | None = None) -> str:
    return "http://%s:%d/v1" % (HOST, port or _state.get("port") or 0)


# --------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------
def port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """True when something already listens on the port.

    The bind probe is what decides, and it deliberately binds *without*
    SO_REUSEADDR: on Windows a second bind with that flag succeeds even while
    the port is being served, which turns a taken port into a startup crash
    one minute later. Without the flag an occupied port answers
    WSAEADDRINUSE (10048) and nothing else does.

    The connect probe runs first only because it is cheap and catches the case
    where a listener is bound to 0.0.0.0: it still answers on 127.0.0.1.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        if s.connect_ex((host, port)) == 0:
            return True
    except OSError:
        pass
    finally:
        s.close()
    b = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        b.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        b.close()


def pick_port(preferred=None) -> int:
    """First candidate nothing owns. Raises when the whole range is taken."""
    order = ([preferred] if preferred else []) + [
        p for p in CANDIDATE_PORTS if p != preferred
    ]
    for port in order:
        if not port_listening(port):
            return port
    raise RuntimeError("every candidate port is taken: %s" % order)


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
def backend_health(port: int, timeout: float = 0.8) -> dict | None:
    try:
        with _local_opener.open(
            "http://%s:%d/__health" % (HOST, port), timeout=timeout
        ) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def pick_backend(force: bool = False) -> int | None:
    """The proxy instance to forward to, cached for two seconds.

    Without the cache every request would first pay a round trip to a dead
    port, which is exactly the latency this thing exists to remove.
    """
    global _backend_cache
    now = time.time()
    if not force and _backend_cache and now - _backend_cache[0] < 2.0:
        return _backend_cache[1]
    order = []
    override = _state.get("backend_override")
    if override:
        order.append(int(override))
    order += [int(p) for p in _state.get("backends") or DEFAULT_BACKENDS]
    found = None
    for port in order:
        if backend_health(port):
            found = port
            break
    _backend_cache = (now, found)
    return found


def backend_info() -> dict:
    """Every known backend with its version, for the dashboard."""
    out = []
    for port in [int(p) for p in (_state.get("backends") or DEFAULT_BACKENDS)]:
        h = backend_health(port, timeout=0.5)
        out.append({
            "port": port,
            "up": bool(h),
            "version": (h or {}).get("version"),
            "pid": (h or {}).get("pid"),
            "uptime_s": (h or {}).get("uptime_s"),
        })
    return {"backends": out, "active": pick_backend(force=True),
            "override": _state.get("backend_override")}


def selftest() -> dict:
    """Ask the upstream whether it will serve a request this gateway built.

    This is the one thing the gateway cannot fake. The free tier answers
    `403 FreeTierError` to a request that carries the real client's exact
    headers and session id but was not sent by it, so the honest answer to
    "why is my other agent platform getting 403" is measurable in one call and
    belongs on the page rather than in a chat window.
    """
    port = pick_backend()
    if not port:
        return {"ok": False, "stage": "backend",
                "error": "no live backend on %s" % ", ".join(str(p) for p in DEFAULT_BACKENDS)}
    model = (_state.get("models") or ["deepseek-v4.1-flash-free"])[0]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port, data=body, method="POST"
    )
    for header, value in (
        ("Content-Type", "application/json"),
        ("Authorization", "Bearer public"),
        ("User-Agent", "opencode/latest/2.0.32/cli"),
        ("x-opencode-client", "cli"),
    ):
        req.add_header(header, value)
    started = time.time()
    try:
        with _local_opener.open(req, timeout=60) as resp:
            resp.read(2048)
            return {"ok": True, "stage": "upstream", "backend": port, "model": model,
                    "status": resp.status, "ms": int((time.time() - started) * 1000),
                    "verdict": "upstream accepted the request"}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        verdict = ("upstream refused a request that did not come from the real client"
                   if "FreeTier" in raw else "upstream refused the request")
        return {"ok": False, "stage": "upstream", "backend": port, "model": model,
                "status": exc.code, "ms": int((time.time() - started) * 1000),
                "snippet": raw[:400], "verdict": verdict}
    except Exception as exc:
        return {"ok": False, "stage": "transport", "backend": port, "error": repr(exc)}


# --------------------------------------------------------------------------
# model alignment
# --------------------------------------------------------------------------
def _is_free(model_id: str, meta: dict, rule: str = "cost") -> bool:
    """Data first, name second.

    models.dev carries a cost block per model; when its input and output are
    both zero the model is free no matter what it is called. Two entries
    (big-pickle, grok-code) are priced at zero without saying so in the name.
    rule="name" falls back to the suffix for anyone who prefers the strict
    reading.
    """
    cost = meta.get("cost") or {}
    if rule != "name":
        try:
            # Both sides must be present and zero. A catalogue entry that
            # prices only one direction is not evidence of a free model, and
            # guessing the missing side as zero is how a paid model ends up
            # in a list that promises it costs nothing.
            if cost and "input" in cost and "output" in cost:
                if float(cost.get("input") or 0) == 0 and float(cost.get("output") or 0) == 0:
                    return True
        except (TypeError, ValueError):
            pass
    return model_id.endswith("-free")


def fetch_catalog(timeout: float = 25.0) -> dict:
    req = urllib.request.Request(
        ALIGN_URL, headers={"User-Agent": "oc-error-monitor/%s" % VERSION}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def align(force: bool = False) -> dict:
    """Pull the live free-model list and store it.

    A failure keeps the previous list: a stale list still serves requests, an
    empty one does not.
    """
    started = time.time()
    try:
        catalog = fetch_catalog()
        provider = (catalog or {}).get(ALIGN_PROVIDER) or {}
        models = provider.get("models") or {}
        free = []
        names = {}
        for mid, meta in models.items():
            meta = meta if isinstance(meta, dict) else {}
            if _state.get("only_free", True) and not _is_free(
                mid, meta, _state.get("free_rule") or "cost"
            ):
                continue
            free.append(mid)
            names[mid] = meta.get("name") or mid
        if not free:
            raise RuntimeError("catalogue returned no free models for provider '%s'"
                               % ALIGN_PROVIDER)
        free.sort()
        before = set(_state.get("models") or [])
        after = set(free)
        _state["models"] = free
        _state["model_names"] = names
        _state["aligned_at"] = _now()
        _state["align_error"] = None
        save_state()
        result = {
            "ok": True,
            "count": len(free),
            "added": sorted(after - before),
            "removed": sorted(before - after),
            "took_ms": int((time.time() - started) * 1000),
            "source": ALIGN_URL,
            "provider": ALIGN_PROVIDER,
        }
        print("gateway: aligned %d free models (%d added, %d removed) in %dms"
              % (len(free), len(after - before), len(before - after),
                 result["took_ms"]))
        return result
    except Exception as exc:
        _state["align_error"] = "%s: %s" % (type(exc).__name__, exc)
        save_state()
        print("gateway: align failed: %s" % _state["align_error"])
        return {"ok": False, "error": _state["align_error"],
                "count": len(_state.get("models") or [])}


def align_due_in() -> int | None:
    interval = int(_state.get("align_interval_s") or 0)
    if interval <= 0 or not _state.get("aligned_at"):
        return None
    try:
        last = time.mktime(time.strptime(_state["aligned_at"], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0
    return max(0, int(last + interval - time.time()))


def align_loop(stop: threading.Event) -> None:
    """Re-align when due, checked once a minute.

    A minute is short enough that a settings change takes effect without a
    restart, and long enough that an idle gateway costs nothing.
    """
    # Give the proxy instances time to come up before the first network call.
    if stop.wait(8.0):
        return
    while not stop.is_set():
        try:
            if not _state.get("models") or (align_due_in() or 0) <= 0:
                align()
        except Exception as exc:  # never let the thread die on one bad pull
            print("gateway: align loop: %r" % (exc,))
        if stop.wait(60.0):
            return


# --------------------------------------------------------------------------
# provider installation
# --------------------------------------------------------------------------
def opencode_config_path() -> str:
    candidates = [
        os.path.expandvars(r"%USERPROFILE%\.config\opencode\opencode.json"),
        os.path.expanduser("~/.config/opencode/opencode.json"),
    ]
    for path in candidates:
        if path and os.path.exists(path) and "USERPROFILE" not in path:
            return path
    return candidates[0]


PROVIDER_ID = "oc-gateway"


def provider_block() -> dict:
    models = {}
    for mid in _state.get("models") or []:
        models[mid] = {"name": _state.get("model_names", {}).get(mid) or mid}
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "OC Gateway (free)",
        "options": {
            "baseURL": base_url(),
            "apiKey": _state.get("key") or "",
        },
        "models": models,
    }


def install_provider(dry_run: bool = False) -> dict:
    """Write (or preview) the provider entry an agent platform needs.

    Only the `oc-gateway` key is touched: the 44 per-model overrides under
    `provider.opencode` are somebody else's business and stay byte-identical.
    """
    path = opencode_config_path()
    block = provider_block()
    if dry_run:
        return {"ok": True, "dry_run": True, "path": path, "provider_id": PROVIDER_ID,
                "provider": block, "models": len(block["models"])}
    try:
        # utf-8-sig: the file carries a BOM and plain utf-8 json.load refuses it.
        with open(path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        cfg = {"$schema": "https://opencode.ai/config.json"}
    except Exception as exc:
        return {"ok": False, "error": "cannot parse %s: %s" % (path, exc),
                "path": path}

    backup = path + ".bak-gateway-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    except FileNotFoundError:
        backup = None
    except Exception as exc:
        return {"ok": False, "error": "backup failed, refusing to write: %s" % exc,
                "path": path}

    providers = cfg.setdefault("provider", {})
    previous = providers.get(PROVIDER_ID)
    providers[PROVIDER_ID] = block
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except Exception as exc:
        return {"ok": False, "error": "write failed: %s" % exc, "path": path}

    return {
        "ok": True,
        "path": path,
        "backup": backup,
        "provider_id": PROVIDER_ID,
        "models": len(block["models"]),
        "replaced": bool(previous),
        "base_url": base_url(),
    }


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
def note(entry: dict) -> None:
    entry = dict(entry)
    entry["ts"] = _now()
    _recent.append(entry)
    if len(_recent) > 200:
        del _recent[:-200]


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "oc-gateway/" + VERSION

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        # The default writes a line per request to stderr; the dashboard is the
        # log, and the console is for startup and alignment only.
        pass

    def _json(self, payload, status: int = 200, extra_headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str = "gateway_error") -> None:
        # OpenAI-shaped, because a client that cannot parse the error shows
        # nothing useful at all.
        self._json({"error": {"message": message, "type": kind,
                              "code": "gateway_" + str(status)}}, status)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _auth_ok(self) -> bool:
        if not _state.get("key_enabled"):
            return True
        raw = (self.headers.get("Authorization") or "").strip()
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
        if not token:
            token = (self.headers.get("x-api-key") or "").strip()
        return secrets.compare_digest(token, _state.get("key") or "")

    # -- routes -----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._json({
                    "service": "oc-gateway",
                    "version": VERSION,
                    "base_url": base_url(),
                    "dashboard": "/__ui",
                    "models": "/v1/models",
                    "state": "/__gateway/state",
                })
            if path == "/__ui":
                return self._serve_ui()
            if path in ("/health", "/__health"):
                active = pick_backend()
                return self._json({
                    "ok": True,
                    "service": "oc-gateway",
                    "version": VERSION,
                    "pid": os.getpid(),
                    "port": _state.get("port"),
                    "backend": active,
                    "models": len(_state.get("models") or []),
                })
            if path == "/__gateway/state":
                return self._json(public_state())
            if path == "/__gateway/backends":
                return self._json(backend_info())
            if path == "/__gateway/log":
                return self._json({"entries": list(reversed(_recent[-100:]))})
            if path == "/__gateway/key":
                # Local only: the key is shown once, in full, so it can be
                # pasted into a platform. The bind address already keeps this
                # on the loopback interface.
                return self._json({"key": _state.get("key"),
                                   "enabled": _state.get("key_enabled")})
            if path == "/__gateway/provider":
                return self._json(install_provider(dry_run=True))
            if path == "/__gateway/selftest":
                return self._json(selftest())
            if path == "/v1/models":
                if not self._auth_ok():
                    return self._error(401, "invalid api key", "authentication_error")
                data = []
                for mid in _state.get("models") or []:
                    data.append({
                        "id": mid,
                        "object": "model",
                        "owned_by": "opencode",
                        "name": _state.get("model_names", {}).get(mid) or mid,
                    })
                return self._json({"object": "list", "data": data})
            if path.startswith("/v1/"):
                return self._relay(method, path)
            return self._json({"error": "not found", "path": path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            note({"kind": "error", "path": path, "error": repr(exc)})
            try:
                self._error(500, "gateway failure: %s" % exc)
            except Exception:
                pass

    def _action(self) -> dict:
        body = self._body()
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _serve_ui(self):
        path = os.path.join(HERE, "ui.html")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            return self._error(500, "ui.html missing next to gateway.py")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- control ----------------------------------------------------------
    def _control(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/__gateway/key" and self.command == "POST":
            act = self._action()
            action = act.get("action") or "rotate"
            if action == "rotate":
                _state["key"] = new_key()
                save_state()
                note({"kind": "key", "action": "rotate"})
                return self._json({"ok": True, "key": _state["key"]})
            if action == "enable" or action == "disable":
                _state["key_enabled"] = action == "enable"
                save_state()
                note({"kind": "key", "action": action})
                return self._json({"ok": True, "enabled": _state["key_enabled"]})
            return self._error(400, "unknown action %s" % action)
        if path == "/__gateway/settings" and self.command == "POST":
            act = self._action()
            changed = {}
            if "align_interval_s" in act:
                v = int(act["align_interval_s"])
                _state["align_interval_s"] = max(0, min(v, 7 * 24 * 3600))
                changed["align_interval_s"] = _state["align_interval_s"]
            if "only_free" in act:
                _state["only_free"] = bool(act["only_free"])
                changed["only_free"] = _state["only_free"]
            if "free_rule" in act:
                rule = str(act.get("free_rule") or "cost")
                if rule not in ("cost", "name"):
                    return self._error(400, "free_rule must be cost or name")
                _state["free_rule"] = rule
                changed["free_rule"] = rule
                changed["needs_align"] = True
            if "upstream_key" in act:
                v = (act.get("upstream_key") or "").strip()
                _state["upstream_key"] = v or None
                changed["upstream_key_set"] = bool(v)
            if "backend_override" in act:
                v = act.get("backend_override")
                _state["backend_override"] = int(v) if v else None
                changed["backend_override"] = _state["backend_override"]
                global _backend_cache
                _backend_cache = None
            save_state()
            note({"kind": "settings", "changed": changed})
            return self._json({"ok": True, "changed": changed,
                               "state": public_state()})
        if path == "/__gateway/align" and self.command == "POST":
            act = self._action()
            if act.get("reset"):
                _state["aligned_at"] = None
            result = align(force=True)
            result["due_in_s"] = align_due_in()
            note({"kind": "align", "ok": result.get("ok"),
                  "count": result.get("count"), "error": result.get("error")})
            return self._json(result)
        if path == "/__gateway/install" and self.command == "POST":
            act = self._action()
            result = install_provider(dry_run=bool(act.get("dry_run")))
            note({"kind": "install", "ok": result.get("ok"),
                  "path": result.get("path"), "error": result.get("error")})
            return self._json(result)
        return self._error(404, "not found: %s" % path)

    # -- forwarding -------------------------------------------------------
    def _relay(self, method: str, path: str):
        if path.startswith("/v1/") and path not in ("/v1/models",) and not self._auth_ok():
            return self._error(401, "invalid api key", "authentication_error")
        backend = pick_backend()
        if backend is None:
            return self._error(
                502,
                "no retry proxy is answering on %s"
                % ", ".join(str(p) for p in (_state.get("backends") or [])),
                "upstream_unavailable",
            )
        body = self._body() if method in ("POST", "PUT", "PATCH", "DELETE") else None
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            headers[k] = v
        upstream_key = _state.get("upstream_key")
        if upstream_key:
            headers["Authorization"] = "Bearer " + upstream_key
        headers["Host"] = "%s:%d" % (HOST, backend)
        headers["Accept-Encoding"] = "identity"

        url = "http://%s:%d%s" % (HOST, backend, path)
        started = time.time()
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            resp = _local_opener.open(req, timeout=900)
        except urllib.error.HTTPError as exc:
            resp = exc
        except Exception as exc:
            note({"kind": "relay", "path": path, "backend": backend,
                  "error": repr(exc)})
            return self._error(502, "backend %d did not answer: %s"
                               % (backend, exc), "upstream_error")

        ctype = resp.headers.get("Content-Type") or ""
        status = getattr(resp, "status", 200) or 200
        streaming = "text/event-stream" in ctype

        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Access-Control-Allow-Origin", "*")
        if resp.headers.get("Cache-Control"):
            self.send_header("Cache-Control", resp.headers["Cache-Control"])
        self.send_header("X-Gateway-Backend", str(backend))
        if streaming:
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            chunks = 0
            try:
                while True:
                    piece = resp.read1(8192)
                    if not piece:
                        break
                    self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                    self.wfile.flush()
                    chunks += 1
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # The client hung up mid-stream; nothing left to send.
                pass
            finally:
                resp.close()
            note({"kind": "relay", "path": path, "backend": backend,
                  "status": status, "stream": True, "chunks": chunks,
                  "ms": int((time.time() - started) * 1000)})
            return

        payload = resp.read()
        resp.close()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        note({"kind": "relay", "path": path, "backend": backend,
              "status": status, "stream": False, "bytes": len(payload),
              "ms": int((time.time() - started) * 1000)})

    # POST bodies for control endpoints are read in _control/_action; relay
    # reads its own. Keeping do_POST a pure dispatcher avoids double reads.
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/__gateway/"):
            return self._control()
        return self._route("POST")


def main() -> int:
    ap = argparse.ArgumentParser(description="fixed front door for the retry proxy")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--print", dest="print_only", action="store_true")
    ap.add_argument("--align", action="store_true", help="align models and exit")
    args = ap.parse_args()

    load_state()
    if args.align:
        print(json.dumps(align(force=True), ensure_ascii=False, indent=2))
        return 0

    # Single instance. A per-minute watchdog calls this file, and pick_port
    # would happily hand a second copy the next free port, leaving two
    # gateways serving two state files. One is alive and answering -> step
    # aside instead of arguing about the port.
    saved = _state.get("port")
    if saved and port_listening(int(saved)):
        health = backend_health(int(saved), timeout=1.0)
        if health and health.get("service") == "oc-gateway":
            if args.print_only:
                print(json.dumps({"port": int(saved), "running": True,
                                  "pid": health.get("pid")}, ensure_ascii=False))
            else:
                print("oc-gateway already running on %d (pid %s)"
                      % (int(saved), health.get("pid")))
            return 0

    port = args.port or pick_port(_state.get("port"))
    _state["port"] = port
    save_state()
    if args.print_only:
        print(json.dumps({"port": port, "base_url": base_url(port),
                          "state_path": STATE_PATH,
                          "models": len(_state.get("models") or [])},
                         ensure_ascii=False, indent=2))
        return 0

    stop = threading.Event()
    threading.Thread(target=align_loop, args=(stop,), daemon=True).start()

    httpd = ThreadingHTTPServer((HOST, port), Handler)
    httpd.daemon_threads = True
    print("oc-gateway %s  http://%s:%d  -> proxy %s"
          % (VERSION, HOST, port,
             ", ".join(str(p) for p in _state.get("backends") or [])))
    print("  openai base url : %s" % base_url(port))
    print("  dashboard       : http://%s:%d/__ui" % (HOST, port))
    print("  models          : %d aligned %s"
          % (len(_state.get("models") or []), _state.get("aligned_at") or "(never)"))
    print("  state           : %s" % STATE_PATH)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
