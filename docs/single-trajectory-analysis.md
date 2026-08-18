# 单轨迹错误分析

> Purpose: define the deterministic/LLM boundary, report flow, and runnable
> entry points for analyzing one action-level trajectory.

## 设计结论

单轨迹分析采用两阶段流程：

```text
trajectory.jsonl
  → deterministic precheck（不调用模型）
  → trajectory.precheck.v1
  → semantic interpretation（仅在需要时调用模型）
  → analysis.trajectory_error_report.v1
```

原则是：能由格式、枚举、顺序、显式状态或文件系统事实稳定回答的问题，不交给 LLM；
只有依赖任务意图、行为效果或因果推断的问题才进入 prompt。这样同一条 trajectory 的事实
提取可复现、可测试，也避免模型把非零检查结果或 stderr 自动误判为用户可见错误。

## 职责划分

| 问题 | 无需 LLM | 需要 LLM |
| --- | --- | --- |
| trajectory 完整性 | UTF-8/JSONL、envelope、schema、run_id、`seq`、时间顺序、started/finished/sealed、seal count、outcome/seal status | 无；invalid/incomplete 结论直接传播 |
| 显式执行信号 | failed/interrupted tool、`action_interrupted`、RPC protocol error、stderr 是否存在、process exit、outcome、failure stage、observer error | 该 signal 是真实错误、预期控制流、症状还是无关观察 |
| Skill 与生命周期 | Skill loaded 字段、agent start/end/settled 数量和直接矛盾 | 为什么未加载/未终止，以及责任属于 Skill、runtime、provider 还是 framework |
| session | missing/partial/invalid lines | sidecar 缺陷是否限制当前因果判断；它不能单独推翻已证实的成功 |
| artifact | 注册路径安全、存在性、bytes、空文件、记录与当前文件系统是否一致 | 内容是否正确、完整并满足 TaskCase；需要 artifact 或 validator 证据 |
| recovery | 找到后来相同 tool 或相同 target 的 success candidate | 后续行为是否修复了同一失败影响且最终目标仍完成 |
| 因果与修复 | 不在脚本中推断 | root cause、contributor、symptom、归属边界和 Skill 修复适用性 |

脚本不会执行 trajectory 中记录的命令或代码，也不会把原始 tool 参数、result、stderr、
用户内容或 error message 复制进 precheck 报告。

## 运行确定性 precheck

```bash
python3 scripts/trajectory_precheck.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-id <execution-id>
```

该命令在对应 Execution 下创建一个 deterministic Analysis，并打印其
`result.json` 路径。历史路径兼容入口仍可显式使用：

```bash
python3 scripts/trajectory_precheck.py \
  .skill-evolution/trajectories/<run-id>/trajectory.jsonl \
  --output .skill-evolution/trajectories/<run-id>/trajectory-precheck.json
```

也可以通过 Skill 自带入口执行同一实现：

```bash
python3 skills/analyze-single-trajectory/scripts/precheck_trajectory.py \
  .skill-evolution/trajectories/<run-id>/trajectory.jsonl \
  --output .skill-evolution/trajectories/<run-id>/trajectory-precheck.json
```

退出码含义：

- `0`：trajectory integrity 为 `valid`；这不表示没有中途错误；
- `1`：checker 正常完成，但 integrity 为 `invalid` 或 `incomplete`；
- `2`：CLI 本身无法完成。

`deterministic_status` 包含：

- `completed_clean`
- `completed_with_signals`
- `failed`
- `incomplete`
- `invalid`
- `indeterminate`

其中 `candidate_recoveries[].proves_recovery` 固定为 false。它只负责给 LLM 一个需要
验证的候选关系，不产生恢复结论。

## LLM 分析边界

当前生产语义分析使用
`prompts/analysis/trajectory-error-analysis-v2.md`。模型必须先读
`trajectory.precheck.v1`，只能按 signal 引用的 `run_id + seq` 下钻原记录，不能重新完整
扫描 trajectory。确定性事实使用 `report_path + json_pointer` 引用；语义与因果结论
还需要 action、TaskCase、artifact 或 validator 证据。

`trajectory-error-analysis-v1.md` 和 `analyze-single-trajectory` contract 已于 2026-08-07
由项目负责人批准，但 v1 在五次真实运行中均发生输出契约漂移，禁止原样重试。v2 明确了
JSON-only 交付、四种合法 EvidenceRef、精确字段和禁止自指因果关系，并于 2026-08-11
获批。生产入口默认指向 v2，任何内容变化都会使批准失效。

