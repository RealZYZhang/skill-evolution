# 0013 — 使用确定性 Harness 生成分析 Evidence

Status: Accepted
Date: 2026-07-26
Owners: project owner

## Context

重复 replay 的 HTML 产物、执行策略和 token 使用差异明显。只让模型直接阅读原始
trajectory，会重复提取事实、浪费上下文，并可能把不完整观察误写为质量结论。

项目需要先稳定记录中间数据，再让不同 agent 从一致性、能力覆盖和资源效率等角度
解释同一批证据。

## Decision

- 用 `TrajectoryProfiler` 生成版本化 `trajectory.profile.v1`，记录每条 run
  的资源事实、策略序列、失败、重试和跨 run 离散度。
- 用只读 `HTMLArtifactComparator` 生成
  `artifact.comparison.v1`，记录 HTML/Markdown 事实、pairwise delta 和固定
  viewport 截图。
- Harness 不计算总分、不选最佳 artifact，也不做问题归因。
- Profiler 不累加每条 message 的累计 `totalTokens`。
- 报告用 `run_id + seq`、JSON pointer、artifact line 或 selector 引用原证据，
  不复制大段命令和 heredoc。
- 截图只渲染注入 CSP 和 reduced-motion 的临时副本；Chrome 不可用时报告为
  partial，静态比较继续完成。
- 分析输入冻结为脱敏 EvidenceBundle，保留 trajectory `seq`，排除 Pi session、
  credential 和 hidden reasoning。

## Alternatives considered

- 让每个 agent 各自读取全部原 trajectory：灵活，但事实提取口径不一致、成本高，
  也难以复现。
- 为 artifact 计算单一质量分：方便排序，但会隐藏结构、内容、一致性和资源之间的
  权衡。
- 第一版加入像素相似度或视觉模型：能扩展视觉判断，但引入新的非确定性、成本和
  评估器校准问题。

## Consequences

- 相同 replay campaign 可以用相同 Harness schema 重算并比较。
- 模型分析从引用事实开始，而不是重新发明统计口径。
- Harness 只能证明可观察事实；语义质量、视觉偏好和因果归因仍需独立分析或人工
  判断。
- 第一版只对 Markdown 做来源保留提取，其他格式需要后续 adapter 或 replay 证据。
- Screenshot partial 必须与 artifact 质量失败区分。

## Revisit when

当真实分析证明需要浏览器 DOM、像素差异、视觉模型，或当前静态 parser 无法支持
关键结论时，单独增加版本化 Harness 组件；不得在原 schema 中静默改变含义。
