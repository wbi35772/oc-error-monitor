# 项目状态总览

> 生成时间：2026-10-20　｜　分支：`master`（运行中） / `feature/docs-tooling`（实验基线）
> 运行时数据来自 `oc-error-monitor/v1.0.0/testenv/docs_baseline/snapshot_state.py`

## 一句话

OpenCode 免费模型（不登录、不用 key）在本机跑着一层请求级重试代理 + 三层看门狗，
44 个模型全部指向它，OpenChamber 的自动路由已经打开。现在是可日常使用状态。

---

## 1. 架构：三层防线

```
OpenCode CLI / OpenChamber
        │  http://127.0.0.1:8788/v1
        ▼
┌──────────────────────────────────────┐
│ P4  请求级重试代理 (retry_proxy.py)    │  ← 唯一入口
│  · 身份闸与 IP 信誉分别判定           │
│  · 429 走更长的退避阶梯               │
│  · SSE 断流只观测不重放               │
│  · 端口独占 + 零停机换手               │
└──────────────────────────────────────┘
        │  上游
        ▼
   opencode.ai 免费层  ──(失败时)──▶  mihomo :7899  ──(拒绝时)──▶ 直连
```

| 层 | 组件 | 作用 |
|---|---|---|
| P1 | `monitor.py` | 分类器：把错误分成 8 类（OK / IDENTITY / IP_REP / RATE / HTTP400 / 网络错误 …） |
| P2 | `controller.py` | 策略层：按分类决定退避、换路由、换模型（24 个用例全绿） |
| P3 | `session_failover.py` | 会话级故障转移：单次请求失败时在模型之间切换 |
| P4 | `retry_proxy.py` | 请求级重试代理：真正的入口，44 个模型全部走它 |
| 看门狗 | `supervisor.py` + 计划任务 | 代理挂了约 3.2 秒自愈；端口变更时自动改写 `opencode.json` |

## 2. 两个 403 是两回事（关键设计）

| 现象 | 判定 | 处理 |
|---|---|---|
| `FreeTierError: free tier can only be used from within OpenCode` | **身份闸**：请求形状不是 OpenCode 客户端 | **永不重试**——重试没有意义，代理只转发原样的 header/body |
| 检测到滥用流量 | **IP 信誉**：按请求随机出现 | 退避重试（0.4/1.0/1.8/3.0/4.5s） |
| 429 限流 | 速率 | 独立退避阶梯（2/5/9/15/25s），比 403 更有耐心 |

> 重要推论：**第三方工具无法直接用免费层**。身份闸会检查请求体形状（是否像 OpenCode
> 自己的 system prompt），只补 header 会被识别。所以本项目另造了
> `cli_llm_server.py`：用**真 CLI** 当后端提供 OpenAI 兼容端点，让任何工具合法用上免费模型。

## 3. 当前运行态（快照实测）

| 项 | 值 |
|---|---|
| 代理端口 | `8788`（8787 被 playwright-mcp 的 `node.exe server.mjs` 永久占用，不争） |
| 代理健康 | `ok=true`，`service="opencode retry proxy"`，pid 704 |
| 模型接线 | **44 / 44** 全部指向 `http://127.0.0.1:8788/v1` |
| OpenChamber 路由 | `enabled=true`，fallback `opencode/deepseek-v4.1-flash-free`，`minConfidence=0.6` |
| 看门狗（每分钟） | 已安装 `OpenCode Retry Proxy` |
| 看门狗（登录触发） | **未安装**——ONLOGON 任务需要管理员权限，`start-all.bat` 才能装上 |
| 错误分类累计 | OK 301 / RemoteDisconnected 145 / RATE 43 / IDENTITY 6 / HTTP400 2 |
| 隔离测试 | 14 个场景 / **48 条断言全绿**（`testenv/test_retry_proxy.py`） |

## 4. 已提交的四个版本

```
629ff82  fix: give a rate limit more patience than a transient 403
37749b9  feat: dashboard, zero-downtime handover and port ownership
d9bf062  fix: add a logon trigger and document the three-layer watchdog
4e9cda6  feat: P4 request-level retry proxy for the free tier
f1344bb  (基线) Backtest resilience policies against real history
```

