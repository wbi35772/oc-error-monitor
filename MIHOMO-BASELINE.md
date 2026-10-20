# mihomo / Clash Verge 接入基线（只读侦察）

> 日期：2026-10-20 · 本文件只记录现状，**没有修改任何 mihomo 配置、规则或节点**。

## 1. 运行态

| 组件 | 进程 | 说明 |
|---|---|---|
| Clash Verge GUI | `clash-verge.exe` (PID 3228) | `D:\Network\Clash Verge\clash-verge.exe` |
| mihomo 内核 | `verge-mihomo-alpha.exe` (PID 25700) | 版本 `alpha-e4dd968` |
| 系统代理 | `127.0.0.1:7897` | 当前 Windows 系统代理指向该 mixed 端口 |

监听端口：

| 端口 | 用途 |
|---:|---|
| 7897 | mixed（HTTP + SOCKS） |
| 7898 | SOCKS |
| 7899 | HTTP |
| 9097 | external-controller（仅 `127.0.0.1`） |
| 1053 | DNS |

配置文件：`%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\config.yaml`
（由 Clash Verge 生成，内含 controller `secret`；不要把该文件提交到仓库）。

## 2. 当前策略状态

通过 controller API 实际读取：

- 模式：`rule`
- `allow-lan: true`
- `tun.enable: false`（TUN 未启用）
- 代理节点/策略条目：238
- 策略组：16（Selector / URLTest / Fallback 等）
- 规则：82 条
- 日志级别：`info`

Controller 需要 `Authorization: Bearer <secret>`；secret 从生成配置读取即可，本文不复制。

## 3. 已验证 / 可用的控制面

已实际验证：

- `GET /version` → `alpha-e4dd968`
- `GET /configs` → 模式、端口、TUN、日志级别
- `GET /rules` → 82 条
- `GET /proxies` → 238 个节点/策略组

mihomo/Clash 标准控制面还提供（下一阶段使用前逐项验证）：

- `PUT /proxies/{group}`：在策略组内切换节点（body: `{"name":"..."}`）
- `GET /providers/proxies`、`GET /providers/proxies/{name}/healthcheck`：订阅与健康检查
- `PATCH /configs`：运行时改模式/端口等
- `GET /connections`、WebSocket `/connections`：当前连接与实时事件
- `GET /logs`（WebSocket）：实时日志
- `GET /traffic`、`GET /memory`：内核吞吐/内存

## 4. 对下一阶段的关键约束

1. **TUN 关闭**：只有走系统代理或显式配置代理的应用会进入 mihomo。要让“忽略系统代理”的进程也走代理，需要启用 TUN、透明重定向或 WinDivert 类方案；仅靠 mihomo 的 `PROCESS-NAME` 规则无法捕获未到达内核的流量。
2. **allow-lan 已开启**：代理端口监听在所有接口；controller 仍只绑 loopback。后续做自动化时要确认防火墙边界，避免把代理暴露到不可信网络。
3. **Clash Verge 会重写 config.yaml**：直接改生成配置可能被覆盖。优先使用 controller API、profile merge/override，或修改 Clash Verge 的 profile 扩展。
4. **WireWatch 看不到原始进程**：本地代理流量是 loopback，WireWatch 会丢弃 loopback，或把它归到 `verge-mihomo-alpha.exe`，而不是真正发起请求的应用。做“按进程优化”时需要把 mihomo 连接表、WireWatch 事件和系统连接表做关联。
5. **节点数量大（238）**：智能切换必须有健康检查、迟滞/冷却、失败回退，避免抖动；并且只应操作专用策略组（例如已有的 OpenCode 相关组），不要直接改全局节点。

## 5. 建议的接入顺序（尚未实施）

1. 只读采集：controller `/connections` + `/traffic` + 节点健康，建立基线。
2. 定义“对话状态”信号：模型请求延迟、错误率、当前会话阶段/模型、OpenChamber 事件。
3. 先做单策略组的节点切换闭环（带冷却与回退），不动全局配置。
4. 再做按进程/规则的分流；最后评估是否需要 TUN 或透明重定向来实现真正的进程级接管。
