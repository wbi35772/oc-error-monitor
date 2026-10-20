# 四个 Windows 网络监控项目分析

> 分析对象：WireWatch、Simple NetMon、OpenProcMon、System Informer
> 代码与仓库数据截止：2026-10-19

## 0. 结论先行

这四个项目经常被放在一起比较，但它们并不是同一类工具，数据源和能回答的问题完全不同：

| 项目 | 一句话定位 | 最擅长回答 |
|---|---|---|
| **WireWatch** | WinDivert 驱动的实时连接面板 | 「哪个进程、连了哪个域名/IP、属于哪家云、可不可疑」 |
| **Simple NetMon** | 无驱动的按应用流量账本 | 「这个程序过去 90 天用了多少流量、连过哪些主机」 |
| **OpenProcMon** | 带内核驱动和 MCP 的开源 Procmon | 「这个网络请求前后，进程读了哪些文件、改了哪些注册表、调用栈是什么」 |
| **System Informer** | 老牌系统/进程透视工具 | 「这个进程的父子关系、模块、句柄、线程和当前 TCP 连接是什么」 |

如果目标只是观察 OpenCode 这类应用的联网行为：

1. **实时看域名和目标**：WireWatch 最直接（TLS SNI + DNS + ASN + 进程归属）。
2. **看长期流量与历史**：Simple NetMon 更合适（按应用/主机聚合、SQLite 持久化）。
3. **做因果取证**：OpenProcMon（文件/注册表/网络/调用栈 + PML + MCP）。
4. **核对进程上下文**：System Informer（进程树、模块、句柄、连接表）。

没有一个能单独完成全部四件事，组合使用才完整。

---

## 1. 它们分别站在哪一层

Windows 上做网络可观测，常见有四种数据源：

```
应用层         HTTPS 内容（需要解密，四者都不做）
  │
传输层         TCP/UDP 五元组、字节数
  ├── WinDivert NETWORK 层         → WireWatch（逐包）
  ├── ETW Kernel-Network           → Simple NetMon（按事件聚合）
  └── ETW NT Kernel Logger TCPIP   → OpenProcMon（网络事件 + 调用栈）
  │
内核对象层     TCP/UDP 端点表、进程句柄
  └── iphlpapi GetExtendedTcpTable → System Informer（快照轮询）
  │
进程/内核层    文件、注册表、进程、线程
  └── minifilter + 内核回调        → OpenProcMon、System Informer
```

关键差异：

- **逐包 vs 事件**：WireWatch 能看到包长、方向、TLS ClientHello；Simple NetMon 只拿到内核上报的「这次收/发了 N 字节」。
- **流式 vs 快照**：前三个是持续采集；System Informer 的网络页是枚举当前端点表，短连接可能抓不到。
- **有无驱动**：WireWatch 用 WinDivert，OpenProcMon 用 minifilter，System Informer 官方版带 KSystemInformer；只有 Simple NetMon 完全无驱动。
- **是否解密**：四者都不解密 HTTPS。能看到的域名来自 TLS SNI（WireWatch）或系统 DNS 解析结果（Simple NetMon），看不到 URL 路径和正文。

这决定了它们不是替代关系。

---

## 2. WireWatch

仓库：<https://github.com/ImMike/wirewatch> · MIT · Python · 约 1,800 行（含测试）

### 2.1 定位

一个本地运行的「谁在联网」面板：用 WinDivert 在内核层旁路复制流量，把**内核 FLOW 事件里的 PID** 和 **NETWORK 层的字节数**按五元组拼成一条流，再补上进程路径、DNS、TLS SNI、GeoIP/ASN 和威胁情报标签，最后通过 WebSocket 推到浏览器。

它不抓包存 PCAP，也不做解密，重点是**实时归因和可读性**。

### 2.2 数据流

```
WinDivert FLOW 层 (SNIFF|RECV_ONLY)
   └─ 五元组 + ProcessId ─┐
                          ├─→ FlowTable（按五元组融合）
WinDivert NETWORK 层      │      ├─ PID → 进程名/路径/用户（psutil，60s 缓存）
   └─ 逐包字节/方向 ──────┘      ├─ DNS 应答 → IP→域名
                                 ├─ TLS ClientHello → SNI
                                 ├─ HTTP 明文 → 方法/Host
                                 └─ GeoIP / ASN / 威胁列表
                                        │
                                   Engine 单线程
                                   ├─ 1s 聚合，推 WebSocket
                                   ├─ 严重级别分类（normal→alert）
                                   └─ SQLite（flows / events）
                                        │
                              FastAPI :8080 + 静态面板
```