均未 push。

## 5. 关于「脱离 Clash Verge 独立运行」

**已经可以。** 实测把代理强制切到直连（模拟 Clash 掉线）：

```
direct run1 -> 'ok'     direct run2 -> 'ok'
direct run3 -> 'ok'     direct run4 -> 'ok'
```

直连比走 mihomo 更抖（同期的 `RemoteDisconnected` 明显更多），但这些都被代理的
重试吸收掉了。代理的路由策略是「先走 mihomo，连接被拒就直接切直连」，
所以 Clash Verge 关掉不影响可用性，只是失败率会短暂上升。

## 6. 模型稳定性

用户记的那个「一直存在的官方大模型」是 **`opencode/big-pickle`**：

- 在 zen 模型列表里**排第一位**（`/zen/v1/models` 共 86 个）
- 实测 3/3 成功
- 名字里没有 `-free` 后缀，但它是 OpenCode 免费层提供的大模型

**建议**：路由 fallback 与 Hard 档固定用 `big-pickle`；其余档位用 `deepseek-v4.1-flash-free`
之类的稳定轻量模型。免费层模型会 churn，但 `big-pickle` 是结构性存在的那个。

## 7. 本分支（`feature/docs-tooling`）新增

| 文件 | 作用 |
|---|---|
| `oc-error-monitor/v1.0.0/cli_llm_server.py` | OpenAI 兼容端点，后端是**真 OpenCode CLI**，让第三方工具合法用上免费层（提示词走 stdin，避免 Windows 命令行长度上限） |
| `testenv/docs_baseline/snapshot_state.py` | 状态快照 + 差异判定，用来**证明**实验没有影响在跑的版本 |
| `testenv/docs_baseline/run_readmex_test.ps1` | 隔离跑 readmeX：独立端口 8791、独立 scratch、只走环境变量配置、跑前跑后各快照一次 |
| `testenv/docs_baseline/flow_demo.html` | 零依赖流场动画文档页（canvas 流场 + 渐变位移 + 数据流带） |

工作区：`D:\Project\Openchamber\opencode-network-docs`（`git worktree` 独立检出，
master 那份一个字没动）。

## 8. 已知问题

1. **登录触发看门狗缺失**——需要管理员权限运行 `start-all.bat`。
2. **SSE 断流不能救**——上游在 `finish_reason` 之前断开，字节已上线，只能记
   `truncated_streams`，无法重放。这是协议本身的限制。
3. **代理会自己换端口**——`SO_REUSEADDR` 让两个进程能绑同一个端口，然后随机分流连接。
   所以端口归属用 IP Helper API 判定（`port_owner.py`），端口变更由 supervisor 改写配置。
4. **readmeX 自带的 `source.env` 里有作者的明文 key**——已弃用，改用本机代理 + 环境变量。

## 9. readmeX 实跑结果（本分支已验证）

命令：`powershell -File testenv/docs_baseline/run_readmex_test.ps1`
（模型 `opencode/deepseek-v4.1-flash-free`，走 shim `127.0.0.1:8791`，不碰 8788）

| 项 | 值 |
|---|---|
| 结果 | `exit=0`，耗时 446 s |
| 产物 | `README.md` 21 678 B、`script_descriptions.json` 9 039 B、`python_dependencies_analysis.txt` 1 080 B、`requirements.txt` 367 B、`project_structure.txt` 117 B |
| 质量 | 徽章头 + About / Project Structure / Getting Started / Usage / Roadmap 全部成形，正确引用 `monitor.py`、`controller.py`、`session_failover.py`、`retry_proxy.py` 与 shim 的环境变量 |
| 中间失败 | 1 次 `502`，shim 第 2 次尝试成功（`x_cli_attempts` 计数生效） |
| 对生产影响 | 快照 diff 8 项**全是流量计数器**（requests 330→399、OK 301→367 等），接线 44/44 未变，`VERDICT: wiring unchanged` |

结论：免费层 + 本机 CLI shim 足以驱动一个真实文档生成工具跑完整套流程，
且实验全程在独立端口、独立 scratch、独立快照下完成，master 工作区未改动。
