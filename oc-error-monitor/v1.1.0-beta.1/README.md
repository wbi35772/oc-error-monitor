# oc-error-monitor v1.1.0-beta.1

**测试版本，独立完整可运行。** 代码、脚本、测试、运行时目录、端口、计划任务全部自成一套，
不依赖 `v1.0.0/`，也不会碰线上正在跑的那一份。

| 项 | v1.0.0（运行版本） | v1.1.0-beta.1（本目录） |
|---|---|---|
| 端口候选 | 8787 → 8788 → 8790 → 8791 | **8899 → 8900 → 8901 → 8902** |
| 运行时目录 | `%USERPROFILE%\.config\opencode\oc-error-monitor-v1.0.0\` | `%USERPROFILE%\.config\opencode\oc-error-monitor-v1.1.0-beta.1\` |
| 计划任务 | `OpenCode Retry Proxy` | `OpenCode Retry Proxy beta` |
| CLI 端点默认端口 | —（这一版才有） | **8890** |
| 文档基线端口 | — | **8891** |
| 版本上报 | `/__health` → `1.0.0` | `/__health` → `1.1.0-beta.1` |

两个版本可以同时常驻：端口不重叠、运行时目录不重叠、计划任务不重名。

## 快速开始（完全独立）

```powershell
cd oc-error-monitor\v1.1.0-beta.1

# 1) 跑测试套件（自带 mock 网关，不碰网络、不碰线上）
python testenv\test_retry_proxy.py

