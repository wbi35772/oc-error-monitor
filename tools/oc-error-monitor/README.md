# OpenChamber 会话韧性（P1 监控 / P2 节点切换 / P3 每会话换模型 / P4 请求重试）

这个目录把 OpenChamber/OpenCode 的错误读出来、分类、落盘，并按错误类型做**分层**缓解：

| 层 | 作用域 | 手段 | 默认 |
|---|---|---|---|
| P1 `monitor.py` | 只读 | 读 `opencode.db` + `main.log`，分类落盘 | 永远只读 |
| P2 `controller.py` | 全局（一个 Clash 选择组） | 对瞬时网络错误切 mihomo 节点 | dry-run |
| P3 `session_failover.py` | **单个会话** | 只给卡住的会话换模型 | dry-run |
| P4 `retry_proxy.py` | **单次请求** | 对 IP 信誉 403 / 429 / 5xx 重放同一份请求 | 常驻 127.0.0.1:8787 |

## 文件

- `monitor.py`：P1 只读监控，Python 3.10+，只用标准库。
- `test_classify.py`：分类器回归测试（8 个用例）。
- `controller.py`：P2 控制器；tail P1 的 JSONL，按错误类型决定是否切节点；默认 dry-run。
- `test_controller.py`：控制器策略/候选过滤/打分排序测试（16 个用例，不联网）。
- `probe_matrix.py`：**测量**每个节点/模型的免费模型成功率，写入 `logs/resilience/node-scores.json`。
- `backtest.py`：**用历史数据验证策略**。读 `opencode.db` 里过去所有会话的成功/失败记录，
  回放策略，算出“误报率”和“漏掉了多少真正卡死的会话”。不用等一周 dry-run。
- `session_failover.py`：P3 每会话换模型；调用 OpenCode API `POST /api/session/{id}/model`；默认 dry-run。
- `read_proc_env.py`：从运行中的进程读取环境变量（用于取 OpenCode 服务密码，密码不落盘）。
- `check_skill_frontmatter.py`：扫描 `~/.config/opencode/skills/**/SKILL.md`，找出会让 OpenChamber 刷警告的 YAML frontmatter。
- `fix_skill_frontmatter.py`：修复单引号 YAML 标量里未转义的撇号（`ticket's` → `ticket''s`），自动留 `.bak`。
- `direct_probe.py`：一次性诊断工具；临时把 `🎯 OpenCode专用` 组切到 `DIRECT`，跑一次 OpenCode CLI，然后恢复原节点。会调用 mihomo API，默认不要常驻。
- `start-monitor.bat`：启动 P1 监控。
- `start-controller-dryrun.bat`：启动 P2 控制器（只看不做）。
- `start-controller-apply.bat`：启动 P2 控制器（真实切节点，带预算/冷却）。
- `start-failover-dryrun.bat`：启动 P3 每会话换模型（只看不做）。
- `start-failover-apply.bat`：启动 P3 每会话换模型（真的改会话模型）。
- `retry_proxy.py`：P4 重试代理；把 OpenCode 指向 `http://127.0.0.1:8787/v1`，
  对 IP 信誉 403 / 429 / 5xx / 连接错误 / 200 空流重放同一份请求。
- `supervisor.py`：P4 看门狗；代理崩了 1 秒内拉起，计划任务只作最后一道网。
- `start-retry-proxy.bat`：启动 P4（幂等，端口已在监听就直接退出）。
- `restart-retry-proxy.ps1`：带停机测量地重启 P4（只杀自己的进程）。
- `install-watchdog.bat`：装每分钟一次的计划任务（无窗口）。
- `wire_retry_proxy.py`：把 44 个 free 模型的 `provider.api` 指向本地代理。
- `testenv/`：**隔离测试环境**，自带 mock 网关，不碰 Clash、不碰真网关。

## 用法

一次性看最近 20 条错误：

```powershell
python tools\oc-error-monitor\monitor.py --once --backfill 20
```

持续监控（默认每 2 秒）：

```powershell
python tools\oc-error-monitor\monitor.py --follow
```

指定路径/间隔：

```powershell
python tools\oc-error-monitor\monitor.py --follow `
  --db "$env:USERPROFILE\.local\share\opencode\opencode.db" `
  --log "$env:APPDATA\OpenChamber\logs\main.log" `
  --interval 2
