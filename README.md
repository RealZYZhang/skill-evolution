# Skill Evolution

> Purpose: introduce the project and show every supported way to run it.

Skill Evolution 是一个从 agent execution trajectory 中迭代 skill 的 MVP
框架。长期方向是先由确定性 Harness 记录可审计事实，再让相互独立的 Pi agent 分析
证据，并在另行批准后进入补测、Candidate、隔离 replay 和人工发布审阅。当前的多
Trajectory 分析是错误中心（error-centric）的产品能力：主 agent 识别所有错误，每个
错误由一个子 agent 从行为/条件/一致性/资源四个维度分析并只报告有问题的维度。

## 当前状态

- Skill-first 层级核心已实现：`Skill → 执行 → 输入 / 输出 / trajectory /
  单 trajectory 分析`。每次执行绑定不可变 Skill 版本；Replay 只作为执行批次。
  真实历史数据已在一次可核验切换中迁入，并由显式切换记录确定新层级是唯一权威来源。
  未来新执行的 Revision 一致性和 Improvement 既有契约复用仍是下一阶段工作。
- Action-level Trajectory、N-run replay、Skill Explorer 界面、TaskCase、Profiler、
  Artifact Comparator 和 EvidenceBundle 已实现。新写入使用
  `.skill-evolution/skills/<skill-id>/...`；旧 workflow 根只供迁移兼容。
- Package-local thin Skill Contract 和 `skill.validation_report.v1` 已正式启用；当前
  文档可视化 Skill 在 `SKILL.md` 同级包含已批准的 `skill_contract.json`。Contract
  只绑定 identity/approval、runtime 和 EvaluationSuite 引用，不复制或提取 Skill
  语义。
- 单 trajectory 分析已拆成无模型 `trajectory.precheck.v1` 和仅负责语义/因果判断的
  TrajectoryErrorAnalyst，并封装为 `analyze-single-trajectory` Skill。Prompt 与
  package-local contract 已批准；脱敏单-run EvidenceBundle、独立 AgentRun 和经过
  JSON 字段验证的结构化提交入口已实现。
- 当前代码、schema、命令和新运行数据统一使用 `trajectory` 命名。0021 期间冻结的
  `trace.jsonl` / `trace.actions.v1` 等旧名称仍以原始内容保留，当前 reader 会只读
  兼容并显式标记 legacy 来源；新 writer 不再产生旧名称，也不双写。
- 已对全部 11 个历史 doc-to-HTML trajectory 完成确定性检查和正式语义分析，所有基础检查
  均有效，11 份最新语义报告均通过结构、signal、证据、因果和一致性门禁。一条报告的
  首次提交因“无需修改 Skill”与“给出 Skill 修改方案”相互矛盾而被拒绝；该失败记录
  保留，重新分析后通过。
- 单 trajectory 分析现在会额外生成一个五层 `user-report.json`，依次呈现结论、关键经过、
  已验证问题、证据和下一步。Skill 管理器可从执行直接展开，且无效语义输出不会显示
  未经验证的归因或 Skill 修改建议。
- Skill 管理器以中文为主。左侧按 `Skill → trajectory → 单 trajectory 分析` 展开；执行卡片只显示
  任务摘要，完整任务 prompt 单独展开；Skill 版本、执行批次等概念带有问号说明，所有状态
  标签均显示中文。11 份已接纳语义报告都有独立的人工审阅中文呈现；原始报告继续保留，
  页面只在来源内容未变化时优先展示对应中文版本。
- 多 trajectory 分析已重构为错误中心的产品能力（决策 0031/0032）：主识别 agent 从冻结语料
  找出所有影响 skill 可复用性与可靠性的错误，每个错误由一个子 agent 从行为/条件/一致性/
  资源四个维度重新推导并只报告有问题的维度；错误清单与每错误报告面向用户。旧的“盲测→
  能力证书→四 Specialist”内部研究链已作为 legacy 移出产品主路径。
