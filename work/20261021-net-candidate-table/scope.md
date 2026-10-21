# Case Scope

## meta
- case_id: 20261021-net-candidate-table
- created: 2026-10-21T04:17:12.0835790-07:00
- operator: local
- primary_skill: reverse-engineering/SKILL.md
- primary_id: R0
- lead_role: lead
- specialist_roles: []
- hint: network proxy dashboard defect analysis

## auth
- status: granted
- basis: own_system
- evidence_of_auth: own workstation; local loopback proxy + own Clash Verge profile
- MUST NOT proceed if status != granted

## in_scope
- assets:
  - http://127.0.0.1:8899/__net
  - http://127.0.0.1:8899/__net/probe
  - http://127.0.0.1:8899/__ui
  - http://127.0.0.1:8788/__health
  - http://127.0.0.1:9097/proxies
- surfaces: [local HTTP API, local dashboard page, local mihomo controller]
- activities: [read controller responses, probe candidate latency, read source, write regression test, deploy dev build, drive local page]

## out_of_scope
- assets: []
- activities: [dos, phishing_real_users, unrestricted_exfil]

## network_profile
- mode: authorized_target_only
- notes: |
    offline | lab_only | authorized_target_only | unrestricted_lab
    Change mode only after auth.status = granted.

## deliverables
- report: true
- field_journal: true
- diagrams: true
- timeline: true

## constraints
- timebox: {}
- stealth: low
- data_handling: anonymize

## signoff
- ready_for_act: true
- checklist:
  - [x] auth.status = granted
  - [x] in_scope.assets non-empty OR offline sample path set
  - [x] network_profile.mode chosen
  - [x] out_of_scope reviewed — only local loopback assets; no third-party system touched
  - [x] roles assigned (see skills/ops/role-map.md) — single lead, no specialists needed

## ops_refs
- skills/ops/scope-contract.md
- skills/ops/evidence-finding-path.md
- skills/ops/role-map.md
- skills/ops/timeline-workitem.md
- skills/ops/IDENTITY.md