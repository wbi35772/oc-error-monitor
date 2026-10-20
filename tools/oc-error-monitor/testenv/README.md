# Isolated test environment

Everything here runs on loopback inside this project. No Clash Verge, no real
gateway, no live network, no config file of yours is touched.

    python tools/oc-error-monitor/testenv/test_retry_proxy.py

## mock_gateway.py

A stand-in for the opencode free-tier gateway. It reproduces the behaviours the
retry proxy has to survive, on demand:

| behaviour   | what it does                                       | proxy reaction |
|-------------|----------------------------------------------------|----------------|
| `ok_sse`    | 200 + SSE stream ending with a finish_reason       | pass through   |
| `truncated` | 200 + SSE stream that stops before finish_reason   | observed, not retried |
| `empty`     | 200 with an empty body                             | retried        |
| `ip_rep`    | 403 FreeTierError "detected abusive traffic"       | retried        |
| `identity`  | 403 FreeTierError "free tier can only be used ..." | pass through, never retried |
| `rate`      | 429                                                | retried        |
| `server`    | 503                                                | retried        |

It also enforces the request-shape gate the real gateway enforces, so the
tests double as documentation of those rules:

* `User-Agent: opencode/<channel>/<version>/<client>` - three segments
* `x-opencode-client: cli`
* a `x-opencode-session*` header
* `"stream": true` in the body

Control plane (on the mock, not proxied):

    POST /__plan   {"responses": ["ip_rep", "ok_sse"], "default": "ok_sse"}
    GET  /__stats  {"counts": {...}}

## test_retry_proxy.py

Eight scenarios, 26 checks:

1. 403 abusive traffic is retried and rescued
2. identity 403 is passed through without a retry
3. 200 with an empty body is retried
4. all attempts failing ends as 403 with the upstream body, counted exhausted
5. 429 is retried
6. a truncated SSE stream is observed (truncated_streams) and not retried
7. a dead upstream produces 502 with a named error
8. a second instance on the same port exits quietly

The proxy under test is a copy of `../retry_proxy.py` placed in `_run/`, so its
logs land in `_run/` and never mix with the live instance.
