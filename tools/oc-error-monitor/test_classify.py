#!/usr/bin/env python3
"""Tiny regression test for the monitor's classifier.

Run:
  python tools/oc-error-monitor/test_classify.py
"""

from monitor import classify


CASES = [
    (
        "OpenCode's free models aren't available ... abusive traffic from this IP address ... FreeTierError",
        "provider.auth",
        403,
        "AUTH_IP_REPUTATION",
    ),
    (
        "OpenCode's free tier can only be used from within OpenCode. Download it at https://opencode.ai",
        "provider.auth",
        403,
        "AUTH_FREE_TIER",
    ),
    (
        "Connection lost while reading the response: ECONNRESET: The socket connection was closed unexpectedly",
        "provider.transport",
        200,
        "NET_STREAM",
    ),
    (
        "getaddrinfo ENOTFOUND opencode.ai",
        "provider.transport",
        0,
        "NET_DNS",
    ),
    (
        "Model unavailable: eac/our-free-model/opencode/deepseek-v4.1-flash-free",
        "invalid_request",
        503,
        "LOCAL_RELAY",
    ),
    (
        "429 Too Many Requests: rate limit exceeded",
        "provider.rate_limit",
        429,
        "RATE_LIMIT",
    ),
    (
        "UNKNOWN_CERTIFICATE_VERIFICATION_ERROR: unknown certificate verification error",
        "provider.transport",
        0,
        "NET_TLS",
    ),
    (
        "Step interrupted",
        "aborted",
        0,
        "ABORTED",
    ),
]


def main() -> int:
    failed = 0
    for text, error_type, status, expected in CASES:
        got = classify(text, error_type, status)
        ok = got.error_class == expected
        print(("PASS" if ok else "FAIL"), expected, "->", got.error_class, "|", got.action)
        if not ok:
            failed += 1
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
