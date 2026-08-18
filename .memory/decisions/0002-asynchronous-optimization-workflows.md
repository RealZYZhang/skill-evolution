# 0002 — 采集、评估、候选生成和回放使用独立异步 workflow

Status: Accepted
Date: 2026-07-24
Owners: project owner

## Context

Skill 优化需要从大量执行轨迹中寻找稳定模式并完成离线归因，而不是在每次执行完成后
立即完成 capture、evaluate 和 propose。同步闭环会把采样、分析和优化强耦合，也
不利于批次重算、故障恢复和对比多个候选。

项目负责人还要求：

- 执行失败必须进入 trajectory；
- 归因优化后自动 replay；
- 所有 candidate 和测试结果都交给用户；
- 自动门禁判定无效的 candidate 也不能被隐藏或丢弃。

## Decision

将流程拆分为五个独立异步 workflow：

1. Trajectory Capture；
2. Offline Evaluation and Attribution；
3. Candidate Proposal；
4. Automatic Replay and Gate Classification；
5. User Review and Promotion。

workflow 之间只通过持久化对象和版本化事件连接。MVP 的文件存储与简单 queue
实现见决策 0003。

每次执行尝试在启动 runtime 前创建 trajectory。成功、失败、中止和启动失败都
封存为 trajectory outcome。

Candidate 一经生成必须永久保存。Replay 自动创建；自动门禁只写入分类和原因。
用户审阅包必须包含全部 candidate、diff、replay attempt、测试结果和失败信息。

## Alternatives considered

- 单次执行后同步完成评估、归因和候选生成：无法有效利用大量 trajectory，阶段
  耦合且失败恢复复杂。
- 每产生一条 trajectory 就立即异步评估：虽然解耦进程，但仍以单条轨迹为中心，
  不满足离线批次归因需求。
- 自动门禁直接丢弃无效候选：减少界面噪声，但破坏审计性，也让用户无法判断门禁
  是否合理。

## Consequences

- 系统需要保存 workflow attempt、失败原因和稳定对象 ID。
- 各阶段最终一致，而不是一次命令内立即完成。
- 同一 trajectory batch 可以使用新版本评估器重新分析，而不重新执行采样。
- 失败 trajectory 成为归因证据，而不是被视为缺失数据。
- 用户界面和报告必须支持展示无效、失败和未完成候选。
- 评估模型可以独立演进，但必须保持输入输出版本化。

## Revisit when

如果后续决定不使用持久化 job 模式，或新方案无法保证阶段独立、失败恢复和审计，
需要重新评估。无论使用何种基础设施，都保持 workflow 边界和领域事件语义不变。
