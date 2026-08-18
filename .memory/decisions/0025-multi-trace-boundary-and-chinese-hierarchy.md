# 0025 — 多 trajectory 分析独立存放，界面采用中文层级导航

> Purpose: record the accepted product boundary for real multi-trajectory analysis
> and the owner-facing Chinese Skill → trajectory → analysis navigation.

Status: Accepted and implemented; historical source deletion pending expanded approval
Date: 2026-08-11
Owners: project owner

## Context

旧界面把 Harness 确定性检查和三次未完成的多角色尝试都称为“多 trajectory 分析”，导致页面
显示七条尚不存在的分析结论。界面同时混用大量中英文、在执行卡片中展示完整任务要求，
并直接暴露 Skill 版本和执行批次的内部编号，项目负责人难以理解。

## Decision

- 多 trajectory 分析是一种独立产品结果，不等同于 Harness、批次检查或多角色流程尝试。
- 只有明确标记为多 trajectory 分析、属于同一 Skill 版本、通过严格校验并写入独立位置的结果
  才能进入多 trajectory 页面和统计。
- Harness 与其他批次级检查归属于对应执行批次，不能生成或伪造多 trajectory 用户报告。
- 当前多 trajectory 能力尚未实现，因此独立位置保持为空，页面明确显示 0 和“尚未实现”。
- 页面以中文为主，只保留 Skill、trajectory 等常用 AI 词。所有状态标签使用中文。
- 左侧导航采用 `Skill → trajectory → 单 trajectory 分析`。Skill 是保留统计信息的可展开卡片，
  二级菜单不是卡片。
- 执行卡片只显示摘要；完整任务要求必须单独展开。Skill 版本、执行批次等概念使用直白
  名称，并通过鼠标悬停或键盘聚焦的问号说明其含义。

## Historical correction

七条旧记录已经从多 trajectory 的产品读取路径和统计中移除。它们包括四条 Harness 检查和
三次未完成的旧多角色尝试。运行目录中的永久删除尚未发生：此前明确批准只覆盖四条隔离
记录，安全门禁要求在删除全部七条前取得覆盖完整清单的新授权。

## Consequences

- 当前可信数量是十一条执行、十份单 trajectory 分析、零份多 trajectory 分析。
- 未来实现多 trajectory 分析时可以增加报告内容和评估指标，但不能再次复用 Harness 结果
  充当分析。
- 内部编号仍可用于审计和链接，但不是用户理解版本或批次的主要名称。
- 该决策修正 `0023` 和 `0024` 中把七条旧记录描述为可展示多 trajectory 分析的部分，不改变
  已迁移执行和十份单 trajectory 分析的有效性。
