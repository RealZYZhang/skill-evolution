# 0008 — Pi 默认使用 DeepSeek V4 Pro

Status: Accepted
Date: 2026-07-24
Owners: project owner

## Context

P0 需要一个真实模型执行指定 skill。项目负责人已在 Pi 用户配置中设置
DeepSeek V4 Pro，并明确授权当前示例文档通过 DeepSeek API 执行，同时要求后续
对话默认使用该 API，除非特别指出。

## Decision

Pi 运行默认使用 provider `deepseek`、model `deepseek-v4-pro`。任务没有特别说明
时沿用该选择；项目负责人可以对单次任务覆盖 provider、model 或禁止外部调用。

认证信息只保留在 Pi 用户配置中，不写入仓库、命令行、trajectory、文档或 memory。

## Alternatives considered

- 每次运行都重新确认 provider/model：披露最明确，但会阻碍连续实验。
- 把 API key 注入项目配置或命令行：自动化更直接，但会进入日志、进程列表或版本
  控制，风险不可接受。
- 只使用本地模型：数据边界更小，但当前尚未配置可用的本地模型。

## Consequences

- 后续 Pi 实验具有稳定的默认 runtime。
- 发送给 Pi 的任务内容可能传输至 DeepSeek API；单次任务如需不同边界，项目
  负责人必须特别指出。
- trajectory 只记录非敏感模型元数据，不记录 credential。

## Revisit when

更换默认模型、引入本地模型、调整外部数据披露边界或增加多模型 replay 时。
