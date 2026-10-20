# OpenChamber 会话网络韧性设计（方案稿）

- 日期：2026-10-20
- 状态：P1/P2 已实施并验证；P3 待定
- 范围：OpenChamber 2.1.0 / OpenCode 2.x / Clash Verge Rev + mihomo / Windows
- 相关文件：`README.md`、`ANALYSIS.md`、`MIHOMO-BASELINE.md`

## 0. 结论先行

1. **免费模型不需要账号，本机已验证可用。**
   - 没有 `~/.local/share/opencode/auth.json`，也没有 `OPENROUTER_API_KEY` / `OPENCODE_API_KEY` 等任何环境变量。
   - `opencode` provider 的 `apiKey` 就是字符串 `public`；`openrouter ...:free` 同样可直接调用。
   - 2026-10-20 实测 `openrouter/minimax/minimax-m3.1-flash:free` 在本机成功率 **100%（5/5）**。

2. **403「abusive traffic from this IP」是间歇性的，不是硬 IP 封禁。**
   - 同一个节点、同一个模型、同一个出口，8 次里 3 次成功（3/8）。
   - 逐节点各 6 次实测：多线-01 83%、电信-01 83%、联通-01 67%、移动-01 67%、联通-02 50%、电信-21 33%。
   - 结论：**先原地重试**；持续失败才换节点/换模型。原先“永不重试、永不换节点”的策略是错的，已改。

3. **延迟不能预测免费模型可用性。**
   - 最快的是 移动-01（200ms，成功率 67%），最慢的是 多线-01（615ms，成功率 83%）。
   - 因此节点排序必须用**实测成功率**，延迟只当存活过滤。这是 P2 的关键修正。

4. **模型差异远大于节点差异。**
   - 同一节点上：`openrouter/...:free` 100%、`opencode/minimax-m3.1-flash-free` 80%、
     `opencode/glm-5.3-flash-free` 80%、`opencode/deepseek-v4.1-flash-free` 60%。
   - 晚些时候重跑 8 次/模型：`openrouter/...:free` 8/8、`opencode/deepseek-v4.1-flash-free` 4/8、
     `opencode/minimax-m3.1-flash-free` 3/8。
   - 缓解优先级：**换模型 > 换节点 > 原地重试**。
   - 因此已把 OpenChamber 项目默认模型从 `opencode/deepseek-v4.1-flash-free`（50%）
     改成 `openrouter/minimax/minimax-m3.1-flash:free`（100%），并实测返回 `DEFAULT_OK`。

5. **四个已克隆仓库只能做观测，不能做自动缓解。**
   - WireWatch、Simple NetMon、OpenProcMon、System Informer 都没有“按会话分类错误并调用 Clash API”的控制面。
   - 它们适合做证据采集、连接/进程定位和故障复盘，不适合被改造成策略引擎。

6. **Clash/mihomo 看不到 OpenChamber 的会话 ID，但 OpenCode API 可以。**
   - mihomo 的规则维度是进程、域名、端口、IP；所有会话共用一个 OpenCode server 和同一个出口。
   - 所以“每会话独立出口”在 Clash 层做不到；但 `POST /api/session/{id}/model` ���以只改**一个会话**的模型，
     不动全局节点、不影响其它会话。这就是 P3 的真正落点（不需要本地网关）。

7. **推荐路线：先确认免费模型是否可用，再做韧性。**
   - P0：已实测 —— 免费模型可用；DIRECT 路径反而更差（5/5 全 403），代理路径才是可用的。
   - P1：只读采集 + 错误分类 + 通知，不改网络。**已实施**。
   - P2：对真实网络错误自动切换 `OpenCode 专用` 代理组，带实测打分、预算、冷却、回滚。**已实施**。
   - P3：每会话换模型（OpenCode API），不动全局网络。**已实施**。

### 实施状态（2026-10-20）

- **P1 已实施**：`tools/oc-error-monitor/monitor.py`，只读轮询 `opencode.db` + tail `main.log`，输出 JSONL；`test_classify.py` 8/8 通过。
- **P2 已实施**：`tools/oc-error-monitor/controller.py`，tail P1 JSONL，默认 dry-run；`test_controller.py` **24/24 通过**。
  - 节点排序改为优先读 `logs/resilience/node-scores.json`（实测成功率），延迟只作存活过滤。
  - mihomo 缓存的 `alive` 标志会滞后：有实测分数的节点即使被标记 dead 也保留在候选里，
    但仍需通过实时健康探测才会被切过去。
  - `AUTH_IP_REPUTATION` / `AUTH_FREE_TIER` / `LOCAL_RELAY` **不再切全局节点**，交给 P3
    每会话换模型（依据见 1.7 节回测：51/52 个卡死会话只出现过一次 403，
    “攒够 3 次”会全部漏掉；且换模型修复率 100% vs 换节点 33%-83%）。
  - dry-run 用影子状态模拟同一套预算，预览与 `--apply` 一致，且不消耗真实预算。
  - 支持 `--event-time`：历史回放时用事件自身的时间戳算预算窗口，
    否则两天的历史会被压成同一瞬间。
