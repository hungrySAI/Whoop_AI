# WHOOP Personal Copilot · V1

在本机查看 WHOOP 官方数据、趋势、周回顾和恢复关联，并追溯每个数值的来源。
默认使用合成数据；真实接入使用 WHOOP 官方 OAuth、SQLCipher 加密数据库与系统钥匙串。

**V1 已完成。当前技术版本为 0.4.2 / schema 3**，包含本地看板、按需同步与长间隔补取、
一键启动/停止、使用验收及数据稳健性加固。V1 是产品里程碑名称，包版本沿用 0.4.2。
真实数据保留在本机；没有模型调用、云部署或公开端点。
完整历史对账、真实撤销、跨设备恢复及实际导出兼容性仍属独立待验收项，详见 [实施状态](STATUS.md)。
Journal 仅为可选历史补充，日常使用无需定期导出或导入。

## 文档入口

| 目的 | 文档 |
|---|---|
| 首次连接 WHOOP | [创建应用与本地接入](docs/WHOOP_SETUP.md) |
| 日常打开、更新与恢复 | [看板使用说明](docs/DASHBOARD.md) |
| 已完成范围与已知缺口 | [实施状态](STATUS.md) · [验收记录](docs/ACCEPTANCE.md) |
| V1 归档与安全发布 | [V1 发布说明](docs/V1_RELEASE.md) |
| 架构、开发与后续方向 | [文档目录](docs/README.md) · [路线图](ROADMAP.md) |

## 快速运行

需要 Python 3.11+ 和 uv，在此项目根目录执行：

```sh
uv sync --locked
uv run whoop-copilot demo
```

首次演示导入 14 天恢复/周期记录与 14 条体重记录，创建一份未提交的训练草稿。
HRV 合成结果应为：14 个值、均值 **53 ms**、前半均值 **46 ms**、后半均值 **60 ms**、差值 **14 ms**。
重复演示会去重；输出 JSON 包含完整证据与草稿动作 ID。`demo` 从本仓库 `tests/fixtures/` 读取样本，
也可通过 `--fixtures` 指定同样格式的合成样本目录。

数据库默认位于 `runtime/synthetic/copilot.sqlite3`。所有命令可在子命令前用
`--db /absolute/path/synthetic.sqlite3` 指定其他合成数据库。
真实环境需要先执行指南中的 `init-real`；后续数据命令显式指定 `--environment real`。

## 本地看板

