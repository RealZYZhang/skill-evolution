# 研究隔离沙箱（Docker Research Laboratory）组件指南
> Purpose: explain, for developers and owners, every component inside one isolated
> multi-Trajectory research run — host-side Pi, the Docker laboratory, the ten
> research tools, resource budgets, evidence safety, and execution identity.

本文面向想理解"研究批次的一次研究运行（AgentRun）到底在什么环境里跑、里面有哪些组件、各自干什么"的开发者与负责人。它不逐行讲代码，而是把隔离环境拆成组件清单，说明每个组件的职责、约束和容易误解的地方。所有名称与当前仓库一致（仓库已完成 trace→trajectory 全局改名，本文只使用 trajectory）。

## 1. 一句话概览

一个研究运行不是一个进程，而是一个"宿主侧 Pi 进程 + 一个一次性 Docker 容器 + 十个工具扩展 + 一套身份/预算/证据闸门"的组合：

- **宿主侧**：Pi 进程持有模型凭证，负责推理、决定调用哪些研究工具、以及最终提交结果。它拥有网络（真实 provider 推理需要），但被严格隔离，不能从宿主环境继承任何未认证能力。
- **容器侧**：所有研究工具的执行都发生在无网络的 Docker 容器里。容器只有只读的冻结证据（/evidence）和一个一次性的、受配额限制的可写工作区（/work），非 root，无法写宿主，无法逃逸。
- **连接两者**：十个研究工具（九个体现在 extensions/research-tools.ts，一个提交工具在 extensions/research-output.ts）由 Pi 扩展进程调用 `docker exec` 路由进容器；每次调用都被资源预算闸门（budgetedDocker）和"残留进程证明 + cleanup failure 毒化 session"纪律包裹。
- **信任底座**：execution identity 把代码、Pi 可执行树、Docker 身份、镜像、工具哈希、资源限制全部绑定为一份内容寻址摘要；Harness 验收（faux provider 驱动）与真实运行走同一条工具路径，任何边界变化都会让后续运行失败关闭。

## 2. 分层总览：宿主侧 vs 容器侧

| 层面 | 进程/组件 | 位置 | 职责 | 有无网络 |
| --- | --- | --- | --- | --- |
| 宿主侧 | 研究运行时 `ResearchPiAgentRuntime`（skill_evolution/research_agent_runtime.py） | 宿主 | preflight、执行身份校验、渲染 Prompt、驱动 Pi 到 settled、验收提交 | — |
| 宿主侧 | Pi 进程（通过 `PiRpcClient` 以 JSONL RPC 启动） | 宿主 | 真实推理、工具决策、RPC 事件流；被 `--no-builtin-tools --tools <十工具>` 约束 | 有（真实 provider 推理） |
| 宿主侧 | 研究工具扩展 `research-tools.ts` / `research-output.ts`（加载进 Pi） | 宿主进程内 | 定义十个工具的 schema；把每次调用经预算闸门路由为 `docker exec`；每次返回前证明容器无残留进程 | — |
| 容器侧 | 一次性 Docker 容器（镜像 python:3.11-slim） | Docker | 无网络执行分析程序；只读挂载 /evidence；tmpfs /work；非 root；PID 1 回收 | 无（--network none，仅 lo） |
| 容器侧 | PID 1 Python 回收循环 `_RESEARCH_INIT` | 容器 | `waitpid(-1,0)` 循环回收子进程，防止僵尸 | — |
| 连接 | Docker control plane 身份（client/daemon/context/镜像 ID） | 宿主 | 每次启动前核对 Docker CLI 可执行文件、daemon、context、镜像 ID 未变 | — |
| 验收 | 确定性 Harness（faux provider/model + driver 扩展） | 宿主 | 无模型驱动生产工具走完整路径，证明隔离、预算、残留清理、提交门禁 | 无凭证 |

## 3. 组件清单（每个组件的用途）

### 组件 1：宿主侧研究运行时（ResearchPiAgentRuntime）

一次研究运行的"总导演"。它负责：