- **P2 实测**：
  - `--list-candidates`：组 `🎯 OpenCode专用`，218 成员、125 个叶子候选；带打分时选 `多线-01`（80%）而不是更快的 `联通-03`。
  - `--self-test`：`PUT '🎯 OpenCode专用' -> 当前节点` 返回 HTTP 204，写路径可用且不改变选择。
  - 临时事件回放：`NET_STREAM -> WOULD_SWITCH 联通-02 -> 多线-01`；
    `AUTH_IP_REPUTATION -> NOOP（交给 P3）`。
- **实测工具**：`tools/oc-error-monitor/probe_matrix.py` —— 逐节点/逐模型测量成功率，
  同时记录 gstatic 延迟用于对比；结果写入 `logs/resilience/node-scores.json` 供控制器使用。
- **P3 已实施**：`tools/oc-error-monitor/session_failover.py` —— 只给卡住的会话换模型。
  - 真实会话验证：`POST /api/session/{id}/model` 返回 HTTP 204，会话模型从
    `opencode/deepseek-v4.1-flash-free` 变为 `openrouter/minimax/minimax-m3.1-flash:free`，复查确认生效。
  - 认证：OpenChamber 生成的托管密码只在内存里（每次启动轮换），用 `read_proc_env.py`
    从 OpenChamber 进程环境读 `OPENCODE_SERVER_PASSWORD`，全程不落盘。
- **P2/P3 默认都是 dry-run**，任何写操作都要显式 `--apply`。
- **P4 已实施**（2026-10-20）：请求层重试代理 `tools/oc-error-monitor/retry_proxy.py`
  （127.0.0.1:8787），把 IP 信誉 403 / 429 / 5xx / 空流在**单次请求内**救回。
  - 前提：抓包证明「身份闸 403」与「IP 信誉 403」必须分开，前者由请求形状决定，后者逐请求随机。
  - 代理原样透传客户端请求（保住身份闸），只在尚未写出任何字节时重试。
  - 44 个 free 模型已通过 `provider.<id>.models.<model>.provider.api` 指向代理。
  - 常驻：计划任务（每分钟）→ `start-retry-proxy.bat` → `supervisor.py` → `retry_proxy.py`；
    崩溃自愈 3.2 秒，单实例用文件锁（Windows `SO_REUSEADDR` 不可靠）。
  - 隔离测试环境 `tools/oc-error-monitor/testenv/`：mock 网关 + 26 断言全绿，不碰 Clash、不碰真网关。
  - 详见第 8 节 P4。

## 1. 已确认的事实与证据

### 1.1 运行环境

- Clash Verge Rev + `verge-mihomo-alpha`，控制器 `http://127.0.0.1:9097`，secret 在：
  `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\config.yaml`
- 监听：mihomo `1053/7897/7898/7899/9097`；xray `127.0.0.1:10808`；netmon `8888`。
- 系统代理：`HTTP_PROXY=HTTPS_PROXY=http://127.0.0.1:7897`。
- 当前 `OpenCode` 选择器组（名称含 emoji/中文，控制台会乱码）当前节点为 `电信-04`；组内还有大量地区节点和 `DIRECT`。
- 没有监听 `8326`（eac 本地反代）或 `8080`（deepseek-web2api-free）。

### 1.2 OpenChamber / OpenCode

- OpenChamber 2.1.0；主进程 PID 31136。
- 内置 OpenCode server：
  `opencode.exe serve --stdio --hostname 127.0.0.1 --port 58063`
- **注意：58063 返回的是 OpenChamber Web UI 的 HTML，不是 OpenCode HTTP API。** OpenCode 与 OpenChamber 通过 stdio 通信。
- OpenCode 数据库：
  `C:\Users\24999\.local\share\opencode\opencode.db`（WAL，只读可访问）
- 关键表：
  - `session_v2`：`id`、`title`、`directory`、`model`（JSON）、`idle_outcome`、`time_updated`、`time_idle`、`resume_attempts` 等。
  - `session_message`：`id`、`session_id`、`type`、`seq`、`time_created`、`data`（JSON）。
  - `event`、`event_sequence`：可用于增量读取。
- `session_message.data` 中的 assistant 消息包含：
  - `model`（providerID/id）
  - `finish`
  - `error.type`、`error.message`、`error.status`、`error.response.body`

### 1.3 观察到的错误样本

| 样本 | 关键字段 | 含义 |
|---|---|---|
| `provider.auth` / 403 | `FreeTierError`，body 含 “free tier can only be used from within OpenCode” | 客户端授权限制 |
| `provider.auth` / 403 | “abusive traffic from this IP address” | IP 信誉/免费额度网络限制 |
| `provider.transport` / 200 | `ECONNRESET: socket connection was closed unexpectedly` | 流式响应中途断连 |
| `provider.transport` | `getaddrinfo ENOTFOUND opencode.ai` | DNS 解析失败 |
| `provider.internal` / 500 | `Internal Server Error`（deepseek-web） | 本地/上游 5xx |
| `aborted` | “Step interrupted” | 用户中断或客户端取消 |
| session-assist / 503 | `small-model-unavailable`，`eac/our-free-model/opencode/deepseek-v4.1-flash-free` | 小模型指向了未启动的 eac 本地反代 |