- 确定性研究 Harness 已升级为可执行的 v2 验收：可信的无模型驱动器通过 Pi 0.81.1
  的正常 Agent 循环调用生产搜索、读取、结构化查询、脚本执行和正式提交工具；验收还会
  核对每类查询返回的具体原证据，从实际容器读取网络、只读根、用户、资源、
  无宿主日志和进程清理边界，并封存命令、程序与 Trajectory 审计包。每次启动都会重新核对
  完整项目内 Python 依赖、Pi 可执行文件、解释器与 npm 依赖树，以及 Docker 客户端、
  context、endpoint 和 daemon engine 身份。任一边界漂移都会在模型请求前停止；调用者
  不能用自报布尔值或外部报告绕过这些检查。
- 研究 Pi 的宿主侧启动也被完整封闭。基础命令只能是一个直接、package-bound 的 Pi
  可执行入口；wrapper、`npm/npx`、shell 和 `python -m/-c` 会被拒绝。每次启动替换宿主
  环境，使用一次性的 `HOME`、临时目录和 Pi 配置目录；关闭 session、project approval、
  内建工具以及自动发现的 extension、skill、context、prompt template 和 theme，只加载
  身份绑定的扩展与十个研究工具。真实分支只通过只读临时文件描述符暴露所选 provider
  的 literal API key，Harness faux 分支无凭证；真实模型来自已绑定 Pi package，faux
  模型只来自已验真的固定 driver。Prompt 发送前还会核对实际 model/thinking、无 session
  file、模型可用性和唯一的 active-tools attestation。
- 研究容器的 PID 1 会回收子进程，每个工具返回前必须证明残留进程为零；一次清理无法证明
  就会使整个 session 失效，包括禁止正式提交。Docker 日志驱动被强制关闭，Harness 会实际
  尝试绕过工具输出限制写入 PID 1，并证明宿主无可读日志。验收报告、审计包和研究结果
  都从逐段禁止符号链接的已锁定文件中一次读取；读取中替换文件或祖先路径会失败关闭。
- 可靠性、一致性和资源效率中的“可比组”不能只靠同名标签成立。框架会核对任务语义、
  输入内容、模型、thinking、平台和运行参数。调用者显式选择已批准的 EvaluationSuite
  时，即使本次目标不含覆盖分析，每条 Trajectory 也必须存在 TaskCase 映射，Suite 条件会参与
  比较；缺失或不一致的执行在模型调用前被判为不可比。
- 当前五条同 Revision 成功 Trajectory 已通过行为模式研究门槛，冻结语料
  `corpus-8d046a5ca7b1a110f37a` 及其内部批次均保持不可变。批次仍是 `prepared`：
  本地 Docker daemon 可用，但默认研究镜像 `python:3.11-slim` 尚未预装，框架按设计
  不会自动拉取，因此尚无通过的真实 Docker Harness 报告，也没有启动研究 Agent。
- 新的研究 Harness、四 Specialist、现有 Candidate、Docker fail-closed comparison、
  ReplayJudge 和人工 Review 都有不调用真实模型的测试。研究 AgentRun 和内部结果板不会
  作为普通 Execution 或正式 Analysis 出现在产品层级。新层级的 Improvements 原型目前
  使用了平行 schema，尚未复用已接受链路，因此不能用于生产数据。
- 历史五次 replay 的确定性 Harness 已完成；当前受限环境中的 Chrome 截图为
  `partial`。
- execution prompt v2、要求模型直接生成中文结论的单 trajectory prompt v3、四份新研究
  Prompt、研究 Harness context 和完整研究 EvaluationSuite 仍为 `proposed`。因此新
  Harness 实现已经就绪，但当前环境不能完成真实 Docker 验收，也不能运行真实盲测或
  四 Specialist。当前 11 条正式单 trajectory 分析继续使用已批准的 v2；中文页面由独立、
  来源绑定且不改写原报告的审阅流程生成。Harness context 是审批并绑定工具表面与摘要
  的 manifest，不会作为 system/append prompt 注入；Agent 收到的是批准的角色协议和
  有限动态语料地图。当前研究相关回归 183/183 通过；全仓上一次允许 loopback 的基线为
  382/382，本轮扩展后的 395 项中 388 项在严格默认沙箱通过，余下 7 项只因本地 HTTP
  测试无权绑定 loopback 而在 socket setup 被阻断。真实 Pi 0.81.1 已完成不调用模型的
  state、模型可用性和固定工具 attestation 启动检查。

## 前置条件与配置

