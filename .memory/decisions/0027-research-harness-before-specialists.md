# 0027 — 先验证完整研究 Harness，再运行四个 Specialist

> Purpose: record the accepted raw-Trajectory research, sandbox, submission, and
> specialists-only boundary for multi-Trajectory work.

Status: Accepted
Date: 2026-08-14
Owners: project owner

## Context

只让 Agent 阅读 Harness 摘要会丢失跨 Trajectory 的隐式行为，例如多次执行在相同逻辑
阶段各自编写用途相近的脚本。反过来，在搜索、代码执行、研究协议或正式提交尚未完成时
先测试 Agent，又无法区分失败来自 Agent 能力还是 Harness 缺失。

项目负责人批准把原先的环境试验和 Harness 暴露阶段合并：先完整交付研究环境和协议，
通过确定性、安全及假 Runtime 验收后，最后用两个独立单 Agent 盲测证明研究能力；通过
后才允许四个 Specialist 运行。

## Decision

- 研究输入是同一 Revision 下封存、脱敏且完整可观察的 Trajectory、产物、任务映射和已接受
  单 Trajectory 分析。任何损坏、序号缺失、身份不一致或必需映射缺失都在模型调用前失败。
- Prompt 只接收有上限的确定性语料地图。完整导航索引和原 Trajectory 留在本地；索引只定位
  action、脚本、文件、失败、恢复、验证和资源事实，不预先生成语义模式。
- 研究 Agent 使用独立 Pi 进程和一次性 Docker 实验室。证据只读，研究工作区可写并可
  执行自编分析程序；容器无网络、无凭证、非 root、资源受限，且绝不回退宿主执行。
- 真实模型只在 Evidence、索引、Sandbox、研究协议和正式提交的确定性及安全门禁全部
  通过后运行。行为模式 Prompt 不泄露预期脚本模式；两个全新 session 都必须形成通过
  证据校验的跨 Trajectory 发现，才把 Harness 标记为 capability verified。
- 确定性 Harness 验收必须走真实 Pi 工具调用、搜索/查询/窗口/执行和提交状态机，只把
  模型替换为可信的无网络确定性驱动器；不仅验证工具成功状态，还核对每类查询返回的
  已知原证据及其关系。调用者不能提交自报检查结果或导入外部报告。
- Harness 通过和后续真实 Agent 必须计算同一份完整能力身份。该身份覆盖传递执行依赖、
  Pi 可执行文件与版本、有效启动参数、Prompt、工具、Sandbox、Docker 镜像和资源边界；
  任一项漂移都使既有 Harness 结果失效，不能用“相同语料”代替重新验收。
- 传递执行依赖指从研究入口可达的完整项目内 Python 闭包，以及 Pi 可执行文件、shebang 解释器、
  npm package 树与允许的附加参数。Docker 执行身份另外绑定客户端与解释器、context、
  effective endpoint、daemon engine ID 和稳定安全事实；Harness reload 与每次启动均重新核对。
- Pi 基础命令只能是一个直接、npm-package-bound 的可执行入口。wrapper、shell、`npm/npx`
  及 `python -m/-c` 不属于可认证入口；可执行文件、shebang 解释器和完整 package 树都进入
  同一身份。每次研究启动使用替换式 allowlist 环境以及一次性的 `HOME`、临时目录和
  Pi agent 目录，不继承宿主 credential、proxy、`NODE_*` 或任意 `PI_*` 配置。
- 固定 RPC 政策关闭 session file、project approval、内建工具、自动发现 extension、skill、
  context、prompt template 和 theme，并启用 offline 启动。真实分支只加载 tools/output
  两份绑定扩展；Harness faux 分支额外加载唯一固定 driver。两个分支都只开放同一组十个
  研究工具，使用 package-bound Pi 内建 system prompt，且不追加 system prompt。
- 真实 provider 每次 spawn 只接受所选 provider 的 literal API key，并通过只读临时文件
  描述符暴露；不复制完整 `auth.json`，也不执行 env/command/OAuth credential。Harness
  faux 分支没有凭证。真实模型只能来自已绑定 Pi package 的内建 catalog；faux 模型只能
  来自已验真的 driver extension。两种条件分支属于同一能力身份，faux 路径不放宽真实路径。
- 发送研究 Prompt 前必须精确核对实际 provider/model/thinking、无 session file、所选模型
  可用，以及唯一 active-tools attestation 与十工具 allowlist 完全相等。Harness context
  文件是负责人审批并绑定 prompt-visible 工具表面及摘要的 manifest，不作为 system 或
  append prompt 注入；Agent 实际收到批准的角色协议和有限动态语料地图。