### 1.4 供应商现状

- OpenCode 内置免费模型：`opencode/deepseek-v4.1-flash-free`，端点在 `https://opencode.ai/inference/openai/v1`，但实际上受客户端与 IP 限制。
- OpenRouter：已连接，缓存显示 388 个模型，存在免费模型（如 `minimax/minimax-m3.1-flash:free`）。
- `eac` 本地反代：曾提供 `our-free-model/opencode/*`，当前未运行，源码未在工作区找到。
- `deepseek-web2api-free`：DeepSeek Chat Web 反向代理，当前未运行；它不能绕过 OpenCode 的免费额度限制，只能作为另一个上游。
- `clashverge-oc轮换`：目前只有 `.omo\run-continuation`，没有可复用的轮换实现。

### 1.5 2026-10-20 决定性测试：直连更差，但代理路径可用且是间歇性的

在未修改任何 Clash 配置的前提下，用 OpenChamber 自带的 OpenCode CLI 做了一次最小验证：

```powershell
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''
$env:NO_PROXY='*'
opencode.exe run --standalone \
  --model opencode/deepseek-v4.1-flash-free \
  "Reply with exactly: OK"
```

结果（直连，5 次全部相同）：

```text
OpenCode's free models aren't available from your current network because we've detected abusive
traffic from this IP address. This isn't related to your account. If you're on a VPN or proxy, turn it off and try
again, or subscribe to OpenCode Go at https://opencode.ai/go to keep building.
```

结论（**已按实测修正，早期结论是错的**）：

- 直连路径 **5/5 全部 403**，比代理路径更差 —— 直连出口被硬标记。
- 但代理路径**不是**硬封禁：同一节点、同一模型 8 次里 3 次成功（3/8）。
  上游按请求轮换 provider IP，部分 IP 被标记、部分没有。
- 所以策略应是「**先原地重试**，持续失败再换节点/换模型」，而不是「永不重试」。
- 早期把 `AUTH_IP_REPUTATION` 定为“不可重试、绝不换节点”是因为当时只做了 1 次直连 + 1 次代理测试，
  把间歇性误判成了确定性。`probe_matrix.py` 的重复测量推翻了它。
- 小模型报 `eac/our-free-model/opencode/deepseek-v4.1-flash-free` 是另一件事：运行中的 OpenCode 进程启动于 02:04，
  而当前 `opencode.json` 修改于 03:04；进程里仍残留旧的 `small_model: eac/...`。重启/重载后会回落到 OpenCode 免费模型。
- 在“只用免费模型、不登录账号”的约束下，现在有**可用的合法路径**：
  - 用 `openrouter/...:free`（实测 100%）或 `opencode/minimax-m3.1-flash-free`、`opencode/glm-5.3-flash-free`（各 80%）；
  - 用 P3 给卡住的单个会话换模型，不动其它会话；
  - 或换节点挑实测成功率高的出口。

### 1.6 2026-10-20 免费额度不需要账号（实测）

```text
无 ~/.local/share/opencode/auth.json
无 OPENROUTER_API_KEY / OPENCODE_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY
opencode provider settings: { apiKey: "public", baseURL: "https://opencode.ai/inference/openai/v1" }
openrouter provider settings: { baseURL: "https://openrouter.ai/api/v1" }
```

`opencode` 与 `openrouter` 两个 provider 都不需要用户登录或提供 Key，
`:free` / `-free` 后缀的模型可以直接调用。这一点与用户“不用登录任何账号”的约束一致。

### 1.7 2026-10-20 历史回测：报错数据本来就在盘上

`opencode.db` 的 `session_message` 表保存了每条 assistant 消息：
失败带 `error` 字段，成功带 `finish: "stop"`。
所以策略不必等 dry-run 攒够数据，可以直接拿真实历史回放（`backtest.py`）。

样本：2026-10-18 06:02 → 2026-10-20 07:36，135 个会话，150 个错误。

判定标准是**该错误的下一次尝试**的结果：下次就成功 → 瞬时错误，这次动作是误报；
下次仍失败 → 错误持续，动作有依据；再无尝试 → 会话直接死了。

| 错误类 | 错误数 | 会话直接死 | 下次仍失败 | 下次就成功 |
|---|---|---|---|---|
| `AUTH_IP_REPUTATION` | 100 | **52** | 9 | 39 |
| `NET_STREAM` | 39 | 0 | 27 | 12 |
| `UNKNOWN` | 4 | 0 | 0 | 4 |
| `ABORTED` | 3 | 1 | 0 | 2 |
| `AUTH_FREE_TIER` | 2 | 0 | 0 | 2 |
| `NET_CONNECT` | 1 | 0 | 1 | 0 |
| `NET_TLS` | 1 | 1 | 0 | 0 |

**回测推翻了原方案的两个判断：**

