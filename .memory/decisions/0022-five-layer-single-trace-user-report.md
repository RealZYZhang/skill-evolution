# 0022 — 单 trajectory 分析使用五层用户报告

> Purpose: record the accepted user-facing result contract and its read-only
> Trajectory Viewer integration.

Status: Accepted
Date: 2026-08-09
Owners: project owner

## Context

单 trajectory 的确定性 precheck 和语义报告面向程序校验与证据审计，直接展示会把内部字段、
状态和引用细节暴露给不熟悉实现的用户。项目负责人要求用五层结构呈现：结论卡片、发生
经过、问题卡片、证据下钻，以及建议与下一步，并将五层保存为单个 JSON 文件，复用现有
只读 Trajectory Viewer。

首批五次真实语义分析均未通过结果质量检查。用户界面必须继续展示已经确认的基础事实，
同时禁止把被拒绝的模型文字包装成正式结论。

## Decision

- 每个单 trajectory AgentRun 生成一个 `analysis.single_trajectory_view.v1`，固定文件名为
  `user-report.json`。它是面向用户的 projection，不替代 precheck、语义报告或原 trajectory。
- JSON 固定包含五层：`overview`、`narrative`、`incidents`、`evidence` 和
  `recommendation`；另保留简短分析状态与折叠的来源信息。
- `overview` 分开表达 Trajectory 数据、执行流程、异常影响和 Skill 建议，不能把“分析运行
  完成”“执行流程完成”和“任务语义正确”合并成一个成功状态。
- 只有通过严格质量检查的语义报告才能生成 incident、归因、恢复和 Skill 修改建议。
  `invalid_output`、失败、超时或不确定的分析只能使用 precheck 事实，并明确要求重新分析。
- 证据默认显示自然语言摘要；技术位置折叠。Trajectory step 可以从报告跳回 Viewer 原时间线。
- Viewer 继续只读、本机运行。它只读取经过校验的 `user-report.json`，不解析
  `result.invalid.txt`，也不从原始模型文字回退生成结论。
- 同一 run 存在多个 attempt 时，Viewer 选择生成时间最新的有效用户报告；历史 AgentRun
  和报告继续保留，不覆盖失败证据。

## Alternatives considered

- 直接展示语义报告 JSON：审计信息完整，但不回答用户最关心的影响和下一步。
- 在浏览器中临时拼装五层：减少一个文件，但结果无法独立保存、测试和复用。
- 对无效模型输出做宽松提取后展示：看起来信息更多，但会绕过质量门禁并污染后续决策。
- 把五层写回 replay run：定位简单，但会修改已封存的原始执行证据。

## Consequences

- 用户可以先阅读设计和功能结论，需要时再下钻技术证据。
- 无效分析仍有可用页面，但只能表达确定性事实与“尚不能判断”。
- 新的单 trajectory 分析入口必须在每次终态 AgentRun 后生成用户报告；生成失败需要显式返回，
  原 AgentRun 仍保留。
- Viewer 增加 analyses root 和只读 analysis endpoint，但保持原 replay API 与安全边界。

## Revisit when

需要跨 trajectory 聚合、国际化、多种用户角色、可下载审阅包，或五层内容需要支持人工批注时，
发布兼容的新 projection 版本；不要静默改变 v1 字段含义。
