# 0020 — 单轨迹分析先做确定性 precheck

Status: Accepted
Date: 2026-08-06
Owners: project owner

## Context

初版 TrajectoryErrorAnalyst prompt 要求模型完整读取 trajectory，并自行检查 JSONL、schema、
run identity、`seq`、边界记录、显式失败状态和 artifact 存在性。这些事实可以由程序
稳定提取。重复让模型检查会增加成本、降低复现性，并容易把非零检查结果或 stderr
误当作真实任务错误。

项目负责人要求明确区分无需 LLM 和必须使用 LLM 的单-trajectory 错误分析，并将完整流程
打包为 Skill。

## Decision

- 增加 `trajectory.precheck.v1` 和无模型 `scripts/trajectory_precheck.py`。它检查 trajectory
  完整性、直接状态、lifecycle、session 与 artifact 文件事实，并提取显式 signals。
- precheck 不执行 trajectory 中记录的代码或命令，不复制原始 tool 参数/result、
  stderr、用户内容或 error message。
- 相同 tool 或 target 的后续 success 只形成 recovery candidate，固定声明
  `proves_recovery=false`。
- LLM prompt 不再完整扫描 trajectory。它只解释 precheck 提出的 signal，判断预期
  控制流、真实恢复、语义完成度、因果关系、责任边界和 Skill 修复适用性。
- 确定性事实使用 precheck JSON pointer 引用；语义结论再引用所需 action、TaskCase、
  artifact 或 validator 证据。
- `skills/analyze-single-trajectory/` 封装两阶段工作流和脚本入口，并携带 package-local
  `skill_contract.json`。Prompt 和新 Skill contract 在负责人审核前保持 proposed。

## Alternatives considered

- 保留全量 LLM 扫描：实现简单，但重复事实提取、成本高且难以稳定回归测试。
- 把所有 signal 直接判成错误：完全确定性，但不能正确处理 `grep` 未匹配等预期控制流，
  也无法证明替代 action 恢复了任务目标。
- 在 precheck 中执行捕获到的命令或代码：可以增加诊断，但会扩大权限与安全边界，并
  改变“只读已保存证据”的含义。

## Consequences

- 无效、不完整、失败和成功含中间 signal 的 trajectory 都能在不调用模型时生成一致报告。
- 模型上下文和 prompt 更小，语义判断与机器事实可以分别测试和审计。
- `completed_with_signals` 不是 `errors_recovered`；只有语义证据能完成该判定。
- 当前仍需批准 prompt/Skill contract，并实现严格 TrajectoryErrorReport parser 与单-run
  AgentRun 入口，才能进行 production LLM 分析。

## Revisit when

出现新的 trajectory schema、并行 action 因果关系，或安全的语言/产物 validator
需要成为 precheck 插件时，发布新的报告版本或独立 validator；不要静默改变 v1 含义。
