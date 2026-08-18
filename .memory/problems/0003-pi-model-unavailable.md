# 0003 — 受限环境中 Pi 没有可用于 P0 的模型

Status: Resolved
First observed: 2026-07-24

## Symptom

目标 skill 注册成功，但 `prompt` RPC 返回 `success: false`：

```text
No API key found for the selected model.
```

`get_state` 中的 provider 和 model 均为 `unknown`，`get_available_models`
返回空列表。Pi 同时报告无法创建全局 settings lock。

## Reproduction

运行 `docs/trajectory-spike.md` 中的真实样本命令。

## Diagnosis

受限运行环境无法写 `~/.pi/agent` 的 settings lock。在该环境中，Pi 没有选中且
通过认证的可用模型，因此在 agent event、tool call 或 session message 产生之前
拒绝 prompt。采集器、skill 注册和 RPC 通信均已成功。

尚不能断言本机沙箱外的 Pi 也没有可用认证。沙箱外运行可能读取本机凭据，并把
标记为内部规划材料的示例文档发送给外部模型提供方，因此未获得明确数据披露授权
前不能执行。

失败证据保存在
`.skill-evolution/spikes/20260724T103732Z-c581b561/`。

## Workaround

优先配置本地模型后使用同一命令重跑。若要使用外部模型，需要项目负责人明确确认
provider/model，并授权将本示例文档发送给该提供方。不要把 API key 写入命令、
trajectory 或 memory 文件。

## Resolution

项目负责人确认使用 DeepSeek API，并授权当前示例文档。沙箱外 RPC 成功识别
`deepseek/deepseek-v4-pro`。

成功执行证据保存在
`.skill-evolution/spikes/20260724T115551Z-7b474577/`。

## Recurrence on 2026-07-25

首次五次 replay 在受限环境中再次触发相同表象。Pi 用户配置中的 DeepSeek
credential 和默认模型均存在，但 Pi 读取认证时需要在 `~/.pi/agent/` 创建锁；
受限环境禁止该写入，认证存储将锁失败降级为空状态，因此五次 prompt 都被拒绝为
`No API key found`。

失败 campaign 保存在
`.skill-evolution/replays/20260725T154718Z-b7d0c5f9/`，包含五条失败
trajectory。经项目负责人授权，使用可读取并锁定 Pi 用户认证且可访问 DeepSeek
API 的执行边界重跑，五次全部成功，保存在
`.skill-evolution/replays/20260725T154836Z-9aacc0cb/`。

当前 workaround 是真实 Pi replay 必须在该授权边界内运行。不要把 credential
复制到仓库、命令行、trajectory 或环境日志中。
