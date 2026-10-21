# 变更记录

版本号写在仓库根目录的 `VERSION` 里，`retry_proxy.py` 会向上查找读取它，
并通过 `/__health` 与 `/__stats` 的 `version` 字段对外报告——所以「现在跑的是哪个版本」
可以直接问进程，不用猜目录。

发版时打同名 tag：`git tag v1.0.0`。

## 1.1.0-beta.1 — 实验基线（分支 `feature/docs-tooling`）

新增，不影响 `1.0.0` 的运行版本。

- `cli_llm_server.py`：OpenAI 兼容端点，后端是**真 OpenCode CLI**。
  免费层只认真客户端（身份闸会看请求体形状），第三方工具无法直连；
  这个 shim 让 readmeX 一类工具合法用上免费模型。提示词走 stdin，避开
  Windows 命令行长度上限（`WinError 206`）；失败重试 2 次后兜底 `big-pickle`。
- `testenv/docs_baseline/`：隔离测试基线。独立端口 8791、独立 scratch、
  只走环境变量配置，跑前跑后各做一次状态快照并给出 `VERDICT`，
  用它证明实验没有改动在跑的版本。
- `docs/DOC-TOOLING.md`：文档工具选型与「带流动的动态」实现方案对比。
- `docs/generated/`：readmeX 对 `oc-error-monitor/v1.0.0` 的真实产物。

## 1.0.0 — 可用版本（`master`）

请求级重试代理 + 三层看门狗，44 个模型全部经它代理，无需登录与 API key。

- **P4 `retry_proxy.py`**：请求级重试。区分两个 403——
  身份闸（`free tier can only be used from within OpenCode`）**永不重试**，
  IP 信誉 403 与 429 退避重试；429 用更长的阶梯（2/5/9/15/25s）。
  客户端 header/body 原样转发，所以请求形状始终是真客户端的形状。
- **SSE 断流只观测不重放**：`finish_reason` 之前的断开记为 `truncated_streams`，
  因为字节已经在线上，重放会污染客户端状态。
- **端口独占**：用 IP Helper API（`GetExtendedTcpTable`）判定端口归属，
  不用 bind 试探——`SO_REUSEADDR` 会让两个进程绑同一端口再随机分流。
- **零停机换手**：`handover-request.json` 文件握手 + drain，
  实测 `drain=True, downtime 0 ms`，80 次探活中 79 次穿过切换。
- **看门狗**：计划任务每分钟拉起 `supervisor.py`，代理退出约 3.2 秒恢复；
  端口变更时自动改写 `opencode.json` 里的模型地址。
- **仪表盘**：`/__ui` 单文件零依赖，`/__events` SSE，`/__control` 提供
  pause/resume/restart/shutdown/drain。
- **隔离测试**：`testenv/test_retry_proxy.py` 14 个场景 / 48 条断言。

## 0.0.0-dev — 占位

`VERSION` 读不到时进程报告这个值，表示「不是从仓库或暂存目录启动的」。