- Python 3.11 或更新版本；MVP 运行和测试只使用标准库。
- 真实 Pi workflow 还需要安装与
  [Pi 参考版本](docs/pi-agent/README.md) 兼容的 Pi，并在 Pi 的用户配置中完成
  API key 认证。研究 Runtime 当前只接受所选 provider 的 literal API key；env、command、
  OAuth 或混合 provider 凭证不会进入隔离研究进程。
- 多 Trajectory 研究需要 Docker daemon 和预先安装的、摘要可核对的研究镜像。框架不会
  自动下载镜像；Docker 或镜像不可用时会停止，不会在宿主机执行分析代码。
- 根目录 [config.yaml](config.yaml) 是 provider、model 与 thinking mode 的唯一
  项目级默认来源。当前默认是 `deepseek/deepseek-v4-pro`、`thinking: off`；不得把
  API key 写入此文件、命令行、运行数据或仓库。

正常新代码把运行数据写入 `.skill-evolution/skills/`。Revision、Execution、Execution Set、
Analysis 和 Improvement manifest 是权威状态；`catalog.json` 与 Skill `index.json`
可随时重建。不要把 credential、隐藏模型 reasoning 或全局 Pi auth 文件写入 Trajectory、
EvidenceBundle 或 sandbox。

当前真实 runtime 已完成一次性历史迁移。页面包含 1 个 Skill、2 个 Skill 版本、11 次
执行、31 份单 trajectory 分析记录和 0 份多 trajectory 分析。31 份记录包括 11 份通过的基础检查、
11 份通过的正式语义分析、8 份被拒绝的语义输出，以及 1 份在模型启动前失败的分析。
历史执行时没有 Skill Contract 的版本与当前已批准版本分开显示；被拒绝的语义尝试保留原状态。七条曾被误分类为
多 trajectory 分析的旧记录已从产品读取路径中移除，永久删除仍等待覆盖全部七条的明确授权。

## 运行项目

先运行完整的、不会调用真实模型的测试：

```bash
python3 -m unittest discover -s tests -v
```

在运行任何真实模型工作前，检查 Skill contract、package 和 TaskCase 覆盖：

```bash
python3 scripts/skill_contract.py \
  --skill skills/document-html-visualizer-skill \
  --task-case task-cases/document-formats/markdown.json \
  --task-case task-cases/document-formats/text.json \
  --task-case task-cases/document-formats/docx.json \
  --task-case task-cases/document-formats/pdf.json \
  --task-case task-cases/document-formats/inline-text.json
```

在允许 loopback socket 的环境启动本地 Skill Explorer：

```bash
python3 scripts/trajectory_viewer.py
```

页面默认读取 `.skill-evolution` 下已经切换完成的 Skill 层级。旧 Campaign API
由执行批次投影。所有页面读取均不重建或改写运行数据；导航索引只由写入、迁移
或显式维护动作更新。页面地址会保存 Skill、执行、页签和 trajectory 位置。执行概览不展示
原始记录；Markdown/文本输入可安全预览；trajectory 默认展示固定规则选出的关键步骤，也可
切换到全部步骤。动作参数、返回和长消息均按需展开。

对同一 Revision 的 Execution Set 运行确定性 Harness（不调用模型）：

```bash
python3 scripts/harness.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-set <execution-set-id>
```

对冻结语料运行错误中心的多 Trajectory 分析（主识别 agent + 每错误一个子 agent，真实模型调用）：

```bash
python3 scripts/error_analysis.py \
  --pi-command /path/to/pi run \
  --corpus-directory .skill-evolution/internal-research/corpora/<corpus-name>
```

产出错误清单（`analysis.error_identification.v1`）与每错误报告（`analysis.error_report.v1`）。

旧的内部研究链（`scripts/multi_trajectory_research.py` 的 assess / build-corpus / prepare /
validate-harness / run-smoke / review-smoke / issue-capability / run-specialists 等）已按决策
0032 标记为 legacy，不再进入产品主路径；命令与失败语义仍见
[多 Trajectory 研究说明](docs/multi-pi-analysis.md)。

对一次 Execution 运行确定性错误 precheck（不调用模型）：

```bash
python3 scripts/trajectory_precheck.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-id <execution-id>
```

命令会打印保存在该 Execution 下的 precheck `result.json` 路径。

对已生成 precheck 的 Execution 运行语义分析：

