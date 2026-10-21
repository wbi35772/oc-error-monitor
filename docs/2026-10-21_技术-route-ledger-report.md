# 2026-10-21_技术-route-ledger-report

> 让「出口节点」从一个人肉开关，变成一个会自己记账、自己换人的账本
- **case_id**: `20261021-route-ledger`
- **scope**: [`work/20261021-route-ledger/scope.md`](../work/20261021-route-ledger/scope.md)
- **evidence**: [`work/20261021-route-ledger/evidence/`](../work/20261021-route-ledger/evidence/)，E-001…E-011，索引见 `INDEX.md`
- **timeline**: [`work/20261021-route-ledger/timeline.md`](../work/20261021-route-ledger/timeline.md)
- **修复提交**: `1ea68f4 settings: 让 force_route 和路由池活过重启 (L0)`、`e6536a9 route: 用账本决定出口，失败就换成员 (L1-L3)`
- **受影响组件**: `oc-error-monitor/v1.2.0-dev`（开发版 8899）。生产 `v1.1.0`（8788）本轮未改代码，行为差异见 §5.3

---

## 1. 摘要

上一版把候选表修好了：218 个成员全部可见，每个都有明确结论。但那 218 个成员**只用来给人看**，代理自己并不使用它们——它只有一个 `force_route` 开关，`direct` 或者 `proxy`，二选一，选完就定。

这一版把这个开关换成三样东西：

1. **池**：成员从 mihomo 组实时发现，`DIRECT` 作为伪成员入池。订阅换了、cfnew 部署了新 Worker，代理零改动就认得。
2. **账本**：每个成员记自己的成败，带半衰期衰减。能用的成员就一直用，坏了才换。
3. **策略**：换不换、换到哪，由注册的策略决定，失败类是唯一触发条件。

过程中挖出三个**真缺陷**，每一个都是发一个真实请求才暴露的，而它们叠加起来的效果是：**代理看上去在换节点，实际上没有；该换的时候，也不换**。

| 缺陷 | 表面症状 | 真实原因 | 证据 |
|---|---|---|---|
| F-001 | 重启后路由设置回到出厂值 | 设置只在内存，`settings.json` 不存在 | E-001 |
| F-002 | 改了默认值，老实例不认 | route 块整块落盘，把默认值冻死 | E-002 |
| F-003 | 看上去在轮换，`switches` 恒为 0 | 策略分支从不写回账本，sticky 永不生效 | E-003 |
| F-004 | 死节点连撞四次，谁都不冷却 | 账本吃日志词汇，与可轮换类没有交集 | E-004 |
| F-005 | 池子被静默绕过 | 零数据时并列，DIRECT 排序胜出 | E-005 |
| F-006 | 整族逐个撞 | 96/216 的成员每轮全死，且扎堆 | E-006 |

**最重要的量化结论**：216 个可测成员里 **96 个（44%）是结构性全死**（承接上一版报告 E-009/E-010），而这 96 个**不是散落的，是扎堆的**——`优选域名` 66 个 + `IPv6优选` 30 个，两个族，每轮 0 存活。逐成员冷却等于逐个撞完整族：实测一个请求连撞 `IPv6优选-01` → `-02` → `-03` → `-04`，四个死节点各失败一次。所以必须有**族级冷却**。

---

## 2. Scope 摘要

| 项 | 值 |
|---|---|
| auth.status | granted（own_system：本机回环代理 + 用户自己的 Clash Verge 配置） |
| network_profile | authorized_target_only |
| in_scope.assets | `http://127.0.0.1:8899/__pool`、`/__route`、`/__stats`、`http://127.0.0.1:9097/proxies`、`http://127.0.0.1:8788/__health` |
| out_of_scope | dos、phishing_real_users、unrestricted_exfil |
| ready_for_act | true |

分析对象是*用户自己机器上的本地代理与面板代码*，未触达任何第三方资产。

---

## 3. 证据