v2 的首条 DeepSeek V4 Pro 验证表明：候选 JSON 本身通过了严格 schema、全部 EvidenceRef
和因果关系检查，但文本回答仍包含额外说明和 Markdown fence。框架按设计拒绝整个输出，
没有截取候选 JSON。随后引入独立的结构化提交动作：模型提交时由 Pi 检查完整 JSON 字段，
运行时只接受恰好一次成功提交，再执行原有语义和证据检查。

同一条脱敏 trajectory 的复验已经通过两层检查并生成首份 accepted 报告。这个边界解决的是
“哪一份数据是模型正式提交的报告”以及“字段是否合法”；它不替代对因果、归因、Skill
修改建议和用户呈现质量的人工抽查。

负责人审阅首份报告后，已授权对其余十条脱敏证据运行同一 v2 流程。十条首次运行中
九条通过；一条因同时声明“不适用 Skill 修改”并给出修改目标而被一致性门禁拒绝，保留
失败记录后重新分析并通过。因此当前十一条 trajectory 都有正式接纳的语义报告。

## 运行语义阶段

```bash
python3 scripts/trajectory_error_analysis.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-id <execution-id> \
  --precheck <path-printed-by-precheck>
```

当前入口仍要求调用者传入 precheck 路径；它只核对 run identity，尚未证明该文件是
同一 Execution 下已接受的 deterministic Analysis。层级改造的下一步是改为通过
Analysis ID 自动解析对象内结果，不再接受机器绝对路径。历史 Skill wrapper 继续支持
直接的 Trajectory/Contract 路径，用于冻结旧数据而非正常新流程。

每次命令只创建一个新 Pi process、session、AgentRun 和 EvidenceBundle。冻结
证据可包含脱敏 trajectory、precheck、analyzer contract、subject contract、task context
和输入/输出 artifact；不包含 credential、完整环境映射、hidden reasoning 或 Pi
session。

模型必须通过 `submit_trajectory_error_analysis` 提交一个完整的
`analysis.trajectory_error_report.v1`。Pi 在提交动作执行前检查字段、类型、枚举、必填项和
未知字段；运行时按 tool-call ID 配对开始与完成事件，只接受恰好一次成功提交。框架随后
检查 run/precheck identity、signal 完整分区、incident、causal link、
assessment/disposition 和 Skill-fix 一致性，再确认每个 `evidence.ref.v1`
能在当次 EvidenceBundle 中下钻。普通文本不会被解析为报告，框架也不会自动去掉
Markdown fence、截取看似 JSON 的片段或追加临时修复 prompt。

无论语义结果是否通过质量检查，入口都会在当前 AgentRun 下另存一个
`user-report.json`。它使用 `analysis.single_trajectory_view.v1`，把用户需要的五层信息放在
同一个 JSON 文件中：结论卡片、关键经过、经过验证的问题、可下钻证据，以及建议与下一步。
它是面向用户的只读 projection，不替代 precheck、语义报告或原 trajectory。

当语义结果有效时，五层报告只投影已经通过严格检查的结论；当语义结果无效、失败、超时
或状态不确定时，报告只使用 precheck 事实，禁止生成 incident、归因、恢复结论或 Skill
修改建议。现有报告也可以通过以下命令补建，每个 AgentRun 只允许生成一次：

```bash
python3 scripts/trajectory_user_report.py \
  .skill-evolution/analyses/agent-runs/<agent-run-id>
```

已批准的 v2 不强制自然语言为中文。项目使用独立的人工审阅中文 projection，避免为了
翻译而改写正式结果。中文输入必须覆盖原报告的摘要和每个问题，可选择替换 Skill 建议
说明；发布器验证 run identity 和问题集合，并记录原报告内容摘要：

```bash
python3 scripts/localize_trajectory_user_report.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-id <execution-id> \
  --analysis-id <accepted-analysis-id> \
  --localization <reviewed-zh-CN-input.json>
```

原始 `user-report.json` 与正式语义结果保持不变。只有来源内容摘要仍匹配时，Viewer 才
使用 `user-report.zh-CN.json`；来源变化、问题缺失、英文-only 文本或重复发布都会失败
关闭。当前十一份正式报告均已通过该流程并在 dashboard 以中文呈现。

## 已有 doc-to-HTML trajectory 验证

2026-08-07 使用 Skill 自带的 precheck 入口检查了 replay campaign
`20260725T154836Z-9aacc0cb` 的 5 个运行。2026-08-11 又为其余 5 条失败执行和 1 条
standalone 成功执行补齐了同一检查。它们是命名迁移前的冻结证据，因此 reader
从历史 `trajectory.jsonl` 和 `trajectory.actions.v1` 读取，并在报告中标记
`source_format=legacy`；没有改写原文件。