1. **“攒够 3 次再切节点”是错的。**
   52 个死掉的会话里 **51 个只出现过一次** `AUTH_IP_REPUTATION` 就死了 ——
   第一次 403 之后用户就放弃，根本没有第二次机会。
   按“攒够 3 次”触发的那 6 次动作，**6 次全是误报**（那些会话本来就在恢复），
   而真正需要救的 52 个**一个都没覆盖到**。

2. **`AUTH_IP_REPUTATION` 不该由切节点处理，该由每会话换模型处理。**
   - 52 个死掉的会话中，**46 个在 5 分钟内新开会话**（用户放弃重来），
     其中 **18 个新会话又立刻死了** —— 死亡螺旋。
   - 换节点的修复率 33%-83%，换模型的修复率 **100%**（8/8 实测）。
   - 换节点动**所有会话**的出口；换模型只动**一个会话**。

于是分工改成：

```text
P2 controller.py       NET_DNS / NET_CONNECT / NET_STREAM / NET_TIMEOUT  -> 切全局节点
                       AUTH_IP_REPUTATION / AUTH_FREE_TIER / LOCAL_RELAY  -> 不动，交给 P3
P3 session_failover.py AUTH_IP_REPUTATION / AUTH_FREE_TIER / LOCAL_RELAY  -> 第一次就换该会话的模型
```

分层后的回测：

```text
P2 node switch     :   40 actions,  12 FP ( 30%),   0 stalled sessions
P3 model switch    :  102 actions,  41 FP ( 40%),  52 stalled sessions
```

P3 的 40% 误报代价小（只动一个会话的模型，fallback 是实测 100% 的那个），
换来的是覆盖全部 52 个卡死的会话。`test_controller.py` 有 6 个用例保证
两层不会重复管同一类、也不会有类没人管。

## 2. 目标与非目标

### 目标

1. 对 OpenChamber 会话的模型错误做**可观测、可分类、可追溯**的处理。
2. 对真实网络错误（DNS、TCP reset、超时、TLS、5xx、429、流中断）做**有限、可回滚**的自动缓解。
3. 不破坏现有 Clash 配置；不默认开启 TUN；不修改生成的 `config.yaml`。
4. 对授权/配额/IP 信誉类错误，明确识别并给出正确建议，不进行无意义的节点轮换。
5. 为后续“按会话网关”留出接口。

### 非目标

- 不自动规避 OpenCode 的免费额度客户端限制。
- 不做「无脑轮换节点」：只有同一错误类在时间窗内持续失败才换，且按实测成功率排序、
  受最小间隔/冷却/预算约束。单纯的 IP 信誉抖动**不**触发切换。
- 不把四个监控仓库改造成控制面。
- 不在未确认的情况下自动切换用户的模型或全局代理模式（两个控制器默认 dry-run，写入需 `--apply`）。
- 不实现“Clash 原生按 session 路由”——该能力不存在；每会话隔离用 OpenCode 的模型切换 API 实现。

## 3. 设计原则

- **先只读，后写入。** P1 只读数据库和日志；P2 才允许调用 mihomo 的受限写接口；P3 才允许改会话模型。
- **错误分类优先于重试。** 先判断“该不该重试”，再决定“怎么重试”。
- **只对网络类错误动网络。** 授权、配额、客户端限制、请求格式错误不触发代理切换。
  `AUTH_IP_REPUTATION` 例外：实测为间歇性，按“持续失败才动”的原则处理。
- **最小写入面。** 只允许切换一个白名单代理组；不改 mode、不改 TUN、不改配置文件。
- **可回滚。** 启动时快照当前选择；退出/异常时恢复；所有动作写审计日志。
- **滞回与预算。** 连续失败才切换；最小驻留时间；每小时最大切换次数；超限转人工通知。
- **会话级证据，供应商级动作。** 能定位到 session；网络动作作用于共享代理组，
  但每会话问题（模型不可用）走 P3 的模型切换，不动全局网络。
- **按实测效果排序，不靠直觉。** 节点优劣用 `probe_matrix.py` 反复测量得出，不拿延迟当代理指标。

## 4. 错误分类（Error Taxonomy）

分类器输入：`error.type`、`status`、`message`、`response.body`、`providerID`、`modelID`、目标地址、耗时、是否流式。

