# workitems

| id | 事项 | 结果 | 证据 |
|---|---|---|---|
| W-01 | 设置持久化（L0） | `settings.json` 原子写 + 导入读回；真重启后 force_route / attempts 存活 | E-001, E-009 |
| W-02 | route 只落盘差量 | 修掉「默认值被冻死」；补测试 `test_only_differences_are_stored` / `test_a_changed_default_reaches_an_existing_file` | E-002 |
| W-03 | 未知键保留 | 顶层 / route / select 三层都原样保留；补测试 | — |
| W-04 | 池发现（`/__pool`） | 实时从 mihomo 组发现，DIRECT 作伪成员入池；发现依据回退顺序：配置 > 实时连接 > 模式 > 规则 > 组名 | — |
| W-05 | 成员账本 | ok / fail / fail_by_class / last_ok_ts / last_fail_ts / ewma_ms / cooldown_until / decay_ts，半衰期衰减 | E-009 |
| W-06 | 粘性决策 | adopt（沿用 mihomo 当前节点）→ sticky（能用就不换） | E-003, E-009 |
| W-07 | 策略注册表 | least_fail / ewma_ok / lowest_latency / round_robin / weighted_random + 被动 off / none / mihomo / manual | — |
| W-08 | 词汇翻译层 `policy_class` | 日志词汇 → 策略词汇；未识别类原样返回（只记账不轮换） | E-004 |
| W-09 | 族级冷却 | 同族 3 个成员在窗口内失败 → 整族冷却 900s；孤立节点不连坐 | E-006 |
| W-10 | `/__route` 端点 | GET 看账本；POST 动作 pin / strategy / set / clear / switch / reset_current | E-009 |
| W-11 | 面板「路由」视图 | 决策、策略旋钮、族判死表、218 行成员健康表、锁定 | E-010 |
| W-12 | 回归测试 | 新增 `test_route_policy.py` 41 例；全套 75 例 | E-009 |

## 做过的判断

| 判断 | 理由 |
|---|---|
| `rotate_on` 对齐 `controller.SWITCH_CLASSES` 而不是自创 | controller 里的集合有回测依据（等 3 次爆发是反效果；换模型 8/8 优于换节点 33–83%），自创一套会与之冲突 |
| `AUTH_IP_REPUTATION` 不进 `rotate_on` | 它归会话层处理（换模型），不是换出口 IP 能解决的 |
| `RATE_LIMIT` 不进 `rotate_on` | 退避即可，轮换会掩盖真正的限流原因 |
| `DIRECT` 作为池成员而不是模式 | 用户要求「不要写死」；DIRECT 本来就是池里的一员 |
| `paused` 不写盘 | 忘了的暂停在重启后看起来跟这次修的 bug 一模一样 |
| 探针不自动跑 | 218 个成员一轮两百次延迟测试；做成面板按钮，结论在 `stale_s` 内复用 |

## 没做

| 事项 | 原因 |
|---|---|
| 生产 8788 回移 L0 | 本轮只验证不改生产；等用户校验节奏与 P-007 实验结论 |
| 自动探针（L1 自动消费） | 218 成员 × 每分钟 200 次延迟测试代价高；P-006 待办 |
| 429 是否按出口 IP 记的对照实验 | 属于下一轮校验迭代的题目（P-007） |
| 跨节点并发探测（L3 后半） | 节点数还没到需要并发的规模 |
