#!/usr/bin/env python3
"""Regression tests for the fixed-door gateway.

Every case here is something that already went wrong once, or that would go
wrong silently:

  * a paid model leaking into the free list        -> _is_free
  * a provider block that opencode cannot load      -> provider_block
  * the API key appearing in the dashboard payload  -> public_state
  * the gateway taking a port that is already busy  -> pick_port
  * a 403 from the upstream read as "gateway broke" -> selftest

Run:
  python oc-error-monitor/v1.2.0-dev/test_gateway.py
"""

import json
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gateway


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), name, ("| " + detail) if detail else "")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# _is_free: the two rules must disagree in exactly the documented places
# --------------------------------------------------------------------------
FREE_COST = {"cost": {"input": 0, "output": 0}}
PAID_COST = {"cost": {"input": 0.5, "output": 1.5}}


def test_is_free():
    failed = 0
    failed += check("cost: zero price is free",
                    gateway._is_free("some-model", FREE_COST, "cost"))
    failed += check("cost: priced model is not free",
                    not gateway._is_free("some-model", PAID_COST, "cost"))
    failed += check("name: -free suffix is free",
                    gateway._is_free("deepseek-v4.1-flash-free", {}, "name"))
    failed += check("name: a zero-price model without the suffix is not free",
                    not gateway._is_free("big-pickle", FREE_COST, "name"))
    failed += check("cost: no cost block at all is not free",
                    not gateway._is_free("mystery", {}, "cost"))
    failed += check("cost: a missing side counts as paid",
                    not gateway._is_free("half", {"cost": {"input": 0}}, "cost"))
    return failed


# --------------------------------------------------------------------------
# provider_block: the shape opencode reads, and nothing else
# --------------------------------------------------------------------------
def test_provider_block():
    failed = 0
    saved_models = gateway._state.get("models")
    gateway._state["models"] = ["deepseek-v4.1-flash-free", "beam-free"]
    gateway._state["model_names"] = {"beam-free": "Beam"}
    gateway._state["key"] = "sk-ocg-testkeytestkeytestkey"
    try:
        block = gateway.provider_block()
    finally:
        gateway._state["models"] = saved_models

    options = block.get("options") or {}
    failed += check("block: openai-compatible npm",
                    block.get("npm") == "@ai-sdk/openai-compatible", str(block.get("npm")))
    failed += check("block: baseURL ends in /v1",
                    str(options.get("baseURL", "")).endswith("/v1"),
                    str(options.get("baseURL")))
    failed += check("block: key is carried in options",
                    str(options.get("apiKey", "")).startswith("sk-ocg-"))
    failed += check("block: every free model is declared",
                    sorted(block.get("models") or {}) == ["beam-free", "deepseek-v4.1-flash-free"],
                    str(sorted(block.get("models") or {})))
    failed += check("block: a display name survives",
                    (block["models"]["beam-free"] or {}).get("name") == "Beam")
    failed += check("block: no stray top-level key",
                    set(block) <= {"npm", "name", "options", "models"}, str(sorted(block)))
    return failed


# --------------------------------------------------------------------------
# public_state: the key is shown once, on purpose, and never in the listing
# --------------------------------------------------------------------------
def test_public_state():
    failed = 0
    secret = "sk-ocg-abcdefghijklmnopqrstuvwxyz"
    gateway._state["key"] = secret
    state = gateway.public_state()
    failed += check("state: raw key is stripped", state.get("key") is None)
    failed += check("state: masked key keeps only the ends",
                    state.get("key_masked") and secret not in state["key_masked"],
                    str(state.get("key_masked")))
    failed += check("state: nothing in the payload leaks the secret",
                    secret not in json.dumps(state))
    return failed


# --------------------------------------------------------------------------
# pick_port: an occupied candidate must be skipped, not fought over
# --------------------------------------------------------------------------
def test_pick_port():
    failed = 0
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    busy = holder.getsockname()[1]
    try:
        chosen = gateway.pick_port(preferred=busy)
        failed += check("port: a busy port is not chosen", chosen != busy,
                        "busy=%d chosen=%d" % (busy, chosen))
        failed += check("port: it still returns a usable port",
                        isinstance(chosen, int) and chosen > 0, str(chosen))
        failed += check("port: the busy port is reported as listening",
                        gateway.port_listening(busy))
    finally:
        holder.close()
    return failed


# --------------------------------------------------------------------------
# selftest: the upstream's refusal must be reported, not raised
# --------------------------------------------------------------------------
class _Stub(BaseHTTPRequestHandler):
    status = 403
    payload = b'{"type":"error","error":{"type":"FreeTierError","message":"no"}}'

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(_Stub.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_Stub.payload)))
        self.end_headers()
        self.wfile.write(_Stub.payload)

    def log_message(self, *args):
        pass


def test_selftest():
    failed = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved_backend = gateway.pick_backend
    saved_models = gateway._state.get("models")
    gateway._state["models"] = ["deepseek-v4.1-flash-free"]
    gateway.pick_backend = lambda force=False: port
    try:
        refused = gateway.selftest()
        failed += check("selftest: a 403 is a result, not an exception",
                        refused.get("ok") is False and refused.get("status") == 403,
                        str(refused.get("status")))
        failed += check("selftest: the refusal is named",
                        "real client" in str(refused.get("verdict")), str(refused.get("verdict")))

        _Stub.status = 200
        _Stub.payload = b'{"choices":[]}'
        accepted = gateway.selftest()
        failed += check("selftest: a 200 is a pass",
                        accepted.get("ok") is True and accepted.get("status") == 200,
                        str(accepted))
    finally:
        gateway.pick_backend = saved_backend
        gateway._state["models"] = saved_models
        server.shutdown()
    return failed


# --------------------------------------------------------------------------
# install_provider(dry_run): a preview must not touch the file
# --------------------------------------------------------------------------
def test_dry_run_writes_nothing():
    failed = 0
    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "opencode.json")
    with open(target, "w", encoding="utf-8-sig") as fh:
        json.dump({"provider": {"opencode": {"models": {}}}}, fh)
    before = open(target, "rb").read()
    saved = gateway.opencode_config_path
    gateway.opencode_config_path = lambda: target
    try:
        preview = gateway.install_provider(dry_run=True)
        after = open(target, "rb").read()
        failed += check("dry run: reports ok", preview.get("ok") is True)
        failed += check("dry run: the file is byte-identical", before == after)
        failed += check("dry run: it still describes a provider",
                        bool((preview.get("provider") or {}).get("models")))
    finally:
        gateway.opencode_config_path = saved
    return failed


def main() -> int:
    gateway.load_state()
    failed = 0
    for name, fn in (
        ("_is_free", test_is_free),
        ("provider_block", test_provider_block),
        ("public_state", test_public_state),
        ("pick_port", test_pick_port),
        ("selftest", test_selftest),
        ("install dry run", test_dry_run_writes_nothing),
    ):
        print("\n== %s ==" % name)
        failed += fn()
    print("\n%s" % ("all passed" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