| run | integrity | deterministic status | signals | recovery candidates |
| --- | --- | --- | ---: | ---: |
| `20260725T154836Z-a58d6715` | valid | completed_with_signals | 4 | 2 |
| `20260725T155939Z-885eacfe` | valid | completed_with_signals | 4 | 4 |
| `20260725T160532Z-62d5c057` | valid | completed_with_signals | 4 | 4 |
| `20260725T161117Z-551972b5` | valid | completed_with_signals | 2 | 2 |
| `20260725T161732Z-9b0938dc` | valid | completed_with_signals | 2 | 2 |

共提取 16 个显式 non-success signal 和 14 个 recovery candidate。所有 candidate
仍为 `proves_recovery=false`。报告保存在
`.skill-evolution/analyses/trajectory-prechecks/doc-to-html-20260725/`。

当前 11 条 trajectory 的 integrity 全部为 valid：6 条 outcome 为 succeeded，5 条为 failed。
新增 standalone 成功 trajectory 有 3 个 signal 和 3 个 recovery candidate；5 条失败 trajectory
各有 6 个 signal、没有 recovery candidate。上述结果只证明运行事实，不构成错误归因、
恢复成立或 Skill 修改建议。

在负责人批准将上述脱敏证据发送给项目配置的 DeepSeek provider 后，同一天
对五条 trajectory 各运行了一次独立语义分析：

| run | AgentRun | runtime status | 严格失败原因 |
| --- | --- | --- | --- |
| `20260725T154836Z-a58d6715` | `agent-run-20260807T065630Z-b9e28034` | invalid_output | JSON 前有说明文字；候选对象的 evidence 不是 EvidenceRef，且含自引用 causal link |
| `20260725T155939Z-885eacfe` | `agent-run-20260807T065824Z-3468f9a6` | invalid_output | JSON 前有说明文字；候选对象使用 `ref/value` evidence |
| `20260725T160532Z-62d5c057` | `agent-run-20260807T070005Z-d182af9e` | invalid_output | JSON 前有说明文字；候选对象使用 `ref/note` evidence |
| `20260725T161117Z-551972b5` | `agent-run-20260807T070131Z-bda60dfb` | invalid_output | JSON 前有说明文字并使用 fence；候选对象使用 `ref/note` evidence |
| `20260725T161732Z-9b0938dc` | `agent-run-20260807T070256Z-125cf86f` | invalid_output | JSON 前有说明文字；候选对象使用 `ref/note` evidence |

五次 Pi session 都正常 settled，因此这不是 provider 中断或超时。第一层
JSON parser 全部在第一个字符失败；仅为诊断而从原文中找出的候选 JSON
又全部违反 EvidenceRef schema。这些候选内容没有写入 `result.json`，其中的
恢复、归因或 Skill 修复判断都不是被接纳的分析结论。

五个 AgentRun 现在都保存了五层 `user-report.json`。它们一致显示：Trajectory 数据完整、
执行流程已结束、异常影响尚未判断、暂不建议修改 Skill，并要求先修复分析报告后重跑。
这些页面没有使用五份被拒绝输出中的语义判断。

2026-08-11 又使用已批准的 v2 对 `20260725T161732Z-9b0938dc` 验证结构化交付。
一次接收事件配对缺陷使成功工具调用被误记为 0 份提交，该 attempt 保留为
`invalid_output`；修正并补充模拟测试后，新 attempt
`agent-run-20260811T155023Z-aad81c83` 通过全部现有门禁，正式状态为 `succeeded`，
其 Analysis 状态为 `accepted`。

2026-08-12 完成其余十条结构化 v2 分析。九条第一次通过；
`20260725T154720Z-ea40f239` 的第一次结果因 Skill 修改结论自相矛盾被拒绝，第二次通过。
最终十一条 trajectory 都有 accepted 语义报告和中文五层呈现。按执行结果看，五条失败执行均
在模型工作前因运行环境缺少可用 API key 而终止，不应修改 Skill；六条成功执行均最终
交付 HTML，其中多条在大型输出生成时触及模型输出上限并恢复，另有若干 grep/diff 验证
非零退出属于预期行为或误报。一条报告建议把稳定的大型输出生成策略作为 Skill 改进候选，
但不会自动修改 Skill。

## Skill package

`skills/analyze-single-trajectory/` 封装了完整编排、确定性和语义脚本入口以及 UI
metadata。其 `skill_contract.json` 已批准，可通过 production model-work
approval gate。EvaluationSuite `analyze-single-trajectory-v1` 目前仍只是引用；独立
suite object 尚未建立（通用 resolver 已实现），因此 `dynamic_test_ready=true` 不能被解释为
该 suite 已经执行。
