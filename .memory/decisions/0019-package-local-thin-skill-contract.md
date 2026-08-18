# 0019 — 使用 package-local thin Skill Contract

Status: Accepted
Date: 2026-08-06
Owners: project owner

## Context

旧 `skill.capability_contract.v1` 把文档格式、能力声明和自然语言 evidence 要求复制到
独立 contract。该拆分会随 Skill 类型增加而膨胀，也无法独立证明 Skill 行为没有发生
goal drift。经过 v2 草案评审，项目负责人确认 Contract 不应成为 `SKILL.md` 的语义
提取或另一份领域说明。

项目同时需要一个所有 Skill 都能采用的稳定文件名，并希望将来在 evaluation 下增加
metrics 等字段，但当前不提前定义这些字段。

## Decision

- 每个可执行 Skill package 必须在 `SKILL.md` 同级提供
  `skill_contract.json`。文件名不携带版本；版本由文件内 `schema` 和 `version`
  字段表达。
- 当前 schema 是 `skill.contract.v2`。它只绑定 identity/approval、最大 runtime
  边界和独立 EvaluationSuite 引用，不包含 `semantics`、capability taxonomy、
  inputs/outputs、scenario 或 validator 定义。
- `runtime` 使用通用 registry identifier，区分 required/allowed tools，并明确
  permissions、network、sandbox credentials、dependencies 和 assets。
- `evaluation` 当前只允许 `suite_refs`。未来增加 `metrics` 等字段时必须发布新的
  schema 版本并更新 parser；当前版本继续 `additionalProperties: false`，不能为了
  扩展性静默接受未知字段。
- 文档可视化 Skill 的 package-local contract 已由项目负责人批准。trajectory、
  replay、analysis 和 candidate comparison 在执行前读取获批 contract；candidate
  comparison 拒绝修改过 `skill_contract.json` 的候选。
- Analysis EvidenceBundle 中统一使用 `skill_contract.json`；CLI 和 manifest 不再用
  `capability-contract.json` 作为当前名称。
- `skill.capability_contract.v1` 和集中存放的文档可视化 v1 文件只作为历史数据继续
  读取，不再作为当前 source of truth。

## Alternatives considered

- 保留自然语言 `semantics`：容易阅读，但与 Skill 同步变化，不能形成独立门禁。
- 为所有能力、输入和错误建立统一 taxonomy：机器结构丰富，但无法稳定覆盖数千个
  不同领域的 Skill。
- 允许任意 evaluation 扩展字段：无需 schema 升级，但拼写错误和未审核行为会被
  静默接受。
- 继续将 contract 集中放在 `contracts/skills/`：历史版本清楚，但 Skill package
  本身不完整，复制或发布时容易遗漏当前 contract。

## Consequences

- Skill package 可以独立携带当前运行与评测绑定，文件发现规则稳定。
- Contract checker 不再声称通过 contract 验证领域能力；具体行为仍由 TaskCase、
  EvaluationSuite 和 Harness 证明。
- 当前 `suite_refs` 会被严格保存和传递，但 EvaluationSuite object、reference
  resolver 和 metrics schema 仍需单独实现，不能把 suite ID 当作已经执行的测试。
- 历史 v1 campaign 不需要迁移或重写。

## Revisit when

首个独立 EvaluationSuite contract 或第一组 metrics 被接受时，发布新的版本化 schema
并明确向后兼容；不要改变 `skill.contract.v2` 的既有含义。