- **preflight**：核对两个扩展文件存在、Harness context（research-harness-context-v1.json）已审批且其绑定的 research-tools.ts / research-output.ts 哈希与磁盘一致、Prompt 已审批且无未解析占位符、沙箱可用（backend 必须是 `docker_research_lab` 且带镜像 ID 与控制平面身份）、真实 provider 的凭证可校验。
- **执行身份门禁**：每次运行重新计算 execution identity 摘要，必须与批次里"已通过的 Harness"摘要完全一致（见组件 11）。
- **Prompt 组装**：把已批准的静态协议 + 有界的语料导航地图（corpus map，上限 256 KiB）渲染成最终 Prompt；地图被 `<untrusted-research-corpus-map>` 边界包裹并做 JSON 转义，防止不可信内容注入。
- **驱动 Pi**：用替换式环境 spawn Pi，做启动 attestation（模型/session/十工具），发 Prompt，消费 RPC 事件直到 `agent_settled`，然后执行严格提交验收（见组件 7）。
- **审计**：把 RPC 记录、stderr、工具事件写入 trajectory.jsonl，脱敏隐藏推理字段与凭证。

注意：研究 Specialist 拿不到宿主可写工作区（`candidate_workspace` 传入即拒绝）——它只能写容器内的 /work。

### 组件 2：DockerResearchSandbox（研究沙箱核心）

skill_evolution/research_sandbox.py 中的 `DockerResearchSandbox`，backend 名为 `docker_research_lab`。职责：

- **preflight**：Docker CLI 存在 → 采集 control plane 身份（见组件 11）→ `docker image inspect` 确认 `python:3.11-slim` 已在本地。**绝不自动拉取镜像**（--pull never），缺镜像直接返回不可用并失败关闭，且 `host_fallback_allowed = False`——永远不会回退到宿主执行。
- **isolated_run 上下文管理器**：启动一个容器，产出 sandbox context（backend、container_id、镜像、limits、tool_environment 等），并在退出时"封存"（seal）：暂停容器 → `docker cp` 导出 /work 快照 → 校验证据没变 → 把快照原子替换到 `research/work` → `docker rm --force` 删掉一次性容器。
- **工具环境**：把容器 ID、Docker 命令、各预算值以 `SKILL_EVOLUTION_*` 环境变量交给工具扩展（`validate_research_sandbox_context` 会逐项核对后才能用）。

### 组件 3：Docker 容器本身

一次研究运行 = 一个一次性容器。`docker run` 的完整约束：

| 维度 | 设置 | 含义 |
| --- | --- | --- |
| 镜像 | `python:3.11-slim`（以本地镜像 ID 启动，--pull never） | 固定、不可变、已审批 |
| 网络 | `--network none` | 无外部网络；容器内只有 lo 接口（验收用 /sys/class/net == [lo] 证明） |
| 根文件系统 | `--read-only` | 根只读，容器不能往根写（宿主写拒绝） |
| 用户 | `--user 65534:65534`（nobody） | 非 root |
| 能力 | `--cap-drop ALL --security-opt no-new-privileges` | 丢弃全部 Linux 能力 |
| 资源 | `--pids-limit 128 --memory 1g --cpus 1 --ulimit nofile=1024:1024` | 进程数/内存/CPU/打开文件数上限 |
| 临时区 | `--tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=64m` | 每次运行一次性 tmpfs，不可执行 |
| 工作区 | `--tmpfs /work:rw,nosuid,nodev,mode=1777,size=64m` | 一次性可写工作区（见组件 5） |
| 证据 | `--mount type=bind,src=<evidence>,dst=/evidence,readonly` | 只读证据（见组件 4） |
| PID 1 | `python3 -c _RESEARCH_INIT` | Python 回收循环：`waitpid(-1,0)` 循环，回收所有子进程、防僵尸 |
| 日志 | `--log-driver none` | 容器日志被禁用；Harness 会尝试经 PID 1 输出绕过工具输出限额并证明这些内容无法从 `docker logs` 读回 |
| 工作目录 | `--workdir /work` | 工具默认在 /work 下执行 |
| 停止 | `--stop-timeout 2` | 强制删除时快速终止 |

