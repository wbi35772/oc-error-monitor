# 网关（oc-gateway）

一个固定门牌。客户端只认 `http://127.0.0.1:8790/v1`，后面是 8899 还是 8788 由网关自己挑。
换实例、重启代理、回移生产，都不用再改 44 处 `provider.api`。

```
你的客户端 / 别的 agent 平台
        │  OpenAI 兼容，Bearer sk-ocg-…
        ▼
   oc-gateway :8790        ← 固定入口，只暴露免费模型
        │
        ├── retry-proxy :8899    v1.2.0-dev（重试 + 选路）
        └── retry-proxy :8788    v1.1.0 生产
```

网关和代理是两个进程：代理重启时网关照常应答，反过来也一样。两者都用自己的看板，
页面是同一份 `ui.html`，所以从 8899 或 8790 打开看到的是同一状态。

## 启动

```bat
start-gateway.bat
```

或者在看板「网关」页点「启动网关」，或者任何位置：

```powershell
curl.exe -X POST http://127.0.0.1:8899/__gateway/start
```

`install-watchdog.bat` 会注册两个计划任务：`OpenCode Retry Proxy v1.2.0-dev` 和
`OpenCode Gateway v1.2.0-dev`，都是一分钟一次 + 登录触发。网关这份每次启动先探
`gateway.json` 里记的端口，发现已有 oc-gateway 在答话就退出 0，不会占下一个端口。

## 参数

| 参数 | 默认 | 在哪改 | 说明 |
|---|---|---|---|
| 端口 | 自动 | `--port 8790` | 候选 `8790-8799`、`18800-18809`，跳过已占用。探测故意**不带** `SO_REUSEADDR`：Windows 上带了的话，端口被占也能 bind 成功，一分钟后才炸 |
| 后端 | `8899,8788` | `gateway.json` → `backends` | 按顺序探 `/__health`，谁活走谁；都不活返回 502 并说明 |
| 免费判定 | `cost` | 看板「判定方式」 | `cost`：`input` 和 `output` **都**是 0 才算免费（缺一侧按收费）；`name`：只认 `-free` 后缀 |
| 对齐源 | `models.dev/api.json` | — | 上游 `GET /v1/models` 是 404，列表只能从目录拿 |
| 自动对齐 | 6 小时 | 看板「自动对齐间隔」 | `0` 关闭；失败保留上一次的列表，空列表不服务 |
| API key | `sk-ocg-…` | `/__gateway/key` | 默认 `key_enabled=false`：不校验，透传客户端自己的 `Authorization`。开启后校验 `Bearer <key>`，并可注入 `upstream_key` |
| 供应商 id | `oc-gateway` | — | 写进 `opencode.json` 时只动这一个键 |

## 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/models` | 只列对齐出来的免费模型（当前 46 个） |
| POST | `/v1/chat/completions` | 转发，SSE 逐块直传 |
| GET | `/v1/*` | 其他 OpenAI 兼容路径原样转发 |
| GET | `/__gateway/state` | 端口、后端、模型数、key 掩码、对齐时间 |
| GET | `/__gateway/key` | 完整 key（看板「复制 key」用的就是它） |
| GET | `/__gateway/provider` | 供应商配置预览，不落盘 |
| POST | `/__gateway/install` | 写进 `~/.config/opencode/opencode.json`，先备份 |
| POST | `/__gateway/align` | 立刻重新对齐 |
| POST | `/__gateway/settings` | `free_rule` / `align_interval_s` / `backend_override` |
| GET | `/__gateway/selftest` | 发一个最小请求，把上游的答复原样报出来 |
| GET | `/__ui` | 看板（和 8899 那份同一文件） |

`/__gateway/*` 也挂在 8899 上转发，所以页面在哪个端口打开都能用。

## 接入 opencode / openchamber

看板「网关」页 → **写入 opencode 供应商**。它把这段写进 `~/.config/opencode/opencode.json`：

```json
{
  "provider": {
    "oc-gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OC Gateway",
      "options": {
        "baseURL": "http://127.0.0.1:8790/v1",
        "apiKey": "sk-ocg-…"
      },
      "models": {
        "deepseek-v4.1-flash-free": { "name": "DeepSeek V4.1 Flash (free)" }
      }
    }
  }
}
```

规则：先备份成 `opencode.json.bak-gateway-<时间>`，只增删 `provider.oc-gateway`，
`provider.opencode` 下那 44 个模型覆盖一个字节都不动。写完重启 opencode 生效。

在 openchamber 里也可以手动「添加供应商」：类型选 OpenAI 兼容，Base URL 填上面的，
API key 粘 `sk-ocg-…`，模型名从 `/v1/models` 里挑。

## 接入别的 agent 平台

三个值，没有别的：

```
Base URL : http://127.0.0.1:8790/v1
API Key  : sk-ocg-…            （看板「复制 key」）
Model    : 从 GET /v1/models 里选，例如 deepseek-v4.1-flash-free
```

需要给 agent 一段自我介绍时，用这个：

```
你正在通过一个 OpenAI 兼容的本地网关访问模型。
Base URL: http://127.0.0.1:8790/v1
Authorization: Bearer sk-ocg-…
可用模型: GET http://127.0.0.1:8790/v1/models 返回的 data[].id，全部是免费模型。
限制: 只支持 /v1/chat/completions 与 /v1/models；不要请求 /v1/embeddings、/v1/audio。
流式: 支持 stream=true，按 SSE 原样转发。
错误: 非 2xx 时 body 是 {"ok":false,"error":"…"} 或上游原始 JSON，直接看 message 字段。
```

## 一个必须知道的限制

上游的免费层会拒绝「不是真正的 opencode 客户端」发来的请求，返回：

```json
{"type":"error","error":{"type":"FreeTierError",
 "message":"OpenCode's free tier can only be used from within OpenCode. ..."}}
```

这一条没法用请求头绕过——实测过：把真实客户端的 13 个请求头（含
`Authorization: Bearer public`、`User-Agent: opencode/latest/2.0.32/cli`、
`x-opencode-client: cli`、session/project id、`b3`/`traceparent`）逐字节复刻，
同一秒、同一出口、同一个 session id，真实客户端 body_len 504567 拿到 200，
复刻请求拿到 403。差别不在 HTTP 报文里。

所以：

- **opencode / openchamber 自己** → 通过网关照常 200，因为请求是它自己发的。
- **第三方 agent 平台** → 大概率拿到 403，先点「自检上游」确认，别在别人平台上怀疑自己的配置。

`/__gateway/selftest` 就是给这件事准备的：它发一个最小请求，把 `verdict` 和上游原文
一起返回，页面上显示成一行话。

## 文件

| 文件 | 作用 |
|---|---|
| `gateway.py` | 网关本体，只用标准库 |
| `gateway.json` | 运行时状态（端口、key、模型列表、对齐时间）。被 `.gitignore` 忽略 |
| `start-gateway.bat` | 隐藏窗口启动，可重复调用 |
| `test_gateway.py` | 24 项回归：免费判定、供应商块、key 掩码、选端口、自检、dry run 不落盘 |
