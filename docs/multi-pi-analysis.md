# 多 Trajectory 研究 Harness 与四 Specialist

> Purpose: explain the implemented Harness-first multi-Trajectory research flow,
> its launch gates, commands, evidence boundaries, and current approval state.

状态：研究环境、可执行 Harness 验收和内部编排已实现；当前批次尚未通过真实 Docker
验收，真实单 Agent 盲测也尚未获准运行；最终聚合与正式多 Trajectory 用户报告不在本阶段
范围内。  
更新日期：2026-08-14

## 1. 当前实现解决什么问题

多 Trajectory 研究不能只把若干单 Trajectory 总结塞进一个 Prompt。那会丢失临时脚本、重复
验证器、相似绕路等只有回到原 Trajectory 和产物才能发现的模式。当前实现保留完整、脱敏
后的可观察证据在本地，只把有限语料地图放进 Prompt，让 Agent 自己搜索、下钻、写
分析程序并核验证据。

系统按以下顺序工作：

1. 按本次研究目标检查样本、版本、Trajectory、单 Trajectory 报告和资源记录是否足够；
2. 冻结内容寻址的只读语料、导航索引和确定性基线；
3. 在无网络、受资源限制的 Docker 实验室中验证搜索、读取、分析代码和正式提交；
4. 用两个全新 session 做行为模式盲测，并由人工对照隐藏基准复核；
5. 把两次通过绑定为一份能力认证；
6. 只有完整六类研究都满足启动条件时，才运行四个相互隔离的 Specialist；
7. 保留四份结构化内部结果，不做综合、投票、归因、改进或用户报告。

任何一步失败都会停在该步。Harness 记录、盲测结果、确定性基线和 Specialist 结果板
都不是产品中的正式多 Trajectory Analysis；产品计数继续为零。

## 2. 六类分析由谁产生

| 研究内容 | 生产者 | 含义 |
| --- | --- | --- |
| 结果与可靠性 | 确定性基线 | 统一计算结果、成功率、失败、重试、耗时和 token，明确分母与缺失值。 |
| 重复问题模式 | 行为模式 Specialist | 回到原 Trajectory 查找至少两条 Trajectory 支持的重复问题和隐式行为。 |
| 恢复与成功模式 | 行为模式 Specialist | 比较失败、重试、恢复、验证及最终成功路径。 |
| 发生条件与覆盖 | 条件与覆盖 Specialist | 对照已批准 EvaluationSuite 判断发生条件、零样本区域和证据不足。 |
| 一致性 | 结果与过程一致性 Specialist | 比较同条件 Trajectory，并定位结果分叉前最早的可观察差异。 |
| 资源效率 | 资源效率 Specialist | 比较耗时、token、失败动作和返工集中阶段。 |

四个 Specialist 第一轮不能读取彼此的结果。当前只允许串行执行；并发必须另行通过
隔离与认证测试，不能靠命令参数直接打开。

## 3. 研究启动门槛

`assess` 只读检查，不创建语料或批次。目标不同，门槛也不同：

- 结果与可靠性、资源效率和一致性需要同一明确可比组中的至少两次执行。组名只是一项
  声明；框架还会核对 Revision、任务语义、输入内容摘要、模型、thinking、平台和稳定
  运行参数。调用者显式选择已批准的 EvaluationSuite 时，所有入选 Trajectory 都必须映射到
  TaskCase，Suite 条件也会参与比较，无论本次是否同时分析覆盖。事实缺失或任一事实
  不一致时，同名执行也不会被计入可比样本；
- 重复模式需要至少三条可检查 Trajectory，正式模式仍至少需要两条独立 Trajectory 支持；
- 恢复研究需要至少两条出现可观察恢复的 Trajectory；
- 条件与覆盖需要已批准的 EvaluationSuite、完整 TaskCase 映射和足够条件组；
- 所有执行必须属于同一 Skill Revision，并具有完整、连续、身份一致的 Trajectory；
- 资源统计只有在每次模型调用的 usage 字段完整时才算完整，不以零填补缺失值。

