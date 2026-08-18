# 0004 — 原样重复保存 message_update 导致 RPC trajectory 膨胀

Status: Resolved
First observed: 2026-07-24
Resolved: 2026-07-25

## Symptom

一次约 331 秒的成功运行产生 24,161 条 RPC record，其中 24,021 条是
`message_update`。`pi-rpc.jsonl` 约 1.94 GB，而对应 Pi session 只有
136,595 bytes。

300 秒超时样本也产生约 1.94 GB RPC 文件。两条样本当前各占约 1.8 GiB 磁盘。

## Reproduction

运行 `docs/trajectory-spike.md` 中的文档可视化任务，并让 DeepSeek 流式生成
较大的 `write` 工具参数。

## Diagnosis

Pi 0.81.1 的 `message_update` 同时包含不断增长的 `message` 快照和
`assistantMessageEvent`。探索性 recorder 又同时保存原始 JSON 字符串与完整解析
对象，使相同内容在一条 record 内重复，并跨数万条 update 继续累积。

## Workaround

保留现有 P0 样本，不再把该探索性格式视为正式 schema。后续大型采样前先完成 P1
存储设计。不要未经项目负责人确认删除现有 trajectory。

## Resolution

第一步采用决策 0009：

- 只保存解析后的结构化事件，不再同时保存 raw。
- `message_update` 删除累计 `message` 和 `partial`，只保存真实 delta 与核对
  指纹。
- `message_end` 只保存描述和 hash，完整消息以 Pi session 为权威。
- manifest、event、stderr 和 outcome 写入一个有全局 `seq` 与 hash chain 的
  `trajectory.jsonl`。
- session 缺消息时写 `message_recovery`；运行中断时保存一次最新 partial。

同一 skill 和输入的 2026-07-25 成功样本包含 54,214 个 `message_update`，
比旧成功样本更多，但 journal 只有 31,891,482 bytes。相对旧
1,935,000,437-byte RPC 文件减少 98.352%，约缩小 60.67 倍。

第二步发现 evaluator 不需要流式过程，采用决策
`.memory/decisions/0010-action-level-trajectory.md`：

- `message_start` 和 `message_update` 不落盘，只在 `message_end` 保存完整
  `message_action`。
- 工具 start/end 合并为一条包含参数、结果、错误和状态的 `tool_action`。
- 不保存 RPC request/response、tool progress 或完整性 hash。
- Pi session 作为调试 sidecar，不与 message 做 hash 核对。

最新真实样本观察到 17,484 个 `message_update`，但最终 journal 只有 90 条、
248,381 bytes。它相对旧 RPC 文件减少 99.987%，约缩小 7,790 倍。

中间 delta-journal 目录已按项目负责人授权删除；旧 P0 原始 RPC 样本仍作为问题
复现证据保留。