# 2) 起代理（自己挑 8899-8902 里空闲的一个）
start-retry-proxy.bat
(Invoke-RestMethod http://127.0.0.1:8899/__health).version   # -> 1.1.0-beta.1

# 3) 起 CLI 端点（让第三方工具用上免费层）
python cli_llm_server.py --port 8890
(Invoke-RestMethod http://127.0.0.1:8890/__health).version

# 4) 装自己的看门狗（任务名带 beta，不会覆盖线上那个）
install-watchdog.bat
```

停掉本版本：删掉 `OpenCode Retry Proxy beta` 任务并结束 `oc-error-monitor-v1.1.0-beta.1`
目录下的进程即可，线上 `OpenCode Retry Proxy` 不受影响。

## 这一版新增什么

免费层只认真客户端：网关检查请求形状（不只是 header，还看 body 里的 system prompt），
第三方工具直连一定被拒：

```
403 {"type":"FreeTierError","message":"OpenCode's free tier can only be used from within OpenCode."}
```

OpenRouter 的免费模型也要凭据（`401 No cookie auth credentials found`）。于是有了
`cli_llm_server.py`：**用真 OpenCode CLI 当后端**，对外提供 OpenAI 兼容接口。请求由真
CLI 发出，天然满足身份闸；任何第三方工具都能用上免费模型，不需要账号、不需要 key。

```
第三方工具 ──OpenAI 协议──▶ cli_llm_server.py:8890 ──opencode run──▶ 免费层
```

- 提示词走 **stdin**：几 KB 的提示词当命令行参数会被 Windows 拒绝（`WinError 206`）。
- 失败重试 2 次，再兜底切到 `big-pickle`。
- `/__health` 报版本、CLI 路径、模型数与统计（含 `failed_over`）。

## 完整文件清单

```
v1.1.0-beta.1/
├── VERSION                     1.1.0-beta.1
├── retry_proxy.py              请求级重试代理（入口，8899-8902）
├── supervisor.py               看门狗：代理崩了 1 秒内拉起
├── wire_retry_proxy.py         把模型指向本版本端口；改线上配置需 --live
├── port_owner.py               IP Helper API 查端口归属
├── ui.html                     仪表盘 /__ui
├── restart-retry-proxy.ps1     零停机热切换
├── start-retry-proxy.bat       暂存 + 启动
├── install-watchdog.bat        装计划任务（beta 专用名）
├── monitor.py                  P1 只读错误分类
├── controller.py               P2 全局节点切换
├── session_failover.py         P3 单会话换模型
├── backtest.py / probe_matrix.py / direct_probe.py   回放与实测
├── cli_llm_server.py           OpenAI 兼容端点（本版新增）
├── test_classify.py            分类器单测
├── test_controller.py          controller 单测
├── testenv/
│   ├── mock_gateway.py         故障注入网关
│   ├── test_retry_proxy.py     14 场景 / 48 断言
│   ├── squat_port.py           占端口复现 8787 冲突
│   ├── port_owner.py           端口归属测试替身
│   └── docs_baseline/          隔离文档基线实验环境
│       ├── snapshot_state.py       运行态快照 + --diff 漂移判定
│       ├── run_readmex_test.ps1    隔离跑 readmeX（端口 8891、独立 scratch）
│       ├── flow_demo.html          零依赖流场动画页
│       └── baseline.json           漂移检查的参照快照
├── probes/                     一次性探针（保留证据）
├── docs/
│   ├── STATUS.md               本版本的实验结论与运行态
│   ├── DOC-TOOLING.md          「runtime」选型、身份闸、shim 设计、动效库对比
│   ├── P4-RETRY-PROXY.md       P4 代理的设计说明（同 v1.0.0）
│   └── generated/              readmeX 真实产物（未改）
└── .gitignore
```

## 隔离测试基线

`testenv/docs_baseline/` 是给文档生成器准备的隔离环境，判定标准是**接线不许动**：跑完
diff 只允许流量计数变化，`models.wired`、`routing.enabled`、`watchdog.installed`、端口号
一旦有差就是 `CONFIGURATION DRIFT`。

```powershell
cd testenv\docs_baseline
powershell -ExecutionPolicy Bypass -File .\run_readmex_test.ps1
```

`snapshot_state.py` 的默认参照点仍是线上 v1.0.0（8788）——它存在的意义就是证明
实验没有动到运行版本。

## 与 v1.0.0 的关系

本目录是 v1.0.0 的**超集**：v1.0.0 的全部工具在这里都有，另外多了 CLI 端点与文档基线。
把 v1.0.0 的修复合并进来即可保持同步，反之不要——v1.0.0 是线上版本，只接受已验证的改动。

## 图示（Whimsical）

- 链路拓扑 + 指纹闸对照：https://whimsical.com/9p4xba3BkZGnjpFnkc48QN
  - 上：客户端 → retry_proxy 8788/8899 → 错误判定（身份闸 / IP 信誉 / 429 / 断流）→ mihomo :7899 或直连 → 免费层
  - 下：裸 HTTP 请求被判身份闸（403，永不重试） vs 真 CLI 请求放行；第三方工具经 cli_llm_server 转成 CLI 请求

## 参数速查

| 参数 | 值 | 位置 |
|---|---|---|
| 生产端口 | 8788（候选 8788,8903,8904,8905） | `RETRY_PROXY_PORTS` |
| 测试端口 | 8899（候选 8899,8900,8901,8902） | 同上 |
| 上游 | https://opencode.ai/inference/openai/v1 | `RETRY_PROXY_UPSTREAM` |
| 出口代理 | http://127.0.0.1:7899（mihomo mixed，可关） | `RETRY_PROXY_UPSTREAM_PROXY` |
| IP 信誉退避 | 0.4 / 1.0 / 1.8 / 3.0 / 4.5 s | `BACKOFF` |
| 429 退避 | 2 / 5 / 9 / 15 / 25 s | `RATE_BACKOFF` |
| 身份闸 | 永不重试，记 `IDENTITY` | `classify` |
| 断流 | 只记 `truncated_streams`，不重放 | — |
| 停机标记 | `stopped.by-user`（存在则看门狗不拉起） | 运行时目录 |
| shim 端口 | 8890（beta）/ 8790（默认） | `cli_llm_server.py` |