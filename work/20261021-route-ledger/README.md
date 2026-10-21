# case 20261021-route-ledger

把出口节点从人肉开关变成会记账、会自己换人的账本。

| 项 | 值 |
|---|---|
| case_id | `20261021-route-ledger` |
| 报告 | [`docs/2026-10-21_技术-route-ledger-report.md`](../../docs/2026-10-21_技术-route-ledger-report.md) |
| 受影响组件 | `oc-error-monitor/v1.2.0-dev`（开发版 8899） |
| 生产 | `oc-error-monitor/v1.1.0`（8788）本轮未改代码 |
| 提交 | `1ea68f4`（L0 持久化与池发现）、`e6536a9`（L1–L3 账本与策略） |

## 目录

- `scope.md` — 范围与边界
- `timeline.md` — 事件时间线
- `workitems.md` — 做了哪些事、结果如何
- `evidence/` — 证据 E-001…E-011，索引见 `INDEX.md`
- `notes/field-journal.md` — 过程笔记（脱敏）

## 一句话

三个真缺陷（策略不写回账本、词汇不统一、DIRECT 抢平手）叠加起来，让代理**看上去在换节点、实际上没有**；修完之后还发现必须按族冷却，因为 44% 的成员是成族死掉的。
