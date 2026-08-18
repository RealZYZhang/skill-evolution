# Skill Explorer

> Purpose: describe the implemented Skill-first read-only Viewer, APIs, user
> navigation, compatibility projection, and security boundary.

状态：Skill-first 历史切换、中文层级导航、完整单 trajectory 五层展示、严格只读行为和
稳定导航已完成；内部四 Specialist 结果板已实现，但正式产品多 trajectory 分析尚未实现。

## 目标与导航

Skill Explorer 是只读 localhost Web 应用。主导航不再从 Replay Campaign 开始，而是：

```text
Skill
├── Skill 首页：说明、Skill Contract、Skill 版本历史
├── trajectory 01
│   └── 单 trajectory 分析
├── trajectory 02
│   └── 单 trajectory 分析
└── ……
```

点击左侧 Skill 卡片会展开二级菜单；二级 trajectory 和分析项是普通层级菜单，不是重复卡片。
Skill 卡片继续显示执行数、版本数和分析数。点击 trajectory 会直接进入其步骤记录，点击分析
会直接进入对应的单 trajectory 分析。

每次执行绑定一个 Skill 版本，版本也是筛选标签，但不是用户必须穿过的一层。执行批次
只作为标签和筛选条件。界面不直接展示难懂编号，而是使用“当前 Skill 版本”“历史
Skill 版本 1”“复测批次 1”等直白名称；确需核对时可展开查看内部记录编号。

页面以中文为主，仅保留 Skill、trajectory 等常用 AI 词。所有状态标签均转换为中文。
Skill、trajectory、Skill Contract、Skill 版本、执行批次、单 trajectory 分析和多 trajectory 分析等
概念旁带问号，鼠标悬停或键盘聚焦即可看到通俗解释。

执行卡片只显示任务名称、输入输出摘要、状态、时间、版本和批次，不在卡片正文展示完整
任务 prompt。完整任务 prompt 可在卡片下方单独展开，也可进入执行详情的“任务 prompt”
页签查看。概览只展示状态、耗时、trajectory 完整性和分析数量，不展示原始记录信息。

单 Trajectory 分析继续使用严格的五层 `analysis.single_trajectory_view.v1`。同一 Execution
存在多个 attempt 时选择最新的 schema-valid 报告。`invalid_output` 原文不会被解析成
问题、归因或 Skill 修改建议。若同一报告附有 `zh-CN` 中文呈现版本，页面会优先展示；
中文版本必须绑定原报告的内容摘要，原报告一旦变化便自动失效并回退，不能把翻译错误地
套到另一份分析上。原始报告始终保留，不被中文呈现覆盖。

多 trajectory 分析目前尚未实现。页面只读取为该能力预留的独立位置；该位置为空，因此页面
明确显示“尚未实现”和 0 条结果。Harness 检查和旧多角色尝试不会作为多 trajectory 分析
出现，也不能产生 Skill 修改建议。新的内部 Harness、盲测记录、确定性基线和四份
Specialist 结果板同样不进入该位置。

## 启动

```bash
python3 scripts/trajectory_viewer.py \
  --runtime-root .skill-evolution \
  --port 8765
```

打开 `http://127.0.0.1:8765/`。服务只能绑定 `127.0.0.1`。`--port 0` 可用于随机
测试端口。当前真实 runtime 已迁移；页面显示一个 Skill、两份版本记录、十一条执行、
三十一份单 trajectory 分析记录和零份多 trajectory 分析。三十一份记录包括十一份确定性基础检查、
十一份通过全部门禁的正式语义分析、八份被拒绝的语义输出，以及一份在模型启动前失败的
分析。十一份正式报告都有来源绑定的人工审阅中文版本；被拒绝的历史记录继续可审计，
但不会覆盖最新的有效结论。
旧 API 仅在显式切换记录存在时从新层级投影，避免部分写入提前隐藏历史数据。

## Skill-first API

