请你执行以下 skill：

<skill>
{{SKILL_CONTENT}}
</skill>

本次执行使用以下结构化任务定义：

<task-case>
{{TASK_CASE}}
</task-case>

执行要求：

1. 完整遵守 skill 中定义的执行流程、限制和验收要求。
2. `input.type` 为 `file` 时，读取 `input.path` 指定的文件。
3. `input.type` 为 `inline_text` 时，把 `input.text` 作为本次任务输入，不要用
   工作目录中的其他文件替代它。
4. 生成 `expected_artifacts` 列出的每一个产物，路径相对于当前工作目录。
5. 只能在当前工作目录内读写。
6. 不要修改被测 skill 或任务输入。
7. 逐一检查每个预期产物是否已经生成；无法完成时明确说明，不要用空文件或无关
   内容代替。

完成后简要说明：

1. 本次完成了什么；
2. 每个预期产物的路径和检查结果；
3. 失败项和已知限制。
