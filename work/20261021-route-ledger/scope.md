# scope

| 项 | 值 |
|---|---|
| auth.status | granted |
| network_profile | authorized_target_only |
| ready_for_act | true |

## in_scope.assets

| 资产 | 用途 |
|---|---|
| `http://127.0.0.1:8899/__pool` | 候选池发现（新增） |
| `http://127.0.0.1:8899/__route` | 路由账本与动作（新增） |
| `http://127.0.0.1:8899/__stats` | 计数器与设置 |
| `http://127.0.0.1:8899/__ui` | 面板（无构建步骤） |
| `http://127.0.0.1:9097/proxies` | mihomo 控制器，只读 |
| `http://127.0.0.1:8788/__health` | 生产存活确认，只读 |
| `http://127.0.0.1:7897` | mihomo mixed 口，用于出口 IP 回显 |

## out_of_scope

- dos、phishing_real_users、unrestricted_exfil
- 生产 8788 的代码改动（本轮只验证，不改）
- 线上 mihomo 的 `ipv6` 开关（用户已决定永久关闭）
- 用户的 Clash 脚本与路由器配置

## 边界说明

分析对象是*用户自己机器上的本地代理与面板代码*，未触达任何第三方资产。所有流量测量都指向 opencode.ai（用户自己的免费模型网关），且只做只读回显与健康检查。

数据留存：`route_state.json` / `settings.json` 只在运行目录，不入仓库（`.gitignore` 已加）；证据文件只保留命令与回显摘要，不含任何凭据。
