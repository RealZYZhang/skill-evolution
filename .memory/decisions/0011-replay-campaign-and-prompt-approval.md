# 0011 — Replay Campaign 固定任务并强制 Prompt 人工审批

Status: Accepted
Date: 2026-07-25
Owners: project owner

## Context

评估过程需要先对同一个 skill 重复运行 N 次，得到同一批次内可检查的 trajectory
和 Pi session。仅提供 skill 和 N 不能定义可比较任务，还需要固定任务输入、
执行 prompt 和 runtime 参数。

项目负责人要求所有 framework prompt 在执行前由其亲自审核。

## Decision

- 一个 replay campaign 固定 skill、source、prompt 版本、N、timeout 和 runtime
  参数。
- MVP 串行执行 N 次，复用现有 action-level trajectory capture。
- Campaign 使用一个目录，`runs/` 下每次执行各有独立子目录、trajectory、
  Pi session 和 artifacts。
- Campaign 根目录使用原子更新的 `replay.json` 记录固定输入、每次 run 状态和
  汇总。
- 单次 execution 失败不会停止后续 run；失败 trajectory 与 session 必须保留。
- 只有未能生成 N 条 trajectory 才算 campaign 自身失败。N 条都生成但其中存在
  execution failure 时，状态为 `completed_with_run_failures`。
- 所有生产 prompt 都是版本化文件，不接受临时 CLI prompt 字符串。
- Prompt 相邻的 approval sidecar 必须记录 `approved`、prompt ID、version、
  批准人、批准时间和正文摘要。
- Prompt 正文在批准后变化会使审批失效，必须重新提交项目负责人审核。
- Skill execution prompt 是结构化 template，必须且只能包含一个
  `{{SKILL_CONTENT}}`。
- Campaign 启动前读取当前 `SKILL.md` 全文并替换占位符，只渲染一次，N 次 run
  使用完全相同的最终 prompt。
- Campaign 保存 template、approval、rendered prompt 和被测 skill 快照。

Prompt 摘要用于把人工审批绑定到具体正文，不属于 trajectory hash 或 evaluator
输入完整性机制。

## Consequences

- 同一 campaign 内的 run 具有一致的任务和 prompt 边界。
- 用户批准的是 prompt 结构；实际注入的 `SKILL.md` 和最终 rendered prompt 都会
  随 campaign 保存并可直接检查。
- 用户可以从一个目录检查 N 条 trajectory、N 个 session 和全部失败。
- Replay 运行时间随 N 线性增长；MVP 暂不并发。
- Prompt 未批准时，系统会在创建 campaign 和调用模型前失败。
- 当前模块只提供重复采样，不包含结果评分、归因、candidate replay 或 gate。

## Revisit when

需要多任务 replay set、并发调度、随机种子策略、candidate/baseline 配对或自动
gate 时。
