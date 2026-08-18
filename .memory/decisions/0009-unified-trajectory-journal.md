# 0009 — 使用统一 Journal 表达 Trajectory 顺序

Status: Superseded by 0010
Date: 2026-07-25
Owners: project owner

## Context

P0 将每条 Pi RPC record 的原始 JSON 字符串和完整解析对象同时写入
`pi-rpc.jsonl`。Pi 的 `message_update` 又包含累计 `message` 与
`assistantMessageEvent.partial`，导致一次约 331 秒的运行产生约 1.94 GB RPC
文件，而最终 Pi session 只有约 137 KB。

分散的 run、RPC、stderr、session 和 outcome 文件还需要事后按不可靠的时间戳拼接，
无法直接表达 framework、Pi 与 artifact 的统一观察顺序。

## Decision

每次运行使用一个 append-only `trajectory.jsonl` 作为唯一顺序真相：

- 第一条是包含 immutable manifest 的 `trajectory_started`。
- Framework、Pi RPC、stderr、message delta、tool attempt 和 artifact observation
  由一个 writer 分配单调递增 `seq`。
- `message_update` 只保存真正的增量，删除累计 `message` 和 `partial`。
- `message_end` 只保存 message 摘要与规范化 hash；完整已结束 message 只保存在
  Pi session。
- 每次 tool start/end、最终参数、结果和错误都保留，失败重试不得合并。
- 尚未结束的 message 或 tool 在封存时只保存一次最新 partial snapshot。
- Pi session 无论成功失败都封存；成功终态标记 `complete`，否则标记 `partial`。
- Session 缺少已经结束的 message 时，journal 增加完整 recovery record。
- Artifact 保持为文件，由 journal 使用相对路径、大小和 SHA-256 引用。
- Outcome 是 journal 的尾部事件；独立 run/outcome JSON 只允许作为派生视图，
  不是 canonical source。

运行目录的 canonical 内容为：

```text
trajectory.jsonl
pi-session.jsonl
artifacts/
```

## Alternatives considered

- 继续保存 raw 与 parsed：证据最直接，但体积不可接受。
- 只保存最终 Pi session：非常紧凑，但丢失启动失败、in-flight message、工具时序、
  stderr、runtime 和 framework outcome。
- 多个独立日志靠 timestamp 合并：实现简单，但并发观察无法形成可靠全序。
- 把 session 和 artifact 全部内嵌 journal：单文件物理封装直观，但会重复大型
  payload，并破坏 Pi session 的原生可恢复格式。

## Consequences

- `seq` 是 trajectory 的主顺序；wall-clock 时间只用于展示和耗时分析。
- Pi session 是已结束 message 与 Pi 会话状态的 canonical snapshot。
- Journal 是执行过程、错误、配置与 framework outcome 的 canonical log。
- 不再提供逐字节的全部 RPC 原始流；协议解析失败时例外保存原始 record。
- 需要在封存时校验 message hash 与 session，并测试成功、失败和中断恢复。
- 该决策取代 0005 中“原样保存每条 Pi RPC event”的具体存储方式，但保留
  Pi-specific evidence 必须属于 trajectory 的原则。

## Revisit when

需要逐字节协议审计、跨进程 writer、并行 tool causality、远程 artifact store 或
正式 schema migration 时。
