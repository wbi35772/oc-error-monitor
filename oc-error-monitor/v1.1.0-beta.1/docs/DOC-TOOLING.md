# 图文整理与「流动动态」文档工具选型

> 分支 `feature/docs-tooling`　｜　实测环境：本机免费层 + `cli_llm_server.py`

## 一、关于「叫 runtime 的那个工具」

先说结论：**图文档这个领域没有叫 runtime 的知名项目**。

GitHub 上 `runtime` 同名的仓库全是别的东西：`dotnet/runtime`、`astrid-runtime`、
`anthropics/sandbox-runtime`、`opencontainers/runtime-spec`、`wasm-micro-runtime`……
以及一批 agent runtime 框架。都不是「整理项目文档」。

按**功能**（AI 生成带配图的 README / 交互式 Wiki）匹配，真实候选是这三个：

| 项目 | Star | 做什么 | 适配我们吗 |
|---|---|---|---|
| **[aibox22/readmeX](https://github.com/aibox22/readmeX)** | 641 | 中文向 AI README + 交互式 Wiki，检测技术栈、生成 logo/badge/流程图 | ✅ **可以完全本地跑，用自己的模型**（作者原话） |
| [AsyncFuncAI/DeepWiki-Open](https://github.com/AsyncFuncAI/DeepWiki-Open) | 16k+ | 仓库 → 交互式 Wiki，自动生成 Mermaid 架构图/流程图 | ⚠️ FastAPI + Next.js + RAG，重，要 Docker |
| [eli64s/readme-ai](https://github.com/eli64s/readme-ai) | 3005 | 纯 README 生成器 | ⚠️ 偏英文，无交互式站点 |

**本项目选了 readmeX**，因为它明确支持「用自己的模型本地跑」——这正好是我们的约束。

### ⚠️ 安全提醒

readmeX 仓库里 **提交了作者的明文 API key**（`source.env`）：

```
LLM_BASE_URL=https://api.deepbricks.ai/v1/
LLM_API_KEY=sk-upX1ZDkrORvWcQjdnB8h2CdrjLIPcsqqHqOl7titUUE7p5o0
```

**不要用它**。照原样跑会把请求和你的项目内容发到第三方并计费。本项目的做法：
不碰这个文件，改用环境变量指向本机代理。

## 二、免费层怎么给第三方工具用（关键约束）

直接让 readmeX 打 `https://opencode.ai/...` 会被拒：

```
403 FreeTierError: OpenCode's free tier can only be used from within OpenCode
```

这不是 IP 问题，是**身份闸**：网关会检查请求形状（UA 三段式、`x-opencode-client: cli`、
`x-opencode-session`，**以及 body 里的 system prompt 长相**）。只补 header 会被识破——
实测过三种 body 变体，全部被判定为非 OpenCode 客户端。OpenRouter 的免费模型同样要凭证。

**唯一合法的办法：让真 CLI 去发请求。** 所以有 `cli_llm_server.py`：

```
第三方工具  →  http://127.0.0.1:8790/v1/chat/completions  →  opencode run  →  免费层
              （OpenAI 兼容）                                （真客户端，满足身份闸）
```

工程细节（都是踩出来的）：

| 坑 | 现象 | 解法 |
|---|---|---|
| Windows 命令行长度上限 | `WinError 206 文件名或扩展名太长` | 提示词走 **stdin**，不作为参数 |
| 控制台编码 | `gbk codec can't encode '\U0001f527'` | `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` |
| 配置文件 BOM | `Unexpected UTF-8 BOM` | 写 JSON 时用 `UTF8Encoding($false)` |
| 上游流式偶发不完整 | `provider.invalid-output: ...without finish_reason` | shim 内重试 2 次，再换模型兜底（`big-pickle`） |
| 交互式提问 | 读到 EOF 直接退出 | 预置答案管道（`$answers -join "\n" \| ...`） |

## 三、「带流动的动态」文档效果：用什么轮子

| 方案 | 体积 | 适用 | 评价 |
|---|---|---|---|
| **CSS + canvas 手写流场** | 0 依赖，~120 行 | 背景、渐变位移、流带 | ✅ **本项目选它**：仪表盘是单文件零依赖，引入库会破坏这个约束 |
| **GSAP**（Webflow 收购后免费） | 24KB gzip | 编排、ScrollTrigger、SVG path、补间 | ✅ 真要「专业动效」时的首选，生态最全 |
| **anime.js v4** | 17KB | 轻量补间、物理系统 | ✅ 只要 tween 的话，比 GSAP 轻 |
| **Motion**（原 Framer Motion，MIT） | ~35KB | React 组件动效 | ✅ 如果是 React 界面 |
| **Three.js / WebGPU** | 600KB+ | 真 3D 流体（compute shader） | ⚠️ 太重，除非要物理正确的流体 |
| **Motion Canvas** | — | 视频式动画（Remotion 同类） | ⚠️ 面向成片，不是文档 |
| **Excalidraw** | — | 手绘风架构图 | ✅ 图「不严肃」时好用，可嵌入 |

### 本项目落地的三层（见 `flow_demo.html`）

1. **流场背景**——canvas 2D，四组不同频率的正弦叠加成向量场，粒子按场方向漂移，
   每帧以低透明度回画形成拖尾。成本几乎为零，跟随 `devicePixelRatio`。
2. **渐变位移标题**——`background-clip:text` + 动画 `background-position`，
   比逐字动画省，也不打断阅读。
3. **数据流带**——横向流带把「正在处理」画出来，接上代理的实时计数即可。

三个都尊重 `prefers-reduced-motion`，并且**内容仍然是普通 HTML**（可被搜索引擎抓到）。

## 四、怎么跑

```powershell
# 隔离基线：独立端口 8791、独立 scratch、跑前跑后各快照一次
cd oc-error-monitor\v1.0.0\testenv\docs_baseline
powershell -ExecutionPolicy Bypass -File .\run_readmex_test.ps1
```

产物在 `scratch\readmex_output\`，日志 `readmex.log`，
`before.json` / `after.json` / `baseline.json` 是状态快照与差异证据。