完成 [首次接入](docs/WHOOP_SETUP.md) 后，双击 [打开看板](scripts/open-dashboard.command)，
服务就绪后自动打开 [本地看板](http://127.0.0.1:8766)，启动成功后可关闭终端。
重复打开会复用同项目、同数据库的服务。看完后双击 [停止看板](scripts/stop-dashboard.command)；
同步进行中会提示稍后停止，保护原断点。关闭浏览器本身不会停止本机服务。

```sh
uv run whoop-copilot --environment real dashboard-open
uv run whoop-copilot --environment real dashboard-stop
```

无需凭据的合成看板使用单独数据库：

```sh
uv run whoop-copilot dashboard --demo
```

看板提供每日摘要、六项 7/30 天趋势、周回顾、恢复回看和来源详情。
打开时最多进行一次条件检查，按需同步与有限历史补取；页面停留时不定时采集，
电脑关机后服务退出，下次需要时再打开。后台定时同步暂缓。

真实环境仅在本机浏览器显示获准 SQLCipher 数据；脚本、图表和样式均本地提供。
[看板使用说明](docs/DASHBOARD.md)详述指标与日期口径、记录覆盖、更新与失败恢复、
端口冲突及可选 Journal 时间线。它保留原 README 中的完整日常操作说明。

## CLI 查询

```sh
uv run whoop-copilot metrics
uv run whoop-copilot analyze whoop.hrv_rmssd --start 2026-08-01T00:00:00Z --end 2026-08-15T00:00:00Z
uv run whoop-copilot analyze body.weight --start 2026-08-01T00:00:00Z --end 2026-08-15T00:00:00Z
uv run whoop-copilot import-whoop tests/fixtures/synthetic_whoop.json
uv run whoop-copilot import-csv tests/fixtures/synthetic_body.csv
uv run whoop-copilot tasks
uv run whoop-copilot work
```

时间参数必须带时区；窗口按 UTC 的 `[start,end)` 及周期开始时间选取。
`analyze --as-of <获知时间>` 查询当时已知版本。来源修改后旧分析标记 `stale`；
`work` 执行已排队的重算，`run <run_id>` 查看保存结果，`reproduce <run_id>` 用原版本重新计算。
页面概览、周回顾和日常来源列表使用即时计算，不新增持久分析；明确调用 `analyze` 才保存分析证据。
新重算任务只针对相关指标，最多 100 个请求一批，复用待处理任务；旧的超大任务明确失败而不静默截断。
早于首次获知时间的历史明确返回不足，不借用后来导入的数据。

## 训练草稿与确认

```sh
uv run whoop-copilot plan draft --title '演示计划' --details '仅用于合成流程验证' --key demo-request-2
uv run whoop-copilot plan inspect <action_id>
uv run whoop-copilot plan approve <action_id>
uv run whoop-copilot plan commit <action_id> --token <approval_token>
```

`approve` 展示完整动作，由本地操作者输入其 `payload_hash` 后签发短期 token。
经过明确授权的本地脚本可传 `--accept-hash <已审阅的哈希>`。该能力属于可信 CLI，MCP 不提供签发工具。
审批绑定内容、目标版本、范围和期限；重复提交只返回原结果。token 仅哈希落盘。
token 是短期能力凭据，不应共享给无关接收方或提交到仓库。

更新已生效计划使用 `plan propose-update <plan_id> --title ... --details ... --version <当前版本> --key ...`，
再走同一审批流程。`plan revoke <action_id>` 撤销尚未执行的动作。计划不是已执行训练记录。

## MCP

协议使用官方 Python SDK 的已锁定 v1 兼容线（当前锁文件为 1.29.1），不自行实现 JSON-RPC。
stdio 启动命令为：

```sh
uv run whoop-copilot-mcp --db /absolute/path/to/runtime/synthetic/copilot.sqlite3
```

客户端所需的通用启动参数示例见 [examples/mcp-server.example.json](examples/mcp-server.example.json)。
将其中项目与数据库路径替换为你的绝对路径。接口提供指标查询、分析、证据读取/复算、训练草稿及受控提交；
没有审批签发、导入任意文件、SQL 或 Shell 工具。MCP 目前仅开放合成环境；
本地真实数据存储授权不包含向模型发送健康信息。
测试使用官方 SDK 客户端实际启动子进程完成 stdio 握手与工具调用；Codex 桌面客户端尚未配置或联调。

## 备份、恢复与验证

```sh
uv run whoop-copilot backup runtime/synthetic-backups/f0.sqlite3
uv run whoop-copilot restore runtime/synthetic-backups/f0.sqlite3 runtime/restored/copilot.sqlite3
uv run whoop-copilot --db runtime/restored/copilot.sqlite3 analyze whoop.hrv_rmssd --start 2026-08-01T00:00:00Z --end 2026-08-15T00:00:00Z
uv run pytest -q
uv run ruff check .
```

备份复用 SQLite 一致性备份 API，执行完整性检查；恢复不会覆盖现有目标文件。
合成库使用普通 SQLite。真实库与备份使用 SQLCipher，密钥放系统钥匙串；
来源到期会清除关联数据、分析和受管备份，恢复不重置期限。
清理发生在应用运行时，独立副本与系统快照不在管理范围，跨设备密钥恢复尚未实现。
把真实数据标为 synthetic 不会使其变成合成数据。

## 官方资源复用与扩展

优先复用 [WHOOP 官方 OpenAPI](https://api.prod.whoop.com/developer/doc/openapi.json)、
[API 字段与既有指标](https://developer.whoop.com/api/)，保留官方分数，
仅计算明确标为本应用派生值的均值与差值。
[资源清单](docs/WHOOP_RESOURCES.md)记录官方导出、OAuth 示例、分页、限流、webhook 与 Coach 的复用安排。
`import-csv` 保留手工体重格式；`export-inspect` / `import-export` 处理显式映射的 WHOOP CSV/ZIP。
官方没有公开完整导出表头契约，示例映射使用合成列名，实际导出仍需逐字段核对。
OAuth 使用 Authlib 与 HTTPX，API 响应通过固定的官方 OpenAPI 快照验证，不重建评分算法。

- 新增来源：编写返回 `SourceRecordInput` 的适配器，保持来源身份、单位、时间和版本契约。
- 新增指标/分析：在 `analytics.py` 注册定义或函数，并添加其输入约束与回归验证。
- 新增动作：在 `commands.py` 增加命令及授权/版本/幂等规则，再按需适配 CLI/MCP。

架构见 [ARCHITECTURE](docs/ARCHITECTURE.md)，决策见 [DECISIONS](docs/DECISIONS.md)，
实际结果与未完成项见 [STATUS](STATUS.md) 和 [ACCEPTANCE](docs/ACCEPTANCE.md)。

## 第一版使用与恢复验收

已实际双击启动/停止脚本验证本机入口。进程独立于启动会话；真实恢复演练在临时隔离路径执行，
使用 SQLCipher 与独立 Keychain 密钥，恢复后的概览、周回顾、恢复回看及保留时间与源库一致。
临时产物和临时密钥均已清理。这是一次同机恢复演练，不是新增常驻备份或跨设备恢复功能。

0.4.1 修复恢复副本继承源库备份登记的问题。通过 `restore` 创建的库不拥有源库的备份清理权限，
只登记并清理其后自行创建的备份；源备份仍由原库管理。恢复目标仍须为不存在的新路径，
过期来源快照禁止恢复，恢复不延长来源保留期限。

合成进程异常中断可重复验收：

```sh
uv run python tests/process_recovery_drill.py
```

此命令只使用临时合成库，在受控子进程执行 SIGKILL，验证断点续传与后续补取，结束后清理。
普通 `backup`/`restore` 的运行方式继续沿用前文，密钥仍需本机钥匙串可用；原始导出、独立复制备份、
系统快照及跨设备密钥恢复不因此获得自动治理。完整证据见 [验收记录](docs/ACCEPTANCE.md)。
