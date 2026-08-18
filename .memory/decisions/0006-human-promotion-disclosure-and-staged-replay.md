# 0006 — 人工批准发布，并分阶段实现自动 replay

Status: Accepted
Date: 2026-07-24
Owners: project owner

## Context

评估模型和自动门禁尚未成熟，不能自动替换活动 skill。用户需要看懂优化的完整因果链。
同时，自动 replay 涉及生成代码执行和隔离，不应阻塞第一阶段人工采样。

## Decision

只有用户明确批准后才能发布 CandidateSkill。审阅必须披露：

1. skill 是什么；
2. trajectory 长什么样；
3. 发现的问题是什么；
4. 提出的修复及 diff 是什么；
5. 为什么认为修复可行或不可行。

第一阶段实现全部人工发起的采样。自动 replay 保留在计划中，在人工采样、离线分析、
候选生成和基础审阅稳定后实现。

## Alternatives considered

- 自动门禁通过后直接发布：速度快，但早期误判风险不可接受。
- 先实现自动 replay 再做人工采样：闭环完整，但增加隔离和调度前置成本。
- 只展示分数和推荐结果：界面简单，但无法审计问题、修复和判断依据。

## Consequences

- 发布速度受人工审阅限制。
- ReviewPackage 必须支持从摘要下钻到 trajectory、Pi 证据、diff 和测试结果。
- 第一阶段可以更快获得真实 trajectory。
- 自动 replay 的隔离、gate 和完整测试展示仍是后续必做工作。

## Revisit when

当评估模型、门禁和隔离经过足够验证后，可以考虑自动进入非生产候选渠道，但
`stable` 发布是否继续要求人工批准需要另行决策。