### 2.3 关键实现

- **强制 SNIFF**：`wd.py` 把所有 WinDivert 句柄的创建收敛到 `open_handle()`，固定加 `Flag.SNIFF`。这是很关键的安全设计——不带 SNIFF 的 WinDivert 会把包从协议栈「劫走」，忘掉 re-inject 就会直接断网。
- **双句柄串行打开**：FLOW 和 NETWORK 共用一个驱动服务，同时 open 会撞上 `WinError 1058`，代码用全局锁 + 5 次重试解决。
- **端口字节序校准**：`config.FLOW_PORTS_NETWORK_ORDER` 是针对 WinDivert FLOW 结构体端口字节序的实测开关，默认主机序；仓库还带 `smoke_test.py` 做校准。
- **融合而非轮询**：FLOW 事件给 PID，NETWORK 包给字节数，两者到达顺序任意，`FlowTable` 都能处理；空闲流 120 秒回收。
- **单线程所有权**：`FlowTable` 只由 Engine 线程访问，因此内部无锁；跨线程只走队列，避免了这类工具常见的竞态。
- **被动 L7 解析**：`l7_parse.py` 从 TLS ClientHello 里取 SNI（即使 DNS 走了 DoH 也能看到主机名），从 80 端口取 HTTP 请求行和 Host。
- **本地启发式分类**：`classify.py` 不发外部请求，只根据端口、方向、进程路径（`\AppData\`、`\Temp\` 等）、公网/私网给出 normal/info/notice/warn/alert 和标签，例如 `rdp-exposed`、`unusual-location`、`nonstandard-port`。
- **外部富化**：`enrich/geoip.py` 调 ip-api.com 批量接口（免费版仅 HTTP，自带限速与冷却）；`enrich/lists.py` 加载 StevenBlack、URLhaus、Feodo 等列表做命中标记。
- **存储与接口**：SQLite 落 `data/watcher.db`；REST 有 `/api/flows`、`/api/events`，实时推送走 `/ws`，根路径挂静态面板。

### 2.4 优点

- 代码量小、注释清楚，安全边界（SNIFF）和并发模型都写得明白，适合作为二开底座。
- 「PID + 域名 + ASN + 严重级别」在一屏内看完，排查体验比裸抓包好得多。
- 不依赖 Npcap，装 Python 依赖即可跑；自带离线测试，能在不抓包的情况下验证解析逻辑。
- 对 DoH 场景仍能靠 SNI 识别大部分 HTTPS 目标。

### 2.5 局限与风险

- **项目极新**：2026-06-03 创建，只有 1 个提交、0 star，没有 CI、没有发布流程，等于一份「可运行的样稿」，生产使用前必须自己审代码。
- **主动丢弃 loopback**：`is_local_only()` 会过滤 127.0.0.1。若应用把流量交给本地代理/VPN（例如本机的 `127.0.0.1:10808`），你看到的是**代理进程**的出站流，而不是原应用的直连；原应用那条 loopback 流不会显示。
- **GeoIP 走 HTTP**：免费接口是明文 HTTP，会把目标 IP 发给第三方；对隐私敏感的场景应换成本地 MaxMind/DB-IP。
- **Python 逐包处理**：高吞吐下 NETWORK 层是瓶颈，队列满时可能丢包/丢事件；它适合桌面级观测，不适合做网关级采集。
- **无历史查询 UI**：REST 能查最近 flows/events，但面板以实时为主，长期检索能力弱于 Simple NetMon。
- **无 ECH 对策**：SNI 被 ECH 加密后只能看到 IP。

### 2.6 二开入口

| 需求 | 改哪里 |
|---|---|
| 改端口/过滤/超时 | `wirewatch/config.py` |
| 自定义严重级别与标签 | `wirewatch/classify.py` |
| 换 GeoIP 数据源 | `wirewatch/enrich/geoip.py` |
| 加自己的情报列表 | `wirewatch/enrich/lists.py` |
| 加 REST 接口 | `wirewatch/server/app.py` |
| 改面板 | `web/` |
| 改归因逻辑 | `wirewatch/attribution.py` |

---

## 3. Simple NetMon

仓库：<https://github.com/simple-netmon/oss> · Apache-2.0（另有 MIT 与 AGPL-3.0 组件） · Go · 约 1,700 行 + 30 KB Web UI

### 3.1 定位

「GlassWire 的极简开源版」：不用驱动、不用 Npcap，靠 Windows 自带的 ETW 做**按应用、按远端主机的流量记账**，数据落 SQLite，网页看历史，并能一键调用 Windows 防火墙封禁某个程序。

它关心的是**用量与历史**，不是单个请求的内容。

### 3.2 数据流

```
Microsoft-Windows-Kernel-Network (ETW)
   └─ TCP/UDP send/recv 事件（PID + 五元组 + size）─┐
                                                      ├─→ monitor 聚合