| 类别 | 判定依据 | 可重试 | 是否动网络 | 首选动作 |
|---|---|---|---|---|
| `AUTH_FREE_TIER` | 403 + `FreeTierError` / “only be used from within OpenCode” | 否 | 否 | 切换允许第三方客户端的供应商；通知 |
| `AUTH_IP_REPUTATION` | 403 + “abusive traffic from this IP” | 否（同出口） | 谨慎 | 允许一次 DIRECT 诊断；仍失败则换供应商；禁止自动节点轮换规避 |
| `AUTH_CREDENTIAL` | 401/403 + invalid key / OAuth expired | 否 | 否 | 刷新密钥/重新登录；通知 |
| `RATE_LIMIT` | 429 / `rate_limit` / too many requests | 是 | 否 | 指数退避 + 抖动；有账号池时换账号/key；不换节点 |
| `UPSTREAM_5XX` | 500/502/503/504 / `provider.internal` | 是（有限） | 可能 | 重试 1–2 次；持续失败则 fallback 供应商；本地反代则重启/检查 |
| `NET_DNS` | `ENOTFOUND` / `EAI_AGAIN` / getaddrinfo | 是 | 是 | 清 DNS/健康检查；切换节点；重试一次 |
| `NET_CONNECT` | `ECONNREFUSED` / `ECONNRESET`（响应前）/ `EHOSTUNREACH` | 是 | 是 | 健康检查；切换节点；重试一次；本地地址则检查本地服务 |
| `NET_STREAM` | 200 后 `ECONNRESET` / `terminated` / fetch failed | 是（谨慎） | 是 | 同会话重试一次；有部分输出则续写/继续；重复失败再切节点 |
| `NET_TIMEOUT` | `ETIMEDOUT` / `AbortError` / timeout | 是 | 是 | 长请求放宽超时；健康检查；切节点；重试一次 |
| `NET_TLS` | 证书/握手错误 | 可能 | 是 | 检查系统时间、MITM 证书、节点；不关闭证书校验 |
| `LOCAL_RELAY` | `ECONNREFUSED 127.0.0.1:8326/8080` / `Model unavailable: eac/...` | 是 | 本地 | 启动/重启本地反代；健康检查 `/v1/models`；否则切供应商 |
| `CLIENT_REQUEST` | 400/422 / context length / invalid schema | 否 | 否 | 调整上下文/模型/参数；通知 |
| `ABORTED` | `aborted` / Step interrupted | 否 | 否 | 记录，不动作 |
| `UNKNOWN` | 其他 | 可能 | 可能 | 记录完整 body；通知；不自动切换 |

## 5. 缓解矩阵（摘要）

| 错误类 | 重试 | 切代理节点 | 切模型/供应商 | 重启本地服务 | 通知 | 上限 |
|---|---|---|---|---|---|---|
| `AUTH_FREE_TIER` | 0 | 禁止 | 建议/可配置 | 否 | 是 | 每会话 1 次 |
| `AUTH_IP_REPUTATION` | 0 | 仅手动 DIRECT 诊断 | 建议 | 否 | 是 | 每 24h 1 次诊断 |
| `AUTH_CREDENTIAL` | 0 | 禁止 | 否 | 否 | 是 | 1 |
| `RATE_LIMIT` | 是 | 禁止 | 可换 key/账号 | 否 | 可选 | 3 次，指数退避 |
| `UPSTREAM_5XX` | 是 | 可选 | 是（fallback） | 本地反代可 | 持续失败时 | 2 次 |
| `NET_DNS` | 是 | 是 | 否 | 否 | 连续失败时 | 1 次重试 + 1 次切换 |
| `NET_CONNECT` | 是 | 是 | 否 | 本地地址可 | 连续失败时 | 1 次重试 + 1 次切换 |
| `NET_STREAM` | 是 | 重复时是 | 否 | 否 | 是 | 1 次重试 |
| `NET_TIMEOUT` | 是 | 是 | 否 | 否 | 是 | 1 次重试 + 1 次切换 |
| `NET_TLS` | 否 | 是 | 否 | 否 | 是 | 1 |
| `LOCAL_RELAY` | 是 | 否 | 是（若无法启动） | 是 | 是 | 1 次重启 |
| `CLIENT_REQUEST` | 0 | 否 | 可选 | 否 | 是 | 0 |
| `ABORTED` | 0 | 否 | 否 | 否 | 否 | 0 |

## 6. 架构

```
┌──────────────────────────────┐
│ OpenChamber 2.1.0            │
│  ├─ UI / sessions            │
│  └─ OpenCode server (stdio)  │
│        │                     │
│        ├─ opencode.db (WAL)  │  read-only
│        └─ main.log           │  tail
└────────┼─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│ Session Resilience Controller│
│  1. Collector                │
│  2. Classifier               │
│  3. Policy / State machine   │
│  4. Mitigation adapters      │
│  5. Audit + metrics          │
└──────┬───────────┬───────────┘
       │           │
       │           ├──────────────► Notification (desktop / log / UI)
       │           │
       │           ├──────────────► Local relay manager (eac / deepseek-web)
       │           │
       │           └──────────────► Provider fallback (config / gateway)
       │
       ▼
┌──────────────────────────────┐
│ Mihomo Adapter               │
│  GET /proxies (read)         │
│  PUT /proxies/{group} (write)│
│  healthcheck / delay         │
│  snapshot + restore          │
└──────────┬───────────────────┘
           ▼
┌──────────────────────────────┐
│ Clash Verge / mihomo         │
│  OpenCode 专用 selector      │
│  allowlist only              │
└──────────────────────────────┘
```

P3 的网关模式：

```
OpenChamber → LiteLLM/FastAPI gateway → provider A / B / local
                         │
                         └─ per-session virtual key → provider/model policy
                         └─ outbound via Clash (process-level egress)
```

## 7. 关键设计

### 7.1 Collector：只读、增量、会话级