```

运行分类器测试：

```powershell
python tools\oc-error-monitor\test_classify.py
```

## 输出

- 控制台：一行一个事件。
- JSONL：`logs/resilience/events-YYYY-MM-DD.jsonl`。
- 每行字段：

```json
{
  "ts": "2026-10-20T04:53:16.877-07:00",
  "source": "opencode.db",
  "session_id": "ses_...",
  "session_title": "...",
  "provider": "opencode",
  "model": "deepseek-v4.1-flash-free",
  "error_class": "AUTH_IP_REPUTATION",
  "action": "do_not_retry_change_network_or_wait",
  "retryable": false,
  "status": 403,
  "error_type": "provider.auth",
  "message": "...",
  "raw_id": "db:2532:msg_..."
}
```

## 分类与动作

| error_class | 含义 | 动作 | 可重试 |
|---|---|---|---|
| `AUTH_FREE_TIER` | 免费额度只允许 OpenCode 客户端 | 换允许第三方客户端的供应商，或直接用 OpenCode | 否 |
| `AUTH_IP_REPUTATION` | 当前出口 IP 被上游标记 | **间歇性**：先原地重试；持续失败再换节点/换模型 | 是 |
| `AUTH_CREDENTIAL` | 401/403 凭据问题 | 刷新凭据 | 否 |
| `RATE_LIMIT` | 429 | 退避后重试 | 是 |
| `UPSTREAM_5XX` | 500/502/503/504 | 有限重试或 fallback | 是 |
| `NET_DNS` | ENOTFOUND/EAI_AGAIN | 检查 DNS/代理后重试 | 是 |
| `NET_CONNECT` | ECONNREFUSED/ECONNRESET（响应前） | 检查代理后重试 | 是 |
| `NET_STREAM` | 200 后流中断 | 重试一次，再考虑切节点 | 是 |
| `NET_TIMEOUT` | 超时 | 放宽超时/检查代理 | 是 |
| `NET_TLS` | 证书/握手失败 | 检查证书/时间/代理；不要关闭校验 | 否 |
| `LOCAL_RELAY` | 127.0.0.1:8326/8080 未启动 | 启动/切换本地反代 | 是 |
| `CLIENT_REQUEST` | 400/422/上下文超限 | 调整请求 | 否 |
| `ABORTED` | 用户中断 | 记录 | 否 |
| `UNKNOWN` | 其他 | 记录并通知 | 否 |

## P2 控制器（受控切换）

P2 只做一件事：**把 P1 已分类的瞬时网络错误，变成对 `🎯 OpenCode专用` 选择组的受控切换**。
默认 `dry-run`，必须显式加 `--apply` 才会调用 `PUT /proxies/{group}`。
节点好坏按**实测成功率**排（`probe_matrix.py` 写入 `logs/resilience/node-scores.json`），
延迟只作存活过滤。

### 命令

查看当前组和候选节点（只读 + 健康检查）：

```powershell
python controller.py --list-candidates
```

测量每个节点/模型的免费模型成功率（会临时切节点，测完恢复原节点）：

```powershell
# 4 个节点 x 6 次，同时记录 gstatic 延迟用于对比
python probe_matrix.py --nodes 4 --attempts 6 --filter "^(电信|联通|移动|多线)-(01|21)$"

# 同一节点上比较不同模型
python probe_matrix.py --models "opencode/deepseek-v4.1-flash-free,openrouter/minimax/minimax-m3.1-flash:free" --attempts 5
```

结果会写进 `logs/resilience/node-scores.json`，控制器下次挑节点时直接用。

只看不做（推荐先跑）：

```powershell
python controller.py --follow
```

真实切换：

```powershell
python controller.py --follow --apply
```

安全写路径自检（把当前节点再 PUT 一次，不改变选择）：

```powershell
python controller.py --self-test
```

用临时事件目录回放（不碰真实状态）：

```powershell
python controller.py --once --from-start `
  --events-dir "$env:TEMP\ctrl-test" `
  --state "$env:TEMP\ctrl-test\state.json" `
  --max-candidates 2
```

### 策略