Microsoft-Windows-DNS-Client (ETW)                    │   ├─ 按 (appPath, remoteIP) 累加
   └─ 解析结果 IP→域名（DoH 也能拿到）───────────────┤   ├─ 1s tick 维护 300s 实时曲线
                                                      │   ├─ 15s flush 到 SQLite
反向 DNS（PTR，30 分钟缓存）──────────────────────────┘   └─ 每小时清理过期数据
                                                              │
                                              SQLite: apps / hosts / usage
                                                              │
                                        HTTP :8888 + 嵌入式 UI + SSE
                                                              │
                                        Windows 防火墙 COM（封禁/解封）
```

### 3.3 关键实现

- **ETW 事件号**：只处理带字节数的 8 个事件——IPv4 TCP 10/11、IPv6 TCP 26/27、IPv4 UDP 42/43、IPv6 UDP 58/59；connect/disconnect/retransmit 一律忽略。发送=出站，接收=入站。
- **PID 归属**：事件里的 `PID` 字段优先，缺失时退回 `Execution.ProcessID`；`procinfo` 缓存 PID→路径 10 秒，兼顾 PID 复用。
- **域名归属**：监听 `Microsoft-Windows-DNS-Client` 的解析结果，因此**即使浏览器用了 DoH，只要走系统解析器也能拿到域名**；拿不到时异步做 PTR 反查。这比抓 53 端口更稳。
- **聚合模型**：内存里按 `(应用路径, 远端 IP)` 聚合，15 秒写一次库；`usage` 表按时间桶存 `bytes_in/bytes_out`，默认保留 90 天。
- **Web 接口**：`/api/status`、`/api/live`、`/api/stream`（SSE）、`/api/apps`、`/api/hosts`、`/api/history`，以及防火墙的 `rules/block/unblock/revert`。
- **防火墙**：通过 COM 的 `INetFwPolicy2` / `HNetCfg.FWRule` 添加入站+出站阻断规则，规则名带应用路径标识，支持只撤销自己创建的规则。
- **无驱动**：只用 ETW + iphlpapi + 防火墙 COM，因此安装成本最低；交叉编译出单个 `netmon.exe`（当前 11.7 MB）。

### 3.4 优点

- **部署最轻**：一个 exe，不需要管理员也能启动（但流量记账和防火墙需要管理员）；不会安装内核驱动。
- **历史能力最好**：四者中唯一自带长期按应用/主机统计和保留策略的。
- **域名解析覆盖 DoH**：走 DNS-Client ETW，比只抓 53 端口更实用。
- **防火墙闭环**：看到异常应用可以直接封，不用另开工具。
- **代码干净**：分层清晰（capture/dns/monitor/store/server/firewall），适合快速改。

### 3.5 局限与风险

- **极其年轻**：2026-07-10 创建，只有 v0.1.0、2 star；**没有任何 Go 测试**，边界行为（PID 复用、时区、并发写库）需要自己验证。
- **看不到域名之外的细节**：没有 SNI、没有 URL、没有 ASN/GeoIP（README 把 GeoIP 列为后续计划），只有 IP 和解析到的名字。
- **ETW 背压丢样本**：回调里队列满时直接 `default` 丢弃，极端流量下统计会偏小。
- **按 payload 记账**：拿到的是内核上报的字节数，不含链路层/重传语义，和「网卡计数器」不会完全一致。
- **许可证要小心**：主项目 Apache-2.0，`internal/firewall` 是 MIT、内嵌 dashboard 是 **AGPL-3.0**。如果你把带 Web UI 的二进制对外分发，AGPL 的义务会被触发；闭源商用前必须评估，或替换 dashboard。
- **会改系统防火墙**：封禁是真实生效的全局规则，误封可能影响业务；脚本化调用时要加确认和回滚。

### 3.6 二开入口

| 需求 | 改哪里 |
|---|---|
| 加 GeoIP/ASN | `internal/monitor` 增加富化步骤，或扩展 `internal/procinfo` |
| 改聚合/保留周期 | `internal/monitor/monitor.go`、启动参数 `-retention` |
| 改采集事件 | `internal/capture/capture_windows.go` 的 `eventKinds` |
| 加告警规则 | `internal/monitor` + `internal/server` |
| 换前端/去掉 AGPL | `web/` 与 `internal/server/embed.go` |
| 防火墙策略 | `internal/firewall/firewall_windows.go` |

---

## 4. OpenProcMon

仓库：<https://github.com/progmboy/openprocmon> · MIT · Rust + C（内核） · 约 2.9 万行 Rust + 8,500 行 C

### 4.1 定位

用 Rust 重写 Sysinternals Process Monitor：采集**进程/线程、文件、注册表、网络**事件，带调用栈，写 PML 文件，提供 GUI、CLI 和 **MCP server**。它不是单纯的网络工具，而是「网络事件 + 前后因果」的取证工具。

### 4.2 组成

| 组件 | 规模 | 作用 |
|---|---:|---|
| `crates/sdk` | 13.2k 行 | 驱动通信、事件解析、进程跟踪、PML 读写、符号、网络 ETW |
| `crates/gui` | 10.8k 行 | 基于 GPUI/gpui-kit 的桌面界面：实时表、过滤、详情、进程树、PML |
| `crates/core` | 2.6k 行 | 与 UI 无关的分析：查询、分组、时间线、CSV/XML 导出 |
| `crates/cli` | 2.2k 行 | 采集/查询/导出子命令 + `mcp` 子命令 |
| `crates/example` | 117 行 | SDK 用法示例 |
| `kernel/` | 8.5k 行 C | minifilter（文件/注册表/进程）与通信端口 |

仓库里还保留 `cpp-backup/`（原 C++ 实现），方便对照行为。

### 4.3 数据流

```
kernel/PROCMON24.SYS (minifilter + 回调)
   └─ 文件/注册表/进程/线程事件 ──┐
                                  ├─→ SDK 解析、进程表、符号化