### 组件 4：/evidence —— 只读证据挂载与内容寻址

- 挂载点 `/evidence` 以 `readonly` 方式绑定宿主侧的冻结语料目录（脱敏后的 trajectory、导航索引、确定性基线、产物等）。
- 内容寻址：`research_evidence_tree_digest`（TreeDigest = sha256 + 文件数 + 目录数 + 总字节）在多个时点计算并比对：语料复制进 run workspace 后 → 容器启动前（期望摘要）→ 容器刚启动后（挂载中途变化即失败并删除容器）→ Agent 启动前 → 封存时。任何一处不一致都失败关闭。
- 摘要算法用**逐组件 O_NOFOLLOW 打开目录句柄**（不跟随任何符号链接），只哈希普通文件与目录，遇到符号链接/特殊文件直接拒绝，读文件前后核对 (dev, ino) 与 size/mtime/ctime 防止读取中被替换（TOCTOU）。
- 容器内的工具代码同样拒绝 /evidence 下的符号链接和逃逸（见组件 6）。

### 组件 5：/work —— 一次性可写工作区与封存

- `/work` 是 tmpfs（64 MiB 配额），容器内可写；分析程序、中间产物都写在这里。
- 工作区不落宿主磁盘，直到运行结束封存：`docker pause` 冻结容器 → `docker cp container:/work/.` 到宿主 staging → 对 staging 做 tree digest 并核对不超过 work 配额 → 再次核对证据树未变 → `os.replace(staging, research/work)` 原子落盘 → `docker rm --force` 删除容器。
- 因此 Agent 的脚本、中间结果会作为 `research/work` 归档保留，但宿主侧运行时（运行时与 Specialist 本身）从不直接写这个工作区。

### 组件 6：十个研究工具（allowlist 与路由）

研究 Pi 只加载 `RESEARCH_PI_TOOL_ALLOWLIST` 里的十个工具（`--no-builtin-tools` + `--tools` 白名单 + `--no-extensions` + 显式 `--extension`）：

| 工具 | 文件 | 用途 | 关键参数/限制 |
| --- | --- | --- | --- |
| research_list | research-tools.ts | 列出 /evidence 下某目录一页条目 | path/cursor/limit（页上限 200）；遇到符号链接或特殊文件即失败 |
| research_read | research-tools.ts | 按行读 /evidence 里一个普通文件 | offset/limit（单次最多 1000 行）；单行截断 20k 字符 |
| research_search | research-tools.ts | 在 /evidence 下做字面大小写不敏感搜索 | query/path/cursor/limit；跳过二进制文件并报告跳过数；单条匹配截断 4k 字符 |
| research_query | research-tools.ts | 对导航索引（navigation-index.json）做类型化过滤 | collection: entries/scripts；where 支持 eq/ne/contains/in/exists/gte/lte；select 投影；无原始 SQL |
| research_trajectory_window | research-tools.ts | 回到原 trajectory，围绕 run_id+seq 读动作窗口 | before/after 不超过 20；大记录截断为 50k 字符预览 + sha256 |
| research_work_read | research-tools.ts | 读 /work 里工作文件的行 | 同 research_read 的边界 |
| research_work_write | research-tools.ts | 原子写 /work 文件 | 临时文件 + fsync + rename；content 不超过 1M 字符 |
| research_work_edit | research-tools.ts | 在 /work 文件里做唯一精确替换 | old_text 必须唯一出现；不超过 200k 字符 |
| research_exec | research-tools.ts | 在容器里执行 shell/Python 分析程序 | command 不超过 20k 字符；返回 {stdout, stderr, exit_code, timed_out, aborted, output_limit_exceeded} JSON；工具调用 ID 即 derivation ID |
| submit_multi_trajectory_research | research-output.ts | 唯一允许的终止性提交工具 | 见组件 7 |

