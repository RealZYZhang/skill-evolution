# Replay Execution Set

> Purpose: describe repeated Skill execution after Campaign was demoted to a
> same-Revision batch and retained only as historical provenance.

状态：模块已实现；历史 v1 prompt 已完成五次真实 replay。TaskCase 执行层已
实现，v2 prompt 仍待项目负责人审核，因此尚未用于真实模型调用。

首次成功 campaign：

```text
.skill-evolution/replays/20260725T154836Z-9aacc0cb/
```

5 次运行全部成功，均保存连续封存的 trajectory、完整 Pi session 和
`output.html`。框架外的确定性检查确认基本 HTML 结构完整、本地锚点有效、没有
重复 ID 或外部依赖，且输入文档和 skill 快照未被改变。

## 当前模型

`scripts/replay.py` 使用固定的 skill、`TaskCase`、已批准 prompt template 和
运行配置，串行执行 N 次。它先快照完整 Skill package 为不可变 Revision，再创建一个
同 Revision 的 Execution Set，随后把 N 次 Execution 直接写在 Skill 下。Skill 必须在 `SKILL.md` 同级包含已批准的
`skill_contract.json`；preflight 在创建 campaign 前完成。Campaign 开始前把当前
`SKILL.md` 全文与结构化
TaskCase 注入 template，只渲染一次，并把完全相同的 prompt 发送给 N 次运行。
每次尝试都复用 action-level Trajectory capture，因此成功、模型失败、工具失败、超时和
进程退出都会形成各自的 Trajectory 与 Pi session。Campaign ID 只在历史 provenance 和
兼容 API 中出现，不再是用户必须理解的顶层身份。

当前模块只负责重复采样，不比较结果、不评分、不归因、不执行 gate，也不生成
candidate。

## 为什么还需要 source 和 prompt

仅有 skill 和次数无法定义“重复执行什么任务”。一个 replay campaign 的固定输入
包括：

- `--skill`：被执行的 skill 目录；
- `<skill>/skill_contract.json`：固定 runtime 与 EvaluationSuite 绑定；
- `--source`：兼容入口，把一个文件转换成默认 `TaskCase`；或者使用
  `--task-case` 读取完整 `task.case.v1`；
- `--prompt-file`：项目负责人批准的版本化执行 prompt template；
- `--replays N`：独立运行次数；
- timeout 和 Pi runtime 参数。

固定这些输入后，N 条 Trajectory 才具有可比较的任务边界。普通 Harness 与多 Trajectory 分析只
接受这一个 Execution Set，因此不会无意混入另一个 Revision。

## Prompt 审核门禁

Prompt template 不能作为临时 CLI 字符串传入。每个 template 必须有相邻的
`.approval.json`，并满足：

- `status` 是 `approved`；
- 记录 `prompt_id`、version、批准人和批准时间；
- `content_sha256` 与当前正文一致。

这里的摘要用于保证“template 就是用户批准的版本”，不是 trajectory 完整性
校验。批准后修改 template 会自动使批准失效。

Skill execution template 必须且只能各包含一次：

```text
{{SKILL_CONTENT}}
{{TASK_CASE}}
```

Renderer 读取被测 skill 的 `SKILL.md` 全文，并把 TaskCase 的模型可见部分作为
JSON 数据注入。模型可见部分只有 `input` 和 `expected_artifacts`；完整 TaskCase
中的 ID、capability tags 和 budget 仍由 framework 保存。Template 审批约束固定
的 prompt 结构；campaign 同时快照被测 skill 和最终 `rendered.md`，便于用户
检查实际发送内容。缺失或重复占位符都会在创建 campaign 前失败。

Execution template 只负责通用执行边界，不重复规定某一种 skill 的内容、格式或
产物质量要求。此类要求必须由注入的 `SKILL.md` 自己定义，避免 framework prompt
暗中改变被测 skill 的行为。

v1 prompt 是首个真实 campaign 的历史快照。支持 TaskCase 的 v2 prompt 是：

