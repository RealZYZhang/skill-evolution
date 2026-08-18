# 0012 — 使用只读 localhost Web 应用检查 Replay Trajectory

Status: Accepted
Date: 2026-07-26
Owners: project owner

## Context

首个真实 replay campaign 已产生五条 action-level trajectory。直接阅读 JSONL
可以核对单条记录，但难以同时比较 run 数量差异、工具失败与恢复、耗时、artifact
以及每次执行实际使用的 prompt、Skill 和 runtime setup。

现有计划中的 P5 原本是静态流程图。项目负责人进一步明确，本阶段所需
visualization 是对真实 trajectory 和 agent setup 的 HTML 可视化，并选择本地
Web 应用、campaign 总览后下钻、完整已采集 setup，以及安全 artifact 预览。

## Decision

- P5 实现为只读 localhost Web 应用，替换原静态流程图交付。
- 服务使用 Python 标准库，只绑定 `127.0.0.1`；前端使用原生
  HTML、CSS 和 JavaScript，无外部依赖或构建步骤。
- 数据读取和规范化独立于 HTTP adapter，也不导入 Pi RPC adapter。
- 扫描多个 replay campaign，先展示事实对比，再下钻单条 action timeline。
- 展示 prompt、approval、Skill、input、model、thinking、tools 和 runtime。
- Pi session 只展示 metadata 和下载入口，不作为默认评估数据解析。
- 不产生评分、归因、排序、candidate 或任何源数据写入。
- Agent HTML 只能在无权限 iframe 和 CSP sandbox 中预览；原文件可下载。
- 损坏 campaign、失败 trajectory、非法 JSON、未知 record 和 setup 差异必须
  可见，不能静默过滤。

## Alternatives considered

- 单个自包含静态 HTML：易归档，但无法自然扫描新 campaign，且大数据会重复嵌入。
- 每条 trajectory 一个 HTML：实现简单，但不利于 campaign 级比较。
- React 或第三方 Web framework：组件能力更强，但违背 dependency-free MVP，
  也增加构建与供应链边界。
- 直接同源执行 agent artifact：保留交互，但会让未审查脚本访问 viewer API，
  不符合安全评估要求。

## Consequences

- 用户可以在不修改 trajectory 的情况下检查全部 run、setup 和失败尝试。
- HTTP API 成为新的只读公共接口，需要 schema、路径和错误兼容测试。
- 页面加载时按需读取文件；当前数据量无需数据库、文件 watcher 或缓存。
- 保存内容仍可能包含敏感任务或工具数据，因此只能本机使用并受本地访问边界保护。
- Artifact 预览中的脚本和外部资源不会运行；需要完整交互时必须下载后由用户明确
  决定如何打开。

## Revisit when

需要跨主机共享、身份认证、多个 trajectory 根目录、实时文件监听、超大 campaign
虚拟化，或用户明确要求解析 Pi session 树时。
