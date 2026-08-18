# 0018 — 根配置与计划工作区

Status: Accepted
Date: 2026-08-04
Owners: project owner

## Context

Pi 默认 provider、model 和 thinking mode 曾在运行时类中重复硬编码。根目录还保留
一份面向未来的框架设想，使 README 和 current-state 误将一个不存在的 `plan.md`
作为当前工作入口。项目需要可审计的项目级配置，以及能合并多个计划来源的明确工作区。

## Decision

- 根目录 `config.yaml` 使用 `skill-evolution.config.v1`，保存唯一的非敏感 Pi
  默认 provider、model 与 thinking mode。`PiAgentRuntime` 和
  `SandboxedPiReplayRunner` 未收到显式 model 时读取该文件。
- API key 和其他 credential 继续只由 Pi 用户配置管理，禁止写入项目配置、命令、
  trajectory、EvidenceBundle 或 memory。
- `.plan/next.md` 是唯一当前且有优先级的计划；`.plan/` 内其他文件仅保存提案或
  来源材料。出现多个计划时，开始 major development 前必须合并、排序并记录延后项。
- 根目录只保留 `AGENTS.md` 和 `README.md` 两份 Markdown 入口。未来架构设想迁至
  `.plan/future-framework.md` 并标记为 Proposed。
- `docs/file-catalog.md` 是项目文件用途与目录结构的权威索引；major development
  结束时必须与 README、当前计划和 current memory 一并更新。

## Alternatives considered

- 保留硬编码模型默认值：简单，但项目配置不能作为可审计的来源。
- 将 API key 放入 YAML：调用直观，但会扩大泄漏风险，不能接受。
- 把所有计划直接堆在 README 或 root：方便发现，但会混淆当前状态与未接受设计。

## Consequences

- 更换默认 Pi 模型只需审阅和修改 `config.yaml`，运行时默认值随之更新。
- 有意的测试或单次运行仍可传入显式 `ModelConfiguration`，其值会记录进相应
  manifest。
- 计划数量增加时需要一次显式的合并与优先级判断，避免相互冲突的来源静默并存。

## Revisit when

项目引入第二类共享运行时默认值、需要配置 profile，或计划工作流需要外部 issue
tracker 集成时。