```bash
python3 scripts/trajectory_error_analysis.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-id <execution-id> \
  --precheck <path-printed-by-precheck>
```

该入口每次只在对应 Execution 下创建一个独立 analysis attempt。模型必须通过
`submit_trajectory_error_analysis` 提交完整报告；Pi 先检查 JSON 字段，框架再检查
signal、因果关系和 EvidenceRef。两层都通过才生成 `result.json`。模型在提交前输出的
普通说明或 Markdown 不会被当作正式报告。

当前默认使用负责人已批准的 `trajectory-error-analysis-v2.md`。v1 和早期 v2 文本交付失败
均被原样保留；新的结构化提交边界已在同一条脱敏 trajectory 上通过 DeepSeek V4 Pro 真实验证。
`trajectory-error-analysis-v3.md` 进一步要求所有用户可见自然语言使用简体中文，目前仍为
`proposed`，因此真实运行继续使用已批准的 v2。

若 v2 报告需要中文展示，先准备一份人工审阅的本地 JSON，仅包含 `run_id`、中文摘要、
可选的 Skill 建议说明，以及按原问题 ID 完整对应的中文标题和影响说明，然后发布独立
中文版本：

```bash
python3 scripts/localize_trajectory_user_report.py \
  --runtime-root .skill-evolution \
  --skill-id <skill-id> \
  --execution-id <execution-id> \
  --analysis-id <accepted-analysis-id> \
  --localization <reviewed-zh-CN-input.json>
```

发布流程要求每个问题恰好对应一次，并把中文版本绑定到原报告内容摘要；原报告变化后，
页面会自动停止使用旧中文版本。

当前语义入口仍接收 precheck 文件路径；cutover 前会改为引用同一 Execution 下已接受
的 deterministic Analysis ID，避免机器绝对路径和错误归属。

真实 replay 与多 Pi 分析只能使用已批准、带有匹配 approval sidecar 的版本化 prompt。
查看准确顺序和审批门槛，请遵循[当前计划](.plan/next.md)与
[多 Pi 分析说明](docs/multi-pi-analysis.md)。

## 文档与仓库导航

- [文件与目录目录](docs/file-catalog.md)：每个项目文件的用途和文件头约定。
- [当前计划与优先级](.plan/next.md)：下一步执行顺序与阻塞项。
- [长期框架计划](.plan/future-framework.md)：区分已实现基线、待实现增量和未接受的
  规模化提案；它不是当前执行计划。
- [Skill Contract](docs/skill-contract.md)：正式 package-local contract、严格 schema、
  preflight 和未来版本扩展规则。
- [单轨迹错误分析](docs/single-trajectory-analysis.md)：确定性 precheck、LLM 语义边界、
  输出状态和 Skill package。
- [总体架构](docs/architecture-proposal.md) 与 [评估模型](docs/evaluation-model.md)。
- [Skill-first 层级](docs/skill-hierarchy.md)：当前数据模型、读写流程、API 和迁移门禁。
- [TaskCase](docs/task-case.md)、[Replay Campaign](docs/replay.md)、
  [Skill Contract](docs/skill-contract.md)、[Trajectory 定义](docs/trajectory-definition.md)
  与 [确定性 Harness](docs/harness.md)。
- [Candidate 与隔离 Replay](docs/candidate-replay.md)、
  [Pi RPC 文档](docs/pi-agent/README.md) 与
  [项目开发约定](AGENTS.md)。

## 历史数据迁移

迁移命令仍可用于新的旧格式 runtime。先生成 dry-run：

```bash
python3 scripts/migrate_skill_hierarchy.py
```

迁移器会为所有源文件计算 SHA-256，并在任何对象无法确定 Skill/Revision 归属时停止。
正式 `--apply` 还要求相同 migration ID 的二次确认；请先按
[Skill-first 层级文档](docs/skill-hierarchy.md)审阅清单。当前项目已经完成迁移
`hierarchy-migration-20260809-cutover`：两个 Replay 批次的 10 次 Execution 和一条
保留完整 Skill/Trajectory 的早期独立执行均已迁入。四条无法还原真实 Revision 和标准 Trajectory
的最早试验不进入产品数据，并已在负责人明确批准后永久删除，无法从 runtime 恢复。
