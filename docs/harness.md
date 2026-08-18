# 确定性 Harness

状态：Skill-first 写入已实现；历史数据等待迁移  
更新日期：2026-08-09

## 1. 用途

Harness 只记录可以由代码重复计算的事实，把“发生了什么”和“如何解释”分开。它
由两个组件组成：

- `TrajectoryProfiler`：分析 trajectory 中的资源和执行策略；
- `HTMLArtifactComparator`：分析 HTML、Markdown 保留事实和 artifact 差异。

Harness 不调用模型，不修改 replay，不产生总分、排名、归因或 candidate。

## 2. 运行

```bash
python3 scripts/harness.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-set <execution-set-id>
```

默认请求桌面 `1440×900` 和移动端 `390×844` 截图。Chrome 自动发现不适用时：

```bash
python3 scripts/harness.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-set <execution-set-id> \
  --chrome /path/to/chrome
```

只需要静态事实时可使用 `--no-screenshots`。

## 3. 输出

```text
.skill-evolution/skills/<skill-id>/analyses/multi/<analysis-id>/
├── analysis.json
├── user-report.json
└── payload/
    ├── harness.json
    ├── trajectory-profile.json
    ├── artifact-comparison.json
    └── screenshots/
```

`analysis.json` 使用 `analysis.record.v1`，把 Harness 绑定到一个同 Revision 的
Execution Set。`harness.json` 继续保留组件版本、输出路径和状态：

- `completed`：两个组件都完整；
- `completed_partial`：静态事实可用，但一个组件部分完成，例如 Chrome 不可用；
- `failed`：Harness 无法产生可用报告。

`user-report.json` 使用 `analysis.multi_trajectory_view.v1`。确定性 Harness 本身不做语义
归因，因此该报告明确标记语义结论不可用、findings 为空，并保留确定性结果下钻。
`--campaign` 只供显式旧数据兼容；默认新流程使用 Execution Set。

## 4. TrajectoryProfiler

`trajectory.profile.v1` 的每条 run 包含：

- status、load status 和解析问题；
- message、turn、model call 和 tool action 数；
- input、output、cache read、cache write token；
- provider 报告的费用；
- run duration 和 tool duration；
- failed/interrupted action、retry、repeated read、rework；
- 工具策略序列和 evidence。

策略识别包括：

- 输入与 skill 读取；
- direct、chunked、partitioned artifact write；
- 临时 generator 创建与执行；
- artifact merge；
- validation 和 cleanup；
- 失败后的 retry；
- 重复读取和重新生成。

报告只保存动作类别、target 和 `run_id + seq`。完整工具参数或 heredoc 仍留在
trajectory 中，避免 Harness 复制大段内容。

跨 run 的每个资源字段计算：

- `min`
- `median`
- `max`
- `mean`
- `coefficient_of_variation`
- `outlier_run_ids`
- `missing_run_ids`

Token 口径使用每个完整 assistant message 的非累计 usage 字段分别求和，不把
累计 `totalTokens` 再次相加。

## 5. HTMLArtifactComparator

`artifact.comparison.v1` 对每个预期 HTML 记录：

- DOM 元素数、深度、tag、标题 outline 和 landmarks；
- `section/article/details`、class、自定义组件和 `data-component`；
- ID、重复 ID、本地 anchor 和 unresolved reference；
- ARIA/label 引用；
- table 结构；
- HTML/CSS 外部依赖；
- CSS variables、颜色、字体和 media query；
- inline/external script 和脚本 bytes；
- 规范化可见文本及可下钻文本块。

对于 Markdown 来源，它另行记录标题顺序、数字、URL、表头和规范化文本块是否在
输出中出现。这些是字面保留事实，不等于语义正确性评分。

同一 expected-artifact 路径的 HTML 才进行 pairwise 比较。Delta 包括 tag、
landmark、class、CSS token、标题、unresolved reference、script size 和
visible-text overlap。不同 artifact 角色不会被错误配对。

## 6. 截图安全

Comparator 不直接打开原 artifact。它创建临时副本并注入：

- 禁止外部网络、frame、object 和 form 的 CSP；
- 只允许本地 nonce browser probe；
- 关闭 animation、transition 和 smooth scrolling 的 CSS。

Chrome 不可用或截图失败时：

- 原 HTML 不变；
- 静态比较继续完成；
- artifact/报告保留截图失败原因；
- Comparator 和 Harness 标记为 partial；
- 不能把截图失败解释为 skill 质量失败。

第一版不做像素相似度，也不调用视觉模型。截图只供人工审阅。

## 7. EvidenceRef

Profiler 的动作事实引用原 trajectory `run_id + seq`。Comparator 的事实引用
artifact 路径，并为局部事实记录 HTML 行号或 selector。跨报告结论使用：

```json
{
  "schema": "evidence.ref.v1",
  "report_path": "reports/profile.json",
  "json_pointer": "/runs/0/strategies/first_artifact_write"
}
```

Agent 若需完整动作，应使用报告中的 run/seq 回到 EvidenceBundle 内对应
`trajectory.jsonl`，而不是依赖复制过的命令摘要。

## 8. 当前五次 replay 结果

输入 campaign：

```text
.skill-evolution/replays/20260725T154836Z-9aacc0cb/
```

最新 Harness：

```text
.skill-evolution/harness-runs/20260726T145302Z-166b1ee2/
```

已观察到：

- Profiler `load_status=ok`，5 条 run 全部可读；
- 5 次首次完整 artifact 写入均失败；
- run 采用了 chunked write、临时 generator、重新生成与返工、partition + merge
  等不同恢复策略；
- model calls 为 13 到 23，duration 为 334,170 到 663,004 ms；
- input/output/cache token、费用、失败动作和重试次数都存在明显 run 间差异；
- Comparator 检查 5 个 HTML，产生 10 组 pairwise delta；
- 现有 HTML 的 `section/article/details`、class、CSS token、脚本规模和文本规模
  存在差异。

受限环境未能完成 Chrome 截图，所以 Comparator 为 `partial`，统一 Harness 为
`completed_partial`。这不影响上述静态事实。