**路由方式**：九个操作类工具都走同一个 `helper` → `budgetedDocker`（预算闸门，见组件 8）→ `runDocker`：`spawn(docker exec -i --workdir /work <container> timeout --signal=TERM --kill-after=2s <N>s python3 -c PYTHON_HELPER <operation>)`，参数以 JSON 从 stdin 传入，容器内完成路径约束、符号链接拒绝与结果序列化。`research_exec` 类似但直接跑 `sh -c`。每个工具返回前都执行残留进程清理与证明（见组件 8）。`submit_multi_trajectory_research` 不经过容器，是宿主侧扩展内的终止工具。

### 组件 7：提交工具与"提交后终止"纪律

`submit_multi_trajectory_research` 是研究运行唯一允许的正式出口（output contract = `validated_tool_submission`）：

- **严格 schema**：TypeBox 校验完整参数形状（schema `analysis.multi_trajectory_research.v1`、role、corpus/baseline digest、research_scope、findings、limitations、evidence refs、derivation_ids），`additionalProperties: false`。
- **终止语义**：工具返回 `terminate: true`；扩展内部 `submitted` 标志 + Pi 终止机制共同保证"提交后不再有任何工具调用或 assistant 回复"。
- **运行时二次验收**（`_validate_submission`）：提交尝试必须恰好一次且成功；成功提交后不得再有其他动作（post_submission_actions 非空即拒绝）；提交必须是其 assistant 工具批次里的唯一工具；字节数不超过 2 MiB；`validate_research_result` 检查 role 归属、语料/基线摘要、**全量 eligible 分母**（reviewed 必须覆盖所有 eligible、行为重复模式至少两条 trajectory 支持、每条 finding 的证据都必须来自原 trajectory 且含反例搜索与派生程序 ID 审计）；`validate_research_result_evidence` 逐条把 EvidenceRef 解析回冻结证据束。
- **对比**：单 trajectory 流程有独立的 `submit_trajectory_error_analysis`（extensions/trajectory-error-output.ts）——只接受一条 run，schema 完全不同；二者不可混用。

### 组件 8：资源预算、超时终止、残留进程证明与毒化 session

- **容器级限制**（ResearchSandboxLimits，随容器创建强制）：cpus 1、memory 1g、pids 128、nofile 1024、work/tmp 各 64 MiB。
- **工具级累计预算**（扩展进程内 budgetedDocker 维护）：单次命令超时 120s、单次输出上限 256 KiB、工具调用总数上限 256、**并发工具调用数恒为 1**（串行，这是"每次调用后证明清理干净"的隔离前提）、累计输出上限 16 MiB、累计命令时间上限 30 分钟。任一超限，下一次调用在扩展层直接抛错（如 "Research tool-call budget exhausted"）。
- **超时终止**：容器内用 `timeout --signal=TERM --kill-after=2s` 包裹，超时后 TERM、2 秒后 SIGKILL；扩展侧另有兜底定时器。
- **残留进程证明**：每次 `docker exec` 结束（close 事件）后，扩展再跑一个 `docker exec python3 -c PROCESS_CLEANUP`：它枚举 /proc，把除 PID 1 和它自己之外的所有进程 SIGKILL，循环最多 5 秒，直到快照恰好是 ["pid1","cleanup"]、observed=2、residual=0，否则抛错。这样超时/输出超限命令的后台进程不能在本工具返回后再写文件或继续执行。
- **cleanup failure 毒化 session**：一旦清理证明失败，扩展调用 `poisonSession`：设置 `SKILL_EVOLUTION_RESEARCH_SESSION_POISONED=1` 并向 Pi 追加 `research-session-poisoned` 条目。此后**所有**工具调用都被拒（session 被永久标记无效），提交工具也会拒绝；运行时在事件流里读到该条目会把结果判为失败。这不是"这次调用失败"，而是整个 session 作废。
- **验收证明**：Harness 的 resource_limits 检查用更小预算（budget 模式把工具数降到 2；cleanup 模式把命令超时降到 1s、输出降到 4 KiB）驱动同一套生产工具，验证第三调用被拒、超时命令的延迟写文件不存在、输出超限进程树被同步清理。

