# 0004 — CandidateSkill 是带内部 diff 的独立 Skill 类

Status: Accepted
Date: 2026-07-24
Owners: project owner

## Context

候选修复既需要像普通 skill 一样被完整执行，也需要明确表达它相对父版本修改了什么。
外部临时 patch 无法充分表达 candidate 自身的身份和可审阅性。

## Decision

定义独立 `CandidateSkill` 领域类，可继承 `Skill`。它必须包含：

- 父 skill version；
- 内部 diff；
- 应用 diff 后的完整可执行 skill 内容；
- 归因和生成证据；
- 校验、replay 和 gate 状态。

CandidateSkill 不得原地修改活动 skill。

## Alternatives considered

- 直接修改活动 skill：实现快，但无法安全并行、审阅和回滚。
- Candidate 与 diff 完全分离：模型简单，但 candidate 无法自包含地解释其变化。
- 只保存 diff：体积小，但 replay 前必须依赖父版本重建，长期归档更脆弱。

## Consequences

- Candidate 可以复用 Skill 的加载、校验和执行能力。
- 用户可以直接查看完整内容和 diff。
- 需要定义继承边界，避免 CandidateSkill 混入发布状态等不适用行为。
- 文件增删改、重命名和二进制资源需要明确 diff 表达。

## Revisit when

如果继承导致 Skill 与 CandidateSkill 生命周期耦合过深，改用组合，但保留
CandidateSkill 自带 diff 和完整内容的语义。

