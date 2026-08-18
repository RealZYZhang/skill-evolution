# Trajectory 定义演进：从 RPC 镜像到完整 Action

状态：Accepted  
更新日期：2026-08-07

## 当前结论

本项目当前把 trajectory 定义为：

```text
Trajectory = 完整 action 的有序 trajectory.jsonl
           + Pi session 原生调试副本
           + 输入、skill 快照和输出 artifacts
```

`trajectory.jsonl` 是 evaluator 的输入，回答“一次 skill 执行按顺序完成了哪些
message 和 tool action，最后结果如何”。Pi session 不参与默认评估，只用于调试
Pi 或重新提取 action。

当前代码和新运行只使用 Trajectory 命名。0021 期间（2026-08-07 起）冻结的证据可能
使用 `trace.jsonl`、`trace.actions.v1`、旧边界记录和旧目录名；reader 会把它们
投影为当前 Trajectory 模型并标记 legacy 来源，但不会原地改写。writer 不再产生旧格式，
也不双写。这个边界见决策
[`0021`](../.memory/decisions/0021-trajectory-naming-and-legacy-read-compatibility.md)，
其命名方向已由
[`0028`](../.memory/decisions/0028-trajectory-naming-restored.md) 反转。

## 1. 初版如何记录，以及为什么出问题

P0 为了观察 Pi 的真实 RPC，采用了最保守的镜像式采集：

```text
spikes/<run-id>/
├── run.json
├── pi-rpc.jsonl
├── pi-session.jsonl
├── stderr.log
├── outcome.json
└── workspace/
```

`pi-rpc.jsonl` 同时保存每条 RPC 的原始字符串和解析对象。一次约 331 秒的成功
运行产生 24,161 条 RPC record，其中 24,021 条是 `message_update`，主文件达到
1,935,000,437 bytes；对应 Pi session 只有 136,595 bytes。

根因不是 JSONL 本身，而是：

1. Pi 的每次 `message_update` 除了新增 delta，还带有截至当前的完整
   `message` 和累计 `partial`。
2. 初版又同时保存 raw 和 parsed。
3. 不断增长的同一消息因此在数万次 update 中反复复制，形成近似二次增长。

## 2. 如何 debug，以及考虑了哪些选择

我们按 event type 统计记录数，并对照 Pi 0.81.1 文档、源码和 session，确认：

- `message_update` 是流式过程，不是一个完成的 agent action；
- `message_end` 才提供完整 message；
- 工具 start/update/end 描述一个工具 action 的生命周期；
- Pi session 保存完整消息和 Pi 自己的会话树，但不包含 framework 的配置、
  启动失败、超时阶段、artifact 状态和统一 outcome。

考虑过的方案：

| 方案 | 优点 | 主要问题 |
| --- | --- | --- |
| 原样保存 raw + parsed | 协议证据最全 | 约 1.94 GB，无法大规模采样 |
| 只保存 Pi session | 很小，完整消息清晰 | 缺少 framework 状态和统一执行边界 |
| 压缩原始 RPC | 改动小 | 重复仍存在，分析仍需读取传输细节 |
| 保存 message delta | 能重放流式过程 | evaluator 不需要逐 token 过程 |
| 只保存完整 action | 直接面向评估，最简洁 | 不再能恢复半截消息或逐 token 时序 |

## 3. 第一次重定义：Delta Journal

第一次优化采用 `trajectory.journal.v1`：

- 删除 raw；
- `message_update` 只保留 delta；
- 完整 message 只放在 session；
- journal 使用 hash chain；
- message 与 session 使用 hash 核对。

同一 skill 的成功样本包含 54,214 个流式 update，journal 为
31,891,482 bytes。它相对初版减少 98.352%，证明累计快照重复已经解决。

但它仍然有 54,454 条记录和约 31.9 MB。重新检查 evaluator 的目标后发现：

- evaluator 关心完整消息，而不是消息如何逐 token 生成；
- evaluator 关心一次工具调用的完整参数、结果和错误，而不是 progress update；
- message hash 和 journal hash chain 不帮助判断 skill 执行质量。

因此 delta journal 虽然技术上完整，却超出了 MVP 的评估需求。该样本目录
`20260725T035220Z-9e60cc81` 已在项目负责人授权下删除；指标保留在本文用于复盘。

## 4. 最终选择：Action-level Trajectory

当前 schema 是 `trajectory.actions.v1`。一个典型序列为：

```text
trajectory_started
agent_start
message_action
message_action
tool_action
message_action
...
session_captured
artifact_registered
trajectory_finished
trajectory_sealed
```

记录只使用递增 `seq` 表达全局顺序，不保存 message hash、artifact SHA 或 journal
hash chain。

### 4.1 Message

- `message_start` 和 `message_update` 不落盘。
- 只在 `message_end` 后写一条 `message_action`。
- `message_action.payload.message` 保存 Pi 返回的完整 message，包括 role、content、
  model、usage、stop reason 和 tool-call content。