- 主数据源：`opencode.db` 的 `session_message` + `session_v2`。
- 轮询间隔 1–2 秒；记录 `last_seq` / `last_time_created`，只处理增量。
- 只读连接：`file:...?mode=ro`；不执行 `VACUUM`、不写表。
- 辅助数据源：`main.log` 的 `[session-assist]` 行（小模型错误不在主 DB 的会话消息里）。
- 去重键：`(session_id, message_id, error_type, status, message_hash)`。
- 输出统一事件：

```json
{
  "ts": "2026-10-20T12:34:56.789+08:00",
  "session_id": "ses_...",
  "provider": "opencode",
  "model": "deepseek-v4.1-flash-free",
  "error_class": "AUTH_IP_REPUTATION",
  "status": 403,
  "transport": null,
  "message": "...",
  "source": "session_message",
  "raw_ref": "message_id/seq"
}
```

### 7.2 Classifier + 状态机

- 先匹配结构化字段（`error.type`、`status`、body 中的 `FreeTierError`），再匹配文本关键字。
- 每个会话状态：
  `healthy → degraded → recovering → healthy | failed`
- 每个节点状态：
  `unknown → healthy → suspect → unhealthy → cooldown → probing → healthy`
- 切换条件：
  - 同一网络类错误连续 2 次，且间隔 < 60s；
  - 或健康检查连续 2 次失败；
  - 且距离上次切换 ≥ 120s；
  - 且 15 分钟内切换次数 < 3。
- 超过预算：停止自动切换，发通知，保留证据。

### 7.3 Mihomo Adapter：最小写入面

- 只允许操作一个白名单组：名称包含 `OpenCode` 的 selector（实施时从 `/proxies` 读取精确 UTF-8 名称，不硬编码控制台乱码）。
- 读：
  - `GET /proxies`
  - `GET /proxies/{group}`
  - `GET /providers/proxies`（若可用）
- 写：
  - `PUT /proxies/{group}`，body `{"name": "<node>"}`。
- 健康检查：
  - 优先 `GET /proxies/{node}/delay?url=https://www.gstatic.com/generate_204&timeout=5000`（mihomo API 支持 delay 测试）；
  - 或通过本地代理端口发一次 HEAD 请求；
  - 不对模型端点做频繁探测，避免消耗额度/触发风控。
- 安全：
  - 启动快照当前选择；
  - 只切换到组内 `all` 列表中的节点；
  - 跳过 `DIRECT`，除非用户显式允许诊断；
  - 退出/异常时恢复快照；
  - 所有动作写 JSONL 审计日志。
- 禁止：
  - 修改 `mode`；
  - 开启/关闭 TUN；
  - 修改 `config.yaml`；
  - 切换非白名单组；
  - 对 `AUTH_*` 错误自动切节点。

### 7.4 Provider / Relay Adapter

- `LOCAL_RELAY`：
  - 检查 `127.0.0.1:8326` / `8080`；
  - 若配置了启动命令，启动并等待 `/v1/models` 就绪；
  - 失败则通知并建议切换模型。
- `UPSTREAM_5XX`：
  - 本地反代：重启一次；
  - 远程供应商：冷却该 provider/model，fallback 到下一个。
- `AUTH_*`：
  - 不自动改配置；
  - 生成明确建议：切换 OpenRouter/本地模型、使用 OpenCode 本体、或订阅 OpenCode Go。

### 7.5 按会话网关（P3）

- 推荐 LiteLLM Proxy（成熟、OpenAI-compatible、支持 fallbacks/retries/cooldowns/virtual keys）。
- OpenChamber 配置一个 provider 指向 `http://127.0.0.1:4000/v1`。
- 每个 OpenChamber 会话/工作区分配一个虚拟 key；网关按 key 应用：
  - 主模型、fallback 链、最大重试、超时、预算；
  - 供应商冷却；
  - 可选的自定义 callback 调用控制器的分类/审计接口。
- Clash 仍只看到网关进程，因此出口仍是进程级；如果要“每个会话不同出口”，需要网关为每个上游配置不同的本地代理端口，或启动多个网关实例。
- 如果不想引入 LiteLLM，可用 FastAPI + `httpx` + `tenacity` 实现最小网关；但 fallback/计费/密钥管理要自己维护。

### 7.6 可观测性

- JSONL 审计日志：`logs/resilience/events-YYYY-MM-DD.jsonl`
- 指标：
  - 按错误类计数；
  - 按 provider/model 计数；
  - 切换次数/成功率；
  - 平均恢复时间；
  - 误切换次数（人工标记）。
- 现有四个工具继续作为证据层：
  - WireWatch：流级证据（注意 loopback 丢弃、代理进程归因）；
  - Simple NetMon：进程连接/吞吐；
  - OpenProcMon：进程/文件/注册表/网络事件；
  - System Informer：句柄/线程/连接深挖。
- 不把它们的 UI 嵌入控制器；控制器只写自己的事件流。

## 8. 分阶段实施

### P0：确认 OpenCode 免费模型是否可用（不换供应商）