| 错误类 | 动作 |
|---|---|
| `NET_DNS` / `NET_CONNECT` / `NET_STREAM` / `NET_TIMEOUT` | 允许切节点 |
| `AUTH_IP_REPUTATION` | **交给 P3**（每会话换模型）；不动全局节点 |
| `AUTH_FREE_TIER` | **交给 P3**；记录并提示换供应商/用 OpenCode 本体 |
| `AUTH_CREDENTIAL` / `NET_TLS` | **不切**；记录并提示修凭据/证书 |
| `RATE_LIMIT` / `UPSTREAM_5XX` | **不切**；退避/上游 failover |
| `LOCAL_RELAY` | **交给 P3**；启动本地反代或换供应商 |
| `CLIENT_REQUEST` / `ABORTED` / `UNKNOWN` | 不切；记录 |

### 护栏

- 只操作名字含 `OpenCode` 的 `Selector` 组；默认不把 `DIRECT` 作为候选。
- 最小切换间隔 120s；同一错误类冷却 300s；每小时最多 3 次；15 分钟最多 2 次。
- 每次最多健康检查 6 个候选（默认 4 并发），延迟 ≤ 5000ms 才切；全失败就不切。
- **节点排序优先用实测成功率**（`logs/resilience/node-scores.json`），延迟只作存活过滤和同分排序。
  实测证明延迟不能预测免费模型可用性（见下）。
- mihomo 缓存的 `alive` 标志会滞后，所以**有实测分数的节点即使被标记为 dead 也会保留在候选里**，
  但仍然必须通过实时的健康探测才会被切过去。
- **dry-run 会模拟同一套预算**（影子状态），预览结果与 `--apply` 一致；
  同时不会消耗真实预算，也不会写 mihomo。
- 失败/无健康节点也算一次尝试，写进预算，避免对 API 或节点风暴式重试。
- 状态文件 `logs/resilience/controller-state.json`；动作日志 `logs/resilience/actions-YYYY-MM-DD.jsonl`。
- `--from-start --apply` 默认被强制降级为 dry-run，防止回放历史错误触发真实切换；确需回放要加 `--allow-backfill-actions`。
- `--restore-on-exit` 可选：Ctrl+C 时把组恢复为启动时的节点；默认不恢复，避免把已经失败的旧节点恢复回去。

### 已验证

```text
python test_controller.py   -> 19/19 OK
python test_classify.py     -> 8/8 passed
python controller.py --self-test
  PUT '🎯 OpenCode专用' -> current node '联通-02'
  HTTP 204; now='联通-02'; ok=True
```

临时事件回放（dry-run，预算同样生效）：

```text
AUTH_IP_REPUTATION NOOP         ses_ctltest | intermittent; retry first (1/3 failures in 600s)
AUTH_IP_REPUTATION NOOP         ses_ctltest | intermittent; retry first (2/3 failures in 600s)
AUTH_IP_REPUTATION WOULD_SWITCH ses_ctltest | 联通-02 -> 多线-01 | eligible for a node switch
AUTH_IP_REPUTATION SKIP         ses_ctltest | min switch interval not elapsed (4s < 120s)
```

### 实测：延迟不能预测免费模型可用性

`probe_matrix.py` 在同一个模型上逐节点各跑 6 次（2026-10-20）：

| 节点 | gstatic 延迟 | 免费模型成功率 |
|---|---|---|
| 多线-01 | 615 ms | **83%** |
| 电信-01 | 314 ms | **83%** |
| 联通-01 | 235 ms | 67% |
| 移动-01 | **200 ms** | 67% |
| 联通-02 | 207 ms | 50% |
| 电信-21 | 583 ms | **33%** |

最快的是 移动-01（200ms），最慢的是 多线-01（615ms），但 多线-01 成功率最高。
只按延迟挑节点会选错，所以控制器优先读实测成功率。

### 实测：403 是间歇性的，不是硬 IP 封禁

同一个节点、同一个模型、同一个出口，8 次里有 3 次成功：

```text
try 1: FAIL   try 2: FAIL   try 3: OK
try 4: FAIL   try 5: FAIL   try 6: OK
try 7: OK     try 8: FAIL          -> 3/8
```

