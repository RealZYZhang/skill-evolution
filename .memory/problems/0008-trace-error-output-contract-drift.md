# 0008 — 单 trajectory 语义分析输出契约漂移

> Purpose: record the reproducible v1 model-output failure and the conditions
> required before another production semantic-analysis attempt.

Status: Resolved
First observed: 2026-08-07

## Symptom

五个独立 TrajectoryErrorAnalyst AgentRun 都与 DeepSeek 正常通信并 settled，但都以
`invalid_output` 结束。没有任何运行生成通过验证的 `result.json`。

## Reproduction

1. 使用已批准的 `trajectory-error-analysis-v1.md` 和
   `skills/analyze-single-trajectory/skill_contract.json`。
2. 对 replay campaign `20260725T154836Z-9aacc0cb` 的五条冻结 trajectory 分别执行
   `skills/analyze-single-trajectory/scripts/analyze_trajectory.py`。
3. 查看 `.skill-evolution/analyses/agent-runs/agent-run-20260807T*/manifest.json`
   和同目录的 `result.invalid.txt`。

五个 manifest 的 `status` 都是 `invalid_output`，第一层 parse failure 均为
`JSONDecodeError: Expecting value: line 1 column 1`。

## Diagnosis

- 每份最终消息都在 JSON 前输出了自然语言说明；四份还使用了
  Markdown JSON fence。因此它们都不是 prompt 要求的单一 JSON 对象。
- 仅为诊断而从原文中识别候选 JSON 后，五份候选对象仍全部失败：
  incident evidence 使用了自创的 `ref/note` 或 `ref/value` 形状，而不是
  `evidence.ref.v1`。
- 第一个候选对象还包含一条 incident 指向自身的 causal link。
- v1 prompt 虽列出了完整顶层形状，但 evidence 只用空数组表示，没有
  提供可复制的三种正规 EvidenceRef 实例。五次一致漂移说明这是
  prompt/output interface 问题，不是单个随机样本。

## Workaround

保留每次原始 `result.invalid.txt`、manifest、trajectory 和 Pi session，但不截取、
重写或接纳其候选 JSON，也不将其中的语义判断当作正式结论。在 prompt
内容和运行条件没有实质变化前，不原样重试 v1。

五个 AgentRun 已各自生成五层 `user-report.json`。用户可以在 Trajectory Viewer 中看到
已经确认的基础事实和重跑建议；问题卡片、归因、恢复与 Skill 修改结论保持为空。

## Resolution

`trajectory-error-analysis-v2.md` 已批准，包含 JSON-only 交付要求、完整的
trajectory/report/artifact `evidence.ref.v1` 示例、精确字段、signal 分区检查和禁止自指
causal link。fixture tests 已覆盖全部 EvidenceRef 位置和六种整条 trajectory 结论。

2026-08-11 验证更新：负责人批准 v2 后，对
`20260725T161732Z-9b0938dc` 运行了一条 DeepSeek V4 Pro attempt。模型再次在 JSON 前
输出自然语言并使用 Markdown fence，所以正式状态仍为 `invalid_output`。仅用于诊断而
提取的候选 JSON 已通过完整 report schema、EvidenceRef 位置和因果关系验证。这证明 v2
修复了内部 evidence/causal contract，但仅靠 prompt 没有修复外层 delivery framing。

Pi 0.81.1 RPC 没有原生 `response_format/json_schema` 参数。项目采用其官方
terminating structured-output tool 模式，新建 `submit_trajectory_error_analysis`：Pi 在提交时
验证完整 JSON 字段，runtime 按 tool-call ID 配对开始和结束，只接受恰好一次成功提交，
然后继续执行原有 report、signal、EvidenceRef 和因果结构检查。普通说明文字不再作为报告。

首次接入后的真实 attempt 已成功调用提交工具，但 runtime 错误地从不含参数的
`tool_execution_end` 读取 JSON，因此误记为 0 份提交并保留为 `invalid_output`。修复为从
`tool_execution_start` 保存参数、在成功 end 时完成配对后，同一条脱敏 trajectory 的
`agent-run-20260811T155023Z-aad81c83` 通过全部现有门禁并生成 `result.json` 与五层
`user-report.json`。219 项自动测试通过。

输出契约漂移问题到此解决。结构合格不等于所有语义判断完美；首份 accepted 报告仍需在
批量运行前人工检查因果、归因、Skill 修改建议和中文呈现质量。
