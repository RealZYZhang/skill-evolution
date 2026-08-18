# 0005 — Comparator 的 EvidenceRef 字段与公共契约漂移

Status: Resolved
First observed: 2026-07-26
Resolved: 2026-07-26

## Symptom

`artifact.comparison.v1` 把 artifact root 标记为 `evidence.ref.v1`，但同时使用
`report_pointer` 和 `html_line`。公共 `EvidenceRef` 只定义
`report_path/json_pointer/line`；解析这些引用时，非标准字段会被忽略。

Artifact path 仍可验证，因此旧报告可以下钻，但报告 pointer 不再具有契约含义。

## Reproduction

读取旧 Harness
`.skill-evolution/harness-runs/20260726T141327Z-07e9b46c/` 的
`artifact-comparison.json`，将任一 `/artifacts/*/evidence_ref` 交给
`EvidenceRef.from_dict(...).to_dict()`；`report_pointer` 不会出现在结果中。

## Diagnosis

Comparator 在公共 EvidenceRef 确定前拥有自己的局部字段名。后续增加公共解析器时，
没有用解析器对 Comparator 生成的 root reference 做 round-trip 测试。

HTML facts 内部的 `html_line + selector` 是与 artifact root 组合使用的局部位置，
不自称完整 `evidence.ref.v1`，因此可以继续保留。

## Workaround

旧报告仍可用 `artifact_path`，再到对应 fact 的局部 line/selector 下钻。不要把
旧 `report_pointer` 当作已验证的 JSON pointer。

## Resolution

- Comparator 的完整 artifact root reference 只输出公共契约可解析的字段。
- 删除没有 `report_path` 配套的 `report_pointer`；需要引用报告时，由 Agent 使用
  EvidenceBundle 内的 `report_path + json_pointer`。
- canonical line 字段改为 `line`。
- 测试对 Comparator root reference 运行 `EvidenceRef` round-trip。
- 重新生成 Harness
  `.skill-evolution/harness-runs/20260726T145302Z-166b1ee2/`，并由此冻结新的
  EvidenceBundle 和空 AnalysisCampaign。