所以 `AUTH_IP_REPUTATION` 被改成分类器里的**可重试**类（`retry_then_switch_node_if_persistent`），
控制器在 600s 窗口内累计 3 次同类失败才允许切节点。
DIRECT 路径 5 次全部 403（真·封禁），这条结论仍然成立。

### 实测：模型差异远大于节点差异

同一个节点（联通-02）上各跑 5 次：

| 模型 | 成功率 | 需要账号/Key |
|---|---|---|
| `openrouter/minimax/minimax-m3.1-flash:free` | **100%** | 否 |
| `opencode/minimax-m3.1-flash-free` | 80% | 否 |
| `opencode/glm-5.3-flash-free` | 80% | 否 |
| `opencode/deepseek-v4.1-flash-free` | 60% | 否 |

本机没有 `auth.json`，也没有 `OPENROUTER_API_KEY` 等任何环境变量；
`opencode` provider 的 `apiKey` 是 `public`。**免费模型不需要登录任何账号。**

2026-10-20 晚些时候在同节点重跑 8 次/模型（`--models` A/B）：

| 模型 | 成功率 |
|---|---|
| `openrouter/minimax/minimax-m3.1-flash:free` | **8/8 (100%)** |
| `opencode/deepseek-v4.1-flash-free` | 4/8 (50%) |
| `opencode/minimax-m3.1-flash-free` | 3/8 (38%) |

结果写进 `node-scores.json` 的 `model_results` 段，P3 会按这个排序自动挑 fallback。

## 历史回测（`backtest.py`）：不用等一周

报错数据本来就在 `opencode.db` 里 —— 每条 assistant 消息都带 `error` 字段（失败）
或 `finish: "stop"`（成功）。所以策略可以**直接拿真实历史回放**，不必等 dry-run 攒数据。

```powershell
# 回放当前策略，输出误报率
python tools\oc-error-monitor\backtest.py

# 顺便扫一遍阈值
python tools\oc-error-monitor\backtest.py --sweep
```

**判定标准**：每个错误看它**下一次尝试**的结果。

- 下一次就成功了 → 错误是瞬时的，这次动作是**误报**；
- 下一次还是失败 → 错误持续，这次动作**有依据**；
- 再没有下一次尝试 → **会话直接死了**，这才是最该救的情况。

### 实测结果（2026-10-18 06:02 → 2026-10-20 07:36，135 会话 / 150 个错误）

| 错误类 | 错误数 | 会话直接死 | 下次仍失败 | 下次就成功 |
|---|---|---|---|---|
| `AUTH_IP_REPUTATION` | 100 | **52** | 9 | 39 |
| `NET_STREAM` | 39 | 0 | 27 | 12 |
| `UNKNOWN` | 4 | 0 | 0 | 4 |
| `ABORTED` | 3 | 1 | 0 | 2 |
| `AUTH_FREE_TIER` | 2 | 0 | 0 | 2 |
| `NET_CONNECT` | 1 | 0 | 1 | 0 |
| `NET_TLS` | 1 | 1 | 0 | 0 |

### 这次回测推翻了两个原本的判断

**1. “等攒够 3 次再切” 是错的。**
52 个死掉的会话里，**51 个只出现过一次** `AUTH_IP_REPUTATION` 就死了 ——
因为第一次 403 之后用户就放弃了，根本没有第二次机会。
回放证明：按“攒够 3 次”触发的那 6 次，**6 次全是误报**（那些会话本来就在恢复），
而真正需要救的 52 个，**一个都没被覆盖到**。

**2. `AUTH_IP_REPUTATION` 不该由切节点来处理，应该由每会话换模型来处理。**
- 那 52 个死掉的会话中，**46 个在 5 分钟内新开了一个会话**（用户放弃了重来）。
  其中 **18 个新会话又立刻死了** —— 死亡螺旋。
- 换节点的修复率是 33%-83%（最好的节点），换模型的修复率是 **100%**（8/8 实测）。
- 而且换节点会动**所有会话**的出口，换模型只动**一个会话**。

所以现在：

```text
P2 controller.py       NET_* 四类           -> 切全局节点
                       AUTH_IP_REPUTATION  -> 不动，交给 P3
P3 session_failover.py AUTH_IP_REPUTATION  -> 第一次就换该会话的模型
                       AUTH_FREE_TIER      -> 换模型
                       LOCAL_RELAY         -> 换模型
```

