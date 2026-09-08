# F1 官方资源核查记录

核查日期：2026-09-05。本文只登记公开资源及其工程含义，不作为功能验收证明。
本次未登录 WHOOP、创建开发应用、取得 token、读取任何个人数据或安装系统软件。
真实注册、OAuth 和 CSV 样例联调均为 `not_run`；实际软件验证见 `STATUS.md`。

## 官方 CSV 导出：确认到哪里

[WHOOP 官方导出说明](https://support.whoop.com/s/article/How-to-Export-Your-Data)
确认成员可以从应用发起导出，并获得生理周期、睡眠、训练和日志四类 CSV。
导出通过邮件提供下载链接；文章列出了每类数据包含的指标类别。

该说明**未给出可作为解析契约的精确文件名、CSV 表头、时间格式、时区约定、
空值表示或逐列单位**。对官方开发与支持域名的定向检索也未找到此类公开契约。
这表示本轮未核实，不能据此断言 WHOOP 从未向任何渠道提供过它们。
直接打开支持文章遇到页面渲染错误，正文核查使用该官方 URL 的搜索索引结果。

因此，社区代码中常见的 CSV 名称和列名不能标为官方确认。项目采用显式映射配置：
操作者将实际表头、时间列和单位映射到内部指标；未知或冲突配置应拒绝导入。
合成配置只证明解析和数据语义，不能证明已经兼容用户手中的实际 WHOOP 导出。
必须保留导出来源；不能将导出里没有的 API ID、评分状态、来源更新时间伪造成已知。

## OAuth：沿用公开接口与成熟库

| 项目 | 核查结论 |
|---|---|
| 授权地址 | `https://api.prod.whoop.com/oauth/oauth2/auth` |
| Token 地址 | `https://api.prod.whoop.com/oauth/oauth2/token` |
| Flow | Authorization Code；使用注册的 client ID 与 client secret |
| 刷新 | 授权时申请 `offline`；按 `expires_in` 处理有效期；刷新会轮换 access 和 refresh token |
| State | 回调必须验证原始随机值；OAuth 主文写八字符，Postman 教程写至少八字符，存在措辞不一致 |
| 回调 | 必须事先注册且与请求中的 redirect URI 相符；主文示例为 HTTPS 和自定义 scheme |
| PKCE | 本轮公开 WHOOP 官方文档与 OpenAPI 未核实支持；库支持 PKCE 不等于 WHOOP 已支持 |

依据：[WHOOP OAuth 文档](https://developer.whoop.com/docs/developing/oauth/)、
[官方 Postman 教程](https://developer.whoop.com/docs/tutorials/access-token-postman/)。
Postman 教程指定 client credentials 放在请求 body；Python 可复用 Authlib 的
`client_secret_post`，不自行实现 OAuth 编码、状态生成或 token 请求协议。
[Authlib HTTP Clients](https://docs.authlib.org/en/stable/oauth2/client/http/index.html)
提供相应支持。安装版本以项目锁文件为准，不能直接混用不同版本文档。

本项目的强随机、一次性 state，短期会话有效期，固定端点，刷新互斥，以及新 token
原子持久化是应用层落实方案；不能用“refresh 失败就重试旧 token”掩盖轮换冲突。
官方明确并发刷新中首个请求可能使后续请求所持的旧 refresh token 失效。

撤销使用 `DELETE https://api.prod.whoop.com/developer/v2/user/access`，Bearer 授权，
成功响应为 `204`；相关 webhook 也停止。它不是标准 OAuth RFC 7009 撤销路径，
不能让通用库凭默认地址猜测。[官方 API Reference](https://developer.whoop.com/api/)

## 用户创建开发应用的步骤与已知边界

1. 打开 [WHOOP Developer Dashboard](https://developer-dashboard.whoop.com/)，用本人
   WHOOP 账号登录。官方说明登录跳转至 `id.whoop.com`。
2. 首次进入按提示创建 Team，填写自选名称。之后创建 New App。
3. 名称可用 `Personal Copilot`；联系人填本人可收信的邮箱。填写项目自己的真实
   隐私说明 URL，至少选择本轮需要的数据 scope，并注册与本地配置完全一致的回调。
4. 本轮六资源实现选择 `read:recovery`、`read:cycles`、`read:sleep`、`read:workout`、
   `read:profile`、`read:body_measurement`。`offline` 在 OAuth 授权请求中添加，
   不是要求用户在表单勾选一个未经确认存在的开关。
5. Logo、webhook 不属于本轮必要项。点击 Create App 后把 client ID 与 client secret
   只输入项目的本地凭据入口。不要把 secret 发到聊天、源码、命令参数或共享截图。

流程、Team、scope、回调和凭据要求依据
[官方 Getting Started](https://developer.whoop.com/docs/developing/getting-started/)。
文档链接的 `/apps/create` 需要应用路由和登录环境；不能将公开抓取时的 404 当成
控制台不可用证明。

公开官方控制台当前表单额外确认：Name、Contact email、Privacy Policy URL、
至少一个 Redirect URL 和至少一个 scope 必填。Privacy Policy URL 须为 HTTP(S)。
Logo 未见必填规则，webhook 可留空。这些来自页面实际加载的
[官方公开脚本 main.1793346a.js](https://developer-dashboard.whoop.com/static/js/main.1793346a.js)，
并未访问登录态数据或内部 API。脚本可能随部署更新。

**本地 HTTP 回调仍需真实注册验证。** 该脚本的 redirect 字段仅校验 scheme、
非空目标及无 fragment；`http://127.0.0.1:8765/oauth/callback` 可以通过所见前端规则。
官方教程没有明确说明 `localhost` 或 `127.0.0.1` 的特殊许可；前端规则不能证明
服务端注册与授权端点也接受。因此该地址是本地配置候选，端到端状态为 `not_run`。
如果控制台拒绝，应根据实际错误调整注册方式；不能自动架设公开回调或隧道。

核查的公开脚本 SHA-256：
`406e3e707d44527a521c82d38fa400a407e44bfb476dfb1481e0ce96588673dc`。
这里只记录证据，不把该约 4.9 MB 脚本加入项目。

## 固定官方 OpenAPI 契约

`resources/whoop-openapi.json` 是从
[WHOOP 官方机器可读 OpenAPI](https://api.prod.whoop.com/developer/doc/openapi.json)
原样下载的公开响应，未格式化或裁剪。

| 元数据 | 值 |
|---|---|
| 获取日期 | 2026-09-05 |
| 字节数 | 73012 |
| SHA-256 | `087501087b4efe5ec28975b89b3b18448dd4eda7b0d524b74fdbb611194293aa` |
| OpenAPI 版本 | `3.0.1` |
| `info.title` | `WHOOP API` |
| `info.version` | 未提供，不能当作已有语义版本 |
| Server | `https://api.prod.whoop.com/developer` |

此文档含 `/v2` 资源，也含 v1 ID 映射及 Trusted Partner 资源，并非全是个人 API。
普通授权 scope 包括 recovery、cycles、workout、sleep、profile 和 body measurement
六种读取范围；`offline` 的来源是 OAuth 文档，不在该 OpenAPI 的 scope 列表中。
可用固定契约验证本轮实际使用路径、参数、单位和响应模型，无需生成所有 Partner 接口。
固定 hash 只证明契约文件未变，不能替代业务和真实 API 兼容性测试。

## 静态加密：复用 SQLCipher

[SQLCipher 官方 Python 说明](https://www.zetetic.net/sqlcipher/sqlcipher-python/)
明确没有开箱即用的官方 Python 方案；商业支持覆盖 SQLCipher 自身及其官方分发库，
不覆盖第三方 Python 包装边界。故不能把社区包装称作官方 Python SDK。

维护者 [coleifer/sqlcipher3](https://github.com/coleifer/sqlcipher3) 的
[PyPI sqlcipher3 0.6.2](https://pypi.org/project/sqlcipher3/0.6.2/)
提供 `cp313-cp313-macosx_11_0_arm64` wheel，适配本轮检测到的 Python 3.13 / Apple Silicon。
应锁定 `sqlcipher3` 包和文件 hash；不要依据较旧 README 的 Linux-only 文案误选
`sqlcipher3-binary`，也无需为了当前 wheel 修改系统或安装 Homebrew。

建议保留现有 SQLite 数据模型，复用 SQLCipher 引擎实现整库加密。安装后应核实
`PRAGMA cipher_version`，用实际读取确认密钥正确，并验证错误密钥拒读、WAL、
备份和恢复。设置 key 本身不会立即验证它，必须在任何业务操作前设置，随后读 schema。
依据 [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)。
这些是验证要求而非本文声称已通过的结果；密钥恢复与凭据存储仍属应用职责。

## 验证记录

| 项目 | 状态 |
|---|---|
| 公开官方 OAuth、导出、注册文档核查 | 已核查，缺口如上 |
| 公开控制台表单规则检查 | 已核查，未验证服务端接受 |
| OpenAPI 下载、JSON 解析与 SHA-256 | 通过 |
| PyPI 当前平台 wheel 可用性 | 已核查；本研究未安装 |
| 创建真实开发 App / 回调被服务端接受 | `not_run` |
| 真实 OAuth 授权、刷新和撤销 | `not_run` |
| 用户实际 WHOOP CSV 字段兼容性 | `not_run` |

## 2026-09-07 首次真实联调补充

上文保留 9 月 5 日公开资源核查的历史状态。用户创建应用并主动初始化、登录后，
已验证当前本地回调、六项读取 scope + offline、Keychain 访问及六资源小窗口入库；
结果仅以技术检查状态记入 [验收记录](../evidence/F1_LIVE_VALIDATION_2026-09-07.json)。
健康数值、原始记录、账户标识和运行断点不写入本文件或可入 Git 的验证材料。

结构诊断发现训练 `score.distance_meter`、`score.altitude_gain_meter`、
`score.altitude_change_meter` 在不可用时可能为 null。
[官方 OpenAPI](https://api.prod.whoop.com/developer/doc/openapi.json)的 WorkoutScore
将这三项列为可选，说明它们只在有相应距离/海拔资料时出现，但未将其数值类型显式标为 nullable。
这是实际响应与已公开契约的兼容差异，不应因此丢弃整次训练或填零。

保留官方快照原样；应用只在验证副本中把这三项 null 视为缺失，原始响应保留 null。
必填项及其他非空类型仍由同一契约校验。新增回归使用已有合成 fixture 手工构造空值，
未复制真实响应到测试文件。真实刷新/撤销、实际 CSV 和完整历史覆盖仍待验证。