```text
prompts/execution/document-html-visualizer-v2.md
```

它当前仍是 `proposed`，未获项目负责人批准，不能启动 Pi。

查看 prompt 和审批状态：

```bash
python3 scripts/prompt_approval.py inspect \
  --prompt-file \
  prompts/execution/document-html-visualizer-v2.md \
  --skill skills/document-html-visualizer-skill
```

该命令依次展示 approval metadata、template 和注入当前 `SKILL.md` 后的完整
rendered prompt，不会批准 prompt 或调用模型。

项目负责人确认正文后，可以亲自执行：

```bash
python3 scripts/prompt_approval.py approve \
  --prompt-file \
  prompts/execution/document-html-visualizer-v2.md \
  --approved-by project-owner
```

也可以在对话中明确批准，由开发 agent 记录审批。任何正文修改都会使批准失效；
未批准的版本会被 replay 和直接 trajectory CLI 在调用模型前拒绝。

## 运行

Prompt 批准后，运行三次：

```bash
python3 scripts/replay.py \
  --skill skills/document-html-visualizer-skill \
  --source \
  skills/document-html-visualizer-skill/example/AI工具辅助方案_日常法务工作方向_V2.md \
  --prompt-file \
  prompts/execution/document-html-visualizer-v2.md \
  --replays 3
```

默认每次运行最多等待 900 秒。MVP 串行执行，避免并发运行争用工作区、模型配额或
外部工具状态。

## 输出

```text
.skill-evolution/skills/<skill-id>/
├── revisions/<revision-id>/package/...
├── execution-sets/<set-id>/
│   ├── set.json
│   └── prompt/...
└── executions/
    ├── <execution-id-1>/
    │   ├── execution.json
    │   └── payload/
    │       ├── trajectory.jsonl
    │       ├── pi-session.jsonl
    │       ├── artifacts/
    │       └── runtime/
    └── <execution-id-2>/...
```

`set.json` 是可比较批次，记录固定 Skill/Revision、TaskCase、运行配置、顺序化
Execution ID 和 provenance。每个 `execution.json` 独立记录状态、时间、Input、Output、
Trajectory、session 和失败信息。分析不会不断写回 Execution manifest。

- 固定 skill、source、prompt 版本和审批信息；
- template、最终渲染 prompt 和 skill entrypoint；
- 请求的 N；
- 每个 run 的 index、run ID、目录、状态、耗时和失败信息；
- trajectory、session 和 artifact 的相对路径；
- 成功、失败、orchestration failure 和实际 trajectory 数量。

Set 在每个 Execution 后原子更新，因此中途退出时已经完成的运行仍可检查。

## 状态和失败规则

- `completed`：生成了 N 条 Trajectory，且每条 Execution outcome 都成功。
- `completed_with_failures`：生成了 N 条 Trajectory，但至少一条 Execution
  outcome 失败。
- `failed`：Execution Set 自身未能形成要求的运行边界。

一次 Execution 失败不会停止后续运行。失败 Trajectory 和 session 与成功记录使用同一
目录结构，不会被隐藏或删除。

`--output-root` 只保留给明确的 legacy fixture/迁移兼容。默认 CLI 不会创建
`.skill-evolution/replays/`。

## 测试边界

自动化测试使用假 JSONL Pi 子进程，不调用真实模型，覆盖：

- N 次成功生成 N 条 trajectory 和 N 个 session；
- 每次 Pi 失败仍继续并保留全部失败 trajectory；
- 未批准 prompt 在创建 campaign 前被拒绝；
- prompt 批准后发生修改会要求重新审核；
- `SKILL.md` 全文只注入一次，且 N 次收到完全相同的 rendered prompt；
- 缺失或重复 skill placeholder 会在创建 campaign 前被拒绝；
- 非法 N 在创建目录前被拒绝；
- trajectory 创建前的 orchestration failure 会逐次记录，campaign 标为失败；
- manifest 原子更新且不遗留临时文件。
