# 0010 — Trajectory 只保存完整 Action

Status: Accepted
Date: 2026-07-25
Owners: project owner
Supersedes: 0009 and the raw-event storage requirements in 0005

## Context

决策 0009 通过 message delta、message/session hash 核对和 journal hash chain，
把一次真实运行的事件文件从约 1.94 GB 降到约 31.9 MB。该格式保留了精确流式
event 边界，但后续 evaluator 实际关心的是模型完成了什么消息、调用了什么工具、
工具成功或失败，以及整次执行的 outcome。

逐 token delta 和完整性 hash 增加了体积与实现复杂度，却不直接帮助判断 skill
执行质量。

## Decision

`trajectory.jsonl` 改为 action-level 线性记录，schema 为
`trajectory.actions.v1`：

- 保留连续 `seq`、观察时间和 elapsed time，不保存 hash chain。
- `message_start` 和 `message_update` 不落盘；只在 `message_end` 后写一条包含
  完整 message 的 `message_action`。
- `tool_execution_start` 暂存在内存；在 `tool_execution_end` 后把完整参数、
  结果、错误、状态和耗时合并为一条 `tool_action`。
- `tool_execution_update` 不落盘。
- RPC command 和 response 不落盘；协议解析错误仍作为 failure evidence 保存。
- 未结束的 message 不保存 partial，只写一条不含内容的
  `action_interrupted`。
- 未结束的 tool 写成 `status: interrupted` 的完整 `tool_action`，保留已知参数
  和中断原因，不保存 progress update。
- Framework manifest、runtime observation、process failure、stderr、artifact 和
  outcome 继续与 action 共用同一个有序 journal。
- Pi session 继续作为低成本的 runtime-native sidecar 保存，但只用于调试和重新
  提取；evaluator 默认不读取它。
- 不再做 message/session hash 核对，不生成 `message_recovery`。
- Skill inventory、session 和 artifact 只记录路径、存在状态、数量与 bytes，
  不记录 SHA。

## Consequences

- Trajectory 直接呈现 evaluator 所需的完整 action，体积和记录数显著下降。
- Pi session 与 action journal 的完整 message/tool 内容会有一定重叠；该重叠
  是“可移植评估视图”和“Pi 原生调试备份”的职责重叠。
- Pi session 缺失或缺少 message 不再把一次已经成功的 skill execution 改判为
  失败，但会在 session metadata 中显现。
- 运行在 `message_end` 前中断时，半截模型输出无法恢复；journal 只说明该 action
  被中断。
- 不再能逐 token 重放输出过程，也不能用内置 hash chain 检查 journal 是否被
  修改。这些能力不属于当前 MVP 的评估需求。
- `seq` 仍是 framework、完整 action、failure 和 outcome 的唯一线性顺序。

## Revisit when

Evaluator 证明需要流式时序、需要对 trajectory 做敌对环境完整性验证，或引入
并行工具调用导致“完成顺序”不足以表达因果关系时。