```text
GET /api/skills
GET /api/skills/<skill-id>
GET /api/skills/<skill-id>/revisions
GET /api/skills/<skill-id>/executions
GET /api/skills/<skill-id>/executions/<execution-id>
GET /api/skills/<skill-id>/executions/<execution-id>/analyses
GET /api/skills/<skill-id>/analyses/multi
GET /api/skills/<skill-id>/analyses/multi/<analysis-id>
GET /api/skills/<skill-id>/improvements
```

Execution 文件通过 manifest 中的稳定 artifact ID 访问：

```text
GET .../executions/<execution-id>/files/<file-id>/preview
GET .../executions/<execution-id>/files/<file-id>/download
```

调用方不能提交任意文件路径。输入、输出、辅助文件、trajectory 和会话记录只有
已经在 `execution.json` 中声明时才能读取。

旧 `/api/campaigns/...` 暂时保留。存在新层级时，它从 Execution Set 和 Execution
投影，不再读取或恢复 Campaign 作为权威对象。删除兼容 API 需要单独决策。

## Trajectory 与证据下钻

页面同时读取当前 `trajectory.actions.v1` 和历史 `trajectory.actions.v1`，在内存中把历史
边界名规范为 trajectory 名，不改写封存 JSONL。默认的关键步骤由固定类型规则在读取时选出，
不使用 LLM；用户可切换到原 trajectory 的全部步骤。每一步先显示动作、参数和可见消息摘要，
完整消息、长参数、工具返回和原始记录默认关闭。用户可以从分析证据跳到对应步骤。
隐藏推理字段在进入 API 前替换成保护标记，页面只显示可观察的 AI 对外说明。

Input 和 Output 是 artifact 角色，不依赖物理目录名。文件输入、inline task、多输出和
supporting artifact 都由 `execution.json` 定义；缺失输出或缺失 Trajectory 会作为状态显示，
不会导致整个 Skill 从导航中消失。HTML、Markdown 和纯文本输入都可通过同一 allow-list
preview 路由打开；Markdown 与纯文本会先转成转义后的只读 HTML，不执行源内容。

## 安全约束

- 只支持 `GET` 和 `HEAD`；其他方法返回 `405`。
- Host 只接受 `127.0.0.1` 和 `localhost`，降低 DNS rebinding 风险。
- Skill、Revision、Execution、Analysis 和 file ID 只接受安全标识符。
- 解析后的目录、文件和 symlink 必须留在对应 hierarchy object 内。
- 前端只用 `textContent` 展示保存内容，不把 Trajectory 文本作为 Viewer HTML 执行。
- HTML preview 使用空 iframe sandbox 与独立 CSP，禁止脚本、网络、表单、对象和同源权限。
- API、静态页面和预览使用 `no-store`、`nosniff` 与限制性 CSP。

Viewer 读取现有 `catalog.json/index.json`，不会在 GET 中重建或写入。自动测试会比较
访问前后的全部文件内容和修改时间。Skill、Execution、页签和 Trajectory `seq` 会写入 URL，
支持刷新、前进后退和可分享的证据链接。异步请求按 Skill 隔离，切换 Skill 会立即清除
上一页详情，较早请求不能覆盖新选择；附属页面失败也不会拖垮整个 Skill。

## 测试

```bash
python3 -m unittest \
  tests/test_skill_explorer_data.py \
  tests/test_skill_explorer_http.py \
  tests/test_skill_explorer_ui.py \
  tests/test_trajectory_viewer.py -v
```

覆盖 Skill/Revision/Execution 下钻、Campaign 投影、Input/Output、Trajectory redaction、
unknown file、路径穿越、symlink escape、Host、只读方法、CSP、preview 和 download。
HTTP 测试需要允许临时绑定随机 loopback 端口。

自动测试覆盖 Python/API 与 GET 零写入；每次主要页面变更还需在 Chrome 中实际验收
中文状态、问号说明、层级菜单、任务 prompt 展开、输入预览、关键/全部 trajectory 切换、
长内容默认折叠、中文单 trajectory 正文、URL 恢复和多 trajectory 空状态。
