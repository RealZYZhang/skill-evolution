# 0021 — 统一使用 Trajectory 命名并只读兼容历史数据

> Purpose: define the canonical trajectory vocabulary and the boundary for reading
> historical data created under the former name.

Status: Accepted
Date: 2026-08-07
Owners: project owner

## Context

项目的代码、schema、文件名和文档长期混用 `trajectory` 与 `trajectory`。二者实际都指向
同一个 action-level 执行证据对象，双重命名增加了接口、脚本和分析输出的理解成本。
项目负责人要求以后统一称为 trajectory。

仓库同时保留了已经完成的 replay、EvidenceBundle、Harness profile 和决策记录。直接
改写这些历史对象会破坏 manifest 路径、内容哈希和审计链，不能把术语迁移变成证据
迁移。

## Decision

- 当前代码、类型、命令、目录、文档和测试统一使用 Trajectory。
- 新写入的 action schema 为 `trajectory.actions.v1`，文件名为 `trajectory.jsonl`，边界记录为
  `trajectory_started`、`trajectory_finished` 和 `trajectory_sealed`。
- 新写入的 profile schema 为 `trajectory.profile.v1`；脚本、viewer 资源和 Python 公共名称
  使用 `trajectory_*` 或 `Trajectory*`。
- reader 只读兼容历史 action/profile schema、边界记录、文件名、目录名和分析请求
  类型。兼容层在返回当前 projection 时保留 `source_schema` 或 `source_format`，不得
  隐藏来源。
- writer 不再产生旧名称，也不同时写两套文件。
- 已冻结的 runtime evidence、历史 decision/problem 和第三方 Pi 文档不批量改写。
  维护文档引用它们时必须明确标为历史名称。

## Alternatives considered

- 原地重写全部历史文件和 manifest：表面完全统一，但会改变证据内容和引用关系。
- 永久双写新旧文件：兼容简单，但会制造两个权威来源并扩大不一致风险。
- 只改用户文档：变更最小，但代码、schema 和持久化接口仍继续分裂。

## Consequences

- 所有新接口只有一套 Trajectory 词汇，旧名称只存在于兼容 reader、兼容测试和冻结历史
  证据中。
- 历史 doc-to-HTML replay 无需迁移即可由当前 precheck、viewer、profiler 和
  EvidenceRef 读取。
- 删除旧 reader 需要单独的迁移决策和可验证的历史数据升级，不随普通重构进行。

## Revisit when

所有需要保留的历史证据都完成可验证、可回滚的离线迁移，且没有 manifest、hash 或
外部引用继续依赖旧文件名和 schema 时，可以考虑删除 legacy reader。
