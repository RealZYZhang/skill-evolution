# 0026 — 单 trajectory 分析使用经过验证的结构化提交

> Purpose: record the accepted output boundary that separates model prose from
> the machine-validated single-trajectory semantic report.

Status: Accepted
Date: 2026-08-11

## Context

已批准的 v1 和 v2 prompt 都要求模型只返回一个 JSON 对象，但 DeepSeek 仍会在 JSON
外输出说明或 Markdown 围栏。Pi 0.81.1 的 RPC `prompt` 命令没有可传递的
`response_format` 或 `json_schema` 参数；继续依赖 prompt 措辞无法形成可靠边界。

## Decision

- 单 trajectory TrajectoryErrorAnalyst 加载独立的结构化输出扩展。模型必须通过
  `submit_trajectory_error_analysis` 提交最终报告。
- Pi 在工具执行前按严格字段结构验证完整参数；未知字段、缺失字段、错误枚举和错误类型
  不会进入正式提交。
- 提交动作成功后立即终止该 agent turn。运行时按 tool-call ID 配对开始和完成事件，
  并且只接受恰好一次成功提交。
- 模型在提交前产生的普通说明文字不再作为报告输入。正式提交仍须通过原有的
  trajectory identity、signal 分区、跨字段语义和 EvidenceRef 位置验证。
- 当前决定只适用于单 trajectory 错误分析。其他角色只有在出现同类需求并完成独立设计审查后
  才能复用，不能预先做通用化扩张。

## Alternatives considered

- 自动截取 fenced JSON：会把被拒绝的回答事后改写成有效结果，削弱审计边界。
- 放宽为“找到任意可解析对象即可”：无法证明被选中的片段就是模型正式提交。
- 绕过 Pi 直接调用 provider：会失去当前独立 session、trajectory 和受限工具边界。

## Consequences

- JSON 交付不再依赖模型是否遵守文本格式；外围 prose 不会污染正式报告。
- TypeBox 工具结构只负责字段级验证，Python validator 继续负责 signal、因果和证据等
  跨字段规则。两层必须同时通过。
- 首条真实 DeepSeek V4 Pro 复验已成功生成 accepted result 和五层用户报告。
- 结构合格不代表每个语义判断都已完美。批量运行前仍需人工检查首份已接受报告的
  因果、归因、Skill 修改建议和中文呈现质量。
