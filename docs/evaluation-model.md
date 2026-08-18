# Skill 优化评估模型

> Purpose: document accepted evaluation principles while preserving clearly
> marked pre-Decision-0027 workflow material for historical reference.

状态：事实/解释/人工决定分层原则仍有效；AnalysisCampaign、三视角 Synthesis、补证和
Candidate 自动后继是 pre-0027 历史设计，当前多 Trajectory research 不执行这些步骤。  
更新日期：2026-08-14

## 1. 评估对象

本框架不把“某次 skill 最终能否产出文件”当作唯一目标。评估对象是一个冻结的
Replay Campaign 及其完整证据：

- 每条 run 的 TaskCase、runtime、trajectory、outcome 和 artifacts；
- `TrajectoryProfiler` 提取的执行策略与资源事实；
- `HTMLArtifactComparator` 提取的结构、内容和设计差异；
- package-local、经项目负责人审核的 `skill_contract.json`；
- 必要时，由人工批准后补充的新 evidence。

pre-0027 的 `AnalysisCampaign` 曾绑定一个冻结 EvidenceBundle 和固定 Harness schema
版本。当前流程改为内容寻址的 raw-Trajectory corpus、Harness batch、双盲测周期和四份独立
attempt；新数据同样不会静默混入既有批次。

## 2. 事实、解释和决定分层

| 层级 | 生产者 | 可以做什么 | 不可以做什么 |
|---|---|---|---|
| 执行事实 | Capture / Replay | 保存完整 action、失败、outcome、artifact | 评分或归因 |
| Harness 事实 | Profiler / Comparator | 提取资源、结构、内容与差异 | 选“最佳”、给总分 |
| 分析解释 | 四个 Specialist；未来可另行批准 Synthesis | 提出 finding、反证、置信度和限制 | 修改 skill、批准实验 |
| 效果解释 | ReplayJudge + gate 规则 | 解释逐维变化并分类 | 删除 candidate、决定发布 |
| 发布决定 | 项目负责人 | 批准或拒绝发布 | 由模型代替人工决定 |

这样即使模型分析失败，确定性事实仍然可用；即使自动 gate 判断 candidate 无效，
candidate、diff、全部 attempts 和失败原因仍然可见。

## 3. 当前六类研究内容与历史三个视角

当前多 Trajectory 研究的生产者是：结果与可靠性由共同确定性基线生成；BehaviorPattern
Specialist 分析重复问题和恢复/成功模式；ConditionsCoverage、OutcomeConsistency 和
ResourceEfficiency Specialist 分别分析发生条件与覆盖、一致性和资源效率。四个角色
写入不聚合结果板，没有 Synthesis。以下三个视角保留其评估原则，但不再构成完整的
当前编排定义。

### 一致性

目标是回答：同一 Skill、TaskCase 和 runtime 下，产物为什么差异明显，以及差异
从哪个可观察的执行分叉开始。

分析顺序：

1. 从 Comparator 的 pairwise delta 找到结构、内容或 design-token 差异；
2. 下钻对应 artifact 的行号或 selector；
3. 使用 `run_id + seq` 回到 trajectory；
4. 寻找第一次不同的读取、生成、写入、重试或返工策略；
5. 主动检查反例，避免把相关性写成因果。

这一维不判断哪种视觉风格“更美”，只判断输出是否稳定、差异是否可解释，以及
skill 是否需要把重复决策固定下来。

### 能力覆盖

目标是回答：Replay 是否遍历了 skill 对外声明的能力，而不是用一个 Markdown
成功样本代表全部格式。

覆盖判断必须先通过 `skill.validation_report.v1` 的确定性初始检查，再基于 Contract
引用的独立 EvaluationSuite。Contract 本身不定义能力语义。EvaluationSuite 实现后，
覆盖至少区分：

- `.md/.txt/.docx/.pdf/inline_text`；
- `file` 与 `inline_text` delivery；
- 基础样本与格式特有复杂样本；
- 执行成功、产物存在和能力证据充分。

当前 EvaluationSuite 严格契约、resolver、TaskCase 和条件映射门禁已经实现。文档可视化
package-local contract 已批准并绑定 suite 引用，但具体 v2 Suite 仍为 `proposed`，历史
Trajectory 也缺少完整 TaskCase/条件映射。因此 ConditionsCoverage Specialist 仍不能运行或
自行定义能力范围；零样本条件只能记为 coverage gap，不能当作性能证据。完整边界见
[Skill Contract](skill-contract.md)。

### 资源效率

目标是回答：skill 是否稳定、高效地完成任务，而不只是最终成功。

Profiler 分别记录：

- 模型调用次数；
- input、output、cache read、cache write token；
- provider 报告费用；
- run 和工具耗时；
- 工具动作、失败、重试、重复读取和返工；
- 分段写入、临时 generator、重新生成、分片与合并等策略。

效率比较不把每条 message 的累计 `totalTokens` 相加。分析需要同时区分：

