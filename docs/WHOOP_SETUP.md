# WHOOP 本地接入指南

本项目的 OAuth、六资源同步、CSV/ZIP 映射和加密存储已实现；2026-09-07 已通过首次真实授权、
Keychain 访问和六资源同步，随后完成真实刷新与 30 天请求窗口。真实撤销、系统提示界面的完整测试与实际导出映射仍待进行。
默认继续运行合成环境。以下命令是你准备使用本人数据时，在项目目录主动执行的步骤。

macOS 上，创建 App 后也可在本机 Terminal 运行 `./scripts/connect-whoop.command`。
该入口逐项询问 Client ID、已登记回调和首次存储期限，再调用下文相同 CLI；
Client Secret 通过隐藏提示输入，WHOOP 授权由本人在浏览器完成。
此入口仅配置和登录，完成后再进行第 4 节的小窗口同步。已有真实库沿用其原政策。

## 1. 创建开发者应用

1. 登录 [WHOOP Developer Dashboard](https://developer-dashboard.whoop.com/)，使用你的 WHOOP 账号。
2. 如首次使用，创建一个 Team，再创建 New App。名称可用 `Personal Copilot`，联系人填你自己的邮箱。
3. 控制台要求本项目的隐私说明网址。已准备 [隐私说明草稿](PRIVACY_DRAFT.md)；
   先填入实际联系人，并放到你选择的可访问网页。仓库文件不会自动发布，不能用占位网址或 WHOOP 自己的政策替代。
4. 注册回调地址：`http://127.0.0.1:8765/oauth/callback`。
   此地址已通过当前应用的首次真实 OAuth 联调；如其他应用注册被拒绝，保留其错误信息再调整方案。
   本项目没有自动建立公开端点或隧道。
5. 勾选六项只读权限：`read:cycles`、`read:recovery`、`read:sleep`、`read:workout`、
   `read:profile`、`read:body_measurement`。程序在授权请求中额外申请 `offline` 用于刷新。
   Logo 和 webhook 可先留空。
6. 保存应用的 client ID 和 client secret。secret 只在下方本地隐藏输入中填写，不粘贴到聊天或命令参数。

步骤依据 [WHOOP Getting Started](https://developer.whoop.com/docs/developing/getting-started/)
及本轮 [公开资源核查](F1_RESOURCE_FINDINGS.md)。

## 2. 初始化有期限的本地加密存储

```sh
uv sync --locked
uv run whoop-copilot init-real --retention-days 90 --accept-local-storage
```

这条命令表示：你作为数据所有者授权将 WHOOP API 和导出数据保存在本机，
每个来源版本从 API 页面采集或本地文件接收起保留 90 天，并同意清理依赖这些版本的分析和受管备份。
中断续传、重复导入和恢复不会延长已有版本的保留期。
可以把 90 改成你实际需要的天数（1–3650）；也可用 `--allow-source whoop_export` 只允许导出。
这不授权发送给模型、通知接收方或公开网站。

默认真实数据库为 `runtime/real/copilot.sqlite3`，使用 SQLCipher；随机密钥保存到系统钥匙串。
首次访问时 macOS 可能询问钥匙串许可。合成库 `runtime/synthetic/copilot.sqlite3` 保持独立。
钥匙串不可用会报错，不会回退明文。更改现有保留政策需要单独实现受控变更，当前不会静默覆盖。

## 3. 配置并授权 OAuth

```sh
uv run whoop-copilot whoop configure --client-id YOUR_CLIENT_ID
uv run whoop-copilot whoop login
uv run whoop-copilot whoop status
```

`configure` 提示隐藏输入 client secret，保存到专用钥匙串项；本地配置文件只保存 client ID、回调和 scope。
`login` 先启动只绑定 `127.0.0.1:8765` 的临时回调，再打开官方授权页，最多等待约三分钟。
完成后回到终端查看安全状态；输出不会包含 token。端口被占用时先释放端口。
若注册了不同端口，可在 `configure` 中用 `--redirect-uri` 指定完全一致的本地地址。

如果 WHOOP 拒绝 state 或回调格式，不要自行关闭校验。官方 state 文档存在“八字符”与“至少八字符”的措辞差异，
本实现使用强随机长值，已通过当前应用的首次授权。过期或中断的授权重新执行 `login` 即可。

## 4. 同步本人数据

先选一个较小窗口，例如：

```sh
uv run whoop-copilot --environment real whoop sync --start 2026-09-01T00:00:00Z --end 2026-09-06T00:00:00Z
```

同步 profile、body measurement、cycle、recovery、sleep、workout。每页写入加密暂存并保存游标；
下载完成后按固定 OpenAPI 验证，再原子写入领域记录。恢复数据关联的 cycle 若不在窗口列表中，会按 ID 补取。
新窗口最长 366 天，单次完整快照最多 10,000 条；过大时缩小时间范围。

失败或页数预算用完时，输出包含 `run_id`。查看和继续：

```sh
uv run whoop-copilot --environment real whoop sync-status RUN_ID
uv run whoop-copilot --environment real whoop sync --resume RUN_ID
```

继续时保持原窗口；不要更改已存游标。授权过期先重新登录；遇到来源契约变化、混合版本或账户不一致，
应解决错误后新建窗口。同步暂存及过期 run 受相同保留政策约束。
需要回看源修正时主动重跑同一窗口；重复来源会去重。当前没有定时轮询或 webhook，不能把一次成功当成持续同步。

```sh
uv run whoop-copilot --environment real analyze whoop.hrv_rmssd --provider whoop --start 2026-09-01T00:00:00Z --end 2026-09-06T00:00:00Z
uv run whoop-copilot --environment real analyze whoop.strain --provider whoop --resource workout --start 2026-09-01T00:00:00Z --end 2026-09-06T00:00:00Z
uv run whoop-copilot --environment real work
```

周期与单次训练的同名指标不自动合并，需要 `--resource cycle` 或 `--resource workout`。
基本资料和身体测量接口没有测量时间，因此完整保留原始响应，但不伪造带测量时间的体重趋势。
复杂睡眠阶段和训练区间等字段保存在原始记录中，目前只对已注册标量做趋势分析。

## 5. 使用官方导出

按 [WHOOP 官方导出说明](https://support.whoop.com/s/article/How-to-Export-Your-Data)申请本人的导出。
文件仍由你保管；原导出及你的副本不在本应用的自动删除范围内。

```sh
uv run whoop-copilot export-inspect /absolute/path/to/export.zip
```

只输出成员文件、表头、行数和时间格式信息，不输出行内容。官方公开说明没有完整列名契约，
因此需要将实际表头填到 [映射示例](../examples/whoop-export-mapping.example.json) 的结构中，
保存到 `runtime/real/export-mapping.json`。示例中的 `Synthetic ...` 是合成列名，不能直接当实际导出表头。

- `resource`：`physiological_cycles`、`sleep`、`workout` 或 `journal`。
- `member`：ZIP 内精确 CSV 文件名；单 CSV 为 null。
- `identity_columns`：能稳定识别记录的实际列；日期修正时不应把另一列时间自动当新身份。若只能用时间列，需承认匹配限制。
- `start` / `end`：实际时间列和格式；timezone 可用 `from_timestamp`、IANA 时区或明确 UTC 偏移。
  夏令时歧义或不存在时刻会拒绝，需提供明确偏移，不能猜 UTC。
- `metrics`：实际数值列、注册指标名及其确切单位。当前不自动猜单位；日志只保留原始行。

```sh
uv run whoop-copilot --environment real import-export /absolute/path/to/export.zip --profile runtime/real/export-mapping.json --exported-at 2026-09-05T12:00:00-07:00
```

`exported-at` 使用实际导出时间；它不是 WHOOP 的记录修改时间，系统会明确标记。
导出 provider 为 `whoop_export`，API 为 `whoop`；不自动跨两者去重、绑定账户或合并趋势。
同一明确身份及同一版本重复导入会去重。ZIP 在内存中读取，并限制大小、条数、路径和解压比。

无需本人数据也可以验证这条路径：

```sh
uv run whoop-copilot import-export tests/fixtures/export_sample.csv --profile examples/whoop-export-mapping.example.json --exported-at 2026-09-05T00:00:00Z --synthetic
```

## 6. 备份、清理和断开

```sh
uv run whoop-copilot --environment real backup runtime/real-backups/snapshot.sqlite3
uv run whoop-copilot --environment real restore runtime/real-backups/snapshot.sqlite3 runtime/restored-real/copilot.sqlite3
uv run whoop-copilot --environment real purge-expired
uv run whoop-copilot whoop disconnect
uv run whoop-copilot --environment real forget-source whoop --confirm-provider whoop
```

备份同样加密，目标密钥在钥匙串按路径保存；恢复保留原到期时间，含过期数据的备份被拒绝。
过期清理或明确删除来源时，删除关联数据、分析缓存、任务/暂存及本应用登记的备份；自行编写的本地计划保留。
如果受管备份文件被替换，会停下清理并报错，防止删错文件。

自动过期清理发生在打开真实库及读取分析时。应用未运行时不会自行唤醒清理；
你自行复制、移动的文件、原始导出、系统快照和系统备份不在受管清理范围内。
钥匙串的跨设备备份/迁移尚未实现；移动加密文件并不会自动转移密钥，丢失钥匙串会导致无法解密。
当前恢复验证覆盖本机显式路径，不代表生产灾难恢复已经验收。

`disconnect` 请求 WHOOP 撤销连接，成功后清理该 OAuth 凭据；不会隐式删除仍获准保留的本地记录。
若需要移除本地数据，另执行 `forget-source`。WHOOP 删除事件的 webhook 接入尚未实现。
真实库目前只供本地 CLI 使用；向模型或 Codex MCP 发送本人健康信息需要后续单独授权与接入设计。