| id | 观察 | source_ref | severity |
|---|---|---|---|
| [E-001](../work/20261021-route-ledger/evidence/E-001.md) | 设置只存内存：重启后 force_route 回空，runtime 目录无 settings 文件 | command | high |
| [E-002](../work/20261021-route-ledger/evidence/E-002.md) | route 整块落盘会冻死默认值：改了 rotate_on，老实例仍报旧值 | command | high |
| [E-003](../work/20261021-route-ledger/evidence/E-003.md) | pick_route 从不写回 current：sticky 永不生效，switches 恒 0 | command | high |
| [E-004](../work/20261021-route-ledger/evidence/E-004.md) | ERR:URLError 不在可轮换类里：四个死节点各失败一次，无人被冷却 | command | high |
| [E-005](../work/20261021-route-ledger/evidence/E-005.md) | 零数据时 DIRECT 按名排序胜出，安全网静默接管整个池 | command | medium |
| [E-006](../work/20261021-route-ledger/evidence/E-006.md) | 96/216 每轮全死且扎堆在两个族，逐成员冷却会逐个撞完 | command | high |
| [E-007](../work/20261021-route-ledger/evidence/E-007.md) | member_health 读路径建档：route_status 让 clear 看起来没生效 | command | medium |
| [E-008](../work/20261021-route-ledger/evidence/E-008.md) | 生产 8788 重启后回到 attempts=4 / route=null（v1.1.0 无持久化） | command | info |
| [E-009](../work/20261021-route-ledger/evidence/E-009.md) | 真机请求：adopt → sticky；失败时 switches=1 且下一次换节点 | command | info |
| [E-010](../work/20261021-route-ledger/evidence/E-010.md) | 面板五视图实测渲染正常，路由视图 218 行成员表 | screenshot | info |
| [E-011](../work/20261021-route-ledger/evidence/E-011.md) | 我自己用 PowerShell 破坏了 ui.html（ANSI 读 UTF-8）并修复 | command | info |
| [E-012](../work/20261021-route-ledger/evidence/E-012.md) | 429 在 direct 与 proxy 两条路径上先后出现：限流键不在出口 IP | command | high |

---

## 4. 设计

### 4.1 三层结构

```
池成员  = mihomo 组内所有节点（实时发现） + 伪成员 DIRECT
每成员账 = {ok, fail, fail_by_class, last_ok_ts, last_fail_ts,
             ewma_ms, cooldown_until, decay_ts}
选择器  = 字符串注册的 strategy，不是枚举
```

### 4.2 三个来源，优先级从高到低

| 优先级 | 来源 | 语义 |
|---|---|---|
| 1 | `select.pin` | 显式成员（含 DIRECT），压过一切 |
| 2 | `SETTINGS.force_route` | 旧的两值开关，仍然认：`direct` = 用 DIRECT 成员，`proxy` = 由 mihomo 决定 |
| 3 | `select.strategy` | 在健康候选里挑一个，能用就一直用 |

### 4.3 两层健康，不合并

| 层 | 手段 | 回答的问题 | 触发 |
|---|---|---|---|
| L1 | 探针结论（`/__net/probe`） | 节点还在吗 | 探针判死 → 跳过，直到结论过期（`probe.stale_s`） |
| L2 | 真实请求的成败 | 网关认不认这个出口 | 命中 `rotate_on` 的失败类 → 冷却 `cooldown_s` |

混在一起会出事：403/429 表示网关拒绝，不代表节点已死；把还能用的节点判死，等于自己缩池子。

### 4.4 词汇翻译层

`classify()` 说的是**日志**需要说的话（`IP_REP` / `RATE` / `5XX` / `ERR:URLError`），`controller.SWITCH_CLASSES` 说的是**什么允许移动出口 IP**（`NET_DNS` / `NET_CONNECT` / `NET_STREAM` / `NET_TIMEOUT`）。两套词不重叠，`policy_class` 负责翻译：

