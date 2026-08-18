# 0023 — 运行数据采用 Skill-first 层级

> Purpose: record the accepted Skill → Execution ownership model, immutable
> Revision binding, analysis placement, migration boundary, and Viewer change.

Status: Accepted
Date: 2026-08-09
Owners: project owner

## Context

原目录和 Viewer 以 Replay Campaign、HarnessRun 和全局 AgentRun 为主要入口。同一 Skill
的一次执行、输入、输出、Trajectory 和分析分散在多个 workflow 根目录；Viewer 还需要按
`run_id` 全局扫描 AgentRun 来猜测关系。这种布局适合验证早期 workflow，却不能回答
“某个 Skill 有哪些版本、运行和分析”，也无法为数千 Skill 的未来依赖管理提供稳定引用。

项目负责人接受了 Skill 层级式架构，并明确界面采用 `Skill → Execution`；Revision
是强制数据绑定和筛选标签，不增加用户必须穿过的导航层。历史数据选择一次性迁移，不做
长期双写；实际移动前仍须审阅 dry-run 的精确清单。

## Decision

- `skill_contract.json` 中的 `skill_id` 是稳定 Skill 身份；Contract 不记录运行状态、
  Execution ID 或分析 ID。
- 完整 Skill package 按内容计算不可变 `skill.revision.v1`。任何 package 内容变化都会
  形成新 Revision，即使 Contract 的声明版本未变化。
- 每个 `skill.execution.v1` 必须引用一个 Revision。Input、Output 和 supporting artifact
  是 manifest 角色；封存 payload 保留运行时原相对结构。
- Replay Campaign 降为同一 Revision 的 `execution.set.v1`。新界面直接把 Execution
  放在 Skill 下，批次只作为标签和筛选条件。
- `analysis.record.v1` 把确定性或 Agent 分析挂到单个 Execution 或同 Revision 的
  Execution Set。分析 AgentRun 是内部 attempt，不进入分析 Skill 的普通 Execution 列表。
- 普通多 Trajectory 分析拒绝跨 Revision；baseline/candidate 跨版本实验只能作为显式
  Comparison。Candidate、Comparison 和人工 Review 归属于原 Skill 的 Improvements。
- `catalog.json` 与每个 Skill 的 `index.json` 是可删除、可重建的导航缓存；Revision、
  Execution、Execution Set、Analysis 和 Improvement manifest 才是权威对象。
- Viewer 定位为 Skill Explorer。新 API 以 `/api/skills/...` 为主；旧
  `/api/campaigns/...` 暂由 Execution Set 投影，保留只读、Host、CSP 和 artifact sandbox。
- 历史运行没有 Contract 时，迁移创建 `missing_at_execution` legacy Revision；禁止用后来
  批准的 Contract 伪装历史状态。
- 迁移先生成带全文件 SHA-256、正反向路径和未归属对象的 manifest。存在不确定归属时
  fail closed；应用需要精确 migration ID 确认，提交前复核 payload 摘要，失败则回滚。
- Dependency Graph 本轮不实现。稳定 ID、Revision 引用以及 Contract 中现有 tools、
  dependencies、assets 为未来图保留基础，但不发布空页面或 Contract v3。

## Alternatives considered

- 保持 Campaign-first，只给 Viewer 增加 Skill 筛选：无法消除分散 ownership 和全局猜测。
- 在 UI 中强制 `Skill → Revision → Execution`：数据严谨，但增加不必要的用户层级；版本
  更适合作为标签和筛选。
- 长期双写或双读：便于逐步迁移，但两个来源会漂移并使审计结果不唯一。
- 把运行列表写入 Contract：查询直接，但每次运行都会改变 Contract 内容并使审批失效。
- 把分析 AgentRun 登记为分析 Skill 的普通执行：会污染用户看到的业务运行列表，并把
  分析对象与分析实现混为一谈。

## Consequences

- 新 Capture、Replay、Harness、单/多 Trajectory analysis 和 improvement 服务只需持有稳定
  引用，不再自行拼接全局 workflow 路径。
- 历史 API 可以短期兼容，但正常新写入不再创建旧 workflow 根；旧 writer 只作为明确的
  compatibility 测试入口存在。
- 完整迁移必须处理全部旧对象。若产品验收只希望显示成功的五次运行，而扫描发现另一个
  五次失败批次，负责人必须明确选择“也迁移”或“归档排除”，框架不能静默删除。
- 现有单 Trajectory 五层报告保持 v1，不因目录变化改写其内容；无效模型输出继续只能显示
  确定性事实。
- 该决策部分取代决策 `0011` 的 Campaign 顶层身份、`0012` 的 Campaign-first Viewer
  导航，以及旧 workflow 目录作为正常运行布局的约定。它不改变其中的 prompt 审批、
  localhost 只读边界和 artifact sandbox。

## Revisit when

需要跨机器 Registry、数据库索引、Dependency Graph、远程 Viewer、多租户权限，或已证明
文件索引重建不能满足规模需求时，发布独立且兼容的架构决策；不要把这些能力塞入
`skill.contract.v2`。

## 2026-08-11 correction

Decision `0025` narrows the analysis placement defined here. Harness and other
execution-set checks are not multi-trajectory analyses. Real multi-trajectory analyses
use a dedicated store and remain at zero until that product capability is
implemented.
