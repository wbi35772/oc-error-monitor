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
from concurrent.futures import ThreadPoolExecutor
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
    # `key` mirrors the first entry of `keys` and exists so a gateway.json
    # written before multi-key still loads. Everything reads `keys`.
    "key": None,
    "keys": [],
    "key_enabled": False,
    "upstream_key": None,
    "backend_override": None,
    "backends": DEFAULT_BACKENDS,
    "models": [],
    "model_names": {},
    # Per-model verdict from the last verification pass:
    # free / paid / gone / rate_limited / error.
    "model_status": {},
    "verified_at": None,
    "aligned_at": None,
    "aligned_source": ALIGN_URL,
    "align_error": None,
    "align_interval_s": 6 * 3600,
    "only_free": True,
    # "cost" and "name" only decide which models are *candidates*. What gets
    # exposed is whatever the upstream itself still serves on the free tier.
    "verify_models": True,
    "free_rule": "cost",
    # Model fallback when a client asks for something the free tier cannot
    # serve: "auto" rewrites to the next verified model, "manual" rewrites to
    # pinned_model only when that is itself verified, "off" forwards the name
    # untouched. Defaults are opt-in: nothing is rewritten until a mode is
    # deliberately chosen in the dashboard.
    "fallback_mode": "off",
    "pinned_model": None,
    "fallback_retries": 1,
    # A model is only dropped from the live list after this many CONSECUTIVE
    # align runs where BOTH routes call it gone. One flaky route, or one pass,
    # is not evidence; models are flagged pending instead.
    "verify_remove_after": 2,
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
        st["key"] = new_key()["key"]
    if not st["created_at"]:
        st["created_at"] = _now()
    if not isinstance(st["keys"], list):
        st["keys"] = []
    # Older file: one bare key, no list. Promote it without changing its value
    # so every client already configured keeps working.
    if not st["keys"] and st["key"]:
        st["keys"] = [{
            "id": "k_default",
            "name": "default",
            "key": st["key"],
            "created_at": st["created_at"] or _now(),
            "enabled": True,
            "last_used": None,
        }]
    if not st["keys"]:
        entry = new_key("default")
        st["keys"] = [entry]
    st["keys"] = [k for k in st["keys"] if isinstance(k, dict) and k.get("key")]
    for entry in st["keys"]:
        entry.setdefault("id", "k_" + secrets.token_hex(4))
        entry.setdefault("name", "default")
        entry.setdefault("enabled", True)
        entry.setdefault("created_at", _now())
        entry.setdefault("last_used", None)
    st["key"] = st["keys"][0]["key"]
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


def new_key(name: str = "default") -> dict:
    # Deliberately shaped like an OpenAI key: several provider forms refuse a
    # value that does not start with sk-.
    return {
        "id": "k_" + secrets.token_hex(4),
        "name": (name or "default").strip()[:40] or "default",
        "key": "sk-ocg-" + secrets.token_urlsafe(24),
        "created_at": _now(),
        "enabled": True,
        "last_used": None,
    }


def _mask(secret: str) -> str:
    secret = secret or ""
    return (secret[:11] + "…" + secret[-4:]) if len(secret) > 18 else secret


def public_keys() -> list:
    """Every key, masked, with the bookkeeping a client list needs.

    One key per client is the point: revoking the laptop must not log out the
    desktop, and `last_used` is what tells you which entry is still alive.
    """
    return [{
        "id": k["id"],
        "name": k.get("name") or "default",
        "masked": _mask(k["key"]),
        "enabled": bool(k.get("enabled", True)),
        "created_at": k.get("created_at"),
        "last_used": k.get("last_used"),
    } for k in _state.get("keys") or []]


def default_key() -> str:
    for entry in _state.get("keys") or []:
        if entry.get("enabled", True):
            return entry.get("key") or ""
    return (_state.get("keys") or [{}])[0].get("key") or ""


def public_state() -> dict:
    """What the dashboard is allowed to see: keys masked, never the values."""
    st = dict(_state)
    st["keys"] = public_keys()
    st["key_masked"] = st["keys"][0]["masked"] if st["keys"] else ""
    st["key"] = None
    st["keys_enabled"] = bool(_state.get("key_enabled"))
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
    # models.dev drops connections now and then (UNEXPECTED_EOF under TLS).
    # Three tries with a pause between them turn that into a slower success
    # instead of an alignment that silently keeps yesterday's list.
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                ALIGN_URL, headers={"User-Agent": "oc-error-monitor/%s" % VERSION}
            )
            # No system proxy: HTTPS_PROXY points at mihomo, and that hop
            # currently kills the TLS handshake to models.dev (SSL EOF) while
            # the direct route answers 200 with the full 6 MB. The catalogue
            # is a public static file, so it does not need the proxy pool.
            with _local_opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.5 + attempt)
    raise last