- 用户约束：只用免费模型，不登录账号。
- 2026-10-20 已完成直连测试：清空代理环境变量后 5/5 全部 403，直连出口被硬标记；代理路径则是间歇性的（3/8 成功）。
- 合法可行动作（现已实现）：
  - 用实测成功率高的免费模型（`openrouter/...:free` 100%、`opencode/minimax-m3.1-flash-free` 80%、`opencode/glm-5.3-flash-free` 80%）；
  - 用 `probe_matrix.py` 反复测量，挑实测成功率高的出口，而不是按延迟猜；
  - 用 P3 给卡住的单个会话换模型，其它会话不受影响；
  - 重启/重载 OpenChamber，清除运行中 OpenCode 进程里残留的 `eac` 小模型配置。
- 不在本项目范围内：绕�� OpenCode 免费额度的客户端限制（不伪造客户端标识）。
- 验收：明确区分“免费模型可用/不可用”；不可用时给出可操作提示，而不是无意义重试。

### P1：只读遥测 + 分类器（1–2 天）

- SQLite collector + `main.log` tail。
- 分类器 + JSONL 审计。
- 桌面/日志通知；不调用 mihomo 写接口。
- 用历史错误回放验证分类准确率。
- 验收：对 403 FreeTier、403 IP、ECONNRESET、ENOTFOUND、500、aborted 分类正确；对 403 不产生任何网络动作。

### P2：受控 mihomo 切换（2–3 天）

- 白名单组 + 快照/恢复 + 滞回/预算。
- 只对 `NET_DNS`、`NET_CONNECT`、`NET_STREAM`、`NET_TIMEOUT`、`NET_TLS` 生效。
- 健康检查与审计。
- 验收：模拟节点故障，控制器在 5s 内分类、切换、记录；下一次请求成功；重启后恢复原选择。

### P3：网关与 fallback（3–5 天）

- LiteLLM 或最小 FastAPI 网关。
- 每会话虚拟 key、fallback 链、供应商冷却。
- OpenChamber 指向网关；保留直连 provider 作为回退。
- 验收：主供应商 5xx/429 时自动 fallback；会话不中断；密钥隔离；审计可追溯。

### P4：请求层重试代理（2026-10-20 已落地）

P1–P3 都发生在**会话之后**。抓包证明有一类错误在**单次请求内**就能救回，
所以加一层比会话更快的缓解。

**前提：先把两个 403 分开。**

| 现象 | 错误文本 | 性质 | 处置 |
|---|---|---|---|
| 身份闸 | `FreeTierError: free tier can only be used from within OpenCode` | 由请求形状决定，同字节重放 100% 复现 | 不重试；改请求形状 |
| IP 信誉 | `FreeTierError: ... detected abusive traffic from this IP address` | 逐请求随机 | 重放即救回 |

**身份闸的成立条件**（抓包 + 变换实验逐条确认）：端点
`https://opencode.ai/inference/openai/v1/chat/completions`；UA 必须三段
`opencode/<channel>/<version>/<client>`；`Authorization: Bearer public`；
`x-opencode-client: cli`；body 必须 `"stream": true`；需要 `x-opencode-project`（32 hex）
与 `x-opencode-session*`（`ses_...`）之一；body 内容结构要像真实 opencode 请求。
结论：代理**只能原样透传**客户端的头和 body，自己造请求必被身份闸拦掉。

**随机性测量**（同一份字节重放 24 次）：19 OK / 5 IP_REP = 79% 成功率；
`P(ok|上次失败)=3/4`；3 次内恢复 4/5；最长连续失败 2。
=> IP 信誉 403 是逐请求瞬时错误，请求层重试足够，不需要换节点/换模型/改会话。

**代理行为**（`tools/oc-error-monitor/retry_proxy.py`，127.0.0.1:8787 → mihomo 7899 → 网关）：

- 200 → `read1` 无缓冲流式透传；
- 200 但空流 → 重试（此时还没写给客户端任何字节）；
- 403 abusive / 429 / 5xx / 连接错误 → 退避重试（0.4/1.0/1.8s + 抖动，最多 4 次尝试）；
- mihomo 拒连 → 剩余尝试转直连；
- 403 identity → 直接透传，不重试；
- SSE 已经开始后被上游截断 → 只观测（`truncated_streams`）不重放，避免重复内容。

**注入点**：唯一可用的是 `provider.<id>.models.<model>.provider.api`
（`provider.opencode.options.baseURL` 与 `provider.opencode.api` 均无效；
`OPENCODE_MODELS_PATH` 有效但会冻结模型目录）。44 个 free 模型已指向本地代理。

**常驻与自愈**：计划任务（每分钟）→ `start-retry-proxy.bat` → `supervisor.py` → `retry_proxy.py`。
单实例用文件锁（`retry_proxy-<port>.lock`）——Windows socket 默认 `SO_REUSEADDR`，
第二个进程能绑上同一端口并抢走连接，文件锁才靠得住。
崩溃自愈实测 3.2 秒；手动重启实测停机 2.9 秒（`restart-retry-proxy.ps1` 会打印该时长）。

**验收证据**：
- 隔离测试环境 `tools/oc-error-monitor/testenv/`：mock 网关 + 8 场景 / 26 断言全绿，
  不碰 Clash、不碰真网关；
- 线上：10 次 `opencode run` 全成功；16 请求 / 25 尝试 / 7 次需重试 / 7 次救回 / 0 耗尽。

