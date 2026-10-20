# Windows 网络监控工具 — 本地工作区

四个上游仓库已分别克隆到独立目录，并完成了当前环境下可执行的初始化。

> 深入分析（架构、能力边界、二开入口、选型建议）见 [`ANALYSIS.md`](ANALYSIS.md)。
> mihomo / Clash Verge 的现状与下一阶段约束见 [`MIHOMO-BASELINE.md`](MIHOMO-BASELINE.md)（只读侦察，未改配置）。

## 一览

| 目录 | 上游仓库 | 当前提交 | 本地状态 |
|---|---|---|---|
| `wirewatch/` | [ImMike/wirewatch](https://github.com/ImMike/wirewatch) | `ba26218` (2026-06-03) | ✅ Python 3.12 venv + 依赖已安装，模块导入验证通过 |
| `simple-netmon/` | [simple-netmon/oss](https://github.com/simple-netmon/oss) | `a336a5a` (2026-07-10) | ✅ 已用 Go 1.27.3 构建出 `dist/netmon.exe` |
| `openprocmon/` | [progmboy/openprocmon](https://github.com/progmboy/openprocmon) | `d0cb39c` (2026-09-07) | ✅ 官方 v0.5.0 预编译已就位，GUI + MCP 已验证；源码构建仍缺 MSVC `link.exe` |
| `systeminformer/` | [winsiderss/systeminformer](https://github.com/winsiderss/systeminformer) | `dd016bb8c` (2026-10-19) | ✅ 官方 v4.0.26241.138 预编译已解压到 `dist/`，签名有效；源码构建仍需 VS2022 + WDK |

磁盘占用（含 `.git` 与 `dist`）：wirewatch 46 MB、simple-netmon 13 MB、openprocmon 64 MB、systeminformer 145 MB。

> 每个子目录都是**独立的 git 仓库**，`origin` 均指向官方 GitHub 地址。根目录 `.gitignore` 已把这四个目录排除，工作区仓库本身不会误提交它们。
>
> 注意：本机还没有配置 `git user.name` / `git user.email`，在各仓库提交前需要先设置。

## 快速启动

### 0. 启动脚本

每个仓库都有一个 `start.bat`，根目录有汇总的 `start-all.bat`。双击即可，脚本会自动请求管理员权限（UAC）。

| 脚本 | 作用 |
|---|---|
| `start-all.bat` | 一次提权，启动四个工具；两个面板已在运行时只打开浏览器，不重复起服务 |
| `wirewatch\start.bat` | 缺 venv 时自动创建并装依赖；已在运行则只打开 8080 面板 |
| `simple-netmon\start.bat` | 缺 `dist\netmon.exe` 时用本地 Go 自动构建；已在运行则只打开 8888 面板 |
| `openprocmon\start.bat` | 优先运行 `dist\v0.5.0\procmon-gui.exe`，否则回退到 `target\release|debug` |
| `systeminformer\start.bat` | 查找 `bin\Release*` 或 `dist\<version>\<arch>` 下的可执行文件；都没有时打印构建/下载指引 |

> 2026-10-20 验证：`start-all.bat` 提权后，WireWatch(8080)、Simple NetMon(8888)、OpenProcMon GUI、
> System Informer 四个进程同时在运行；两个面板复用了已有实例，没有重复监听端口。

> 两个常驻面板脚本会先探测端口：若服务已在运行，只打开浏览器，不会重复启动。
> WireWatch 启动时会去掉 `NO_PROXY` 中 IPv6 地址的方括号（httpx 不认 `[::1]`），并让 GeoIP 直连
> `ip-api.com`——本地代理对明文 HTTP 可能返回 502，导致 ASN/国家为空。

> 所有脚本都会自提权，因此请在能看到 UAC 弹窗的桌面上运行；在远程/无交互会话中会卡住。

### 1. WireWatch（端口 8080）

需要**管理员权限**（WinDivert 内核驱动抓包）。

```powershell
# 在管理员 PowerShell 中
cd D:\Project\Openchamber\Opencode网络\wirewatch
$env:NO_PROXY = ($env:NO_PROXY -replace '\[|\]','')
.\.venv\Scripts\python.exe -m wirewatch
```

打开 <http://127.0.0.1:8080>。数据落在 `wirewatch\data\watcher.db`。

> 仓库自带的 `run.ps1` 会用系统 `python` 而不是 venv；如需用它，先把
> `wirewatch\.venv\Scripts` 加进 PATH，或直接使用上面的命令。

### 2. Simple NetMon（端口 8888）

需要**管理员权限**（ETW 会话 + Windows 防火墙 COM）。

```powershell
# 在管理员 PowerShell 中
cd D:\Project\Openchamber\Opencode网络\simple-netmon
.\dist\netmon.exe
```

默认数据库：`%APPDATA%\SimpleNetMon\netmon.db`；`-h` 可看全部参数。

重新构建：

```powershell
. .\env.ps1          # 见下方“本地工具链”
cd simple-netmon
$env:GOPROXY = 'https://goproxy.cn,direct'
go build -trimpath -ldflags "-s -w" -o dist\netmon.exe .\cmd\netmon
```

### 3. OpenProcMon

**立即可用（官方 v0.5.0 预编译）**：

```powershell
cd D:\Project\Openchamber\Opencode网络\openprocmon\dist\v0.5.0
.\procmon-gui.exe          # 图形界面；实时抓取会请求管理员权限并加载驱动
.\procmon-cli.exe --help   # CLI / MCP 前端
```

驱动 `PROCMON24.SYS` 已内嵌在二进制里，无需单独安装 WDK。

> 官方 v0.5.0 的 `procmon-gui.exe` / `procmon-cli.exe` 未带 Authenticode 签名；内嵌驱动为微软签名。
> 请保留官方 release 下载来源与哈希，生产环境建议自行构建并签名。

**源码构建（当前被阻塞）**：需要 Visual Studio Build Tools 的 C++ 工具链（提供 `link.exe`）。
装好后：

```powershell
. .\env.ps1
cd openprocmon
cargo build -p procmon-gui --release   # 或 cargo build -p procmon-cli --release
```

所有 Rust 依赖已经预取到本地 `.tools\cargo`（`cargo fetch` 已完成），装好编译器后无需再大量下载。

接入 AI Agent：`procmon-cli.exe mcp` 是 stdio MCP server，配置方式见
`openprocmon\docs\mcp-guide_zh.md`；技能文件在 `openprocmon\skills\procmon\`。
本工作区已在 `C:\Users\24999\.config\opencode\opencode.json` 的 `mcp.servers.openprocmon`
注册，OpenCode 侧可见 16 个工具（实时抓取需要 UAC，分析 PML 不需要）。

### 4. System Informer

**立即可用（官方 v4.0.26241.138 预编译）**：

```powershell
cd D:\Project\Openchamber\Opencode网络\systeminformer
.\start.bat
```

`start.bat` 会优先查找源码构建产物 `bin\Release*`；若没有，再按当前 CPU 架构在
`dist\<version>\<arch>\` 中选择官方预编译。当前已解压到：

- `dist\v4.0.26241.138\amd64\SystemInformer.exe`（本机 AMD64）
- 同版本目录还包含 `arm64`、`i386` 及 x86 兼容版本
- Authenticode 签名有效：`CN=Winsider Seminars & Solutions Inc.`

**源码构建（可选，本次未执行）**：

```powershell
cd systeminformer\build
.\build_init.cmd        # 首次运行，下载工具/第三方依赖
.\build_release.cmd     # 或在 Visual Studio 里打开 SystemInformer.sln
```

前置条件：Visual Studio 2022（含 C++ 桌面开发）+ Windows SDK + WDK。
只使用官方预编译时不需要这些工具链；自编译内核驱动还需要测试签名或正式签名。

## 本地工具链

为避免改动系统环境，Go 和 Rust 装在仓库根的 `.tools\`（已被 git 忽略）：

- Go 1.27.3 — `.tools\go`
- Rust 1.99.1 (stable-x86_64-pc-windows-msvc) — `.tools\cargo` + `.tools\rustup`

在当前 PowerShell 会话中启用：

```powershell
cd D:\Project\Openchamber\Opencode网络
. .\env.ps1
```

如果被执行策略拦住：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\env.ps1"
```

`env.ps1` 同时设置了国内镜像：`GOPROXY=https://goproxy.cn`、Rust 发行版走清华 TUNA；
Cargo 的 crates.io 镜像写在 `.tools\cargo\config.toml`（目前仅对这个工作区的 Rust 生效）。

## 网络与镜像说明

克隆期间 GitHub 直连多次出现 `Connection was reset` / 超时：

- `wirewatch`、`simple-netmon` 直接克隆成功；
- `openprocmon`、`systeminformer` 通过 `ghfast.top` 镜像克隆，完成后已把 `origin` 改回官方 URL；
- Python 依赖走清华 PyPI 镜像安装；Go / Rust 依赖走各自国内镜像。

本机 git 配置了 `http.proxy=http://127.0.0.1:10808`（该端口在初始化过程中时通时断）。
日常 `git pull` 若失败，先确认该代理是否在运行；不需要代理时可临时：

```powershell
git -c http.proxy= -c https.proxy= pull
```

## 尚未完成 / 可选下一步

四个工具当前都已可运行；以下是“源码级”可选项和后续工作：

1. **（可选）安装 VS 2022 Build Tools（C++）**，解锁 OpenProcMon 源码构建：
   ```powershell
   winget install Microsoft.VisualStudio.2022.BuildTools --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
   ```
   需要管理员确认，安装体积数 GB；不装也不影响预编译 GUI/MCP。
2. **（可选）System Informer 源码构建**：在 1 的基础上再装 Windows SDK + WDK，然后 `build_init.cmd` → `build_release.cmd`。
3. **配置 git 身份**，否则无法提交本地改动。
4. **MCP 注册已完成**：OpenCode 配置在 `C:\Users\24999\.config\opencode\opencode.json` 的
   `mcp.servers.openprocmon`；其他客户端可参考 `openprocmon\docs\mcp-guide_zh.md` 手动接入。
5. **下一阶段（尚未开始）**：接入 mihomo 代理系统，做基于 OpenChamber 对话状态的智能选路、
   自动分流规则、进程劫持与节点管理；现状与约束见 [`MIHOMO-BASELINE.md`](MIHOMO-BASELINE.md)，
   需要先梳理“对话状态 → 选路策略”的需求边界，再单独设计。

## 会话韧性工具（`tools/oc-error-monitor/`）

这部分与四个监控仓库是**另一条线**：监控仓库负责“看见”，韧性工具负责“按错误类型分层缓解”。

| 层 | 工具 | 作用域 | 手段 |
|---|---|---|---|
| P1 | `monitor.py` | 只读 | 读 `opencode.db` + `main.log`，把错误分类落盘 |
| P2 | `controller.py` | 全局 | 对持续失败的网络错误切 `🎯 OpenCode专用` 节点 |
| P3 | `session_failover.py` | **单会话** | 只给卡住的会话换模型，不动全局网络 |
| 测量 | `probe_matrix.py` | — | 反复实测节点/模型成功率，写 `node-scores.json` |

全部默认 dry-run，写入要显式 `--apply`。详细说明和实测数据见
[`tools/oc-error-monitor/README.md`](tools/oc-error-monitor/README.md)，
设计与证据见 [`docs/superpowers/specs/2026-10-20-opencode-session-resilience-design.md`](docs/superpowers/specs/2026-10-20-opencode-session-resilience-design.md)。

```powershell
# 只读看错误
python tools\oc-error-monitor\monitor.py --once --backfill 20

# 测量哪个节点/模型最稳（会临时切节点，测完恢复）
python tools\oc-error-monitor\probe_matrix.py --nodes 4 --attempts 6

# 只看不做：切节点 / 换模型
python tools\oc-error-monitor\controller.py --follow
python tools\oc-error-monitor\session_failover.py --follow
```

2026-10-20 实测要点：免费模型**不需要登录任何账号**（本机无 `auth.json`、无任何 API Key 环境变量）；
`abusive traffic from this IP` 是**间歇性**的（同一节点同模型 8 次里 3 次成功），不是硬封禁；
节点延迟**不能**预测免费模型可用性（最快的 200ms 只有 67%，最慢的 615ms 有 83%），
所以选路按实测成功率排序。模型差异比节点差异大得多
（`openrouter/minimax/minimax-m3.1-flash:free` 8/8，`opencode/deepseek-v4.1-flash-free` 4/8），
已据此把 OpenChamber 项目默认模型改成前者。

## 安全提示

这四个工具都会读取本机进程、网络或内核事件；WireWatch、Simple NetMon、OpenProcMon
实时抓取还需要管理员权限并加载内核驱动（WinDivert / PROCMON24.SYS）。
仅在你拥有或已获授权的机器上运行。OpenProcMon 预编译包内嵌的是 Sysinternals Process Monitor 4.02 的微软签名驱动
（`PROCMON24.SYS`），仓库的 MIT 许可不一定覆盖该驱动的再分发；二开并重新分发前请先核实许可，生产环境建议用 WDK
自行构建并签名驱动。

## 备注：项目成熟度

| 项目 | Stars | 说明 |
|---|---:|---|
| System Informer | ~16k | 老牌项目（原 Process Hacker），最成熟 |
| OpenProcMon | ~355 | Rust 重写中，迭代活跃，带 MCP |
| Simple NetMon | ~2 | 很新，ETW 方案无驱动、代码量小 |
| WireWatch | ~0 | 刚发布的单提交项目，功能对路但需自行审查代码 |