| 输入 | 输出 | 依据 |
|---|---|---|
| `OK` | `OK` | — |
| `EMPTY_STREAM` | `NET_STREAM` | 200 之后流断了 |
| `IP_REP` | `AUTH_IP_REPUTATION` | 会话层处理，不动出口 |
| `IDENTITY` | `AUTH_FREE_TIER` | 换 provider，不是换节点 |
| `RATE` | `RATE_LIMIT` | 退避，不轮换 |
| `5XX` | `UPSTREAM_5XX` | 上游问题，不轮换 |
| `CF_UA` | `CLIENT_REQUEST` | 请求问题 |
| `ERR:*` | 归一化 token 流判定 | getaddrinfo→NET_DNS；timeout→NET_TIMEOUT；certificate→NET_TLS；connectionrefused/reset→NET_CONNECT；ssl/eof/handshake→NET_STREAM |
| 其它 | 原样返回 | **只记账，永不轮换** |

最后一行是安全默认：错一次轮换会动所有人的出口 IP。

### 4.5 族级冷却

`select.family_threshold`（默认 3）与 `select.family_cooldown_s`（默认 900）：同一族里连续 N 个成员在 `window_s` 内失败，整族跳过 N 秒。

- 孤立节点（族里只有它自己，例如一个没有 `-NN` 后缀的 cfnew Worker）**不被同族连坐**。
- `rotate_on` 仍然只认 `controller.SWITCH_CLASSES`，并由测试钉住，两套词不能各说各话。

### 4.6 不写死的四条保证

1. `strategy` 用字符串注册，新增策略不改 schema。
2. `rotate_on` 是可扩展列表。
3. **route 块只落盘与默认不同的部分**：默认值改了能传到已经有 `settings.json` 的实例，显式改过的仍然保留。
4. 未知键在顶层 / `route` / `select` 三层都原样保留，旧代码不吃新字段。

### 4.7 状态文件

| 文件 | 内容 | 写入时机 |
|---|---|---|
| `settings.json` | `SETTINGS` + `ROUTE`（差量） | 每次 `/__control` 改动后，原子写 |
| `route_state.json` | 成员账本 + 族账本 + 当前成员 | 记账后按 `persist.fsync_s` 节流写，退出时强制写 |

`paused` **不写盘**：忘了的暂停在重启后看起来跟这次修的 bug 一模一样。

---

## 5. 影响

### 5.1 开发版 8899

- 新增 `GET /__pool`、`GET /__route`、`POST /__route`（动作 `pin` / `strategy` / `set` / `clear` / `switch` / `reset_current`）。
- 面板新增「路由」视图：当前出口与选择依据、策略与旋钮、被整族判死的线路、218 个成员的健康表、可锁定任一成员。
- 日志每条请求新增 `member` 与 `via` 两个字段：换没换、为什么换，日志里看得见。

### 5.2 行为变化

| 场景 | 之前 | 现在 |
|---|---|---|
| 重启 | 路由设置回出厂值 | 从 `settings.json` 读回 |
| 成员失败 | 无记忆，只换一次 | 记进账本，冷却 `cooldown_s` |
| 同一族多个死节点 | 逐个撞 | 第三次失败整族跳过 |
| 出口选择 | mihomo 决定 / 手工二选一 | 账本 + 策略决定，可锁定 |

### 5.3 生产 8788 未变

生产跑的是 v1.1.0，本轮**没有改它的代码**。它仍然不持久化设置（E-008）：重启后 `force_route` 回 `null`、`attempts` 回 4。这一点在 §7 的待办里。

---

## 6. 验证