## 9. 验收标准（总）

- 正确性：授权/配额类错误 0 次自动网络切换。
- 恢复性：网络类错误在 2 次重试/1 次切换内恢复的比例 ≥ 80%（实验室模拟）。
- 安全性：所有 Clash 变更可回滚；控制器异常退出后 60s 内恢复原选择。
- 可观测性：每条错误都有 session_id、error_class、action、result。
- 不破坏现有工具：四个仓库的启动脚本和端口不变；控制器不占用 8080/8888。

## 10. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 自动切模型导致上下文/行为突变 | 只建议不自动切；P3 用显式 fallback 链并通知 |
| 节点轮换被供应商视为规避 | 只对网络错误；AUTH/IP 类禁止自动切换 |
| 健康检查消耗额度 | 用 204 端点，低频；不对模型端点探测 |
| 控制台乱码导致组名错误 | 从 UTF-8 JSON 读取精确名称；不硬编码 |
| OpenCode DB schema 变化 | 版本检测 + 失败降级到日志；只读 |
| 控制器与 OpenChamber 同时写配置 | 控制器不写 OpenChamber/OpenCode 配置；只读 DB |
| LiteLLM 增加故障点 | 保留直连 provider；网关失败时可手动切换 |
| 代理组切换影响其他应用 | 只切 `OpenCode` 专用组；不动全局 mode |

回滚：
- 停止控制器；
- 恢复启动快照的代理组选择；
- 删除/禁用新增 provider；
- 保留日志用于复盘。

## 11. 待确认决策

1. **是否启用 P2/P3 的 `--apply`（真实写入）？**
   - P1 一直只读；P2/P3 默认 dry-run，任何写操作要显式 `--apply`。
   - 建议先跑一周 `--follow`（dry-run）看误报率，再开 `--apply`。
   - 边界：只动 `🎯 OpenCode专用` 一个组；只对持续失败的网络类错误切节点；
     按实测成功率排序；带最小间隔/冷却/预算；`--restore-on-exit` 可回滚。

2. **是否需要“每个会话独立出口”？**
   - 若只需要“每个会话可检测、可通知、可换模型”，P1+P2+P3 已足够（P3 走 OpenCode 模型切换 API）。
   - 若必须每个会话走不同公网 IP，Clash 层做不到，需要 P4 网关 + 多出口，工作量显著增加。

3. **是否启用 LiteLLM 作为 P4 网关依赖？**
   - 接受：P4 直接用成熟轮子。
   - 不接受：写最小 FastAPI 网关，功能少但可控。
   - 前提：只有当确实需要“每会话独立出口”时才做；当前 P3 已解决“每会话可用性”。

4. **要不要恢复 `eac` / `deepseek-web` 两个本地反代？**
   - 两者当前都没运行，`opencode.json` 里 `disabled_providers` 仍禁用它们。
   - 恢复它们可以让 `LOCAL_RELAY` 类错误（503 `small-model-unavailable`）自愈，
   - 但要先确认它们的账号/配额来源是否仍然有效。

## 12. 建议的下一步

- P1/P2/P3 均已实施并有实测证据；下一步是把 `--apply` 打开并观察误报，
  同时用 `probe_matrix.py` 定期刷新 `node-scores.json`（节点和模型质量都会漂移）。
- **P4 已实施**（请求层重试代理）。它改变了分层顺序的性价比：
  IP 信誉 403 现在在**单次请求内**被救回，不再需要 P2 切节点或 P3 换模型；
  P2/P3 的适用范围收缩到「重试也救不回来」的持续性故障。
- **P4 之后仍待处理**：
  - SSE 中途被上游截断（客户端报 `stream ended without finish_reason`）目前**只观测不重试**，
    因为字节已经写到客户端，重放会产生重复内容。要彻底解决需要客户端侧支持「流中断后续传」，
    或在更靠前的一层（CLI 内部）实现。
  - 代理目前是**单进程**，升级时代停机约 3 秒。若要做到零停机，
    需要把监听 socket 固定在一个常驻前端进程里，代理作为其后端热切换。
- **已完成的清理**：
  - OpenChamber 项目默认模型 → `openrouter/minimax/minimax-m3.1-flash:free`（实测 100%），已验证 `DEFAULT_OK`；
  - 运行中进程残留的 `eac` 小模型随 OpenCode 重启消失，`small-model-unavailable` 503 从 00:46 起不再出现；
  - 修掉 `bmad-retrospective/SKILL.md` 的 YAML frontmatter 未转义撇号，OpenChamber 日志不再刷屏。
- 仍未处理：`eac`（8326）和 `deepseek-web`（8080）两个本地反代当前没运行，
  `opencode.json` 仍禁用它们；要不要恢复取决于它们的账号/配额来源是否还有效。

> 本方案不包含、也不会实施“绕过 OpenCode 免费额度客户端限制”（不伪造客户端标识）的功能。
>
> 节点切换是有节制的能力，不是无限轮换：只在同一错误类于时间窗内持续失败时触发，
> 按实测成功率排序，受最小间隔、冷却、每小时/15 分钟预算约束，默认 dry-run。
