# 0014 — 多角色分析使用独立 Pi 进程和文件 Blackboard

Status: Accepted
Date: 2026-07-26
Owners: project owner

## Context

一致性、能力覆盖和资源效率是不同的分析视角。若在一个 Pi session 中串行扮演多个
角色，角色之间会共享未结构化上下文，失败难以隔离，session 也会无限增长。

项目还要求 agent 能够下钻原证据、在证据不足时请求更多测试、提出 candidate 并
独立判断 replay 效果，但 agent 不能直接改变 workflow 状态。

## Decision

- Python `MultiPiOrchestrator` 编排固定六个角色：三个 specialists、
  Synthesis、CandidateProposer 和 ReplayJudge。
- 每个 AgentRun 使用独立 Pi RPC 子进程、工作目录、session 目录、action-level
  trajectory、prompt/approval 快照和结果文件。
- 三个 specialist 独立消费同一冻结 EvidenceBundle；达到终态后才运行 Synthesis。
- Specialist 失败必须保留；Synthesis 必须披露 missing roles 和分析限制。
- 新一轮分析使用全新 Pi session，只通过结构化文件传递上一轮状态。
- Agent 最终消息只能是单个 JSON 对象；非法 schema 记为失败，不发送临时修复
  prompt。
- Agent 只能提交结构化 result 或 ExperimentRequest；Python 层校验后才更新
  campaign。
- 默认串行执行，`max_parallel_agents=1`；并发 auth/session smoke 通过后才可
  提高到 3。
- 分析禁用 Pi 内置工具，只加载 root-jail 的 read/list/search；
  CandidateProposer 额外获得 candidate workspace 的 read/write/edit，不获得
  bash。
- 六个 production prompt 必须分别版本化和经负责人批准。

## Alternatives considered

- 一个 Pi 进程内使用 `fork/new_session` 模拟角色：进程边界、故障和认证状态仍然
  共享，不能满足独立 AgentRun 审计。
- 开发自治 coordinator extension：可以让 agent 自行调度，但扩大可信代码和权限
  边界，并弱化 framework 状态机。
- 所有角色共享同一长 session：上下文复用方便，但角色污染、重试覆盖和无限增长
  风险更高。

## Consequences

- 每个角色的成本、失败、trajectory 和 session 可以单独查看和重试。
- File blackboard 增加了 EvidenceBundle 副本和磁盘占用，但保持边界清晰。
- Synthesis 可以在部分 specialist 失败时继续工作，但必须降低可分析范围。
- 并行接口存在，不代表当前真实 provider/auth 已验证并发安全。
- Prompt 未批准时，preflight 必须在 campaign 状态迁移和模型调用前停止。

## Revisit when

当三个 specialist 的真实并发 smoke 稳定通过，或 AgentRun 数量使独立进程启动
成本成为主要瓶颈时，再评估并发默认值或进程池；不得牺牲独立 session 和失败记录。