- 如果运行在 `message_end` 前中断，只写一条不含 partial content 的
  `action_interrupted`。

### 4.2 Tool

- `tool_execution_start` 暂存在内存，不单独落盘。
- `tool_execution_update` 不落盘。
- `tool_execution_end` 到达后，把工具名、完整参数、完整结果、成功或失败状态和
  耗时合并成一条 `tool_action`。
- 工具启动后进程中断，仍写一条 `status: interrupted` 的 `tool_action`，结果
  为空，并记录中断原因。
- 失败尝试和后续重试是不同的完整 action，不得合并。

### 4.3 Framework

以下内容不是 Pi message/tool action，但仍是一次执行不可缺少的边界：

- task、skill、runtime 和非敏感启动配置；
- skill 是否成功加载；
- agent 和 turn 生命周期标记；
- stderr、协议错误、进程退出、超时和 observer error；
- 输入、输出 artifact 的路径、存在状态和 bytes；
- 最终 outcome 和失败阶段。

## 5. Action Trajectory 与 Pi Session 的区别

二者的完整消息和工具内容会有重叠，但职责不同：

| | Action trajectory | Pi session |
| --- | --- | --- |
| 所有者 | Skill Evolution framework | Pi |
| 结构 | 一次执行的线性 action 序列 | Pi 的会话树和原生 entry |
| 使用者 | evaluator、attribution 和用户审阅 | runtime 调试与重新提取 |
| 额外内容 | 配置、runtime、失败、artifact、outcome | session header、父子关系、模型变化 |
| 启动失败 | 可以记录 | 通常没有 session |
| 是否为评估输入 | 是 | 默认不是 |

因此 session 是低成本的 Pi 原生 sidecar，不是第二套评估数据。session 缺少 message
不会把已经成功的 skill execution 改判为失败，但会显示在 session metadata 中。

## 6. 最新真实运行结果

2026-07-25 使用同一个文档可视化 skill、同一输入和 DeepSeek V4 Pro 运行当时的
action-level schema。以下路径与文件名按冻结历史证据原样保留：

```text
.skill-evolution/trajectories/20260725T063831Z-bbf1a500/
```

结果如下：

| 指标 | 原始 RPC 镜像 | Delta journal | Action trajectory |
| --- | ---: | ---: | ---: |
| 主记录文件 | 1,935,000,437 B | 31,891,482 B | 248,381 B |
| 主记录数 | 24,161 | 54,454 | 90 |
| 观察到的 `message_update` | 24,021 | 54,214 | 17,484 |
| 实际保存的流式 delta | 24,021 份累计镜像 | 54,214 | 0 |
| 完整 message action | 未标准化 | 只在 session | 31 |
| 完整 tool action | start/end 分离 | start/end 分离 | 15 |

Action trajectory 相对 delta journal 再减少 99.221%，约缩小 128.4 倍；相对最初
RPC 镜像减少 99.987%，约缩小 7,790 倍。

本次运行的其他结果：

- 运行成功，耗时 277 秒，收到 `agent_end` 和 `agent_settled`；
- `seq` 从 1 到 90 连续，没有 hash 字段；
- 31 条 message 包括 1 条 user、15 条 assistant 和 15 条 tool result；
- 15 条 tool action 中 12 条成功、3 条失败，失败和后续 action 均保留；
- Pi session 为 121,835 bytes，包含 31 条 message，没有无效 JSON 行；
- 输出 HTML 为 52,671 bytes，无外部依赖、无失效本地锚点，包含 13 个表格。

这些检查证明采集格式符合 action-level 定义。HTML 的结构检查不等于正式的 skill
质量评估；评估模型和问题归因仍属于下一阶段。

## 7. 文件结构与文档同步

```text
trajectories/<run-id>/
├── trajectory.jsonl
├── pi-session.jsonl
├── artifacts/
│   ├── input.md
│   ├── output.html
│   └── skill/
└── runtime/
    └── pi-session/
```

本次定义同步到：

- `scripts/trajectory_spike.py`；
- `tests/test_trajectory_spike.py`；
- `.memory/decisions/0010-action-level-trajectory.md`（历史名称）；
- `.memory/decisions/0021-trajectory-naming-and-legacy-read-compatibility.md`；
- `docs/trajectory-spike.md`；
- `docs/architecture-proposal.md`；
- `docs/pi-agent/rpc-client.md`；
- `.memory/current.md`、问题记录和 `.plan/next.md`。

## 快速复述

1. 初版重复保存累计 message，使一次运行产生约 1.94 GB。
2. Delta journal 降到约 31.9 MB，但仍保存五万多个 evaluator 不需要的流式事件。
3. 最终只保存完整 message、完整 tool action、framework 状态和 outcome。
4. 最新 trajectory 只有 90 条、248 KB；Pi session 只作为原生调试副本。
