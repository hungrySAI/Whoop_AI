# WHOOP 官方资源与复用决策

核查日期：2026-09-05。范围：公开官方文档、公开 OpenAPI 与官方支持文章；没有登录真实账号、获取用户 token 或调用个人数据端点。本文登记设计依据，不代表真实 WHOOP 接入已验收。

F1 的详细官方核查见 [F1_RESOURCE_FINDINGS.md](F1_RESOURCE_FINDINGS.md)，
当前实现与测试见 [STATUS.md](../STATUS.md)。下文保留 F0 核查与后续复用决策的形成依据；
OAuth、六资源同步、导出映射及 SQLCipher 已通过合成验证；2026-09-07 另通过首次真实授权及六资源小窗口联调，
字段空值兼容修订见 [F1 补充记录](F1_RESOURCE_FINDINGS.md#2026-09-07-首次真实联调补充)。

## 2026-09-07：Journal 日常接入复核

本次只读取公开文档，未读取个人健康数据。
[当前 API Reference](https://developer.whoop.com/api/)未列出 Journal 读取端点；
[官方 webhook 事件表](https://developer.whoop.com/docs/developing/webhooks/#webhook-event-types)
列出的更新/删除事件涉及 recovery、sleep 和 workout，没有 Journal 事件。
因此本次未找到可供当前个人应用使用的官方 Journal 自动读取方式；此结论限定于公开文档，
不推断未公开合作接口的能力。官方文件导出仍是已有接入路径，但要求操作者主动取得文件。

用户已明确否定将反复导出/导入作为长期步骤。Journal 导入保留为可选历史补充，
真实导出验收暂缓，不阻塞 API 日常看板或后续开发。
监控下载目录只能自动处理已得到的文件，不能解决反复生成/下载导出的问题。
主线优先复用 API 可获取的官方指标和本地按需更新，不新增重复填写 Journal 的要求。

## F0 采用的边界与 F1 复用依据

F0 按 WHOOP v2 的数据语义制作明确标记的合成样例，验证版本、查询、趋势与证据链；另用独立 CSV 指标验证多来源能力。WHOOP 已提供的 Recovery、HRV、RHR、睡眠等计算结果直接作为来源指标使用。本应用只计算明确标注的均值、差值等派生统计，不复制或声称还原 WHOOP 算法。

| 资源 | 核查结果 | 复用安排 |
|---|---|---|
| [WHOOP API Reference](https://developer.whoop.com/api/) | 普通 OAuth 核心资源包括 cycle、recovery、sleep、workout、profile、body measurement；另外列有独立 Trusted Partner 权限资源 | 本轮复用 recovery v2 字段语义；F1 按实际 scope 逐项接入 |
| [官方 OpenAPI JSON](https://api.prod.whoop.com/developer/doc/openapi.json) | API 页面直接提供的机器可读契约 | 本轮用其核对数据类型、评分状态和单位；F1 优先由固定版本契约生成或校验客户端模型，避免手写整套接口 |
| [官方教程目录](https://developer.whoop.com/docs/tutorials/) | 提供 JavaScript / Passport 与 Postman 的授权、刷新及请求示例 | F1 复用流程与请求模式；本轮核查范围内未找到 WHOOP 官方维护的 Python SDK，不能把社区包标为官方 |
| [官方数据导出](https://support.whoop.com/s/article/How-to-Export-Your-Data) | 成员可获取生理周期、睡眠、训练、日志的 CSV | F1 优先评估作为用户主动导入及历史回填来源；本轮通用 CSV 样例不等于已兼容 WHOOP 导出格式 |
| [WHOOP Coach](https://support.whoop.com/s/article/How-to-Use-the-AI-Powered-WHOOP-Coach) | 官方应用已有训练、恢复和睡眠问答，以及晨间和训练后反馈 | 将其作为已有产品能力；本项目优先实现可复算证据、自有训练记录及获准的跨来源分析。公开个人 API 中未核实到 Coach 对话调用接口 |
| [MCP 官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 官方实现客户端、服务端和标准传输 | 本轮复用 SDK 实现协议层；业务计算留在共享应用服务，不手写 JSON-RPC 协议栈 |

SDK 的安装版本必须与其版本文档一致。[MCP Python SDK 文档](https://py.sdk.modelcontextprotocol.io/)在核查时标明 v2 为当前稳定发布线，并提供 v1 文档入口；若项目固定使用 v1，必须明确版本并验证该版本的接口，不能混用两代示例。

## Recovery v2：实现依据

普通个人数据根路径为 `https://api.prod.whoop.com/developer/v2`。恢复集合使用 `GET /recovery`，单个周期的恢复使用 `GET /cycle/{cycleId}/recovery`，授权范围为 `read:recovery`。[API Reference](https://developer.whoop.com/api/)

恢复记录以 `cycle_id`（整数）关联周期，以 `sleep_id`（UUID 字符串）关联睡眠；还包含 `user_id`、`created_at`、`updated_at`、`score_state` 和可能缺失的 `score`。内部个人身份不能直接等同于 WHOOP `user_id`。[OpenAPI：Recovery](https://api.prod.whoop.com/developer/doc/openapi.json)

| `score` 字段 | 单位 / 语义 | 处理 |
|---|---|---|
| `recovery_score` | 0–100，百分比 | 保留官方结果；均值等为本应用派生结果 |
| `hrv_rmssd_milli` | RMSSD，毫秒（ms） | 保留测量方法；不与其他 HRV 定义静默合并 |
| `resting_heart_rate` | bpm | 不从普通心率样本重算冒充该值 |
| `user_calibrating` | 布尔值 | 显示校准状态，并在统计规则中明确是否排除 |
| `spo2_percentage` | 百分比 | 可选，不把缺失补成 0 |
| `skin_temp_celsius` | 摄氏度 | 可选，不把缺失补成 0 |

字段、范围及可选性依据[官方 OpenAPI](https://api.prod.whoop.com/developer/doc/openapi.json)；RHR 的 bpm 单位另由[WHOOP 的 RHR 定义](https://www.whoop.com/us/en/thelocker/normal-resting-heart-rate-improve-fitness/)核对。SpO2 与皮温的 schema 说明仅在 WHOOP 4.0 或更新设备的数据中提供。

评分状态必须区分 `SCORED`、`PENDING_SCORE` 与 `UNSCORABLE`；未评分不能生成虚构数值。周期没有对应恢复数据也属于正常情况。官方[当前 Recovery 教程](https://developer.whoop.com/docs/tutorials/get-current-recovery-score/)说明了缺恢复、等待评分及新用户校准场景，但示例仍使用 v1 URL；应复用这些处理规则，并以当前 v2 契约确定字段和路径。

**日期归属是本项目的显式规则。** Recovery 自身没有 `start`、`end` 或时区字段；应关联 sleep / cycle 的实际时间，再按定义归属日期。`updated_at` 表示来源修改时间，不能冒充测量时间。本轮 fixture 的观测日期若来自外层合成元数据，必须标明它是适配器上下文，不属于 WHOOP recovery 原生字段。[API 样例](https://developer.whoop.com/api/)

## 后续直接复用的接入机制

- **版本与 ID：** 新实现使用 v2；sleep / workout 使用 UUID。v2 recovery webhook 的 ID 是关联 sleep UUID，并非 cycle ID。无需为新项目开发 v1 历史映射；该映射只在确有旧数据时使用。较旧指南仍出现 v1 描述，版本状态以[迁移指南](https://developer.whoop.com/docs/developing/v1-v2-migration/)及[API Changelog](https://developer.whoop.com/docs/api-changelog/)核对。
- **授权：** 沿用 OAuth 2.0 authorization code、回调 `state` 校验与最小 scope。需后台刷新时申请 `offline`；按 `expires_in` 管理有效期。刷新会轮换 access / refresh token，服务端需原子保存并防止并发刷新冲突；用户断开时调用撤销接口。普通个人授权与 Trusted Partner client credentials 不可混用。[OAuth 文档](https://developer.whoop.com/docs/developing/oauth/)
- **分页：** 响应字段为 `next_token`，下次请求参数为 `nextToken`；保留同一查询条件，直到 token 缺失或为空。recovery 的 `limit` 默认 10、最多 25。不要凭单页结果声称历史完整。[分页指南](https://developer.whoop.com/docs/developing/pagination/)、[API Reference](https://developer.whoop.com/api/)
- **限流：** 文档默认每分钟 100 次、每天 10,000 次；实际运行读取 `X-RateLimit-*` 响应头并处理 429。后续采用有界重试与回看修正，避免用高频轮询代替数据完整性设计。[Rate Limiting](https://developer.whoop.com/docs/developing/rate-limiting/)
- **变更通知：** 复用 v2 webhook 的更新 / 删除事件，再读取获准资源；按官方规则验证时间戳、原始请求体及 HMAC 签名。应用层另需防重放和幂等处理。F0 源修正只模拟重算机制，不代表 webhook 已联调。[Webhooks](https://developer.whoop.com/docs/developing/webhooks/)
- **睡眠与负荷：** 官方睡眠结果已有阶段毫秒时长、睡眠需求，以及表现 / 效率 / 一致性百分比；cycle / workout 已有 strain 与心率等。后续按字段直接映射，不重建官方评分模型；毫秒转分钟等转换保留原单位与来源。[API Reference](https://developer.whoop.com/api/)

普通开发者支持 FAQ 要求拥有 WHOOP 设备，没有承诺通用开发 sandbox。[开发支持](https://developer.whoop.com/docs/developing/support/) 当前 API 中的 `partner/development/add-test-data` 属于 Trusted Partner 的非生产实验室测试资源，不能据此声称普通个人 recovery 有可用官方测试账户。[API Reference](https://developer.whoop.com/api/)

## 真实数据启用前待核实

以下来自[WHOOP API Terms of Use](https://developer.whoop.com/api-terms-of-use/)的工程影响摘要，不是对具体使用方案已经获准的结论：

1. 第 2 节要求用户授权、准确说明使用与共享的隐私政策，以及传输和静态加密。普通 SQLite 与文件访问权限不能单独证明已满足真实 WHOOP 数据的静态加密要求。本轮仅以合成数据验收。
2. 第 4.2 节以数据所有者明确许可或适用法律为例外前提，对永久副本、数据库和超过缓存头期限的保留作出限制。真实接入须记录适用授权依据、缓存 / 保留规则及删除传播；不能仅凭 OAuth 成功就声称无限期版本归档获准。
3. 第三方可见性或模型服务外发须核验明确 opt-in、接收方、用途及适用隐私政策；本轮离线分析没有取得真实数据外发许可。
4. 凭据不得提交到源码；数据来源与所有权需正确标注。条款还对数据售卖、逆向算法和竞争性使用等设有限制，产品范围扩大或发布时需结合实际方案重新核实。

F1 的具体待办包括：实际开发应用及 scope、OAuth 回调与轮换、分页 / 429、修正和删除事件、真实样例逐字段覆盖、WHOOP 导出列名与时区约定、缓存响应头、保留依据、加密与恢复路径。上述事项尚无真实账号验证，不计入 F0 通过项目。

## 2026-09-07：恢复回看的官方关联复核

F2.6 复核官方 [Recovery](https://developer.whoop.com/docs/developing/user-data/recovery/)、
[Sleep](https://developer.whoop.com/docs/developing/user-data/sleep/)、
[Cycle](https://developer.whoop.com/docs/developing/user-data/cycle/) 与
[Workout](https://developer.whoop.com/docs/developing/user-data/workout/) 文档及固定 OpenAPI：
Recovery.cycle_id 指本次恢复适用周期，sleep_id 指其关联睡眠；Sleep 另有 cycle_id 和 nap。
生理周期沿用官方起止时间，不自行以午夜切分；只有明确 ID 命中后才展示该关联，nap 标志独立保留。
Workout 无 cycle_id，本批不把时间邻近的运动声称为官方周期关联，也不推导下一周期关系。

官方已有按 cycle 查询 recovery/sleep、按 sleep_id 查询 sleep 的接口，当前六资源同步已提供本批所需原始字段，
因此优先从本机保留记录组合，不增加新轮询或为列表缺项临时发请求。
睡眠表现、效率、一致性与呼吸率使用已有官方 score 与规范观测，未重建评分模型。
WHOOP 已提供[趋势](https://support.whoop.com/s/article/Viewing-Trends)和
[Recovery Impacts](https://support.whoop.com/s/article/Recovery-Insights)；本批价值是本地关联来源与缺项核查，
不复制 Journal 行为影响分析或[Strain Target](https://support.whoop.com/s/article/Strain-Coach)的活动建议。

## 2026-09-07：长间隔补取复核

复核 [官方 API](https://developer.whoop.com/api/) 与
[分页说明](https://developer.whoop.com/docs/developing/pagination/)：继续用已有 v2 collection
的 start/end、nextToken 和固定分页条件取历史。日期过滤基于资源发生/相交时间，并非 updated_since；
恢复与睡眠/周期关联沿用当前适配器，边界外关联周期仍通过已有 cycle_by_id 取回。
近期重查可获得该范围内已发布的评分或修正，不能据此保证任意旧修正/删除完整；缺行不构成删除事件。
WHOOP 负责设备与手机端同步，本机副本无需持续开机；使用前应已有可从官方 API 读取的数据。
自动回查 366 天及重查 7 个 UTC 日期是本应用策略，不是 WHOOP 更新频率或账户历史完整性的承诺。