### 组件 9：证据安全（no-follow 句柄、内容寻址、前后验证、逃逸拒绝）

- **宿主侧**：证据摘要、Harness 报告读取都用**描述符相对 I/O + O_NOFOLLOW**：`_open_absolute_directory`/`_open_directory_chain` 逐组件打开真实目录，符号链接祖先、读取中 inode 替换、可信输出根替换、审计目录重定向全部失败关闭；报告原子写（O_EXCL 临时文件 + dir_fd rename + fsync）且写入前后核对目标身份。
- **容器侧**：工具路径约束拒绝绝对路径、反斜杠、`..` 父级遍历；对 /evidence 与 /work 都做 realpath 约束 + 逐组件符号链接拒绝（`reject_link_components`）。
- **边界声明**：这些机制阻止不可信 Agent 修改宿主证据；文档明确不宣称能阻止一个已拥有宿主同 UID 写权限的外部进程在 Agent 窗口内替换后再恢复证据。

### 组件 10：Pi 宿主侧启动隔离

Docker 内无网络，但宿主侧 Pi 在真实 provider 推理时需要网络。`--offline` 只禁止启动期在线发现/刷新，不等于"不需要网络"。为避免 Pi 从宿主环境获得未认证能力，每次研究启动执行：

- **一次性 HOME/tmp**（`_isolated_pi_configuration`）：临时目录里建 HOME（0500）、TMPDIR（0700）、Pi agent 目录（0700）；结束后清理。工作区不得含项目本地 Pi 配置（`.pi`）。
- **凭证描述符**：完整 `auth.json` 绝不进入隔离目录；真实分支只读取所选 provider 的 literal API key，写进一个 0400 的临时文件，以 `auth.json` 符号链接指向 `/dev/fd/N` 并通过 `pass_fds` 传给 Pi 子进程。faux（Harness）分支没有凭证。
- **替换式环境**：子进程环境只含 allowlist 的 `SKILL_EVOLUTION_*` 研究变量 + HOME/LANG/LC_ALL/PI_CODING_AGENT_DIR/TMPDIR + 被认证解释器固定的 PATH（`replace_environment=True`）。不继承宿主 credential、proxy、`NODE_*` 或任意 `PI_*`。
- **Pi 参数**：`--no-builtin-tools --tools <十工具> --no-extensions --extension research-tools.ts --extension research-output.ts --no-prompt-templates --no-skills --no-context-files --no-themes --offline --provider/--model/--thinking`；extra args 只允许 `--verbose`。faux 分支额外加载唯一固定、无符号链接、摘要匹配的 driver 扩展（research-harness-driver.ts）。
- **Prompt 前 attestation**（`_drive_pi` 内，先于 prompt）：`get_state`（provider/model/thinking 与策略一致、sessionFile 必须为空、记录 sessionId）→ `get_available_models`（所选模型恰好一个）→ `get_entries`（恰好一条 `research-runtime-attestation` 自定义条目，其 active_tools 必须与十工具 allowlist 完全一致）。
- **faux vs 真实分支**：faux provider `research-harness-faux` / model `research-harness-driver-v1` / thinking off、无凭证、模型只来自已验真 driver；真实分支模型只来自身份绑定的 Pi package 内建 catalog，凭证只来自只读描述符。
- **系统 Prompt**：使用 package-bound 内建 system prompt，append 为空；`research-harness-context-v1.json` 只是负责人审批并绑定 prompt-visible 工具契约与摘要的 manifest，不注入为 prompt。

### 组件 11：execution identity（执行身份）

把"下一次运行的确切边界"认证为一份可复算的摘要（`build_research_execution_identity` → `research_execution_identity_digest`）：

