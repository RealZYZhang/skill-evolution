# 0007 — 先采集真实 trajectory，再设计评估与归因

Status: Accepted
Date: 2026-07-24
Owners: project owner

## Context

Pi RPC event、session entry、工具执行、错误和 usage 的实际结构尚未通过本项目的真实
skill 执行观察。此时先设计评估模型容易对 trajectory 结构和可用证据做错误假设。

## Decision

先建立最薄探索性代码，人工运行一个真实 skill，原样保存配置、runtime、Pi RPC
records、Pi session、stderr 和 outcome。

随后人工检查该 trajectory，基于真实数据稳定最小采集 schema。只有完成上述步骤后，
才开始设计评估和归因模型。

探索性采集阶段不实现 evaluator、attribution、CandidateSkill、queue 或自动 replay。

## Alternatives considered

- 先设计评估模型，再反推 trajectory schema：结构更自上而下，但容易建立在错误
  数据假设上。
- 一次性实现完整 Capture workflow：减少临时代码，但会在不了解 Pi 数据形态时
  过早固化 schema。
- 只阅读 Pi 文档和类型定义：成本低，但无法观察真实运行中的事件顺序、重复、
  session 内容和失败行为。

## Consequences

- 会产生一小段明确可丢弃或重构的探索性代码。
- 第一版 trajectory schema 有真实样本依据。
- 评估模型可以引用实际存在的证据。
- 需要选择一个可控 skill、TaskCase 和已配置的 Pi 模型完成真实运行。

## Revisit when

如果第一次样本过于简单，无法覆盖工具调用或失败行为，应增加少量样本，而不是立即
开始评估模型设计。