`test_controller.py` 里有 6 个用例保证两层不会重复管同一类、也不会有类没人管。

### 分层后的回测结果

```text
P2 node switch     :   40 actions,  12 FP ( 30%),   0 stalled sessions
P3 model switch    :  102 actions,  41 FP ( 40%),  52 stalled sessions
```

P2 的 30% 误报可以接受（它只动网络，误报代价是节点换了一次）。
P3 的 40% 误报代价也小（只动一个会话的模型，而且 fallback 是实测 100% 的那个），
换来的是**覆盖全部 52 个卡死的会话**。

### 顺带发现：默认模型切换后错误率下降

```text
00:00 - 07:05（旧默认 opencode/deepseek-v4.1-flash-free）  error rate  9.1%  44 个会话死
07:05 - 现在（新默认 openrouter/minimax/minimax-m3.1-flash:free）  error rate  3.3%   6 个会话死
```

（窗口长度不同且含我自己的测试流量，所以这是信号不是严格对照。）

### 已生效的配置改动（2026-10-20）

- OpenChamber 项目默认模型：`opencode/deepseek-v4.1-flash-free`（50%）
  → `openrouter/minimax/minimax-m3.1-flash:free`（100%）。
  备份在 `~/.config/openchamber/settings.json.bak-resilience-2026-10-20`。
  改完用 `opencode.exe run --standalone --model openrouter/minimax/minimax-m3.1-flash:free`
  实测返回 `DEFAULT_OK`，5 秒。
- `opencode.json` 的 `disabled_providers: ["eac","deepseek-web"]` 保持不变；
  之前运行中进程残留的 `small_model: eac/...` 已随 OpenCode 重启消失，
  `small-model-unavailable` 503 从 2026-10-20 00:46 起不再出现。

## P4 请求层重试代理（2026-10-20）

P1–P3 都是**会话之后**的缓解：等错误已经发生，再决定换节点还是换模型。
但这次抓包发现，有一类错误根本不用等到会话层——**它在单次请求内就可以重试**。

### 关键发现：两个 403 完全不是一回事

| 现象 | 错误文本 | 性质 | 正确处理 |
|---|---|---|---|
| 身份闸 | `FreeTierError: free tier can only be used from within OpenCode` | **由请求形状决定**，重放同样的字节 100% 复现 | 重试没用，得改请求 |
| IP 信誉 | `FreeTierError: ... detected abusive traffic from this IP address` | **逐请求随机**，同字节重放时好时坏 | 重放即可救回 |

把这两类混为一谈，就会得出「IP 信誉错误要换节点」这种错误结论。

### 身份闸的精确条件（抓包 + 变换实验确认）

| 条件 | 说明 |
|---|---|
| 端点 | `https://opencode.ai/inference/openai/v1/chat/completions` |
| `User-Agent` | `opencode/<channel>/<version>/<client>`，**三段**；`opencode/2.0.32` 这类两段不行；没有 UA 先被 Cloudflare 1010 拦 |
| `Authorization` | `Bearer public` |
| `x-opencode-client` | `cli` |
| body | 必须 `"stream": true`（`stream:false` 稳定复现身份闸） |
| 会话头 | `x-opencode-project`（32 hex）+ `x-opencode-session` / `x-opencode-session-id` / `x-session-affinity` / `x-session-id`（`ses_...`）之一 |
| body 内容 | 必须是真实 opencode 请求：把抓到的 2375 字节原样重放稳定 200；把 system 内容换成等长 `x` 或缩短，稳定复现身份闸 |

另外 7 个 `x-opencode-*` 头都在二进制里：
`client` / `directory` / `parent-session-id` / `project` / `session` / `session-id` / `ticket`
（`x-opencode-ticket` 是 PTY connect token，与网关无关）。

**所以代理必须原样透传客户端的头和 body**——自己造请求一定会被身份闸拦掉。

### 随机性到底有多大

同一份字节重放 24 次：

```text
19 OK / 5 IP_REP  →  成功率 79%
P(ok | 上一次失败) = 3/4
3 次之内恢复       = 4/5
最长连续失败       = 2
```

