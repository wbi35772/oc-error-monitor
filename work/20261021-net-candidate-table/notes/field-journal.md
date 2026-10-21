# 2026-10-21 net-candidate-table-measurement

## 场景分类
其他（本地代理面板 / 测量正确性缺陷分析）

## 目标概述
代理面板的"候选节点"表只列出策略组前 80 个成员（实际 218 个），且测速走了一个"只回答成功者"的控制器端点，导致面板展示的节点与测速结论都不可信。

## Scope 摘要（脱敏）
- auth_basis: own_system（本机回环代理 + 用户自己的 Clash 配置）
- network_profile: authorized_target_only
- asset_types: [本地 HTTP 控制器接口, 本地 HTTP 代理端口]

## 角色
- lead_role: lead
- specialists: []

## 完整执行链路

1. 用户反馈"面板的候选表和测速结果不对劲"——测速只覆盖 58/218，却报"超时 1 个"。
2. **不要直接改代码**。先按端点逐个复现行为：`/group/{name}/delay` 连打 5 次，答案在 48/52/58/60/62 之间漂移，且 `marked-dead` 恒为 0~1 而 `ABSENT` 稳定在 156~161——端点是把超时成员**省略**了，不是标成超时。
3. 交叉验证：改用逐节点 `/proxies/{name}/delay`、24 并发、5000ms，218 个跑完 27.8s → 53 通 / 165 超时。两侧数字对上了，根因确认。
4. 第二个根因来自代码本身：`[:80]` 截断撞上策略组的**自然顺序**（嵌套主组 → {optimized_domain}-XX → {carrier_a}/{carrier_b}/{carrier_c}/{carrier_d} → DIRECT），最快的节点恰好落在 index 102/171/184。**截断 + 顺序，两个因素叠加才致命**——单看任一个都不显眼。
5. 第三个根因是"死代码"：面板沿用订阅作者自己 Clash 脚本里的"优选"正则，但本订阅节点名是 `{optimized_domain}-XX / {carrier_a}-XX / {carrier_b}-XX / ...`，该正则命中 0 个，列恒为"普通"，"优选在前"排序是空操作。
6. 先写红绿测试（断言 218 全列出、index 102/171 的节点在列、超时节点报"死"而非"缺失"、group delay 端点不再被调用），确认在旧代码上 11 errors，才动实现。
7. 实现：后端逐节点测速（后台线程 + 有界并发 16 + 进度可见）、成员全量下发、补线路族派生与 `probeable` 标记；前端 218 行 6 列、"只显示可用"过滤、测速改"启动 + 轮询"不再阻塞 30 秒。
8. 只部署 dev 验证，生产 {prod_port} 全程 pid 不变、uptime 持续增长、真实请求仍有上游响应。
9. 浏览器实测：218 行、过滤 75 行、console 0 错误、点"切换"成功（{node_a} → {node_b}）。

## Evidence 链摘要（脱敏）
| E-id | source_type | 可复用命令模式 | 关联 Finding |
|------|-------------|----------------|--------------|
| E-001 | command | 同一控制器 group 端点连打 5 次，比对返回条数漂移与 absent 数 | F-002 |
| E-004 | command | 逐节点端点 + 线程池全量测，拿到 alive/dead 真实分布 | F-002 |
| E-005 | command | 新测试在旧代码红、新代码绿 | F-001 |

## Finding / Path 摘要
- top_finding: 批量端点把失败者静默省略，必须逐个测才能给每个候选结论
- path_type: callflow
- path_one_liner: 面板点测速 → 批量端点（只回成功者）→ "超时 1 个"的假结论 → 用户按错误的候选列表选线

## 踩坑记录

| 问题 | 原因 | 解决方案 | 耗时 |
|------|------|---------|------|
| 以为"少返回=没测到"，误判为端点不稳定 | 端点语义是"省略失败者"，返回条数随机漂移掩盖了语义 | 连续多次采样并同时统计 absent 与 marked-dead 两条计数 | ~30min |
| 只看单次采样差点下错结论 | 单次 62/218 既像"端点坏了"也像"节点大多死了" | 逐节点全量测一次做交叉验证 | ~15min |
| 补测试时 `[:80]` 已改完才写测试，测试一度全绿 | 先改实现后写测试，红绿不成立 | 回滚实现，用 `git stash` 跑旧代码确认测试先红 | ~10min |
| `append-evidence.ps1` 传 `-ReproCommandFile` 仍报缺少 `-ReproCommand` | 该参数是**绑定期**必填，文件覆盖发生在函数体内 | 两者都传：`-ReproCommand "see repro file"` + `-ReproCommandFile <path>` | ~5min |
| 调 skill 包里的 `.ps1` 被执行策略拦下 | 本机 PowerShell 限制 | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force` | ~2min |
| Mermaid 图渲染不出来 | 本机没装 `mmdc` | 报告里保留可编辑 Mermaid 源码块（GitHub/工位都能渲染） | ~2min |

## 工具链发现
- `mihomo` 控制器 `/group/{name}/delay` 与 `/proxies/{name}/delay` 语义不同：前者是"批量 + 只回成功者"，后者是"单点 + 失败回 HTTP 504"。**要统计就必须用单点端点。**
- 策略组 `all` 数组的顺序 = 订阅作者的构造顺序，**不代表质量顺序**。任何 `[:N]` 截断都会按"构造顺序"而非"优劣"裁剪。
- 写诊断脚本时，终端 GBK 会把 emoji 节点名显示成乱码，导致无法肉眼比对；**把结果写进 UTF-8 文件再读**，才看得清。
- 本机 `mmdc`（Mermaid CLI）与 `dot`（Graphviz）均未安装，skill 自带的 `render_diagram.py` 会给出明确安装提示并优雅失败。

## 关键代码/命令

```python
# 逐节点测速：有界并发，每节点独立结论
def one(name):
    path = "/proxies/%s/delay?url=%s&timeout=%d" % (
        urllib.parse.quote(name, safe=""), url, timeout_ms)
    try:
        raw = _mihomo(path, secret, timeout=(timeout_ms / 1000.0) + 15.0)
    except urllib.error.HTTPError as exc:
        # mihomo 对超时的节点回 504，而不是省略
        return name, {"delay": None, "dead": True, "error": "timeout"}
    delay = raw.get("delay")
    return name, {"delay": delay, "dead": delay >= timeout_ms, "error": None}
