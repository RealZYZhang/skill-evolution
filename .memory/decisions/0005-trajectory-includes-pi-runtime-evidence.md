# 0005 — Trajectory 包含 Pi events、session、配置和 runtime 信息

Status: Partially superseded by 0010
Date: 2026-07-24
Owners: project owner

## Context

框架需要以自己的 trajectory 作为权威数据，同时不能丢失 Pi 的原始运行证据。
仅保存标准化事件会遗漏 Pi 特有信息；仅保存 Pi session 又无法表达启动失败、
框架配置和其他 runtime。

## Decision

Trajectory 包含：

- 完整配置和 `RunSpec`；
- runtime、Pi、模型和环境信息；
- 通用标准元素；
- 作为 trajectory element 的 Pi 原始 RPC event；
- 作为 trajectory 子序列的 Pi session entries；
- 成功、失败、中止和启动失败 outcome。

Pi session 是 trajectory 的 runtime-specific 子序列，不是第二权威数据源。
Capture adapter 为每次 run 使用独立 session 目录并在结束时封存。

## Alternatives considered

- 只保存标准化 trajectory：跨 runtime 方便，但可能丢失原始证据。
- 直接以 Pi session 为 trajectory：实现简单，但无法表达启动前失败和框架配置。
- 把 Pi event/session 仅作为临时日志：存储较少，但离线归因无法复查原始证据。

## Consequences

- 离线分析既可以使用通用元素，也可以下钻 Pi 原始证据。
- Trajectory 数据量增加，并需要处理 event 与 session 内容重叠。
- 需要序号、时间和内容摘要来建立标准元素与 Pi 证据之间的关系。
- 敏感信息过滤必须同时覆盖 event 和 session。

## Revisit when

如果 Pi session 体积或重复内容成为明显问题，可改为内容寻址引用或按需加载，但
不能丢失其作为 trajectory 子序列的语义。
