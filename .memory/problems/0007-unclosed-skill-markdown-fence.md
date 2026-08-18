# 0007 — 文档可视化 Skill 存在未闭合的 Markdown 代码块

Status: Resolved
First observed: 2026-08-04
Resolved: 2026-08-04

## Symptom

`skill.validation_report.v1` 将当前文档可视化 Skill 判为 `error`，错误代码为
`unclosed_fenced_code_block`，location 是 `skill:SKILL.md:151`。

## Reproduction

运行 `docs/skill-contract.md` 中的完整静态检查命令。Contract 的 delivery/format
矩阵由五个 TaskCase 覆盖，但 `valid=false`，因此不能进入动态测试。

## Diagnosis

`skills/document-html-visualizer-skill/SKILL.md` 在中间 JSON 示例前打开一个
`json` fenced code block，之后没有对应的 closing fence。Markdown renderer 和
agent 都可能把后续阶段、验证规则、输出规范和故障处理解释为代码块内容，而不是
Skill 指令。

## Workaround

不要绕过静态 validator 或仅依靠 contract approval。修复前保留现有 Skill 内容和
验证报告作为证据，不运行真实分析或 candidate replay。

## Resolution

已在 JSON 示例结束处增加 closing fence。重新运行 Skill Contract checker 后，
`unclosed_fenced_code_block` 消失。初次报告的剩余 warning 只表示当时的 capability
contract 尚为 `proposed`；2026-08-06 package-local `skill_contract.json` 获批后，
当前报告为 `valid=true`、`status=valid`。相关 Skill Contract 和兼容测试均通过。
