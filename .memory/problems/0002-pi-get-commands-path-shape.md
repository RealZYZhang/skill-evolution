# 0002 — Pi get_commands 的 skill 路径字段与文档示例不同

Status: Resolved
First observed: 2026-07-24

## Symptom

真实 Pi 已列出目标 skill，但 P0 采集器将 `skill_loaded` 判为 `false`。

## Reproduction

使用 Pi 0.81.1，以 `--skill <directory>` 启动 RPC，再调用 `get_commands`。

## Diagnosis

vendored RPC 文档示例把命令路径表示为顶层 `path`。真实响应把本地 skill
路径表示为 `sourceInfo.path`，顶层没有 `path`。

证据保存在
`.skill-evolution/spikes/20260724T103702Z-a9240830/pi-rpc.jsonl`。

## Workaround

读取 `path`，不存在时再读取 `sourceInfo.path`。

## Resolution

`scripts/trajectory_spike.py` 已兼容两种形态，fake RPC 测试使用真实形态覆盖。