PROBE_HEADERS = {
    # The free tier answers FreeTierError only to something shaped like the
    # client; with these headers the reply is a verdict instead of a
    # Cloudflare 1010.
    "Authorization": "Bearer public",
    "User-Agent": "opencode/latest/2.0.32/cli",
    "x-opencode-client": "cli",
    "Content-Type": "application/json",
}

PROBE_WORKERS = 4


def _verdict(status: int, raw: str) -> str:
    low = (raw or "").lower()
    if status in (200, 201):
        return "free"
    if "freetier" in low:
        return "free"
    if "unavailable" in low or status == 404:
        return "gone"
    if status == 401 or "missing api key" in low:
        return "paid"
    if status == 429:
        return "rate_limited"
    return "unknown"


def probe_model(model: str, port: int, timeout: float = 25.0) -> dict:
    """Ask the upstream what it actually does with this model.

    The reply is the whole point: 200 or FreeTierError means the free tier
    still serves it, "Model is unavailable" means the catalogue kept a name the
    service dropped, and Missing API key means it is paid. Name suffixes and
    prices are guesses; this is the answer.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        "http://%s:%d/v1/chat/completions" % (HOST, port), data=body, method="POST"
    )
    for header, value in PROBE_HEADERS.items():
        req.add_header(header, value)
    started = time.time()
    try:
        with _local_opener.open(req, timeout=timeout) as resp:
            resp.read(2048)
            return {"status": resp.status, "verdict": "free",
                    "ms": int((time.time() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", "replace")
        return {"status": exc.code, "verdict": _verdict(exc.code, raw),
                "ms": int((time.time() - started) * 1000),
                "snippet": raw[:160]}
    except Exception as exc:
        return {"status": 0, "verdict": "error",
                "ms": int((time.time() - started) * 1000),
                "snippet": repr(exc)[:160]}


# The origin, for the second opinion. The primary route is the retry proxy -
# the path real traffic takes; this one leaves the machine directly, so a dead
# member of the local pool cannot make every model look dead.
ORIGIN = "https://opencode.ai/inference/openai/v1"


def probe_direct(model: str, timeout: float = 25.0) -> dict:
    """Same probe as probe_model, but straight to the origin, no local hop."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        ORIGIN + "/chat/completions", data=body, method="POST"
    )
    for header, value in PROBE_HEADERS.items():
        req.add_header(header, value)
    started = time.time()
    try:
        with _local_opener.open(req, timeout=timeout) as resp:
            resp.read(2048)
            return {"status": resp.status, "verdict": "free", "route": "direct",
                    "ms": int((time.time() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", "replace")
        return {"status": exc.code, "verdict": _verdict(exc.code, raw),
                "route": "direct",
                "ms": int((time.time() - started) * 1000),
                "snippet": raw[:160]}
    except Exception as exc:
        return {"status": 0, "verdict": "error", "route": "direct",
                "ms": int((time.time() - started) * 1000),
                "snippet": repr(exc)[:160]}


def verify_models(ids: list, port: int) -> dict:
    """Two routes, one verdict per model, at a polite pace.

    Four workers is deliberate: this is somebody else's free tier, and a burst
    of 46 parallel requests is how an account gets rate limited for reasons
    that have nothing to do with the models.

    primary goes through the retry proxy - the path real traffic takes. When
    primary says "unavailable", the same model is asked again over a second
    network path (straight to the origin). Only when BOTH routes call it gone
    does the result carry agree=gone; anything else stays inconclusive,
    because our own network being broken is not evidence a model died.
    """
    out = {}
    lock = threading.Lock()

    def work(mid):
        result = dict(probe_model(mid, port), route="proxy")
        if result.get("verdict") == "gone":
            other = probe_direct(mid)
            result["agree"] = ("gone" if other.get("verdict") == "gone"
                               else "conflict")
            if result["agree"] == "conflict":
                result["other"] = {"verdict": other.get("verdict"),
                                   "status": other.get("status"),
                                   "snippet": (other.get("snippet") or "")[:100]}
        with lock:
            out[mid] = result

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        list(pool.map(work, ids))
    return out


def align(force: bool = False) -> dict:
    """Rebuild the model list from the catalogue, then ask the service itself.

    Two gates, in this order:
      1. the catalogue narrows 130 names to the candidates priced at zero or
         named -free;
      2. the upstream is probed model by model and only FreeTierError (or a
         200) keeps it. A name the service dropped disappears here instead of
         failing later in a chat window.

    A failure keeps the previous list: a stale list still serves requests, an
    empty one does not.
    """
    started = time.time()
    try:
        catalog = fetch_catalog()
        provider = (catalog or {}).get(ALIGN_PROVIDER) or {}
        models = provider.get("models") or {}
        candidates = []
        names = {}
        for mid, meta in models.items():
            meta = meta if isinstance(meta, dict) else {}
            if _state.get("only_free", True) and not _is_free(
                mid, meta, _state.get("free_rule") or "cost"
            ):
                continue
            candidates.append(mid)
            names[mid] = meta.get("name") or mid
        if not candidates:
            raise RuntimeError("catalogue returned no free models for provider '%s'"
                               % ALIGN_PROVIDER)
        candidates.sort()

        status_map = {}
        pending = []
        kept = candidates
        threshold = max(1, int(_state.get("verify_remove_after") or 1))
        backend = pick_backend()
        if _state.get("verify_models", True) and backend:
            fresh = verify_models(candidates, backend)
            old_status = _state.get("model_status") or {}
            served = set(_state.get("models") or [])
            kept = []
            for mid in candidates:
                entry = dict(fresh.get(mid) or {"verdict": "error"})
                verdict = entry.get("verdict")
                # Count a death only when BOTH routes said "unavailable" —
                # one dead local hop or one region-gated reply is not
                # evidence. Verified models reset their streak immediately.
                confirmed = verdict == "gone" and entry.get("agree") == "gone"
                if confirmed:
                    fails = int((old_status.get(mid) or {}).get("fails") or 0) + 1
                    entry.pop("agree", None)
                    entry.pop("other", None)
                else:
                    fails = 0
                    entry.pop("agree", None)
                entry["fails"] = fails
                if verdict == "free":
                    entry["pending"] = False
                    kept.append(mid)
                elif mid in served and fails < threshold:
                    # Still served, but the evidence is stacking up.
                    entry["pending"] = bool(fails)
                    if fails:
                        pending.append(mid)
                    kept.append(mid)
                status_map[mid] = entry

            certain = [m for m in candidates
                       if status_map.get(m, {}).get("verdict")
                       in ("free", "paid", "gone")]
            # Every route answering "dunno" means our network is the problem,
            # not the models: keep the original list instead of emptying it.
            if not certain and not kept:
                raise RuntimeError("model verification could not reach the upstream")

        before = set(_state.get("models") or [])
        after = set(kept)
        _state["models"] = kept
        _state["model_names"] = {m: names.get(m, m) for m in kept}
        _state["model_status"] = status_map
        if status_map:
            _state["verified_at"] = _now()
        _state["aligned_at"] = _now()
        _state["align_error"] = None
        save_state()
        counts = {}
        for entry in status_map.values():
            counts[entry.get("verdict")] = counts.get(entry.get("verdict"), 0) + 1
        result = {
            "ok": True,
            "count": len(kept),
            "candidates": len(candidates),
            "added": sorted(after - before),
            "removed": sorted(before - after),
            "pending": pending,
            "conflicts": sum(1 for e in status_map.values()
                             if e.get("agree") == "conflict"),
            "threshold": threshold,
            "verdicts": counts,
            "took_ms": int((time.time() - started) * 1000),
            "source": ALIGN_URL,
            "provider": ALIGN_PROVIDER,
            "verified": bool(status_map),
        }
        print("gateway: aligned %d/%d models (%d added, %d removed) in %dms %s"
              % (len(kept), len(candidates), len(after - before),
                 len(before - after), result["took_ms"], counts))
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


def _decide_fallback(model: str | None) -> str | None:
    """Which model should actually be sent upstream, per the fallback mode.

    off     -> the client's own model, never rewritten
    manual  -> pinned_model, but only while that is itself verified free
    auto    -> the client's model when it is verified free; otherwise the
               first verified one (the request would otherwise die with
               "Model is unavailable")

    Nothing rewrites silently forever: the response carries
    X-Gateway-Model-From / X-Gateway-Model-To whenever a rewrite happened.
    """
    mode = str(_state.get("fallback_mode") or "off").lower()
    free = _state.get("models") or []
    if mode == "off" or not model:
        return model
    if mode == "manual":
        pinned = _state.get("pinned_model")
        return pinned if pinned in free else model
    if mode == "auto":
        return model if model in free else (free[0] if free else model)
    return model


def _next_free_model(current: str | None) -> str | None:
    """The next verified model after `current`, wrapping around the list."""
    free = _state.get("models") or []
    if len(free) < 2:
        return None
    try:
        idx = free.index(current or "")
    except ValueError:
        return None
    return free[(idx + 1) % len(free)]


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


def provider_block(api_key: str | None = None) -> dict:
    models = {}
    for mid in _state.get("models") or []:
        models[mid] = {"name": _state.get("model_names", {}).get(mid) or mid}
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "OC Gateway (free)",
        "options": {
            "baseURL": base_url(),
            "apiKey": api_key or default_key(),
        },
        "models": models,
    }


def install_provider(dry_run: bool = False, key_id: str | None = None) -> dict:
    """Write (or preview) the provider entry an agent platform needs.

    Only the `oc-gateway` key is touched: the 44 per-model overrides under
    `provider.opencode` are somebody else's business and stay byte-identical.
    `key_id` picks which client key is baked into the entry, so two clients can
    be handed two different keys rather than sharing one.
    """
    path = opencode_config_path()
    chosen = default_key()
    if key_id:
        chosen = next(
            (k["key"] for k in _state.get("keys") or [] if k["id"] == key_id and k.get("enabled", True)),
            "",
        )
        if not chosen:
            return {"ok": False, "error": "no such enabled key: %s" % key_id}
    block = provider_block(chosen)
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
        if not token:
            return False
        # Any enabled key opens the door: one per client means revoking one
        # device never logs out the others.
        for entry in _state.get("keys") or []:
            if not entry.get("enabled", True):
                continue
            if secrets.compare_digest(token, entry.get("key") or ""):
                entry["last_used"] = _now()
                return True
        return False

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
                # Local only: the value is shown in full, once, so it can be
                # pasted into a client. The bind address already keeps this on
                # the loopback interface. `?id=` asks for one key, no id lists
                # them masked.
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                wanted = (qs.get("id") or [""])[0]
                if wanted:
                    for entry in _state.get("keys") or []:
                        if entry["id"] == wanted:
                            return self._json({"key": entry["key"], "id": entry["id"],
                                               "name": entry.get("name")})
                    return self._error(404, "no such key")
                return self._json({"keys": public_keys(),
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
        if path == "/__gateway/stop" and self.command == "POST":
            # Served from the gateway's own dashboard (/__ui on its port):
            # answer first, then leave. The proxy has its own stop path that
            # terminates by pid, so a hung gateway is still stoppable.
            def _bye():
                time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=_bye, daemon=True).start()
            return self._json({"ok": True, "stopped": True, "pid": os.getpid()})
        if path == "/__gateway/key" and self.command == "POST":
            act = self._action()
            action = act.get("action") or "rotate"
            keys = _state.setdefault("keys", [])
            if action == "create":
                entry = new_key(act.get("name") or "client")
                keys.append(entry)
                _state["key"] = keys[0]["key"]
                save_state()
                note({"kind": "key", "action": "create", "name": entry["name"]})
                return self._json({"ok": True, "key": entry["key"], "id": entry["id"],
                                   "name": entry["name"], "keys": public_keys()})
            if action == "rotate":
                target = act.get("id")
                for entry in keys:
                    if (target and entry["id"] == target) or (not target and entry is keys[0]):
                        entry["key"] = "sk-ocg-" + secrets.token_urlsafe(24)
                        entry["created_at"] = _now()
                        _state["key"] = keys[0]["key"]
                        save_state()
                        note({"kind": "key", "action": "rotate", "id": entry["id"]})
                        return self._json({"ok": True, "key": entry["key"],
                                           "id": entry["id"], "keys": public_keys()})
                return self._error(404, "no such key")
            if action == "revoke":
                target = act.get("id")
                if len(keys) <= 1:
                    return self._error(400, "the last key cannot be revoked")
                remaining = [k for k in keys if k["id"] != target]
                if len(remaining) == len(keys):
                    return self._error(404, "no such key")
                _state["keys"] = remaining
                _state["key"] = remaining[0]["key"]
                save_state()
                note({"kind": "key", "action": "revoke", "id": target})
                return self._json({"ok": True, "keys": public_keys()})
            if action == "rename":
                for entry in keys:
                    if entry["id"] == act.get("id"):
                        entry["name"] = (act.get("name") or "client").strip()[:40] or "client"
                        save_state()
                        return self._json({"ok": True, "keys": public_keys()})
                return self._error(404, "no such key")
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
            if "fallback_mode" in act:
                mode = str(act["fallback_mode"]).lower()
                if mode not in ("off", "manual", "auto"):
                    return self._error(400, "fallback_mode must be off, manual or auto")
                _state["fallback_mode"] = mode
                changed["fallback_mode"] = mode
            if "pinned_model" in act:
                pin = act.get("pinned_model") or None
                if pin and pin not in (_state.get("models") or []):
                    return self._error(400, "pinned_model is not a verified free model: %s" % pin)
                _state["pinned_model"] = pin
                changed["pinned_model"] = pin
            if "fallback_retries" in act:
                _state["fallback_retries"] = max(0, min(3, int(act["fallback_retries"])))
                changed["fallback_retries"] = _state["fallback_retries"]
            if "verify_remove_after" in act:
                # Consecutive align runs, both routes agreeing, before a model
                # is actually dropped. 1 = first confirmed death removes it.
                _state["verify_remove_after"] = max(1, min(7, int(act["verify_remove_after"])))
                changed["verify_remove_after"] = _state["verify_remove_after"]
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

        # ---- model fallback ------------------------------------------------
        # Only chat completions carry a model; everything else passes through
        # untouched. Nothing has been sent to the client yet, so rewriting
        # here, and retrying upstream before the first byte, stays invisible.
        model_from = None
        model_to = None
        wire = body
        req_json = None
        is_chat = "chat/completions" in path
        if is_chat and method in ("POST", "PUT", "PATCH") and body:
            try:
                req_json = json.loads(body.decode("utf-8"))
            except Exception:
                req_json = None
            if isinstance(req_json, dict) and req_json.get("model"):
                model_from = str(req_json["model"])
                model_to = _decide_fallback(model_from)
                if model_to and model_to != model_from:
                    req_json["model"] = model_to
                    wire = json.dumps(req_json).encode()

        resp = None
        err_body = b""
        for hop in range(int(_state.get("fallback_retries") or 0) + 1):
            req = urllib.request.Request(url, data=wire, method=method)
            for k, v in headers.items():
                req.add_header(k, v)
            try:
                resp = _local_opener.open(req, timeout=900)
                break
            except urllib.error.HTTPError as exc:
                # auto-only escape hatch: upstream says this model is gone
                # and we still have verified models to rotate through.
                can_retry = (
                    req_json is not None
                    and str(_state.get("fallback_mode") or "off") == "auto"
                    and hop < int(_state.get("fallback_retries") or 0)
                )
                if can_retry:
                    try:
                        err_body = exc.read() or b""
                    except Exception:
                        err_body = b""
                if not can_retry:
                    resp = exc
                    break
                nxt = _next_free_model(req_json.get("model"))
                if not nxt or b"unavailable" not in err_body.lower():
                    resp = exc
                    break
                req_json["model"] = nxt
                model_to = nxt
                wire = json.dumps(req_json).encode()
            except Exception as exc:
                note({"kind": "relay", "path": path, "backend": backend,
                      "error": repr(exc)})
                return self._error(502, "backend %d did not answer: %s"
                                   % (backend, exc), "upstream_error")

        ctype = resp.headers.get("Content-Type") or ""
        status = getattr(resp, "status", 200) or 200
        streaming = ("text/event-stream" in ctype
                     and not isinstance(resp, urllib.error.HTTPError))

        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Access-Control-Allow-Origin", "*")
        if resp.headers.get("Cache-Control"):
            self.send_header("Cache-Control", resp.headers["Cache-Control"])
        self.send_header("X-Gateway-Backend", str(backend))
        if model_to and model_to != model_from:
            self.send_header("X-Gateway-Model-From", model_from or "")
            self.send_header("X-Gateway-Model-To", model_to)
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
                  "model_from": model_from, "model_to": model_to,
                  "ms": int((time.time() - started) * 1000)})
            return

        payload = resp.read()
        resp.close()
        # A pre-read (retry probe) already ate an error body that never
        # triggered a retry; hand the stashed bytes back instead of air.
        if not payload and err_body:
            payload = err_body
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        note({"kind": "relay", "path": path, "backend": backend,
              "status": status, "stream": False, "bytes": len(payload),
              "model_from": model_from, "model_to": model_to,
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
