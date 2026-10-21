# 2026-10-21 重启前快照

电脑重启前从生产 runtime 目录抓下来的运行数据，防止 runtime 被清理或覆盖后无法回溯。

| 文件 | 来源 | 说明 |
|---|---|---|
| `prod-8788-retry_proxy.log.jsonl` | `%USERPROFILE%\.config\opencode\oc-error-monitor-v1.1.0\retry_proxy.log.jsonl` | 生产 8788 全量请求日志，抓取时 345 条 |
| `prod-8788-deployed.json` | 同目录 `deployed.json` | 该版本部署记录，285 字节 |

抓取时间：2026-10-21 06:21 左右（重启前）。

对应的分析与结论见 `docs/2026-10-21_会话存档-重启前.md`。

## 这份日志能回答什么

- direct 与 proxy 两条路径的成功率对比（direct 92.5% vs proxy 67.9%）。
- 429 全部落在 proxy 路径；403 一次没出现。
- `deepseek-v4-flash-free` 的 HTTP400 是模型名不存在，不是网络问题。

## 注意

日志里有模型名、请求耗时、错误分类，没有 api key。提交到仓库前已确认不含凭据。