- 确定性 Harness 的公开入口只能使用完整、与 execution identity 相等的 active sandbox
  context。Docker 命令、容器 ID、control plane、镜像、limits 和工具预算不得由调用者
  自由覆盖；budget/cleanup 验收所需的更小预算由 Runtime 按固定模式派生。
- 研究容器强制 `--log-driver none`，PID 1 回收子进程，每个生产工具返回前同步清理并证明
  残留进程为零。任一清理证明失败永久毒化当前 session，后续工具与提交均失败关闭。
  Harness 必须实际探测 PID 1 输出旁路，不只信任 Docker 声明的日志配置。
- 验收报告、审计包和 Agent 结果引用使用逐段禁止符号链接的目录句柄，并对同一文件句柄
  完成读取、摘要和解析。路径、inode 或可信根在操作中变化时不产生可接受结果。
- 两次盲测形成可移植但严格失效的 capability certificate：绑定代码、协议、Harness
  context、模型、Docker 镜像、资源限制、两个 run/session/结果和人工复核。完整研究
  批次只能从同一受信任工作流中的原始签发批次导入；任何边界变化都要求重新盲测，
  已导入证书不能链式转发。
- 四个活跃 Specialist 是 BehaviorPattern、ConditionsCoverage、OutcomeConsistency 和
  ResourceEfficiency。结果与可靠性由框架生成共同确定性基线。
- 四个 Specialist 首轮共享相同 Evidence、索引和基线摘要，但使用独立进程、session、
  实验室和 attempt，且不能读取其他角色结论。默认串行；并发验证后最多三路。
- Specialist 只能用新的 schema-validated terminating submission 提交正式结果。结果
  至少声明 eligible、affected 和 checked-unaffected Trajectory，逐 Trajectory 证据、共同阶段、
  目的、可观察效果、反例范围、派生分析和限制。重复模式至少跨两个不同 Trajectory。
- 新 workflow 只写执行批次内部的 append-only research batch 和 specialist board。
  Harness smoke、确定性基线及四份 Specialist 结果都不是产品多 Trajectory 报告，不写入
  `multi-trajectory-analyses`，也不生成用户报告。
- Specialist board 只保存和校验各角色结果，不综合、不投票、不裁决冲突。任何角色失败
  时 board 为 incomplete；重试创建新 attempt，历史失败不被覆盖。
- ConditionsCoverage 在已批准的独立 EvaluationSuite 和稳定
  Trajectory → TaskCase/conditions 映射存在前 fail closed。零样本 suite case 是 coverage
  gap，不是性能证据。
- 可比组名称只是操作者声明，不是可比性的证据。可靠性、一致性和资源效率只统计经框架
  核对为同一 Revision、任务语义、输入内容、模型、thinking、平台、运行参数及（存在时）
  EvaluationSuite 条件的执行。调用者显式选择 Suite 时，每条入选 Trajectory 都必须有
  TaskCase 映射，即使本次目标不含覆盖分析；事实缺失或不一致的同名组在模型调用前失败。

## Superseded boundaries

- 本决策取代 `0014` 中“固定三个 Specialist 后必须立即运行 Synthesis”的部分；独立
  Pi 进程、独立 session、文件审计和默认串行边界继续有效。
- 本决策补充 `0013`：确定性 Harness 提供统一事实与导航，但不替代 Agent 按需读取原
  Trajectory。
- 本决策不扩张 `0026` 的单 Trajectory 工具；多 Trajectory Specialist 使用独立工具、schema 和
  Python 跨字段门禁。
- `0025` 的产品边界保持不变：只有未来独立设计、严格校验并正式聚合的报告才计入多
  Trajectory 分析。

## Consequences

- 当前五条成功 Trajectory 可用于行为模式盲测，但缺少稳定 TaskCase/条件映射，不能用于
  声称阶段三的条件覆盖已经就绪。
- Docker、Prompt、EvaluationSuite 或任一 readiness 条件不可用时，研究显式停止，
  而不是降级为宿主工具、摘要分析或部分正式结论。
- 这一隔离边界针对不可信研究 Agent，不把已拥有宿主同 UID 写权限的外部进程当作可防御对手。
  若未来要阻止它在整个 Agent 窗口内替换后恢复证据，需要不同 OS 身份、ACL 或真正不可变快照。
- Synthesis、归因、可行性、改进建议、Candidate 和用户报告继续延期。

## Revisit when

四个 Specialist 在真实批次上稳定完成、结果冲突和证据重叠已经积累到足以设计聚合
规则时，再单独批准 Synthesis 与正式多 Trajectory 用户报告；不得把内部 board 直接升级为
产品结论。
