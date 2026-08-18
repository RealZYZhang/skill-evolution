# 0017 — Execution Prompt 只定义通用执行边界

Status: Accepted
Date: 2026-07-27
Owners: project owner

## Context

Execution prompt v2 曾要求保留源文档层级、检查 HTML 结构和说明可视化策略。这些
要求只适用于文档可视化 skill。若 framework prompt 额外规定某个 skill 的行为，
replay 测到的将是“skill 加隐藏补充要求”，无法准确判断 skill 本身是否完整。

## Decision

- Execution prompt 只定义通用执行边界：
  - 如何按 `delivery` 取得 TaskCase 输入；
  - 预期产物的相对路径；
  - 当前工作目录的读写边界；
  - 不修改被测 skill 和任务输入；
  - 逐项报告产物、失败和限制。
- Framework 保存完整 TaskCase，但只向执行 agent 注入 `input` 和
  `expected_artifacts`。`schema`、`task_case_id`、`capability_tags` 和
  `budget` 不进入模型 prompt。
- 内容保留、输入格式处理、可视化、HTML 检查和其他特定产物质量要求必须写在被测
  skill 中。
- Framework 不使用 execution prompt 弥补 skill 缺失的执行说明。发现缺失时，
  应将其作为 skill 的潜在优化问题记录。

## Alternatives considered

- 每个 skill 配一份带补充要求的 execution prompt：容易使用，但会掩盖 skill
  本身的缺陷，并降低不同 skill 之间的执行口径一致性。
- 把所有可能的质量要求加入通用模板：范围会持续膨胀，而且大量要求与当前 skill
  无关。

## Consequences

- Replay 更接近被测 skill 自身的真实表现。
- Skill 作者需要在 `SKILL.md` 中完整写明领域要求和验收条件。
- Execution prompt 仍可按文件版本管理，但其正文应保持与具体 skill 无关。

## Revisit when

当 TaskCase 增加新的通用执行字段或安全边界时，可以更新 execution prompt；不得
借此加入某一类产物独有的质量要求。