- **实现指纹**：固定的 RESEARCH_IMPLEMENTATION_FILES（含三个研究扩展）逐文件 sha256，并用 AST 计算项目内 Python 依赖闭包，遗漏任何一阶依赖即拒绝。
- **Pi 可执行树**：Pi 基础命令必须是**单一、直接、npm-package-bound 的可执行入口**（wrapper、shell、npm/npx、python -m/-c 全拒）；可执行文件、命令文件、shebang 解释器、完整 package 树（含绑定符号链接根）、版本、extra args、RPC 策略都进入身份。
- **Docker 身份**：Docker CLI 可执行文件与解释器、context（前后一致）、client/server 版本、daemon ID/security options/rootless 一致性、effective endpoint，以及不可变镜像 ID（sha256:…）。
- **工具链**：harness_context_sha256、research_tools_sha256、research_output_sha256 与实现指纹互相交叉校验。
- **何时重校验**：Harness 验收时；每个真实 Agent 运行前（摘要必须等于批次通过值，否则 "Research execution differs from the batch's passed Harness"）；Pi spawn 前立即 `verify_pi_execution_identity_current` 重哈希可执行文件；能力导入时重核对代码/Prompt/模型/镜像/限制。**Harness 通过后任何代码、Pi、Docker 或启动边界变化都会停止运行并要求重新验收。**

### 组件 12：能力认证绑定哪些组件

能力认证（research.capability_identity.v3 / certificate.v1）把以下全部绑成一体，任一边界变化即失效、必须重新盲测：

实现指纹 + 已审批行为研究 Prompt（id/version/content_sha256）+ Harness context（版本/tool_schema_version/工具哈希）+ Pi 执行身份 + 模型（provider/model/thinking）+ 沙箱（backend/image/image_id/limits/control_plane）。认证由两次独立 smoke（各用全新 session）加人工复核与隐藏基准签发，只能从原始签发批次导入，不能链式转授。

### 组件 13：确定性 Harness 验收（研究运行的"预演"）

`validate-harness` 是研究运行的确定性预演，走与真实 Agent 完全相同的 Pi 工具调用路径但不请求模型（faux provider + driver 扩展）：

- **7 项固定检查**（HARNESS_CHECKS，每项含不可省略子检查）：corpus_preflight、navigation_index、evidence_roundtrip、fake_agent_research_loop、sandbox_isolation、resource_limits、structured_submission。空结果或答非所问不能通过。
- **隔离探针**：容器内脚本证明 /sys/class/net 只有 lo、/evidence 可读不可写、根只读、/work 符号链接不能逃逸进 /evidence、环境与挂载无凭证、非 root；再用 `docker inspect` 核对活动配置与声明的 limits 逐项一致；`_verify_disabled_container_logs` 证明 PID 1 输出无法从 docker logs 读回。
- **五模式驱动**：positive（完整正向链）、budget（预算耗尽拒绝）、cleanup（超时/输出超限后无延迟写）、duplicate_submission（重复提交拒绝）、post_submission（提交后动作拒绝）。
- 验收报告绑定语料/基线/Pi 身份/镜像 ID/隔离设置/审计输出，全部通过 no-follow 句柄读取；再次读取批次时重算，不信任此前记录的 passed。

### 组件 14：容易混淆的"邻居"组件（不属于研究隔离环境）

- **extensions/docker-tool-router.ts**：旧的自动 candidate replay（skill_evolution/sandbox_replay.py）用的 Docker 路由扩展——把 read/write/edit/bash 路由进一个挂 /workspace 的容器；与研究沙箱不是同一套（研究用 research-tools.ts 的十个工具）。
- **extensions/root-jail.ts**：单 trajectory 分析/候选流程的宿主侧根目录限制扩展（harness_list/read/search + candidate_read/write/edit），不是 Docker，也不是研究隔离。
- **extensions/trajectory-error-output.ts**：单 trajectory 语义错误报告的终止提交工具，schema 与多 trajectory 提交完全不同，仅作对比。

## 4. 一次研究运行的时序

