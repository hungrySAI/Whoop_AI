# 文档目录

V1 已完成，当前技术版本为 **0.4.2 / schema 3**。返回 [项目首页](../README.md)。
以下文档按用途维护，历史审查与技术验收证据继续保留，便于后续追溯。

| 用途 | 文档 |
|---|---|
| 安装、合成演示与常用 CLI | [项目首页](../README.md) |
| 首次 WHOOP OAuth 与加密存储配置 | [WHOOP_SETUP.md](WHOOP_SETUP.md) |
| 本地看板、同步、周回顾与恢复回看 | [DASHBOARD.md](DASHBOARD.md) |
| 可选 Journal 历史导入 | [JOURNAL.md](JOURNAL.md) |
| V1 归档与安全发布 | [V1_RELEASE.md](V1_RELEASE.md) |
| 当前实现、实测结果与缺口 | [STATUS.md](../STATUS.md) · [ACCEPTANCE.md](ACCEPTANCE.md) |
| 产品范围与未来工作 | [PROJECT_CHARTER.md](../PROJECT_CHARTER.md) · [ROADMAP.md](../ROADMAP.md) |
| 架构、数据语义与决策 | [ARCHITECTURE.md](ARCHITECTURE.md) · [DECISIONS.md](DECISIONS.md) |
| 官方资源与接入核查 | [WHOOP_RESOURCES.md](WHOOP_RESOURCES.md) · [F1_RESOURCE_FINDINGS.md](F1_RESOURCE_FINDINGS.md) |
| 隐私声明草案 | [PRIVACY_DRAFT.md](PRIVACY_DRAFT.md)（部署前须按实际情况审核） |
| 后续开发约定 | [AGENTS.md](../AGENTS.md) |

## 项目目录

| 目录 | 内容 |
|---|---|
| `src/` | Python 应用服务、CLI/MCP、本机 HTTP 与随包前端资产 |
| `scripts/` | 本机看板启动/停止入口 |
| `tests/` | 合成 fixtures、回归测试与隔离演练 |
| `examples/` | 配置与导入映射示例；其中列名和数据不能直接认定为实际导出契约 |
| `docs/` | 当前使用、架构、资源与验收说明 |
| `evidence/` | 可提交的合成结果和不包含健康内容的技术验收记录 |
| `reviews/` | 按日期归档的设计来源、审查和修复前诊断材料 |
| `runtime/` | 本机运行产生的数据与服务状态，不提交到 Git |

## 历史材料

- [2026-09-05 原审查说明](../reviews/2026-09-05/README.original.md)：
  [架构审查](../reviews/2026-09-05/ARCHITECTURE_REVIEW.md)、
  [基础修订规格](../reviews/2026-09-05/FOUNDATION_REVISION_SPEC.md)、
  [原修订任务](../reviews/2026-09-05/CODEX_REVISE_FOUNDATION.md)、
  [F0 状态快照](../reviews/2026-09-05/STATUS.F0.md)。
- [2026-09-07 数据稳健性审查](../reviews/2026-09-07/DATA_ROBUSTNESS_REVIEW.md)：
  记录 0.4.1 修复前的发现及合成复现；当前修复与验证以 STATUS 和 ACCEPTANCE 为准。

历史材料不代表当前功能，也不自动授权新的开发工作；归档中的旧版本测试数量保留其原有时间范围。
