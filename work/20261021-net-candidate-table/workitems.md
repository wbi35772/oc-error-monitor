# Work Items

| ID | title | role | targets | surface | status | evidence | notes |
|----|-------|------|---------|---------|--------|----------|-------|
| WI-001 | Establish scope and auth | lead | case | process | done | | own_system, loopback only, ready_for_act=true |
| WI-002 | Sample the controller group delay endpoint and characterise its omission semantics | lead | mihomo controller | local API | done | E-001 | 5 runs: 48/52/58/60/62 of 218; absent 156-161, marked-dead 0-1 |
| WI-003 | Cross-check with a per-node probe of all 218 members | lead | mihomo controller | local API | done | E-004 | 27.8s, 53 alive / 165 HTTP 504 |
| WI-004 | Locate the backend truncation and the dead classification column | lead | retry_proxy.py, Clash Script.js | source | done | E-002, E-003 | [:80] of 218; preferred regex matches 0 of 218 |
| WI-005 | Write the regression test and make it red on the old code | lead | test_net_candidates.py | test | done | E-005 | RED 11 errors on old code, GREEN 11 OK on new |
| WI-006 | Implement the fix (backend per-node probe + full member list; frontend 6-column table, alive filter, progress polling) | lead | retry_proxy.py, ui.html | code | done | E-005 | commit efe6758 |
| WI-007 | Deploy dev only and confirm production is untouched | lead | 8788 / 8899 | process | done | E-007 | prod pid 7004 unchanged, uptime growing, upstream still answers |
| WI-008 | Live probe over all 216 probeable candidates | lead | /__net/probe | local API | done | E-006 | tested=216 alive=75 dead=141 in 36s; fastest 多线-26 381ms |
| WI-009 | Verify the rendered page, the alive filter and the switch action | lead | /__ui#/network | browser | done | E-008 | 218 rows, filter 75 rows, console 0 errors, switch 多线-06 → 多线-26 |
| WI-010 | Write the report and the anonymized field journal | lead | project docs | document | done | E-001..E-008 | docs/ report + notes/field-journal.md in the project case dir |
| WI-011 | Summarise the probe result per node family and check it is stable, not jitter | lead | /__net/probe | local API | done | E-009 | 优选域名 66 and IPv6优选 30 are 0-alive in every run; the other four families flip only 6.0% between runs |
| WI-012 | Locate the cause of each dead family, outside the proxy | lead | Clash profile + mihomo | local config | done | E-010 | ECH hostnames fail the handshake; IPv6 literals fail because ipv6=false and the host has no public IPv6 egress. Live mihomo flags left untouched |

## Coverage
- [x] Recon/analysis complete for in_scope assets
- [x] Critical/High candidates triaged (or N/A for pure RE) — 4 high, 1 medium, 1 low; all validated
- [x] Validated findings have Evidence (E-*) — F-001..F-006 all reference E-ids
- [x] Path documented (attack/call/solve) — P-001 callflow, P-002 data flow
- [x] Timeline continuous across major phases — 12 append-only entries
- [x] Report via docs-generator — docs/2026-10-21_技术-net-candidate-table-report.md
- [x] field-journal anonymized — notes/field-journal.md (written to the project, not the shared upstream mainline)

## Findings
| ID | title | severity | status | evidence |
|----|-------|----------|--------|----------|
| F-001 | Candidate table truncated at 80 of 218; the fastest nodes were invisible | high | validated | E-002, E-008 |
| F-002 | Group delay endpoint omits timed-out members, so "dead" read as "untested" | high | validated | E-001, E-004, E-006 |
| F-003 | "优选/普通" column was dead code (regex matched 0 of 218) | medium | validated | E-003, E-008 |
| F-004 | Non-probeable members (nested group, DIRECT) mixed into the candidate pool | low | validated | E-004, E-006, E-008 |
| F-005 | 优选域名 family (66) structurally dead: ECH handshake cannot complete | high | validated | E-009, E-010 |
| F-006 | IPv6优选 family (30) structurally dead: ipv6=false and no public IPv6 egress | high | validated | E-009, E-010 |

## Refs
- skills/ops/timeline-workitem.md
- skills/ops/evidence-finding-path.md
