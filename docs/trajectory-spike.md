# Trajectory 人工采样器

状态：action-level Trajectory 与 Skill Contract preflight 已投入使用；Skill-first
Execution 写入已接入，但冻结 Revision 一致性修复和历史 cutover 尚未完成。

## 目标与边界

`scripts/trajectory_spike.py` 用 Pi RPC 人工执行一次指定 skill，并把成功或失败
证据封存在独立目录。当前格式采用已接受的统一 journal 定义；设计演进、字段职责
和实测结果见 [`trajectory-definition.md`](trajectory-definition.md)。

该脚本不评估输出质量、不做问题归因、不生成 candidate，也不触发 replay。

## 使用方式

Pi 必须预先配置可用的 provider、model 和认证。源文档标记为内部材料时，应优先
使用本地模型；使用外部模型前必须确认数据披露范围。运行本次文档可视化样本：

```bash
python3 scripts/trajectory_spike.py \
  --skill skills/document-html-visualizer-skill \
  --source \
  skills/document-html-visualizer-skill/example/AI工具辅助方案_日常法务工作方向_V2.md \
  --prompt-file \
  prompts/execution/document-html-visualizer-v2.md
```

v2 prompt 当前仍是 `proposed`；项目负责人批准前，命令会在调用 Pi 之前停止。
脚本会保留源文档的原文件名和扩展名，并复制到运行工作区的
`input/<原文件名>`，不会修改原始 skill 或示例。也可以使用 `--task-case`
加载 `inline_text` 输入和多个预期产物，契约见
[`task-case.md`](task-case.md)。Pi 只显式加载被测 skill，并关闭其他 skill、
prompt template、context file 和 thinking 输出。允许的内置工具为 `read`、
`write` 和 `bash`。

