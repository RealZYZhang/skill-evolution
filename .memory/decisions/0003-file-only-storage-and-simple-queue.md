# 0003 — MVP 只使用文件存储和简单 queue

Status: Accepted
Date: 2026-07-24
Owners: project owner

## Context

MVP 需要优先降低实现和部署复杂度。SQLite、migration、lease、transactional
outbox 和外部消息队列会在早期引入超出当前规模的基础设施。

## Decision

所有持久化对象使用文件目录、版本化 JSON manifest、JSONL 和普通 artifact 文件。
不使用 SQLite，也不实现全局内容寻址去重。Manifest 可以保存 hash 用于完整性
校验。

异步 workflow 暂时使用标准库 `queue.Queue` 或 `asyncio.Queue`。Queue 只负责
当前进程调度对象 ID，不是权威状态。进程重启后通过扫描未完成 manifest 重建 queue。

MVP 默认单 orchestrator 写入。Manifest 使用临时文件和原子 rename 更新。

## Alternatives considered

- SQLite 元数据 + 文件产物：查询和事务更强，但增加 schema、migration 和双存储
  一致性。
- 外部消息队列：跨进程恢复能力更好，但增加部署和运维复杂度。
- 只使用内存 queue 且不保存 manifest：最简单，但进程退出后无法判断未完成工作。

## Consequences

- 数据可以直接查看、复制和制作测试 fixture。
- MVP 没有数据库服务和 migration。
- 复杂查询、跨对象事务、多机并发和高吞吐能力较弱。
- Queue 丢失时需要扫描文件恢复。
- 文件 schema、原子写入和单写者约束必须明确。

## Revisit when

当文件扫描、关系查询、并发写入或多进程调度成为实际瓶颈时，再评估 SQLite、
服务型数据库或持久化消息队列。