示例：

```bash
python3 scripts/multi_trajectory_research.py assess \
  --skill-id <skill-id> \
  --execution-id <execution-1> \
  --execution-id <execution-2> \
  --execution-id <execution-3> \
  --objective behavior_patterns
```

未达到门槛时输出 `not_ready` 和具体补采要求，退出码为 `3`。不会调用 Agent，也不会
创建正式研究结果。

## 4. 冻结语料与导航索引

确认 `ready` 后建立不可变语料：

```bash
python3 scripts/multi_trajectory_research.py build-corpus \
  --skill-id <skill-id> \
  --execution-id <execution-1> \
  --execution-id <execution-2> \
  --execution-id <execution-3> \
  --objective behavior_patterns \
  --destination .skill-evolution/internal-research/corpora/<corpus-name>
```

语料包含：

- 脱敏后的完整可观察 Trajectory，保留原始 `seq`；
- Task、冻结 Skill Revision、可安全读取的文本产物和已接受单 Trajectory 报告；
- 动作、工具、文件、脚本创建/修改/执行、失败、恢复、验证和资源事实索引；
- 结果与资源的确定性基线；
- 研究就绪结果，以及覆盖研究使用的 EvaluationSuite 和 Task/条件映射。

凭证、完整环境映射、隐藏推理、Pi session、二进制及敏感文件类型不会进入语料。
所有索引位置必须能回到原 Trajectory 或冻结产物；索引不提前标注“重复模式”或“最佳
实践”。任何文件删除、替换或摘要变化都会使后续读取失败。

然后创建内部批次：

```bash
python3 scripts/multi_trajectory_research.py prepare \
  --corpus-directory .skill-evolution/internal-research/corpora/<corpus-name> \
  --batch-id <batch-id>
```

## 5. 确定性 Harness 验收

Harness 验收必须由工作流自己执行；调用者不能提交七个布尔值或导入一份外部 JSON
冒充通过：

```bash
python3 scripts/multi_trajectory_research.py validate-harness \
  --batch-id <batch-id>
```

固定验收覆盖：语料完整性、导航与原证据往返、生产搜索/筛选/窗口/脚本/跨 Trajectory
工具、隔离实验室、资源预算和正式提交门禁。每类查询不仅要返回成功，还必须返回预先
从冻结语料确定的具体记录、原始位置和相互关系；空结果或答非所问不能通过。确定性驱动
器走与真实 Agent 相同的 Pi 工具调用路径，但不请求模型。报告绑定语料、基线、全部
Execution、完整传递执行依赖、Pi 可执行文件与版本、有效启动参数、Docker 镜像 ID、
实际隔离设置、命令/程序及受限审计输出。执行身份还包含完整的项目内 Python 依赖闭包、
Pi 可执行文件/解释器/npm 依赖树，以及 Docker 客户端/解释器、context、effective
endpoint、daemon engine ID 和稳定安全事实。

### 5.1 Pi 宿主侧启动隔离

Docker 内的分析程序无网络；宿主侧 Pi 进程仍需要在真实 provider 推理时访问 provider。
Pi 的固定 `--offline` 只禁止启动期在线发现和刷新，不能被解释成“真实模型推理不需要
网络”。为避免 Pi 在 Prompt 前从宿主环境获得未认证能力，每次研究启动还执行以下门禁：

- 基础命令只能是一个直接、npm-package-bound 的 Pi 可执行入口；wrapper、shell、
  `npm/npx` 和 `python -m/-c` 被拒绝。可执行文件、shebang 解释器和完整 package 树都
  进入 execution identity；
- 子进程使用替换式 allowlist 环境和一次性的 `HOME`、临时目录、Pi agent 目录，不继承
  credential、proxy、`NODE_*` 或任意宿主 `PI_*` 设置；
- 固定关闭 session file、project approval、内建工具以及自动发现的 extension、skill、
  context、prompt template 和 theme，只开放十个研究工具。真实分支加载 tools/output，
  Harness faux 分支额外加载唯一固定、无符号链接且摘要匹配的 driver；