`--prompt-file` 必须经过项目负责人审批，审批流程见
[`replay.md`](replay.md#prompt-审核门禁)。脚本不再接受临时 `--prompt` 字符串。
被测 Skill 还必须在 `SKILL.md` 同级提供已批准的 `skill_contract.json`；缺失、无效
或未批准时，脚本会在创建运行目录和启动 Pi 前失败。
Skill execution template 中的 `{{SKILL_CONTENT}}` 会被当前 `SKILL.md` 全文
替换，`{{TASK_CASE}}` 会被结构化任务数据替换；两个占位符都必须且只能出现
一次。

当前真实 `.skill-evolution` 正等待一次性层级迁移。迁移完成前请把
`--runtime-root` 指向隔离的临时目录进行开发验证，避免创建部分新层级而阻塞或提前
切换历史数据源。

## 当前输出

```text
.skill-evolution/skills/<skill-id>/executions/<execution-id>/
├── execution.json
└── payload/
    ├── trajectory.jsonl
    ├── pi-session.jsonl
    ├── artifacts/
    │   ├── input/
    │   │   └── <原文件名>
    │   ├── <expected-artifacts...>
    │   └── skill/
    └── runtime/
        └── pi-session/
```

- `execution.json`：绑定 Skill/Revision，并声明 Input、Output、Trajectory、session、
  状态和 provenance；
- `trajectory.jsonl`：完整 message、完整 tool action、framework 状态和 outcome
  的唯一全局顺序；使用连续 `seq`，不保存流式 delta 或完整性 hash。
- `pi-session.jsonl`：Pi session 的原生副本，仅用于调试和重新提取。
- `artifacts/`：隔离的输入、被测 skill 快照和本次运行产物。
- `runtime/pi-session/`：运行时 Pi 自己维护的 session 目录。

当前实现先注册 Revision，随后又从可变源 package 复制运行快照；两者之间发生修改时
可能出现“Execution 声明 Revision A、实际运行内容 B”。Cutover 前必须改为 Contract
校验、prompt 渲染和 Pi Skill 全部只读已冻结 Revision，并在封存时复核摘要。

启动失败、prompt 被拒绝、进程退出、执行超时或任一预期产物缺失都会形成
`status: failed` 的 `trajectory_finished`，并在 `trajectory_sealed` 前保存
已经完成的 action。未结束的 message 不保存 partial；未结束的 tool 记为
`status: interrupted`。

## 2026-07-24 首次真实观察

当前保留了四条真实样本：

1. `20260724T103702Z-a9240830`：Pi 0.81.1 的 `get_commands` 把 skill 路径
   放在 `sourceInfo.path`，与文档示例的顶层 `path` 不同。采集器误判 skill
   未加载；现已兼容两种结构。
2. `20260724T103732Z-c581b561`：目标 skill 注册成功，但 Pi 以
   `No API key found for the selected model` 拒绝 prompt。
3. `20260724T114843Z-5706d996`：DeepSeek 接受 prompt 并生成 HTML，
   但 300 秒时仍在输出最终消息，采集器按预算封存为 `agent_execution`
   超时失败。该样本包含 21,441 条 RPC record、16 条 session entry 和
   21,764 bytes 的 HTML。
4. `20260724T115551Z-7b474577`：900 秒预算下于约 331 秒正常收到
   `agent_end` 和 `agent_settled`，outcome 为 `succeeded`。该样本包含
   24,161 条 RPC record、31 条 session entry 和 45,436 bytes 的 HTML。

这些样本证明：

- 失败可以保存完整的 request/response 顺序和 stderr。
- `get_state` 在 session 文件尚未实际创建时也会返回预定 `sessionFile` 路径。
- prompt 接受前没有 agent event，也没有 session entry。
- DeepSeek 能在 supporting resources 缺失时直接读取 `SKILL.md` 和输入，并生成
  自包含 HTML；资源缺失没有造成启动或工具级阻塞。
- 首次 `write` 因工具参数触及模型输出上限失败，agent 随后缩短并重发成功。
- 一个返回退出码 1 的 `grep` 是“没有外部 URL 匹配”的验证结果，不是 artifact
  缺陷。
- 完整 HTML 没有外部 `src`/`href`，没有失效的本地目录锚点，并包含语义地标、
  搜索输入、内联 CSS/JavaScript、13 个表格和 14 个二级标题。

## Event 与 session 的初步边界

- 只在高频 event 中出现：流式 `message_update`、工具 start/update/end 的实时
  时序、`agent_start`、`agent_end` 和 `agent_settled`。
- 只在 session 中稳定出现：session header、`session_info`、`model_change`、
  `thinking_level_change` 和持久化后的对话树。
- 两者都有：assistant message、tool call 和 tool result，但 event 提供细粒度
  时间过程，session 提供紧凑的最终状态。
- framework 自己补充：task/skill、启动参数、runtime、超时阶段、进程退出码、
  artifact 状态和 observer error。

## P0 暴露的存储问题

成功样本的 `pi-rpc.jsonl` 约 1.94 GB，而 session 约 137 KB。24,021 条
`message_update` 包含累计消息快照，探索性格式又同时保存 raw 与 parsed，导致
巨大重复。详情见 `.memory/problems/0004-rpc-message-update-volume.md`。

因此 P0 输出足以进入 P1，但当前格式不能直接成为正式 trajectory schema。

## 2026-07-25 Action-level 验证

当前只在 `message_end` 写完整 message，并把工具 start/end 合并成完整
`tool_action`。真实运行保存在
`.skill-evolution/trajectories/20260725T063831Z-bbf1a500`（冻结的历史目录名）：

- 运行成功，持续 277 秒；
- Pi 发出 17,484 个 `message_update`，没有任何 delta 落盘；
- journal 只有 90 条、248,381 bytes；
- 包含 31 条完整 message 和 15 条完整 tool action；
- 15 次工具调用中 3 次失败，失败与后续 action 均有记录；
- Pi session 为 121,835 bytes，只作为原生 sidecar；
- 输出为 52,671-byte 自包含 HTML；
- 相比旧版 1,935,000,437-byte RPC 文件，主记录缩小约 7,790 倍。

中间版本的 31.9 MB delta journal 已按项目负责人授权删除，其指标保留在
`docs/trajectory-definition.md`。

旧 P0 目录仍作为历史证据保留，未经项目负责人确认不删除。