结论：IP 信誉 403 是**逐请求瞬时错误**，请求层重试就能救回，
不需要换节点、不需要改会话状态、不需要换模型。

### 代理做什么

```text
opencode CLI ──► 127.0.0.1:8787 ──► mihomo(7899) ──► opencode.ai
                     │
                     ├─ 原样转发请求头与 body（保住身份闸）
                     ├─ 200            → read1 流式透传，零缓冲
                     ├─ 200 但空流      → 重试（还没写给客户端任何字节）
                     ├─ 403 abusive     → 退避重试（0.4/1.0/1.8s + 抖动）
                     ├─ 429 / 5xx       → 退避重试
                     ├─ 连接错误        → 退避重试；mihomo 拒连则转直连
                     └─ 403 identity    → 直接透传，不重试
```

**只在还没往客户端写过一个字节时才重试**。一旦流已经开始（SSE 截断），
重放会产生重复内容，所以只观测（`truncated_streams`）不重试。

### 常驻与自愈

```text
计划任务（每分钟）──► start-retry-proxy.bat ──► supervisor.py ──► retry_proxy.py
                            │                       │                    │
                            │                       └── 代理崩了 1 秒内拉起 │
                            └── 端口已在监听就直接退出（幂等）            └── 单实例锁
```

- 单实例用**文件锁**（`retry_proxy-<port>.lock`）。Windows socket 默认带
  `SO_REUSEADDR`，第二个进程能绑上同一个端口并抢走连接——文件锁才靠得住。
- 崩溃自愈实测 **3.2 秒**（杀掉代理子进程 → supervisor 拉起），原来要等最多 60 秒。
- 手动重启用 `restart-retry-proxy.ps1`，它会打印**实际停机时长**（实测 2.9 秒）。

```powershell
# 启动 / 幂等
tools\oc-error-monitor\start-retry-proxy.bat

# 带停机测量地重启
powershell -NoProfile -ExecutionPolicy Bypass -File tools\oc-error-monitor\restart-retry-proxy.ps1

# 看统计
Invoke-RestMethod http://127.0.0.1:8787/__stats | ConvertTo-Json

# 装/卸每分钟看门狗
tools\oc-error-monitor\install-watchdog.bat
schtasks /Delete /TN "OpenCode Retry Proxy" /F
```

### 怎么让 OpenCode 走代理

唯一可用的注入点是 `provider.<id>.models.<model>.provider.api`：

```text
provider.opencode.options.baseURL  ❌ 无效
provider.opencode.api               ❌ 无效
OPENCODE_MODELS_PATH                ✅ 有效，但会把模型目录冻结
provider.<id>.models.<model>.provider.api  ✅ 有效（44 个 free 模型都指向 127.0.0.1:8787/v1）
```

`wire_retry_proxy.py` 负责写这份配置（改前自动备份）。

### 实测

| 场景 | 请求 | 尝试 | 需重试 | 救回 | 耗尽 | 失败 |
|---|---|---|---|---|---|---|
| 6 次 `opencode run` | 16 | 25 | 7 | 7 | 0 | 0 |
| 4 次（新代码） | 9 | 13 | 3 | 3 | 0 | 0 |

分类计数：`OK:9, IP_REP:4`——**没有一次 IP 信誉错误漏到客户端**。

### 隔离测试环境（`testenv/`）

`testenv/mock_gateway.py` 是一个假网关，复制真网关的所有脾气
（IP 信誉 403、身份闸 403、429、503、空流、SSE 截断），并强制真实的请求形状规则。
`testenv/test_retry_proxy.py` 用它跑 8 个场景 / 26 项断言：

```powershell
python tools\oc-error-monitor\testenv\test_retry_proxy.py
# 26/26 checks passed
```

整个测试**不碰 Clash、不碰真网关、不碰你的任何配置文件**，
代理副本跑在 `testenv/_run/`，日志也落在那里。

## 技能 frontmatter 修复

OpenChamber 每次加载技能都会解析 `SKILL.md` 的 YAML frontmatter。失败时不报错、
只打一条 warn，但会持续刷 `main.log`。

