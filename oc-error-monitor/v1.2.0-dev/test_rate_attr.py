#!/usr/bin/env python3
"""Regression tests for 429 attribution (P2).

What this locks down
--------------------
A 429 used to be recorded as RATE and nothing else, so the panel could not
answer the only question that matters at that moment: is this my exit IP, my
account's allowance, or this one model's budget? Each scope implies a
different reaction (rotate, wait, switch model), and guessing wrong either
burns the pool on a model limit or hammers a wall with the same IP.

Run: python test_rate_attr.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retry_proxy as rp


FAILED = 0
TOTAL = 0


def check(name, cond, detail=""):
    global FAILED, TOTAL
    TOTAL += 1
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else " | " + str(detail)))
    if not cond:
        FAILED += 1


def main():
    # --- 1. Cloudflare edge throttle: per exit IP -------------------------
    html = (b"<html><head><title>opencode.ai | 1015</title></head><body>"
            b"Error code: 1015. Too many requests.</body></html>")
    ev = rp.attribute_rate(429,
                           {"cf-ray": "8c1a2b3c4d5e-AMS", "retry-after": "30"},
                           html, {"name": "node-a", "group": "pool"},
                           "glm-5.3-flash-free", 1)
    check("cf body -> issuer cloudflare", ev["issuer"] == "cloudflare", ev["issuer"])
    check("cf body -> scope route", ev["scope"] == "route", ev["scope"])
    check("cf body -> confidence high", ev["confidence"] == "high", ev["confidence"])
    check("cf body -> retry-after parsed", ev["retry_after"] == 30.0, ev["retry_after"])
    check("cf body -> evidence kept", "cf-body" in ev["why"] and "cf-ray" in ev["why"],
          ev["why"])
    check("cf body -> member recorded", ev["member"] == "node-a", ev["member"])

    # --- 2. Account allowance -------------------------------------------
    ev = rp.attribute_rate(
        429, {},
        b'{"error":{"type":"FreeTierError","message":"daily free tier quota exceeded for this account"}}',
        None, None, 1)
    check("quota -> issuer opencode", ev["issuer"] == "opencode", ev["issuer"])
    check("quota -> scope account", ev["scope"] == "account", ev["scope"])
    check("quota -> no rotate hint", ev["retry_after"] is None, ev["retry_after"])

    # --- 2b. IP-reputation refusal: route-scoped but no cooldown ---------
    ev = rp.attribute_rate(
        403, {},
        b'{"error":{"type":"FreeTierError","message":"detected abusive traffic from this IP address"}}',
        {"name": "node-c"}, None, 1)
    check("ip-rep -> issuer opencode", ev["issuer"] == "opencode", ev["issuer"])
    check("ip-rep -> scope route", ev["scope"] == "route", ev["scope"])
    check("ip-rep -> medium (not a cool trigger)", ev["confidence"] == "medium",
          ev["confidence"])
    check("ip-rep -> evidence", "ip-wording" in ev["why"], ev["why"])

    # --- 3. Model budget (TPM), with provider headers --------------------
    ev = rp.attribute_rate(
        429,
        {"x-ratelimit-remaining": "0", "retry-after": "5"},
        b'{"error":{"type":"rate_limit_error","message":"Rate limit exceeded: tokens per minute (TPM) for big-pickle"}}',
        {"name": "node-b"}, "big-pickle", 2)
    check("tpm -> issuer upstream", ev["issuer"] == "upstream", ev["issuer"])
    check("tpm -> scope model", ev["scope"] == "model", ev["scope"])
    check("tpm -> hint parsed", ev["retry_after"] == 5.0, ev["retry_after"])
    check("tpm -> ratelimit headers kept",
          ev["ratelimit"].get("x-ratelimit-remaining") == "0", ev["ratelimit"])

    # --- 4. Bare 429: no marker, no verdict ------------------------------
    ev = rp.attribute_rate(429, {}, b"Too Many Requests", None, None, 2)
    check("bare 429 -> unknown issuer", ev["issuer"] == "unknown", ev["issuer"])
    check("bare 429 -> unknown scope", ev["scope"] == "unknown", ev["scope"])
    check("bare 429 -> low confidence", ev["confidence"] == "low", ev["confidence"])
    check("bare 429 -> says so", ev["why"] == "no-marker", ev["why"])

    # --- 5. cf-mitigated header alone ------------------------------------
    ev = rp.attribute_rate(429, {"cf-mitigated": "challenge"}, b"", None, None, 1)
    check("cf-mitigated -> cloudflare/route",
          (ev["issuer"], ev["scope"]) == ("cloudflare", "route"),
          (ev["issuer"], ev["scope"]))
    check("cf-mitigated -> medium confidence", ev["confidence"] == "medium",
          ev["confidence"])

    # --- 6. A named model is a weak scope hint ---------------------------
    ev = rp.attribute_rate(
        429, {},
        b'{"message":"slow down for deepseek-v4.1-flash-free please"}',
        None, "deepseek-v4.1-flash-free", 1)
    check("named model -> scope model, low", (ev["scope"], ev["confidence"]) ==
          ("model", "low"), (ev["scope"], ev["confidence"]))
    check("named model -> evidence", "model-named" in ev["why"], ev["why"])

    # --- 7. Aggregates and the ring --------------------------------------
    before = rp.rate_snapshot(1)["agg"]
    total_before = before["total"]
    for _ in range(3):
        rp.attribute_rate(429, {"cf-ray": "x"},
                          b"Error code: 1015 Too many requests", None, None, 1)
    after = rp.rate_snapshot(200)["agg"]
    check("agg counts every event", after["total"] == total_before + 3,
          (total_before, after["total"]))
    check("agg by_scope has route", after["by_scope"].get("route", 0) >= 3,
          after["by_scope"])
    check("agg remembers hints", after["with_hint"] >= 1, after["with_hint"])
    check("agg first/last at set", bool(after["first_at"]) and bool(after["last_at"]),
          (after["first_at"], after["last_at"]))
    snap = rp.rate_snapshot(5)
    check("events newest first",
          all(snap["events"][i]["ts"] >= snap["events"][i + 1]["ts"]
              for i in range(len(snap["events"]) - 1)))
    check("ring capacity respected", snap["agg"]["capacity"] == rp.RATE_EVENTS_MAX)

    # --- 8. rate_cool refuses to move without a node ---------------------
    cooled_before = rp.rate_snapshot(1)["agg"]["cooled"]
    check("no node -> no cool", rp.rate_cool(None, 30, None) is False)
    check("cooled counter untouched",
          rp.rate_snapshot(1)["agg"]["cooled"] == cooled_before)

    print("\n%d checks, %d failed" % (TOTAL, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