| 项 | 方法 | 结果 |
|---|---|---|
| 单元与回归 | `python test_classify.py` / `test_net_candidates.py` / `test_l0_settings.py` / `test_route_policy.py` | 8 + 11 + 15 + 41 = **75 例全绿** |
| 持久化 | 真机重启（pid 18144→2340）后 `force_route` / `attempts` 存活 | 通过（E-009） |
| 池发现 | 真机 `/__pool` | 218 成员 / 216 可测 / DIRECT 在池 |
| 粘性与轮换 | 真机请求 | `via=adopted` → `via=sticky`；失败后 `switches=1` 且下次换节点（E-009） |
| 面板 | 浏览器实测五视图 | 宽度正常，路由视图 218 行（E-010） |
| 生产隔离 | 8788 pid / uptime / 设置 | 本轮代码未动生产（E-008 记录其重启行为） |

---

## 7. 结论与待办

| id | 结论 / 待办 | 状态 |
|---|---|---|
| P-001 | 路由必须由账本决定，不能是人肉开关 | 已实现 |
| P-002 | 设置必须落盘，否则重启即回归 | 已实现（L0） |
| P-003 | 失败类词汇必须统一到 `controller.SWITCH_CLASSES` | 已实现并由测试钉住 |
| P-004 | 死节点成族，冷却必须成族 | 已实现 |
| P-005 | 生产 8788 仍不持久化，重启后回 `null`/4 | **待办**：L0 回移生产 |
| P-006 | L1 探针结论与 L2 真实成败目前未合并消费（探针只用于 `lowest_latency` 排序与跳过已判死者） | 待办：接上自动探针 |
| P-007 | 429 的成因（按出口 IP 记）仍是推断，未做对照实验 | **已有强反证，待受控确认**（E-012） |

### 下一步（校验迭代）

P-007 是这一版最该先做的实验：**429 到底是不是按出口 IP 记的**。同模型、同一时段、direct 与 proxy 交替各 N 次。

**这一版已经有了强反证（E-012）**：今天 12:00:33 走 direct（家庭宽带 IP）、12:04:53 走 proxy（Cloudflare 出口），相隔 4 分钟，两条完全不同的出口**都撞上了 429**。而今早 00:47–05:27 那段，direct 是 197/213 成功、0 次 429，proxy 是 67.9%、43 次 429。

两个结论：

1. 「direct 更稳」是**时段性**的，不是结构性的——家庭宽带 IP 一样会被限。
2. 429 的限流键**不（只）在出口 IP**，更可能在账号 / 模型 / 网关会话这一层。这与仓库早就记下的回测一致：换模型 8/8，换节点 33–83%。

所以「多路出口、失败就换」这一整套的价值落在 **403 与网络类失败**上（那部分确实该换 IP，比如 12:04:53 那次混进来的 `ERR:RemoteDisconnected`），而不是 429。429 面前换出口 IP 是徒劳的。

**因此校验迭代的第一个题目应该是受控的 429 归因实验**，而不是继续加路由功能。若实验确认 429 与出口无关，杠杆就明确落在换模型（会话层已有 `session_failover` 在做）；若确认有出口成分，策略才需要去挑低限流出口。

生产 8788 什么时候回移 L0，取决于实验结论和用户的校验节奏——回移本身是机械的（L0 已在 dev 验证过真重启存活）。

---

## 8. 相关文件

| 路径 | 说明 |
|---|---|
| `oc-error-monitor/v1.2.0-dev/retry_proxy.py` | L0–L3 主体：`/__pool`、`/__route`、`policy_class`、账本、策略、族冷却 |
| `oc-error-monitor/v1.2.0-dev/test_route_policy.py` | 41 例：粘性、轮换、优先级、族冷却、词汇、账本持久化、动作 |
| `oc-error-monitor/v1.2.0-dev/test_l0_settings.py` | 15 例：持久化、未知键保留、夹紧、池发现 |
| `oc-error-monitor/v1.2.0-dev/ui.html` | 路由视图 + 成员健康表 |
| `docs/2026-10-21_技术-net-candidate-table-report.md` | 上一版：候选表与测速缺陷（E-006 的来源） |
| `work/20261021-route-ledger/` | 本报告的 case 目录 |