NT Kernel Logger (ETW)            │        │
   └─ TCP/UDP MOF + StackWalk ────┘        ├─→ PML 写入
                                           ├─→ GUI 实时表
                                           ├─→ CLI 查询/导出
                                           └─→ MCP tools（stdio）
```

网络事件**不走 minifilter**：`sdk/src/network.rs` 启动 NT Kernel Logger，启用 `EVENT_TRACE_FLAG_NETWORK_TCPIP`，消费经典 TCPIP/UDPIP MOF 事件，并按时间戳关联 StackWalk 事件拿到调用栈，最后把网络事件合并进同一条 PML 流水线。这解释了为什么网络事件的「因果」能和文件/注册表事件对齐。

### 4.4 MCP 与 CLI

`procmon-cli mcp` 是一个有状态的 stdio MCP server，提供 16 个工具：

`capture`、`start_capture`、`stop_capture`、`capture_status`、`summary`、`query_events`、`get_event`、`process_timeline`、`event_window`、`get_process`、`list_processes`、`process_tree`、`pml_info`、`export`、`list_filter_columns`、`driver_status`。

特点：

- 采集可以按进程名 + 子进程、PID、监控类别（文件/注册表/网络等）、过滤器、时长、最大体积来限定；
- 采完返回 `session_id`，后续分析工具可以直接引用，不必先落盘再传路径；
- 未提权时可通过 `orchestrate.rs` / `worker.rs` 启动一个提权 worker，用管道通信；
- 分析工具统一接受「`session_id` 或 `.PML` 路径」二选一。

这对 AI Agent 很友好：可以让 Agent 自己「抓 60 秒 → 查网络事件 → 看某进程时间线」，而不是人工点 GUI。

### 4.5 驱动与构建

- `crates/gui` 默认开启 `embedded-driver`，把 `bin/PROCMON24.SYS` 嵌进 exe，运行时释放到 `System32\Drivers`。
- 我核验了该文件的签名：**签名有效**，签署者为 `CN=Microsoft Windows Hardware Compatibility Publisher`，文件版本 4.02，产品名 `Process Monitor`，公司 `Sysinternals`。也就是说，仓库里分发的是 **Sysinternals Procmon 的微软签名驱动**，不是本项目从 `kernel/` 编译出来的驱动。
- 这带来两个问题：
  1. **法律/分发**：仓库整体是 MIT，但这枚驱动二进制来自 Sysinternals，其再分发可能受 Microsoft 的 Sysinternals 许可约束，而不是 MIT。闭源商用/二次分发前应核实，最稳妥的是用自己的 WDK 构建并签名。
  2. **供应链**：你信任的是「上游仓库没有替换这枚签名驱动」。加载前建议核对哈希与官方 Procmon 包是否一致。
- 源码构建需要 MSVC 的 `link.exe`（本机当前缺失），Rust 依赖已预取；构建 GUI 还要 GPUI 依赖，首次编译较重。

### 4.6 优点

- **因果能力最强**：网络连接能和文件、注册表、进程、调用栈放在同一条时间线上。
- **PML 兼容**：能读 Sysinternals 的 PML，生态互通。
- **MCP 原生**：四者中唯一为 AI Agent 设计了完整工具面的。
- **工程质量较好**：约 103 处 `#[cfg(test)]`/`#[test]`，模块边界清楚，错误类型和文档齐全。
- **活跃**：355 star、89 fork，2026-09 仍在发版（v0.5.0）。