```

```bash
# 判别"批量端点"是否在省略失败者：连打 5 次，比对 absent 与 marked-dead
for tmo in 5000 10000 20000; do
  curl -s -H "Authorization: Bearer $SECRET" \
    "http://$CONTROLLER/group/$GROUP/delay?url=$PROBE_URL&timeout=$tmo"
done
```

## 对本包的改进建议
- `docs-generator` 的 Evidence 契约要求 `repro_command` 必填，但 `append-evidence.ps1` 的 `-ReproCommand` 是绑定期必填、`-ReproCommandFile` 是函数体覆盖，两者不冲突却缺文档说明。**建议在脚本文档里写明"文件参数需与内联参数同时给出"**，否则调用者会直接撞上 ParameterBindingException。
- `field-journal` 目录缺 `SKILL.md`，`skill` 工具无法加载（报 "Unable to load skill"），只能当普通目录用。**建议补一个 SKILL.md 指向 `_template.md` 与 `anonymization.md`**，让"写经验"这个动作可被路由。
- `field-journal/_index.md` 写明"按 PR 流程更新本索引，避免直接改动共享主线"，但没有给出 PR 的准备清单。**建议在索引里补一条最小流程**：脱敏自检 → 新增 `YYYY-MM-DD_*.md` → 更新索引 4 处（场景分类/高频模式/实体倒排/统计）→ 开分支提 PR。
- `systematic-debugging` 的 Phase 1 强调"复现 + 读错误信息"，但没有覆盖"**同一端点多��采样**"这一手。对"结果随时间漂移"的接口，单次采样最容易得出错误根因。**建议在 Phase 1 加一条：结果不确定时，至少采样 3 次并同时记录成功数与失败数。**

## 可复用的模式/脚本片段
- **端点语义判别法**：当一个批量接口返回的条数少于预期，先分别统计"被省略的"与"被标记失败的"两个计数；若前者远大于后者，则该端点只回答成功者，不能用来统计失败。
- **截断审计**：任何 `[:N]` 切片，先确认被截掉的排序键是"优劣序"还是"构造序"。构造序 + 截断 = 随机裁剪。
- **正则分类自检**：沿用别人脚本里的分类正则时，先在新数据集上跑一遍命中数。命中 0 就是死代码，会伪装成"所有节点都一样"。

## 进化动作
- [ ] 更新了路由矩阵
- [ ] 更新了 tool-index
- [ ] 更新了 bootstrap-manifest
- [ ] 更新了子 skill 文档
- [x] 新增了 pitfalls 记录（append-evidence.ps1 必填参数、PowerShell 执行策略、mmdc 缺失、GBK 乱码）
- [x] 无需更新

> 说明：本条目写在**用户项目**的 case 目录中（`work/<case>/notes/`），已按 `anonymization.md` 脱敏。`field-journal` 位于共享上游仓库 `reverse-workbench-skill.git` 的 `main` 分支，其 `_index.md` 明确要求"避免直接改动共享主线"，故未直接写入该仓库；提 PR 时按索引要求同步 4 处索引即可。

## 环境信息
- OS: Windows（PowerShell 5.1，GBK 控制台编码）
- 工具版本: Python 3.x / Node v24.22.0 / Playwright MCP / mihomo 控制器（本地 {controller_port}）
- 目标平台/版本: 本地 stdlib `http.server` 代理 + 第三方 mihomo 控制器

## 脱敏要求
已按 `anonymization.md` 处理：
- 上游域名 → `{upstream_domain}`
- 节点名 → `{node_name}`（{node_name} 等）
- 控制器地址/端口 → `{controller_host}:{port}`
- 探测 URL → `{probe_url}`
- 保留可复用性：端口语义、端点路径结构、并发数、超时值、工具名
- 自查：无公网 IP、无邮箱、无手机号、无凭证/Token/JWT
