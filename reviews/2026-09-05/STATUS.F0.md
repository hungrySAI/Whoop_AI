# 实施状态

日期：2026-09-05。**F0 合成基础闭环已完成并实测。**
从用户确认的新建范围实施；没有原应用或原测试仓库可迁移。

## 已交付

- Python 模块化单体、SQLite 首版 schema 与独立 subject/provider 连接。
- 14 天 WHOOP v2 形状的合成 recovery/cycle，以及 14 条手工体重 CSV。
- 原始配对版本、规范观测/活动、原单位、测量时间、来源版本分量和获知时间。
- 确定性趋势与 EvidenceBundle；历史 `as_of`、修正失效、固定输入复算。
- 本地训练草稿、修改提案、具体内容审批、版本冲突、幂等提交与撤销。
- 持久重算任务、租约恢复、并发领取保护；CLI 有界 worker。
- CLI 和官方 MCP SDK 的 stdio 入口；SQLite 一致性备份与恢复。
- 新的权威项目说明、官方资源复用记录、锁定依赖及本地 Git 仓库。

原始审查材料及其证据保留。原根 README 已归档到
`reviews/2026-09-05/README.original.md`；旧审查中的“原始 47 项测试”并未随当前材料提供，
本轮不能声称已执行或保留其实现。当前新增验证与历史审查证据分开保存。

## 实测结果

运行环境：Python **3.13.15**；官方 MCP Python SDK **1.29.1**；依赖见 `uv.lock`。

| 验证 | 结果 |
|---|---|
| `pytest -q` | **89 passed**：适配器 51、受控操作 13、基础服务 20、MCP 集成 2、CLI 3 |
| Ruff lint | passed |
| Ruff format check | passed，14 个 Python 文件 |
| `uv lock --check --offline` | passed |
| 本地 `demo` | WHOOP 14 条 + CSV 14 条；训练草稿保持待确认 |
| HRV 合成趋势 | 14 点均值 53 ms；前半 46、后半 60；差值 14 ms |
| MCP 协议 | 通过真实本地子进程 stdio 握手、工具列表、查询与受控写入 |
| CLI / MCP 一致性 | 同一查询的 run ID、结果和证据相同 |
| 备份与恢复 | 两端 integrity_check=ok，恢复后原分析复算一致；禁止覆盖已有目标 |
| Git 忽略规则 | 15 项检查通过：源码/fixtures/示例保留，运行数据与秘密排除 |

具体输出见 [F0_VALIDATION.json](../../evidence/F0_VALIDATION.json) 和
[F0_DEMO.json](../../evidence/F0_DEMO.json)。核心测试还覆盖迟到旧版本、组合版本一进一退、
校准/未评分、历史不足、整批回滚、双连接并发、审批篡改/过期/撤销和重复提交。
任务恢复测试通过重新打开数据库、模拟租约到期验证可重新领取，未做生产故障演练。

## 分环境状态

| 环境或能力 | 状态 |
|---|---|
| 本地合成数据闭环 | passed |
| 官方 SDK 客户端与 MCP 服务端协议 | passed |
| Codex 桌面实际客户端联调 | not_run，未修改用户客户端配置 |
| 真实 WHOOP OAuth / API / 官方 CSV 导出 | not_run |
| 付费或真实模型调用 | not_run |
| 真实健康数据加密、保留、删除传播、生产恢复 | not_run |
| Web 看板、通知、常驻调度、部署 | not_implemented |

## 下一批建议

F1 优先利用 WHOOP 官方导出做主动导入/历史回填，并建立官方 v2 OAuth 的可靠接入。
先确认实际使用路径、数据范围与运行环境，再实施真实数据保护、保留/删除与逐字段验证。
当前首批运行不需要 WHOOP token 或模型 API key；启动方式见 [README.md](../../README.md)。
