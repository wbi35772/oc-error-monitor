# Evidence index

| id | 观察 | source_ref | severity | 关联缺陷 |
|---|---|---|---|---|
| [E-001](E-001.md) | 设置只存内存：重启后 force_route 回空，runtime 目录无 settings 文件 | command | high | F-001 |
| [E-002](E-002.md) | route 整块落盘会冻死默认值：改了 rotate_on，老实例仍报旧值 | command | high | F-002 |
| [E-003](E-003.md) | pick_route 从不写回 current：sticky 永不生效，switches 恒 0 | command | high | F-003 |
| [E-004](E-004.md) | ERR:URLError 不在可轮换类里：四个死节点各失败一次，无人被冷却 | command | high | F-004 |
| [E-005](E-005.md) | 零数据时 DIRECT 按名排序胜出，安全网静默接管整个池 | command | medium | F-005 |
| [E-006](E-006.md) | 96/216 每轮全死且扎堆在两个族，逐成员冷却会逐个撞完 | command | high | F-006 |
| [E-007](E-007.md) | member_health 读路径建档：route_status 让 clear 看起来没生效 | command | medium | — |
| [E-008](E-008.md) | 生产 8788 重启后回到 attempts=4 / route=null（v1.1.0 无持久化） | command | info | P-005 |
| [E-009](E-009.md) | 真机请求：adopt → sticky；失败时 switches=1 且下一次换节点 | command | info | — |
| [E-010](E-010.md) | 面板五视图实测渲染正常，路由视图 218 行成员表 | screenshot | info | — |
| [E-011](E-011.md) | 我自己用 PowerShell 破坏了 ui.html（ANSI 读 UTF-8）并修复 | command | info | — |
| [E-012](E-012.md) | 429 在 direct 与 proxy 两条路径上先后出现：限流键不在出口 IP | command | high | P-007 |

## 缺陷与证据的对应

| 缺陷 | 现象 | 根因 | 证据 | 修复 |
|---|---|---|---|---|
| F-001 | 重启后路由设置回出厂值 | 设置只在内存 | E-001 | `1ea68f4` |
| F-002 | 改了默认值，老实例不认 | route 整块落盘 | E-002 | `1ea68f4` |
| F-003 | 看上去在轮换，switches 恒 0 | 策略分支不写回账本 | E-003 | `e6536a9` |
| F-004 | 死节点连撞四次，谁都不冷却 | 日志词汇与策略词汇无交集 | E-004 | `e6536a9` |
| F-005 | 池子被静默绕过 | 零数据时 DIRECT 排序胜出 | E-005 | `e6536a9` |
| F-006 | 整族逐个撞 | 96/216 成族死掉 | E-006 | `e6536a9` |

## 复现方式

- 单元：`cd oc-error-monitor/v1.2.0-dev && python test_route_policy.py -v`
- 真机：见 E-009 的复现段（改仓库 → Copy-Item 到运行目录 → `POST /__control {"action":"restart"}` → 等 7 秒）
- 面板：浏览器打开 `http://127.0.0.1:8899/__ui#/route`