- 真实 provider 每次 spawn 只读取所选 provider 的 literal API key，并通过只读临时文件
  描述符暴露。完整 `auth.json` 不进入隔离目录，env/command/OAuth 凭证不会执行；Harness
  faux provider 没有凭证；
- 真实模型只来自身份绑定的 Pi package 内建 catalog；Harness faux 模型只来自已验真的
  driver。Prompt 发送前精确核对 provider/model/thinking、无 session file、所选模型可用，
  以及唯一 active-tools attestation 与十工具 allowlist 完全一致；
- Pi 使用 package-bound 内建 system prompt，append system prompt 为空。
  `research-harness-context-v1.json` 只是负责人审批并绑定 prompt-visible 工具契约及摘要的
  manifest，不作为 system/append prompt 注入。Agent 看到的是批准的角色协议和有限动态
  语料地图。

确定性 Harness 的公开无模型入口也不能自行传入 Docker 路由。它必须使用完整 active
sandbox context，并把 backend、镜像、control plane、容器 ID、limits、Docker 命令和
工具预算与 execution identity 逐项核对；budget/cleanup 验收的更小预算只由 Runtime 按
固定模式派生。

验收报告使用严格 v2 契约。每个固定检查还包含不可省略的子检查；通过报告必须同时
保存生产扩展和驱动器快照、四种 Pi 协议运行记录、命令计划和实验工作区摘要。再次读取
批次时会重算报告、审计树、实现、Pi 身份、语料和基线摘要，不能只相信此前记录的
`passed`。真实 Agent 启动前还会重新计算同一执行身份，并在创建容器前再次核对
Docker context、endpoint 和 daemon。Harness 通过后发生的代码、Pi、Docker 或启动边界
变化会停止运行并要求重新验收。

实验室只读挂载证据，为每次运行提供一次性可写工作区，禁止网络、凭证环境、root、
宿主写入、路径与符号链接逃逸。分析程序只能在容器内运行；Docker 或本地镜像不可用
时失败关闭，不会回退到宿主执行，也不会自动拉取镜像。容器日志驱动必须为 `none`；
Harness 会尝试经 PID 1 输出绕过工具输出限额，并证明该内容不能从 Docker 日志读回。PID 1
持续回收子进程，每个生产工具返回前都同步清理并证明只剩 PID 1 与清理进程；一旦无法
证明残留为零，整个 Pi session 被永久标记为无效，后续工具和正式提交都被拒绝。

验收报告、审计 manifest/清单和 Specialist 结果引用都从逐段 `no-follow` 打开的同一文件句柄
中一次读取，摘要和解析不再二次打开路径。符号链接祖先、读取中 inode 替换、可信输出
根替换或审计目录重定向都会失败关闭。这些边界阻止不可信 Agent 修改宿主证据；它不宣称能阻止
一个已经拥有宿主同 UID 写权限的外部进程在整个 Agent 窗口内替换后再恢复证据。

## 6. 两次单 Agent 盲测与能力认证

先冻结与 Agent 证据隔离的隐藏基准：

```bash
python3 scripts/multi_trajectory_research.py freeze-benchmark \
  --batch-id <batch-id> \
  --benchmark-file validation-benchmarks/<benchmark>.json
```

然后运行一个完整验证周期：

```bash
python3 scripts/multi_trajectory_research.py run-smoke \
  --batch-id <batch-id>
```

一次周期固定运行两个独立 AgentRun、Pi 进程和 session。Agent 只能看到批准的行为研究
协议与有限语料地图，看不到隐藏基准。两次都必须通过正式结果门禁，引用至少两条原
Trajectory，并覆盖所有可检查 Trajectory 的反例搜索。

每次结果都需要人工复核：

```bash
python3 scripts/multi_trajectory_research.py review-smoke \
  --batch-id <batch-id> \
  --attempt-id <attempt-id> \
  --reviewer project-owner \
  --evidence pass \
  --protocol pass \
  --safety pass \
  --hidden-benchmark pass
```