### 4.7 局限与风险

- **必须提权 + 内核驱动**：攻击面最大；驱动出问题会影响整机。
- **不是流量分析器**：没有字节级统计、没有 SNI/域名富化（只有 IP 反向解析），看「用了多少流量」不如前两个。
- **实时抓取开销大**：全系统文件/注册表事件量极大，PML 很快膨胀；必须用过滤器限定进程和类别。
- **GUI 技术栈重**：GPUI 编译慢、体积大；如果只想要采集能力，用 CLI/MCP 更划算。
- **驱动源码构建门槛高**：WDK + 测试签名/正式签名，个人开发者不易完成。
- **网络事件依赖 ETW**：经典内核 Logger 同一时间通常只能有一个会话，和其他 ETW 工具可能冲突。

### 4.8 二开入口

| 需求 | 改哪里 |
|---|---|
| 给 OpenCode 做采集预设 | `crates/cli/src/mcp.rs` 的 `CaptureArgs` + `core` 的过滤 |
| 加自定义查询/分析 | `crates/core`，再在 `mcp.rs` 暴露工具 |
| 加网络富化（SNI/ASN） | `crates/sdk/src/network.rs` 之后加管道阶段 |
| 改 GUI | `crates/gui`（成本最高） |
| 换/编译驱动 | `kernel/` + WDK，自行签名 |

> 如果只想「让 Agent 能查网络事件」，不必改 GUI：直接用 `procmon-cli.exe mcp`，在 Agent 侧加一个「按进程名抓 60 秒网络事件」的提示词模板即可。

---

## 5. System Informer

仓库：<https://github.com/winsiderss/systeminformer> · MIT · C · 约 90 万行（含头文件、插件与工具）

### 5.1 定位

原 Process Hacker 的继任者，Windows 上最成熟的系统透视工具之一：进程树、线程、模块、句柄、服务、网络连接、GPU、内核对象，全部可视化，并支持插件和内核驱动。

它不是流量监控器，网络页只是「进程上下文」的一部分；但正因为它能看到进程的模块、句柄、父子关系和连接表，它是核验「这个连接到底属于谁」的好工具。

### 5.2 组成

| 目录 | 规模 | 作用 |
|---|---:|---|
| `phlib` | 21.2 万行 | 通用库：对象管理、符号、列表视图、设置、图 |
| `SystemInformer` | 16.7 万行 | 主程序与各页面（含 `netprv.c` 网络提供者） |
| `KSystemInformer` | 4.0 万行 | 内核驱动 |
| `plugins` | 23.5 万行 | 13 个插件（NetworkTools、ExtendedTools 等） |
| `phnt` | 16.5 万行 | 原生 API 头文件 |
| `kphlib` | 1.26 万行 | 驱动侧公共库 |
| `tools` | 49 万行 | 构建与辅助工具（含第三方代码） |

### 5.3 网络能力

`SystemInformer/netprv.c` 的实现路径很传统：

- 动态加载 `iphlpapi.dll`，调用 `GetExtendedTcpTable` / `GetExtendedUdpTable` 枚举 TCP/UDP 端点；
- 额外用 `InternalGetBoundTcpEndpointTable` / `InternalGetBoundTcp6EndpointTable` 拿「已绑定但未连接」的端点；
- 用工作队列和哈希表维护 `PH_NETWORK_ITEM`，按 PID 关联进程；
- 对远端地址做反向 DNS，带缓存；
- 通过 `GeneralCallbackNetworkProviderUpdatedEvent` 通知 UI 刷新。

