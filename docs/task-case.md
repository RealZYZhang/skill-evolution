# TaskCase 执行契约

状态：`task.case.v1` 已实现；execution prompt v2 待项目负责人审核。

`TaskCase` 把一次 skill 执行的输入交付方式、能力标签、预算提示和预期产物定义为
可持久化 JSON。Capture 和 replay 使用同一个对象，避免再把输入文件名和
`output.html` 写死在执行器中。

## 文件输入

文件路径相对于 TaskCase JSON 所在目录解析。运行时保留原文件名和扩展名，并复制
到 Pi 工作目录的 `input/<原文件名>`：

```json
{
  "schema": "task.case.v1",
  "task_case_id": "docx-basic",
  "delivery": "file",
  "input": {
    "path": "fixtures/example.docx"
  },
  "expected_artifacts": [
    "output.html",
    "reports/validation.json"
  ],
  "capability_tags": [
    "format:docx"
  ],
  "budget": {
    "timeout_seconds": 900
  }
}
```

运行目录中对应的输入是
`artifacts/input/example.docx`。原始 fixture 和 skill snapshot 不会被修改。

## Inline text 输入

`inline_text` 不创建伪装成文件的输入副本。正文保存在完整 TaskCase 中：

```json
{
  "schema": "task.case.v1",
  "task_case_id": "pasted-text-basic",
  "delivery": "inline_text",
  "input": {
    "text": "这里是粘贴的源文档。"
  },
  "expected_artifacts": [
    "output.html"
  ]
}
```

## 路径和完成条件

- `expected_artifacts` 至少包含一项，路径相对于 Pi 工作目录。
- 绝对路径、`..`、重复路径以及 `input/`、`skill/`、`runtime/` 下的输出会在
  创建 trajectory 前被拒绝。
- Pi settled 后，每个预期产物都产生独立 `artifact_registered` 记录。
- 任一预期产物缺失都会形成正常封存的失败 trajectory；`outcome.artifacts`
  保存完整列表，旧接口使用的 `outcome.artifact` 仍指向第一项。

## CLI

旧的文件输入调用方式仍然支持，并自动转换成 `task.case.v1`：

```bash
python3 scripts/replay.py \
  --skill skills/document-html-visualizer-skill \
  --source path/to/source.pdf \
  --expected-artifact output.html \
  --prompt-file prompts/execution/document-html-visualizer-v2.md \
  --replays 3
```

结构化任务使用：

```bash
python3 scripts/replay.py \
  --skill skills/document-html-visualizer-skill \
  --task-case path/to/task-case.json \
  --prompt-file prompts/execution/document-html-visualizer-v2.md \
  --replays 3
```

Skill execution template 必须且只能各包含一次 `{{SKILL_CONTENT}}` 和
`{{TASK_CASE}}`。当前 v2 sidecar 是 `proposed`，在项目负责人批准前，上述命令
会在创建 campaign 和调用 Pi 之前停止。

Execution template 只规定框架边界，例如输入交付、工作目录、预期产物和结果报告。
格式解析、内容保留、页面结构、可视化和特定产物的验收要求必须写在被测 skill
中，不能由 execution template 为某个 skill 另行补充。

完整 TaskCase 由 framework 保存，但不会全部发送给执行 agent。模型只能看到：

```json
{
  "input": {
    "type": "file",
    "path": "input/example.docx"
  },
  "expected_artifacts": [
    "output.html",
    "reports/validation.json"
  ]
}
```

Inline text 的 `input` 改为
`{"type": "inline_text", "text": "..."}`。`schema`、`task_case_id`、
`capability_tags` 和 `budget` 只供 framework 保存、调度和分析，不注入模型
prompt。