失败会归类为证据不可达、协议不清、样本不足、结果门禁或 Agent 探索能力不足。修复后
必须运行新的完整双 session 周期，并准确声明修复覆盖的上一周期失败类别；不能挑选
单次成功结果。

两次复核都通过后显式签发能力认证：

```bash
python3 scripts/multi_trajectory_research.py issue-capability \
  --batch-id <smoke-batch-id>
```

认证绑定两次 run/session/结果/人工复核、隐藏基准、行为 Prompt、Harness context、
模型、不可变 Docker 镜像、资源限制、完整传递实现依赖和实际 Pi 身份。上述任一边界
变化，认证即失效，必须重新盲测。

## 7. 四 Specialist 阶段

完整六类研究通常使用另一份满足全部门槛的语料。目标批次先通过自己的 Harness，再从
同一受信任工作流中的原始签发批次导入能力认证：

```bash
python3 scripts/multi_trajectory_research.py import-capability \
  --batch-id <full-research-batch-id> \
  --source-batch-id <smoke-batch-id>
```

导入时会重新核对当前代码、Prompt、模型、镜像和资源限制。只允许从原始签发批次
导入，不能把一份已导入认证继续传递给第三个批次。

随后运行四个独立视角：

```bash
python3 scripts/multi_trajectory_research.py run-specialists \
  --batch-id <full-research-batch-id>
```

读取结果板：

```bash
python3 scripts/multi_trajectory_research.py board \
  --batch-id <full-research-batch-id>
```

一个角色失败不会删除其他角色的结果，但批次只能是“不完整”。失败角色用新 attempt
重试，旧记录不会覆盖：

```bash
python3 scripts/multi_trajectory_research.py retry-specialist \
  --batch-id <full-research-batch-id> \
  --role <role>
```

## 8. 正式结果门禁

每项发现都必须声明完整适用集合、实际出现集合、已检查但未出现集合、共同逻辑阶段、
共同目的、可观察效果、原始证据位置、反例搜索、派生程序和限制。行为重复模式至少由
两条 Trajectory 支持；一致性结论至少比较两条 Trajectory。所有适用 Trajectory 必须被归为“出现”
或“检查后未出现”，不能用较小分母夸大模式。

普通聊天文字、代码围栏 JSON、虚假引用、未知派生程序、单 Trajectory 冒充重复模式或
提交后的继续操作都不会进入结果板。结果文件及其引用都带摘要；删除、替换或篡改会在
再次读取时失败。

## 9. 当前真实运行状态

当前五条同 Revision 成功 Trajectory 已通过行为研究就绪检查，并冻结为内部 smoke 语料。
但以下边界仍阻止真实模型运行：

- 当前内部批次仍为 `prepared`。Docker daemon 可访问，但默认研究镜像
  `python:3.11-slim` 不在本地；系统没有拉取它，也没有生成通过的真实 Harness 报告；
- 四份 Specialist Prompt 和 Harness context 的 approval sidecar 仍为 `proposed`；
- 完整研究使用的 EvaluationSuite 仍为 `proposed`，旧五条 Trajectory 也缺少完整 TaskCase
  映射，不能证明条件与覆盖；
- 两次真实盲测、能力认证和四 Specialist 都尚未运行。

当前研究相关回归 183/183 通过。全仓上一次允许 loopback 的完整基线为 382/382；本轮
扩展后的 395 项中，388 项在严格默认沙箱通过，余下 7 项只因本地 HTTP 测试无权绑定
loopback 而在 socket setup 被阻断。真实 Pi 0.81.1 已在不请求模型的情况下完成 exact
state、所选模型可用性和 active-tools attestation 启动检查。这些证据证明启动与失败
关闭契约，不等同于当前语料已经取得真实 Docker Harness 通过记录。因而不能宣称单
Agent 研究能力已经
通过，也不能启动四 Specialist。正式多 Trajectory Analysis 数量仍为零。

本阶段明确不包含最终聚合、冲突裁决、归因、可行性判断、Skill 改进、Candidate、
Synthesis 或正式用户报告。
