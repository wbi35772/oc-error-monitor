# oc-error-monitor v1.0.1

运行版本。线上跑的就是这一份代码（暂存副本在
`%USERPROFILE%\.config\opencode\oc-error-monitor-v1.0.0\`）。

| 项 | 值 |
|---|---|
| 版本号 | `1.0.1`（本目录自带 `VERSION` 文件） |
| 入口 | `retry_proxy.py` → `http://127.0.0.1:8788/v1` |
| 仪表盘 | `http://127.0.0.1:8788/__ui` |
| 状态 | `http://127.0.0.1:8788/__health` → `{"version":"1.0.0",...}` |
| 看门狗 | 计划任务 `OpenCode Retry Proxy`（每分钟） |
| 提交 | master `v1.0.1`（tag `v1.0.0` 是它的前身） |

## 组成

```
v1.0.0/
├── retry_proxy.py            P4 请求级重试代理（线上入口）
├── ui.html                    仪表盘（/__ui）
├── supervisor.py              看门狗：拉起代理、改写模型地址
├── wire_retry_proxy.py        把 44 个模型指向代理
├── port_owner.py              IP Helper API 查端口归属
├── restart-retry-proxy.ps1    零停机热切换
├── start-retry-proxy.bat      暂存 + 启动
├── install-watchdog.bat       装计划任务
├── monitor.py / controller.py / session_failover.py   P1 / P2 / P3
├── backtest.py / probe_matrix.py                      回放与实测矩阵
├── testenv/                   隔离测试：mock 网关 + 14 场景 48 断言
└── docs/                      本版本的文档
```

## 三层防线

```
OpenCode CLI / OpenChamber
        │  http://127.0.0.1:8788/v1
        ▼
   retry_proxy.py  ← 身份闸与 IP 信誉分别判定；429 独立退避；断流只观测
        │
        ▼
   opencode.ai 免费层 ──(失败)──▶ mihomo :7899 ──(拒绝)──▶ 直连
```

两个 403 必须分开看：

| 现象 | 判定 | 处理 |
|---|---|---|
| `free tier can only be used from within OpenCode` | 身份闸：请求形状不是 OpenCode 客户端 | **永不重试** |
| `detected abusive traffic from this IP address` | IP 信誉：按请求随机 | 退避重试 0.4/1.0/1.8/3.0/4.5s |
| HTTP 429 | 速率 | 独立阶梯 2/5/9/15/25s |

SSE 在 `finish_reason` 之前断开时记为 `truncated_streams`：字节已上线，
重放会污染客户端状态，所以只观测不重放。

## 端口与换手

- 8787 被 playwright-mcp 的 `node.exe server.mjs` 永久占用，代理自动避让到 8788。
- `SO_REUSEADDR` 允许两个进程绑同一端口再随机分流连接，所以端口归属用
  IP Helper API 判定，**不用 bind 试探**。
- 热切换用文件握手（`handover-request.json`）+ drain，实测 downtime 0 ms。

## 文档

- 本版本设计说明：`docs/superpowers/specs/2026-10-20-opencode-session-resilience-design.md`
- 变更记录：仓库根 `CHANGELOG.md`
- 目录总览：仓库根 `README.md`


## 同时跑两个实例：暂停 / 启动 / 切换

两个版本用不同端口（1.0.1 = 8788，beta = 8899），可以同时跑。仪表盘"手动介入"
区多了三个按钮，也可以不开仪表盘，直接命令行：

```batch
switch-instance.bat status          :: 谁在跑、在哪个端口
switch-instance.bat start beta      :: 起 beta（会清掉"用户停过"的标记）
switch-instance.bat stop beta       :: 真停：写标记，看门狗不会复活它
switch-instance.bat switch beta     :: 先起 beta，健康后再停其它，断流时间最短
```

`stop` 的语义是**用户意图**，不是崩溃：`retry_proxy.py` 写 `stopped.by-user`，
`supervisor.py` 与 `start-retry-proxy.bat` 见到标记都直接退出，
所以分钟级看门狗不会把停掉的实例拉回来。`--force` 或 `start` 才会清标记。

对端信息走 `/__peer`：

```json
{"me":{"version":"1.0.1","port":8788,"pid":7220},
 "peer":{"port":8899,"version":"1.1.0-beta.1","pid":11388},
 "peers":["1.0.1","1.1.0-beta.1"]}
```

`start_peer` 按版本号挑最新的兄弟目录（因此不会误起旧版本去抢 8788）。