这是**快照式**采集：它看到的是「此刻存在的连接」，短于一秒的连接可能完全错过；也没有字节数统计。`plugins/NetworkTools` 额外提供 whois、tracert 等操作；`plugins/ExtendedTools` 的 `etwstat.c` 则用 ETW 做网络统计，但那是插件层的补充。

### 5.4 优点

- **最成熟、最稳定**：16,276 star、1,806 fork，几乎每天有提交，官方发布版带签名驱动。
- **进程上下文最全**：父子进程、命令行、模块、句柄、线程栈、服务、计划任务，排查「这个陌生连接是哪个程序拉起的」非常有效。
- **插件体系**：NetworkTools、ExtendedTools、DotNetTools 等可按需加载。
- **官方构建可信**：发布包由项目签名；自己编译则要处理测试签名。

### 5.5 局限与风险

- **不是流量工具**：没有按应用的字节统计、没有历史、没有域名/SNI、没有告警。
- **轮询漏短连接**：对「一闪而过」的 HTTPS 请求不友好；不适合做请求级审计。
- **代码库巨大**：C 代码 90 万行，理解和小改的成本都高；构建需要 VS2022 + Windows SDK/WDK。
- **本地自编译驱动默认不可加载**：未签名驱动需要开启测试签名模式，不能当日常工具用。
- **UI 信息密度高**：新手上手成本高于前三个。

### 5.6 二开入口

| 需求 | 改哪里 |
|---|---|
| 加一个网络列/操作 | `SystemInformer/netprv.c` + 对应页面 |
| 写插件 | `plugins/`（有插件 SDK 与示例） |
| 改进程页 | `SystemInformer/procprv.c` 等 |
| 驱动能力 | `KSystemInformer/`（成本最高） |

---

## 6. 横向对比

### 6.1 能力矩阵

| 维度 | WireWatch | Simple NetMon | OpenProcMon | System Informer |
|---|---|---|---|---|
| 数据源 | WinDivert FLOW+NETWORK | ETW Kernel-Network | minifilter + ETW TCPIP | iphlpapi 端点表 + ETW 插件 |
| 进程归属 | ✅ PID（FLOW 事件） | ✅ PID（ETW） | ✅ PID + 进程树 | ✅ PID + 完整进程上下文 |
| 字节统计 | ✅ 逐包累计 | ✅ 按事件累计 | ❌（网络事件不带总量） | ❌ |
| 域名/SNI | ✅ SNI + DNS | ✅ DNS-Client + PTR | ❌ 仅 PTR | ❌ 仅 PTR |
| ASN/GeoIP | ✅ ip-api | ❌ | ❌ | ❌ |
| 历史持久化 | ⚠️ SQLite，查询弱 | ✅ 90 天聚合 | ✅ PML | ❌ |
| 因果/调用栈 | ❌ | ❌ | ✅ | ⚠️ 进程/线程栈，非网络因果 |
| 内核驱动 | WinDivert | ❌ | ✅ minifilter | ✅（官方版） |
| 管理员要求 | 是 | 采集/防火墙是 | 是 | 部分功能 |
| 实时性 | 高（1s 聚合） | 高（1s tick） | 高（事件级） | 低（快照） |
| AI/MCP | ❌ | ❌ | ✅ 16 个工具 | ❌ |
| 主要语言 | Python | Go | Rust + C | C |
| 代码量 | ~1.8k | ~1.7k + UI | ~29k Rust + 8.5k C | ~90 万行 |
| Stars | 0 | 2 | 355 | 16,276 |
| 许可证 | MIT | Apache-2.0 + MIT + AGPL | MIT（驱动另议） | MIT |
| 构建难度 | 低 | 低 | 中高（MSVC/GPUI） | 高（VS2022+WDK） |

### 6.2 工程成熟度

| 项目 | 活跃度 | 测试 | 文档 | 供应链风险 |
|---|---|---|---|---|
| WireWatch | 单提交，停更风险高 | 2 个离线测试 | 注释好，无正式文档 | 依赖 0-star 代码 + WinDivert |
| Simple NetMon | v0.1.0，刚发布 | 无 | README 清晰 | 年轻 + AGPL 组件 |
| OpenProcMon | 活跃发版 | ~103 处测试 | 较完整（含 MCP 指南） | 内嵌 Sysinternals 驱动 |
| System Informer | 每日提交 | 项目自带体系 | 完善 | 官方签名，最可信 |

