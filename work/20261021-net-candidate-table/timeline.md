# Timeline (append-only)

## 2026-10-21T04:17:12.0835790-07:00 | lead | init
- action: case-init
- command_or_ref: skills/scripts/case-init.ps1
- result_summary: case directory created; scope ready_for_act=true
- artifacts: [scope.md, workitems.md]
- evidence_ids: []
- next: open PRIMARY SKILL.md and ACT within scope

## 2026-10-21T04:18:00 | lead | root-cause
- action: sample the controller group delay endpoint five times before changing any code
- command_or_ref: mihomo_diag5.py (GET /group/{name}/delay, timeout 5000/10000/20000)
- result_summary: returned 48/52/58/60/62 of 218 members; marked-dead stayed 0-1 while absent held at 156-161 -- the endpoint omits members that time out instead of reporting a timeout
- artifacts: [evidence/E-001.md]
- evidence_ids: [E-001]
- next: cross-check with a per-node probe of all 218 members

## 2026-10-21T04:19:00 | lead | root-cause
- action: probe every member individually with a 24-worker pool, 5000ms timeout
- command_or_ref: mihomo_diag3.py (GET /proxies/{name}/delay per member)
- result_summary: 218 members in 27.8s, 53 answered, 165 returned HTTP 504 -- cross-checks the omission theory and shows the real alive/dead split
- artifacts: [evidence/E-004.md]
- evidence_ids: [E-004]
- next: locate the backend truncation and the dead classification column

## 2026-10-21T04:20:00 | lead | root-cause
- action: read the pre-fix backend and the subscriber's own Clash script
- command_or_ref: git show 8ca8563:oc-error-monitor/v1.2.0-dev/retry_proxy.py
- result_summary: members sliced to [:80] of 218 while the group order puts the fastest nodes at index 102/171/184; the subscriber's "preferred" regex matches 0 of 218 node names, so the 性质 column was constant and its sort a no-op
- artifacts: [evidence/E-002.md, evidence/E-003.md]
- evidence_ids: [E-002, E-003]
- next: write the regression test and make it red on the old code

## 2026-10-21T04:21:00 | lead | test
- action: write test_net_candidates.py, then run it against the old code by stashing the fix
- command_or_ref: git stash push -- retry_proxy.py; python test_net_candidates.py
- result_summary: RED on old code (11 errors, FAILED); GREEN on new code (11 passed, OK)
- artifacts: [oc-error-monitor/v1.2.0-dev/test_net_candidates.py, evidence/E-005.md]
- evidence_ids: [E-005]
- next: implement the fix

## 2026-10-21T04:22:00 | lead | implement
- action: backend per-node probe in a background thread, full member list, family derivation, probeable flag; frontend 6-column table, alive-only filter, start+poll probe
- command_or_ref: oc-error-monitor/v1.2.0-dev/retry_proxy.py, ui.html
- result_summary: commit efe6758; every candidate now ends with a delay or an explicit timeout, and no candidate can be dropped by a slice
- artifacts: [evidence/E-005.md]
- evidence_ids: [E-005]
- next: deploy to dev only and confirm production is untouched

## 2026-10-21T04:23:00 | lead | deploy
- action: deploy dev only, then compare production pid/uptime before and after
- command_or_ref: deploy.ps1 -Version 1.2.0-dev; GET http://127.0.0.1:8788/__health
- result_summary: prod pid 7004 unchanged with uptime still growing; dev live on 8899 pid 12780; a real request through 8788 still reached the upstream (404 = upstream replied)
- artifacts: [evidence/E-007.md]
- evidence_ids: [E-007]
- next: run the live probe over all 216 probeable candidates

## 2026-10-21T04:24:00 | lead | verify
- action: run the live probe and poll progress to completion
- command_or_ref: GET /__net/probe?timeout=5000&start=1 then ?start=0
- result_summary: tested=216 alive=75 dead=141, alive+dead==216 in 36s; fastest 多线-26 381ms
- artifacts: [evidence/E-006.md]
- evidence_ids: [E-006]
- next: verify the rendered page and the switch action in a browser

## 2026-10-21T04:25:00 | lead | verify
- action: drive the network view in a browser and read the table, the filter and the switch
- command_or_ref: Playwright on http://127.0.0.1:8899/__ui#/network
- result_summary: 218 rows in 6 columns, alive-only filter 75 rows, console 0 errors, switch moved the line from 多线-06 to 多线-26 (381ms)
- artifacts: [evidence/E-008.md, report/net-candidates-fixed.png]
- evidence_ids: [E-008]
- next: write the report and the anonymized field journal

## 2026-10-21T04:30:00 | lead | report
- action: write the report with the Evidence -> Finding -> Path chain, plus an anonymized field journal in the project case dir
- command_or_ref: docs/2026-10-21_技术-net-candidate-table-report.md, notes/field-journal.md
- result_summary: report references E-001..E-008 with repro commands; journal written to the project (the shared upstream field-journal mainline was deliberately not edited)
- artifacts: [docs/2026-10-21_技术-net-candidate-table-report.md, notes/field-journal.md]
- evidence_ids: [E-001, E-002, E-003, E-004, E-005, E-006, E-007, E-008]
- next: commit the report and case artifacts
