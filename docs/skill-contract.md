# Skill Contract 与确定性初始检查

> Purpose: define the active package-local Skill Contract, deterministic
> validation report, execution preflight, and versioning boundary.

## 1. 当前 Contract

每个可执行 Skill package 必须在 `SKILL.md` 同级包含固定名称
`skill_contract.json`。当前 schema 是 `skill.contract.v2`，只负责：

- `skill_id`、语义化版本、owner、前序 contract 和 `proposed|approved`；
- 最大 runtime 边界：required/allowed tools、permissions、network、sandbox
  credentials、dependencies 和 assets；
- 独立 EvaluationSuite 的 `suite_refs`。

Contract 不复制或提取 `SKILL.md` 语义，也不定义 capability、inputs/outputs、场景、
validator 或 evidence taxonomy。Skill 指令属于 `SKILL.md`；具体行为要求属于
EvaluationSuite、TaskCase 和 Harness。

当前文档可视化 Skill 的正式 contract 位于：

```text
skills/document-html-visualizer-skill/skill_contract.json
```

它已获负责人批准。旧
`contracts/skills/document-html-visualizer-v1.json` 只为历史 v1 campaign 保留。

## 2. 已实现检查与 preflight

Contract 检查：

- 严格 schema 和字段集合，未知字段会失败；
- `skill_id`、registry identifier 和 `MAJOR.MINOR.PATCH`；
- proposed/approved 与审批字段一致；
- required tools 是 allowed tools 的子集；
- network 和 sandbox credential policy 使用明确类型；
- `evaluation.suite_refs` 非空且唯一。

Skill package 检查：

- `skill_contract.json` 必须与 `SKILL.md` 同级；
- package 和 `SKILL.md` 存在，且 package 不包含 symlink；
- `SKILL.md` 是 UTF-8，front matter 包含 `name` 和 `description`；
- 存在主标题，Markdown fenced code block 正确闭合；
- 记录文件数、bytes、行数和 Unicode code point 数。

TaskCase 检查继续验证每个 `task.case.v1` 可加载、fixture 存在、ID 唯一。对于当前
thin contract，报告只记录 contract 选择的 suite refs 和本次提供的 TaskCase IDs，
不再用 contract 中的自然语言 capability 推导覆盖矩阵。

执行边界：

- 单次 trajectory 和 N-run replay 在创建运行前要求 package-local contract 已批准；
- host Pi adapter 只启用 `allowed_tools` 映射出的内置工具，并把 contract runtime
  快照写入 trajectory；
- automatic candidate comparison 要求 baseline 和 candidate 的
  `skill_contract.json` 完全一致，并继续使用无网络、无 credential、无 host fallback
  的 Docker tool router；
- AnalysisWorkflow 从 EvidenceBundle 的 `skill_contract.json` 执行审批 preflight。

## 3. 运行 checker

检查仓库中的文档可视化 Skill：

```bash
python3 scripts/skill_contract.py \
  --skill skills/document-html-visualizer-skill \
  --task-case task-cases/document-formats/markdown.json \
  --task-case task-cases/document-formats/text.json \
  --task-case task-cases/document-formats/docx.json \
  --task-case task-cases/document-formats/pdf.json \
  --task-case task-cases/document-formats/inline-text.json \
  --require-approved
```

默认读取 `<skill>/skill_contract.json`。`--contract <path>` 只用于显式检查历史 v1
文件。使用 `--output <path>` 可原子写入 `skill.validation_report.v1`。

退出码：

- `0`：结构有效；指定 `--require-approved` 时也已满足审批门禁；
- `1`：报告包含 error，或 contract 未满足审批门禁；
- `2`：命令参数或报告写入失败。

## 4. Report 语义

`valid` 只表示没有确定性结构错误。`dynamic_test_ready` 还要求 contract 已获 owner
批准；它不表示 `suite_refs` 指向的全部测试已经执行。报告状态为：

- `error`：存在结构或 package 错误；
- `warning`：结构有效，但存在审批或 Markdown warning；
- `valid`：没有 error、warning 或 coverage gap。

`suggestions` 不改变结构有效性。所有 location 使用 `contract:`、`skill:` 或
`task_case:` 前缀，便于人工定位。

## 5. 扩展规则与当前缺口

文件名 `skill_contract.json` 保持稳定，schema 可以演进。当前 `evaluation` 只允许：

```json
{
  "suite_refs": ["document-html-visualizer-v2"]
}
```

将来需要 `evaluation.metrics` 时，应发布新的 schema 版本并更新 parser 和迁移规则，
而不是让 v2 接受任意字段。这样既能扩展，又能阻止拼写错误和未审核行为被静默接受。

当前仍未实现独立 EvaluationSuite object、suite reference resolver 和 metrics schema。
因此 `suite_refs` 目前会被严格校验、保存并传递，但不能被解释为对应测试已经执行。
自动提取的 Skill 摘要如果以后有用，应放入非门禁 `SkillProfile`，而不是重新加入
Contract。
