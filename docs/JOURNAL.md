# Journal 可选历史补充

用户已明确排除定期手动导出/导入的日常流程。本功能仅保留为按需历史补充，
不要求下载、不要求定期维护文件，也不作为看板或后续开发的依赖；实际导出验收暂缓。
以下是用户主动需要导入时的技术说明，不是日常使用步骤。
当前公开个人 API 未提供 Journal 读取端点，webhook 也未列出 Journal 事件；
自动处理下载目录只能减少导入操作，不能免去生成和下载导出，见 [资源复核](WHOOP_RESOURCES.md#2026-09-07journal-日常接入复核)。

F2.3 已实现官方导出显式映射与只读日志时间线，合成验证通过。实际 Journal 文件尚未取得，
真实表头、日期语义、本人归属与导入兼容性为 **not_run**。仓库示例不能直接当作真实 WHOOP 表头。

## 下载官方导出

在 WHOOP App 中进入 **More → App Settings → Data Export**，按界面创建导出并通过收到的邮件下载。
官方说明将 Journal Entries 列为导出类别，内容是用户自述的行为与日常记录。
以 [WHOOP 官方导出指南](https://support.whoop.com/s/article/How-to-Export-Your-Data)和当前 App 界面为准
（2026-09-07 核查）。下载文件留在本机；向项目操作者提供完整路径即可，不必将健康内容粘贴到聊天。

## 核对后导入

复用已有 `export-inspect` / `import-export`。浏览器没有上传、编辑或通用文件读取接口。
下列路径和时间是占位示例，须替换成已核对的真实文件与导出时间；不要原样执行。

```sh
uv run whoop-copilot export-inspect /absolute/path/to/export.zip
uv run whoop-copilot --environment real import-export /absolute/path/to/export.zip \
  --profile runtime/real/journal-mapping.json \
  --exported-at 'YYYY-MM-DDTHH:MM:SS+HH:MM'
```

检查器不打印数据行，只提供文件结构、表头、行数及可识别时间列诊断；表头本身也可能包含行为问题，
实际检查输出和映射文件应留在本机。先确定 ZIP 的精确成员名、CSV 编码和实际字段，
再核对以下语义并创建本机 mapping。`runtime/real/` 被 Git 排除。

| 项目 | 必须明确的含义 |
|---|---|
| 本人归属 | 操作者确认这是当前本地 subject 的导出；不能通过 OAuth 自动核验 CSV 账户 |
| 稳定身份列 | 同一日志修正前后应保持相同，回答不得作为身份；没有可靠身份时先解决映射，不伪造官方 ID |
| 日期依据 | 日志归属日期 `reported_date`，或有确切时分的周期开始 `cycle_start`；不自动把填写日移到前一天 |
| 时间精度 | `date` 仅日期；`instant` 必须含时分，不将日期伪装成午夜测量 |
| 时区 | IANA 名称或固定偏移；时间点自带的明确偏移优先，模糊/不存在的当地时间拒绝猜测 |
| 问答 | 指定每个问题/回答的精确列，或宽表中的明确问题文字；保留原文，空白为未填写 |
| 导出时间 | 带偏移的快照时间，由操作者提供用于版本排序；不是 WHOOP 逐条记录修改时间 |

现有真实环境必须已授权 `whoop_export` 来源，并使用 SQLCipher 与专用 Keychain 密钥。
无需重新创建开发者应用或更改 OAuth scope。真实导出不得加 `--synthetic`，也不得导入默认合成库。

## Mapping v2

[合成映射示例](../examples/whoop-journal-mapping.example.json)展示长表：一列问题、一列回答。
与其配套的 [合成 CSV](../tests/fixtures/journal_sample.csv)只用于开发验证。
保留 v1 原有的 `resource`、`member`、`identity_columns`、`start`、`end`、`timezone`、`metrics` 字段，
将 `version` 设为 2、`resource` 设为 `journal`、`end` 为 null、`metrics` 为空列表，并增加：

```json
{
  "journal": {
    "date_basis": "reported_date",
    "time_precision": "date",
    "subject_confirmation": "operator_confirmed_local_subject",
    "answers": [
      {"question_column": "Synthetic question", "answer_column": "Synthetic answer"}
    ]
  }
}
```

宽表可把单个绑定改成 `{"question": "明确的问题文字", "answer_column": "实际回答列"}`。
每个回答列只绑定一次；两种问题来源不可混用在同一个绑定内。
`date` 使用严格的年月日格式，需明确时区；`cycle_start` 必须配合 `instant`。
v1 原始 Journal 仍可保留，但需按原稳定身份重新明确映射后才能显示，界面不会猜列。

沿用 CSV/ZIP 的 32 MiB、32 个归档成员、10,000 行与 256 列等预算，不解压到磁盘。
每条日志最多 128 个问答，问题 1,000 字符、单个回答 8,000 字符、总文本 64,000 字符。
超出预算时应明确调整范围或实现，不静默截断内容。

## 看板与更新语义

导入成功后打开 [本地看板](http://127.0.0.1:8766/)，选择「日志时间线」并「刷新日志」。
7/30 天按日志所标日期筛选，每页 20 条；相同日期、时区和日期依据分组。
每组并列其当日范围内最近的 API 恢复分、睡眠表现和周期负荷，按已有测量起点归属，
恢复沿用关联周期开始时间。保留无记录及未评分，不用更早有效值代替当天最新未评分记录。
并列展示不表示该行为导致该结果，也不是 WHOOP 的行为影响评分。

Journal「日志来源」显示日期精度、导出/导入时间和本人归属说明；API 指标有独立来源。
「同步数据」及 30 分钟新鲜度仅针对 API 指标；它们不会下载 Journal，也不说明日志是否最新。

重复导入继续去重，同一稳定身份的新快照可形成修正版，后来导入的旧快照不会覆盖新版本。
时间线和来源详情只显示当前有效版本。**新导出不含某个旧行，不自动表示该行已被 WHOOP 删除**。
当前没有单条日志编辑/删除；现有本地清除入口作用于整个 `whoop_export` 来源，应按实际范围使用。

原始行和规范问答共享同一加密源版本及保留期限，HTTP 只返回映射内容和必要来源字段；
未映射列、路径和身份列不返回浏览器。没有模型外发，MCP 仍保持合成环境。
到期及受管备份清理沿用原机制；下载的 CSV/ZIP、独立复制件、系统快照和已打开的浏览器内容
不在远程擦除范围，详见 [架构与生命周期](ARCHITECTURE.md)。
