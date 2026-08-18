# 0015 — Candidate Replay 必须 Fail Closed

Status: Accepted
Date: 2026-07-26
Owners: project owner

## Context

Candidate 是模型生成的可执行 skill 内容。自动 replay 可能调用 read、write、edit
和 bash；如果 Docker 或隔离配置失败后静默改用宿主机，candidate 将获得超出批准
范围的权限，并可能修改 active skill、fixture 或其他工作区数据。

Candidate 效果还需要新鲜 baseline 和独立 Judge，且自动门禁不能隐藏任何候选。

## Decision

- Host Pi 只负责模型通信；禁用内置工具，通过受信任的 Docker tool router 把
  read/write/edit/bash 路由到一次性容器。
- 容器只挂载本次 attempt 的 artifact workspace，默认无网络、只读根文件系统、
  drop capabilities，且不接收模型凭据。
- Production comparison 只接受可验证声明 backend、禁用内置工具并禁止 host
  fallback 的 `SandboxedPiReplayRunner`；普通 callback 会在状态迁移前被拒绝。
- Preflight 必须确认 Docker CLI、daemon 和本地 image；框架不隐式 pull。
- Preflight 或 sandbox 失败时 comparison/candidate 停在
  `awaiting_sandbox`，不允许宿主机 fallback。
- 默认 comparison 为 candidate smoke 1 次，加两个 TaskCase 上
  baseline/candidate 各 N=3，共 13 次；超出范围需负责人批准扩展 replay。
- Baseline/candidate 按 repetition 交替，并对所有 run 使用同版本 Harness。
- Smoke 使用单 run Harness；完整 comparison 使用一个冻结的 full batch 统一运行
  Profiler 与 Comparator，从而保留跨 variant 的 aggregate 和 pairwise。
- ReplayJudge 与 CandidateProposer 必须使用不同 AgentRun。
- Gate 只输出 improved、regressed、mixed、inconclusive 或 not_runnable，不使用
  单一加权总分。
- 正确性和能力覆盖是硬约束；全部 candidate、attempt、失败、diff 和结果都进入
  人工 ReviewPackage。

## Alternatives considered

- Docker 不可用时回退宿主机：提高完成率，但突破批准的安全边界。
- 在容器内放置模型 credential 并运行完整 Pi：隔离更完整，但扩大 credential
  暴露面，也增加 auth/session 管理。
- 复用诊断阶段五次 replay 作为 baseline：节省成本，但时间窗口和执行边界与
  candidate run 不一致。
- Gate 自动删除或拒绝 candidate：界面更简洁，但破坏审计性和人工最终决定。

## Consequences

- 没有可用 Docker 环境时，自动 replay 会显式等待，而不是“尽力执行”。
- 需要预先准备和版本化本地 sandbox image。
- Host Pi 仍是模型通信可信边界；容器隔离的是工具和 workspace，不是 provider
  请求。
- 13-run 默认计划有明确时间和费用成本，扩展必须单独审批。
- 无法运行或退化的 candidate 仍会占用存储并出现在审阅中，这是刻意的审计成本。

## Revisit when

当 VM、micro-VM 或受控远程 runner 能提供更强隔离且保持凭据边界时，可替换
Docker backend；任何替代方案仍必须 fail closed，不能提供宿主机 fallback。
