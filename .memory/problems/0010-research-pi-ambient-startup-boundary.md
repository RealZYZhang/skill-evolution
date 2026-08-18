# 0010 — 研究 Pi 启动会继承未绑定的宿主能力

> Purpose: preserve the resolved diagnosis and verification of ambient Pi
> launcher, configuration, credential, extension, and Docker-router bypasses.

Status: Resolved
Date: 2026-08-14

## Symptom

早期研究 Runtime 虽然把 Prompt、研究扩展和 Docker 实验室纳入能力身份，但 Pi 子进程
仍可能继承宿主环境、全局 Pi 配置和完整认证目录。通用 wrapper 或二次解释器也可能使
记录的启动身份与实际加载的 package 不一致。确定性 Harness 的公开入口还曾允许调用者
替换无模型 driver 或直接传入 Docker 命令和工具预算。

## Impact

这些入口会使一份通过的 Harness 无法证明后续 Agent 使用了同一模型、工具、凭证来源和
容器路由。最严重的公开入口绕过可把研究工具的 Docker 路由改成任意宿主可执行文件，
因此在问题解决前不能信任 capability certificate，也不能运行真实研究 Agent。

## Diagnosis

- RPC client 默认合并 `os.environ`，研究 Runtime 未显式使用替换环境；
- Pi 默认目录可以发现全局 settings、extensions、skills、context 和 model override；
- 完整 `auth.json` 会把未选择 provider 的 command/env/OAuth 凭证带入子进程；
- 启动身份只看表面命令时，wrapper、shebang 解释器和 npm 依赖树不会全部被绑定；
- Harness 简表中的实现摘要曾未与完整 execution fingerprint 逐项交叉核对；
- 无模型公开入口曾接受调用者提供的 extension 与 Docker 工具环境。

## Resolution

- 研究启动只接受一个直接、npm-package-bound 的 Pi 入口，并绑定可执行文件、shebang
  解释器和完整 package 依赖树；wrapper、`npm/npx`、shell 及 `python -m/-c` 被拒绝；
- 研究进程使用替换式 allowlist 环境、一次性 `HOME`、临时目录和独立 Pi agent 目录；
- 固定关闭自动发现资源、内建工具、project approval 和 session 文件，只加载经身份
  绑定的研究扩展与十个研究工具；
- 真实 provider 每次启动只读取所选 provider 的 literal API key，通过只读临时文件
  描述符暴露；Harness faux provider 不接收凭证；
- Prompt 前验证实际 provider/model/thinking、无 session file、模型可用性和唯一的
  active-tools attestation；
- Harness faux driver 只能来自固定仓库路径、无符号链接且摘要匹配的实现；
- 无模型入口只接受完整 sandbox context，并把 backend、镜像、control plane、容器 ID、
  limits、Docker 命令和工具预算与 execution identity 逐项相等；测试模式的收紧预算由
  Runtime 内部固定派生；
- Harness 报告中的 validator、Runtime、tools、output 和 driver 摘要必须与完整执行
  fingerprint 中相同文件的摘要一致。

## Verification

单元与假集成测试覆盖环境替换、定向文件描述符、启动 wrapper、package 漂移、非所选
凭证、全局配置、模型与工具 attestation、extension 路径/符号链接/内容替换，以及 Docker
命令、容器 ID、limits 和预算篡改。真实 Pi 0.81.1 的不调用模型启动检查确认选定模型、
thinking、无 session file 和固定工具集合；这不等同于真实 Docker Harness 已通过。

## Remaining boundary

这些门禁把不可信 Trajectory 和研究 Agent 限制在批准的研究能力内。它们不把已经拥有宿主
同 UID 代码执行权的外部进程视为可防御对手；该威胁需要不同 OS 身份、ACL 或不可变
快照。