| 阶段 | 动作 |
| --- | --- |
| 1 | preflight：审批、扩展存在、沙箱可用（backend/镜像 ID/控制平面） |
| 2 | 重算 execution identity 摘要并与批次已通过 Harness 比对 |
| 3 | 验证并复制语料进 run workspace/evidence，计算证据树摘要 |
| 4 | isolated_run：docker run 启动容器（全部隔离参数），挂载后重校验证据 |
| 5 | validate_research_sandbox_context 产出工具环境（8 个 SKILL_EVOLUTION_* 变量） |
| 6 | 一次性 HOME/tmp + 凭证描述符（真实分支）→ 替换式环境 spawn Pi |
| 7 | get_state / get_available_models / get_entries 三项 attestation |
| 8 | 发 Prompt，消费事件直到 agent_settled（内含每次工具调用的预算与清理证明） |
| 9 | _validate_submission：唯一一次成功提交、无提交后动作、结果与证据验收 |
| 10 | 封存：docker pause → cp /work → 摘要核对 → 原子落盘 research/work → docker rm --force |
| 11 | 写 research/sandbox.json、session-identity.json、trajectory.jsonl 审计 |

## 5. 值得注意的实现要点（易误解处）

1. **"无网络"是 --network none，仅剩 lo**；宿主侧 Pi 仍有网络用于真实推理——所以真正的风险面在"Pi 从宿主继承未认证能力"，由一次性 HOME/tmp、替换式环境、凭证描述符和三项 attestation 封死，而不是靠给 Pi 断网。
2. **工具并发恒为 1（串行）不是性能选择**，而是"每次调用返回前必须证明容器只剩 PID 1 + 清理进程"这一隔离纪律的前提；并发会破坏清理证明。
3. **cleanup failure 是毒化整个 session**，不是仅失败一次调用：`SKILL_EVOLUTION_RESEARCH_SESSION_POISONED` 一旦置位，后续所有工具和提交都被拒绝——这是防止"超时命令的后台进程在工具返回后再写文件/继续执行"的兜底。
4. **镜像不会自动拉取**（--pull never + preflight image inspect），且 `host_fallback_allowed=False`：Docker 或镜像不可用就直接失败关闭，绝不回退到宿主执行——这是把"分析程序只能在容器内运行"落实为硬边界。
5. **日志驱动 none 堵住侧信道**：PID 1 可以把输出写到 stdout，但 Harness 专门证明这些内容无法从 `docker logs` 读回，防止绕过工具输出限额。
6. **证据只读是双层的**：容器侧 bind readonly + 工具路径拒绝，宿主侧还有 no-follow 句柄与内容寻址前后验证（多个时点的证据树摘要），防 TOCTOU 与符号链接逃逸；文档明确不宣称能防拥有宿主同 UID 写权限的外部进程。
7. **research-harness-context-v1.json 不是 prompt**：它是绑定 prompt-visible 工具契约与哈希的审批 manifest，不注入为 system/append prompt；Pi 用 package-bound 内建 system prompt。
8. **execution identity 是"变化即停"的**：Harness 通过后代码/Pi/Docker/启动边界任何变化都会让后续真实运行失败并要求重新验收；能力认证同样绑定这些组件，任一边界变化认证即失效。

## 6. 参考文件

| 文件 | 内容 |
| --- | --- |
| skill_evolution/research_sandbox.py | DockerResearchSandbox、limits、证据树摘要、isolated_run 生命周期 |
| skill_evolution/research_agent_runtime.py | RESEARCH_LAB_PROFILE、十工具 allowlist、Pi 启动隔离、attestation、提交验收 |
| skill_evolution/research_capability.py | execution/capability identity、Pi 可执行树、凭证策略 |
| skill_evolution/research_harness_acceptance.py | HARNESS_CHECKS/SUBCHECKS、隔离与预算探针、报告读取 |
| extensions/research-tools.ts | 九个研究工具 + 预算/清理/毒化逻辑 |
| extensions/research-output.ts | submit_multi_trajectory_research 与 attestation 条目 |
| extensions/research-harness-driver.ts | 确定性 faux provider/driver |
| docs/multi-pi-analysis.md | 第 5 节确定性 Harness 验收与 5.1 Pi 宿主侧启动隔离 |
| docs/harness.md | 确定性 Harness 的既有说明 |