- 减少 token；
- 降低 run 间方差；
- 减少失败动作与返工；
- 把重复模型决策变成 skill 内的确定性流程。

## 4. 证据要求

每个 finding 至少有一个 `evidence.ref.v1`。有效定位包括：

- trajectory：`run_id + seq`；
- Harness 报告：`report_path + json_pointer`；
- artifact：`artifact_path + line/selector`。

Finding 同时保存：

- 可复述的 claim；
- `0..1` confidence；
- supporting evidence；
- counterevidence；
- 可选的单一 optimization point。

当前 Specialist 的正式 finding 还必须声明完整 eligible 分母、实际出现和已检查但未
出现的 Trajectory、共同阶段/目的/可观察效果、反例搜索范围、可重放 derivation、限制与
信心。重复行为至少需要两条独立 Trajectory 支持，不能用较小分母夸大模式。

证据引用会在 AgentRun 成功前由框架校验。大段 heredoc、完整命令、隐藏 reasoning
和 credential 不应复制到结构化结果中。

## 5. 证据不足时的处理

> **Superseded for current multi-Trajectory research by Decision 0027.** 本节描述的
> Synthesis 自动补证循环目前 deferred；当前 readiness 不足时只返回明确补采要求，
> 不创建正式研究结果，也不会自动启动 replay。

不能把“没有足够证据”伪装成成功或失败。Synthesis 必须按成本从低到高选择：

1. `HarnessMeasurementRequest`：现有数据足够，但 Harness 尚未提取所需事实；
2. `ExistingTrajectoryRequest`：需要另一批已有 trajectory；
3. `ReplayExperimentRequest`：只有新行为数据才能区分假设；
4. `HumanEvidenceRequest`：需要视觉判断或能力定义确认。

Replay request 必须说明：

- 要区分的假设；
- 改变变量和保持不变的变量；
- TaskCase、格式、skill version、runtime 和 N；
- 所需 Profiler/Comparator；
- 预算；
- 现有证据为何不足；
- 什么结果支持或反驳假设。

所有探索性 request 初始为 `proposed`。项目负责人批准后才允许生产 evidence；
随后可以自动运行 Harness 并开始新一轮分析。每个 campaign 最多三轮，超过上限
仍证据不足时以 `inconclusive` 结束。

## 6. 从 Finding 到 Candidate

> **Deferred:** Decision 0027 未授权从内部 Specialist finding 自动生成 hypothesis 或
> Candidate。本节仅保留未来改进链路的设计原则。

Synthesis 只有在以下条件全部满足时，才生成
`optimization.hypothesis.v1`：

- 问题有明确证据；
- proposed change 只包含一个原子变化；
- expected effect 可由 replay 观察；
- protected dimensions 明确；
- evidence 非空。

一个 hypothesis 对应一个 CandidateSkill。组合修复必须成为新的显式 candidate，
不能混入原子 candidate。CandidateProposer 只实现变化；framework 负责从父快照和
完整 candidate 计算权威 diff。

## 7. Candidate 效果

Candidate 使用新鲜 baseline，不复用诊断阶段的五次 replay。默认实验包括：

- candidate smoke 1 次；
- 触发 TaskCase baseline/candidate 各 3 次；
- 回归 TaskCase baseline/candidate 各 3 次；
- 共 13 次，且 paired run 交替执行。

每次 run 使用相同版本的 Profiler 和 Comparator。ReplayJudge 按维度输出：

- `correctness`
- `capability_coverage`
- `outcome_consistency`
- `token`
- `duration`
- `cost`
- hypothesis 指定的其他保护维度

每个维度只能是 `improved`、`regressed`、`unchanged` 或
`inconclusive`。最终 gate 只能是：

- `improved`
- `regressed`
- `mixed`
- `inconclusive`
- `not_runnable`

不计算加权总分。`correctness` 和 `capability_coverage` 是硬约束；任何硬约束或
指定保护维度退化时，不能分类为 improved。分类只帮助人工理解结果，不控制
candidate 可见性或发布。

## 8. 当前状态与下一步

Harness-first 研究环境、严格结果门禁、两次盲测认证流程和四 Specialist 内部结果板已
实现；当前五条 Trajectory 的 batch 仍是 `prepared`。本地缺少固定研究镜像，四份研究 Prompt
和 Harness manifest 仍为 `proposed`，完整覆盖 Suite 与历史 TaskCase/条件映射也未就绪。
因此真实 Docker Harness、盲测、capability certificate 和 Specialist 都尚未运行，产品
多 Trajectory Analysis 数量保持为零。

当前阶段不包含 Synthesis、补证循环、归因、可行性、Candidate、Comparison 或正式用户
报告。开始真实研究的准确顺序见[多 Trajectory 研究说明](multi-pi-analysis.md)与
[当前计划](../.plan/next.md)。“确定性事实与模型解释分离、无总分、人工发布”仍是长期
有效原则。