---

## 7. 用它们观察 OpenCode 的联网行为

OpenCode 通常是 `opencode.exe`（Bun 运行时打包），会按需拉起子进程（语言服务器、MCP server、终端命令等）。因此「进程归属」不能只看主进程，最好包含子进程。

### 7.1 推荐组合

**第一步：实时定位（WireWatch）**

1. 以管理员启动 `wirewatch\start.bat`，打开 <http://127.0.0.1:8080>。
2. 在面板里按进程名筛 `opencode`，观察：
   - 远端 IP / 端口；
   - SNI 域名（例如模型 API、包管理、更新检查）；
   - ASN/组织（判断是云厂商还是自建机房）；
   - 严重级别与标签。
3. 需要程序化取数时，直接查 REST：
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8080/api/flows?limit=200 |
     Where-Object { $_.process_name -like '*opencode*' } |
     Select-Object process_name,remote_ip,remote_domain,remote_port,bytes_in,bytes_out
   ```

**第二步：长期账本（Simple NetMon）**

1. 以管理员启动 `simple-netmon\start.bat`，打开 <http://127.0.0.1:8888>。
2. 看「应用」页里 `opencode.exe` 的累计上下行、连过的主机；
3. 用 `/api/history` 拉时间序列，确认是持续连接还是周期性心跳；
4. 如发现异常目标，可在防火墙页直接封禁（会写真实规则，谨慎操作）。

**第三步：因果取证（OpenProcMon）**

当你不满足于「连了哪」，而想知道「为什么连、连之前读了什么」：

```powershell
cd openprocmon\dist\v0.5.0
.\procmon-cli.exe capture --name opencode.exe --monitor network --duration 60 --out opencode.pml
.\procmon-cli.exe summary --pml opencode.pml
.\procmon-cli.exe query --pml opencode.pml --filter 'Category == "Network"'
```

- `--name` 可重复，默认跟随子进程（`--no-children` 关闭）；
- `--monitor` 可只选 `network`，避免全量文件/注册表事件把 PML 撑大；
- `--filter` 支持 `&&`、`||`、`!`、`in (...)`，完整词表用 `procmon-cli.exe vocab` 查。

或者把它注册成 MCP server，让 Agent 自己抓和分析：

```jsonc
{
  "mcp": {
    "openprocmon": {
      "type": "local",
      "command": ["D:\\Project\\Openchamber\\Opencode网络\\openprocmon\\dist\\v0.5.0\\procmon-cli.exe", "mcp"],
      "enabled": true
    }
  }
}
```

> 具体参数名以 `procmon-cli.exe capture --help` 为准；不同版本的 CLI 可能略有差异。

**第四步：上下文核验（System Informer）**

用进程树确认 `opencode.exe` 拉起了哪些子进程，用模块/句柄页确认它加载了哪些网络库，用网络页核对当前连接。它适合「人肉确认」，不适合自动化。

### 7.2 必须知道的盲区

- **本地代理/VPN**：如果 OpenCode 走系统代理（例如本机 `127.0.0.1:10808`），WireWatch 会丢弃 loopback 流，你看到的是代理进程的出站连接；Simple NetMon 也会把「应用→代理」和「代理→远端」分成两段。此时应同时看代理日志，或临时关闭代理做对照。
- **TUN 模式**：VPN 的 TUN 会把流量变成虚拟网卡流量，进程归属可能落到 VPN 客户端。
- **HTTPS**：四者都看不到 URL 路径、请求头、响应体；WireWatch 的 SNI 只能告诉你「连了哪个域名」。
- **ECH**：Cloudflare 等启用的 Encrypted ClientHello 会把 SNI 也加密，只剩 IP。
- **HTTP/3 (QUIC)**：WireWatch 只解析 TCP 上的 TLS ClientHello，QUIC 的 SNI 需要额外解析 UDP/443；当前实现看不到。
- **短连接**：System Informer 的端点表轮询可能错过；WireWatch/Simple NetMon 的事件流更可靠。

### 7.3 预期会看到什么

以 OpenCode 这类 AI 编程工具为例，典型流量包括：

- 模型 API（HTTPS/443，SNI 为服务商域名）；
- 包管理器/依赖下载（npm、PyPI、GitHub）；
- 更新检查与遥测；
- MCP server 如果是远程 HTTP/SSE，也会有独立连接（通常在子进程里）。

具体域名以实测为准，不要预设某一家。

---

## 8. 选型建议

| 场景 | 推荐 | 理由 |
|---|---|---|
| 只想快速看「谁在连哪」 | **WireWatch** | 开箱即用，域名/ASN/进程一屏看完 |
| 想知道「这个月谁吃了流量」 | **Simple NetMon** | 唯一有长期按应用/主机账本的 |
| 要写报告/取证/查因果 | **OpenProcMon** | PML + 调用栈 + 文件/注册表关联 |
| 排查进程、模块、句柄 | **System Informer** | 系统透视最全，最成熟 |
| 给 AI Agent 用 | **OpenProcMon MCP** | 原生 16 个工具，有状态会话 |
| 企业生产/长期部署 | **System Informer + 自建采集** | 成熟度和签名链最稳；前两个太新 |
| 学习二开 | **WireWatch / Simple NetMon** | 代码量小，分层清楚 |

如果只能装一个：

- **看网络**：WireWatch；
- **看历史**：Simple NetMon；
- **看因果**：OpenProcMon；
- **看进程**：System Informer。

---

## 9. 安全、合规与供应链

1. **都需要管理员/内核权限**：WireWatch 加载 WinDivert，OpenProcMon 加载 minifilter，System Informer 官方版加载 KSystemInformer。只应在自己拥有或获授权的机器上运行。
2. **WireWatch 的 GeoIP 会外发 IP**：免费 ip-api 是 HTTP 明文；隐私敏感时换本地库。
3. **Simple NetMon 会改防火墙**：封禁规则是全局的，脚本调用要有确认与回滚。
4. **OpenProcMon 的内嵌驱动不是本项目构建的**：它是 Sysinternals Procmon 4.02 的微软签名驱动。MIT 只覆盖仓库代码，驱动的再分发权利需要单独确认；生产环境建议用 WDK 自行构建并签名。
5. **0-star 项目不等于恶意，但等于未审计**：WireWatch 和 Simple NetMon 都只有个位数 star、极短历史；在敏感环境使用前应做代码审查、依赖锁定和哈希校验。
6. **许可证**：Simple NetMon 的内嵌 dashboard 是 AGPL-3.0；对外分发含 UI 的二进制会触发 AGPL 义务。WireWatch/OpenProcMon/System Informer 为 MIT，但第三方驱动/依赖仍有各自条款。

---

## 10. 本地目录与启动脚本

| 路径 | 说明 |
|---|---|
| `wirewatch/` | Python 源码 + `.venv`；`start.bat` 启动 8080 |
| `simple-netmon/` | Go 源码 + `dist/netmon.exe`；`start.bat` 启动 8888 |
| `openprocmon/` | Rust 源码 + `dist/v0.5.0/` 预编译；`start.bat` 启动 GUI |
| `systeminformer/` | C 源码 + `dist/v4.0.26241.138/` 官方预编译；`start.bat` 启动 GUI |
| `start-all.bat` | 一次提权，逐个启动四个工具 |
| `env.ps1` | 启用本地 Go/Rust 与国内镜像 |
| `README.md` | 安装、构建、代理与工具链说明 |

脚本行为：

- 每个 `start.bat` 先检查管理员权限，不是管理员就用 PowerShell `Start-Process -Verb RunAs` 重新拉起自己；
- `start-all.bat` 只提权一次，然后为每个项目开独立窗口；System Informer 优先用源码构建产物，找不到时自动使用 `dist/` 下的官方预编译；
- 所有脚本都是 ASCII + CRLF，避免中文代码页和 `cmd.exe` 解析问题。

> 在远程会话/无交互桌面运行会卡在 UAC；请在本地桌面执行。

---

## 11. 一句话总结

- **WireWatch** 是「实时望远镜」：看得清目标，但项目太新。
- **Simple NetMon** 是「流量账本」：记得久、部署轻，但看不到请求细节。
- **OpenProcMon** 是「手术台」：因果最完整、对 Agent 最友好，但重且带驱动。
- **System Informer** 是「整机透视仪」：最成熟，但网络只是它的一个侧面。

真正好用的方案不是四选一，而是：**WireWatch 定位 → Simple NetMon 记账 → OpenProcMon 取证 → System Informer 核验**。