2026-10-20 修掉的一处：`bmad-retrospective/SKILL.md` 的 description 是单引号标量，
里面的 `ticket's` 没转义，提前闭合了字符串，解析器报
`YAMLParseError: Unexpected scalar at node end at line 2, column 129`。

YAML 里单引号标量内部的 `'` 要写成两个：`ticket''s`。

```powershell
# 扫描所有技能
python tools\oc-error-monitor\check_skill_frontmatter.py

# 修复（先 --dry-run 看一遍，会留 .bak）
python tools\oc-error-monitor\fix_skill_frontmatter.py "<SKILL.md>" --dry-run
python tools\oc-error-monitor\fix_skill_frontmatter.py "<SKILL.md>"
```

## P3：每会话换模型（已实施）

Clash 只能看到进程/域名，看不到 OpenChamber 的 session ID，所以“每会话独立出口”在 Clash 层做不到。
但 OpenCode 自己的 API 可以只改**一个会话**的模型：

```text
POST /api/session/{sessionID}/model
{"model": {"providerID": "openrouter", "id": "minimax/minimax-m3.1-flash:free", "variant": "default"}}
-> HTTP 204
```

这才是真正的“每会话”层：**不动全局节点，不影响其它会话**，只把卡住的那个会话挪到可用模型。

```powershell
# 看当前有哪些免费模型
python tools\oc-error-monitor\session_failover.py --list-models

# 看每个会话现在用的模型
python tools\oc-error-monitor\session_failover.py --list-sessions

# 只看不做
python tools\oc-error-monitor\session_failover.py --follow

# 真的换
python tools\oc-error-monitor\session_failover.py --follow --apply
```

已验证（真实会话 `ses_ea677e001ffewY0FlRW2nG5lAY`）：

```text
[failover] AUTH_IP_REPUTATION ses_... failures=1 WAIT (1/3 recent failures)
[failover] AUTH_IP_REPUTATION ses_... failures=2 WAIT (2/3 recent failures)
[failover] AUTH_IP_REPUTATION ses_... failures=3 SWITCH (3 failures within 600s)
[failover] SWITCHED ses_... HTTP 204: opencode/deepseek-v4.1-flash-free -> openrouter/minimax/minimax-m3.1-flash:free
[failover] AUTH_IP_REPUTATION ses_... failures=4 SWITCH (4 failures within 600s)
[failover] SKIP ses_...: already on fallback openrouter/minimax/minimax-m3.1-flash:free
```

切换后会复查会话模型，确认已生效；测试完已把该会话改回原模型。

### 认证

OpenChamber 给托管 OpenCode 生成的密码只在内存里（每次启动轮换），不落盘。
`read_proc_env.py` 从 OpenChamber 进程环境里读 `OPENCODE_SERVER_PASSWORD`：

```powershell
python tools\oc-error-monitor\read_proc_env.py 17680 OPENCODE_SERVER_PASSWORD
```

也可以手动指定：

```powershell
$env:OPENCODE_SERVER_URL = "http://127.0.0.1:63515"
$env:OPENCODE_PASSWORD = "<password>"
```

## 2026-10-20 实测结论

1. **免费模型不需要账号**：本机无 `auth.json`、无任何 API Key 环境变量，
   `opencode` provider 的 `apiKey` 是 `public`，`openrouter ...:free` 同样可直接用。
2. **代理路径可用，但免费模型成功率随上游波动**：同一出口 25%-100% 不等。
3. **DIRECT 路径**：`direct_probe.py` 临时切到 `DIRECT` 后 5/5 全是 403 IP 封禁
   （比代理路径更差），原节点已恢复。
4. **缓解手段按效果排序**：换模型（60%→100%）> 换节点（50%→83%）> 原地重试（33%-60%）。
   三者现在都有对应实现，且默认都是 dry-run。

## 尚未解决

1. 直连路径的 `UNKNOWN_CERTIFICATE_VERIFICATION_ERROR` 需要单独查证书/系统时间/ISP 劫持。
   用 `NODE_EXTRA_CA_CERTS` 指向导出的 Windows 根证书可以过 TLS，但之后仍然是 403。
2. `eac`（127.0.0.1:8326）和 `deepseek-web`（127.0.0.1:8080）两个本地反代当前都没起，
   `opencode.json` 里 `disabled_providers` 仍然禁用它们。
