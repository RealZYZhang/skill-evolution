// Purpose: render the read-only Skill → Execution hierarchy.
"use strict";

const state = {
  skills: [], skill: null, revisions: [], executions: [], multi: [], improvements: [],
  selectedSkillId: null, selectedExecutionId: null, selectedExecution: null,
  skillTab: "home", detailTab: "overview", executionCache: new Map(),
  expandedSkillIds: new Set(), routeSeq: null, skillRequestEpoch: 0,
  executionRequestEpoch: 0, trajectoryMode: "key",
};

const STATUS_LABELS = {
  succeeded: "成功", failed: "失败", running: "进行中", interrupted: "已中断",
  orchestration_failed: "调度失败", indeterminate: "状态待定", accepted: "已通过",
  approved: "已批准",
  unavailable: "暂不可用", invalid_output: "输出无效", timed_out: "已超时",
  inconclusive: "尚无定论", planned: "待开始", active: "当前使用",
  historical: "历史版本", candidate: "候选版本", retired: "已停用",
  sealed: "已完整记录", unsealed: "记录不完整", complete: "完整",
  completed: "已完成", completed_with_failures: "完成但有失败",
  missing_at_execution: "执行时未记录", partial: "部分完成", unknown: "未知",
  uncertain: "尚待判断", not_applicable: "不适用", recovered: "已恢复",
  unresolved: "未解决", confirmed: "已确认", rejected: "未通过",
  strong: "证据充分", moderate: "证据一般", weak: "证据有限",
  high: "证据充分", medium: "证据一般", low: "证据有限",
  change: "建议评估修改", no_change: "不建议修改",
  yes: "是", no: "否",
};

const ORIGIN_LABELS = {direct: "直接执行", replay: "历史记录导入"};

const DIMENSION_LABELS = {
  behavior: "行为/机制", conditions: "条件/覆盖", consistency: "一致性", resource: "资源",
};

const VALUE_LABELS = {
  forbidden: "禁止访问", required: "必须", optional: "可选", read_only: "只读",
  allowed: "允许", denied: "禁止", none: "无",
};

const TRAJECTORY_TYPE_LABELS = {
  trajectory_started: "开始执行", trajectory_finished: "完成执行", trajectory_sealed: "封存 trajectory",
  message_action: "消息", tool_action: "工具动作", action_interrupted: "动作中断",
  observer_error: "记录异常", artifact_registered: "登记文件",
  pi_process_starting: "准备运行进程", pi_process_started: "运行进程已启动",
  pi_process_exited: "运行进程已退出", runtime_observed: "记录运行环境",
  skill_resolved: "加载 Skill", agent_start: "AI 开始工作", agent_end: "AI 完成工作",
  turn_start: "开始一轮处理", turn_end: "结束一轮处理", session_captured: "保存会话记录",
  assistant_message: "AI 回复", tool_call: "调用工具", tool_result: "工具返回",
  artifact_created: "生成文件", artifact_updated: "更新文件", error: "出现错误",
  user_message: "用户输入",
};

const TERM_HELP = {
  skill: "一组可复用的任务说明和配套文件，告诉 AI 如何完成一类工作。",
  trajectory: "一次执行从开始到结束留下的步骤记录，用来还原发生了什么。",
  revision: "某一时刻冻结保存的 Skill 内容。Skill 发生变化就会产生新版本，编号用于准确对应当时使用的内容。",
  batch: "为同一个测试目标连续运行的一组执行，用来观察表现是否稳定。",
  execution: "Skill 接到一个具体任务后，从开始到结束的一次完整运行。",
  task: "用于触发并检查一次执行的输入要求。",
  contract: "人工确认的使用边界，说明 Skill 可使用的工具、权限、网络和依赖。",
  singleAnalysis: "只检查一次执行的 trajectory，判断其中的问题、影响和是否需要修改 Skill。",
  multiAnalysis: "把同一 Skill 版本的多次 trajectory 放在一起，识别并分析影响可复用性与可靠性的错误。",
  tool: "Skill 被允许调用的外部能力，例如文件读写或网页操作。",
  permission: "Skill 运行时被允许进行的操作范围。",
  network: "Skill 是否可以访问互联网，以及允许访问的范围。",
  dependency: "Skill 运行前需要具备的软件或组件。",
  asset: "Skill 随附并可在执行时使用的模板、图片或其他文件。",
  evaluation: "用于判断 Skill 是否完成任务、结果质量是否合格的检查方案。",
  improvement: "基于可信分析提出、尚待验证和批准的 Skill 修改方案。",
};

const $ = (id) => document.getElementById(id);
const elements = {
  refresh: $("refresh-button"), skillCount: $("skill-count"), skillSearch: $("skill-search"),
  skillList: $("skill-list"), status: $("status-banner"), skillHeader: $("skill-header"),
  skillKicker: $("skill-kicker"), skillTitle: $("skill-title"), skillMeta: $("skill-meta"),
  skillTabs: $("skill-tabs"), skillSummary: $("skill-summary"), description: $("skill-description"),
  currentRevision: $("current-revision"), contractStatus: $("contract-status"),
  contractSummary: $("contract-summary"), revisionList: $("revision-list"),
  revisionFilter: $("revision-filter"), statusFilter: $("execution-status-filter"),
  taskFilter: $("task-filter"), setFilter: $("set-filter"), executionCount: $("execution-count"),
  executionList: $("execution-list"), executionDetail: $("execution-detail"),
  multiList: $("multi-analysis-list"), multiDetail: $("multi-analysis-detail"),
  improvementList: $("improvement-list"),
  detailTemplate: $("execution-detail-template"),
};

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || STATUS_LABELS.unknown;
}

function helpMark(term) {
  const mark = node("span", "help-mark", "?");
  mark.tabIndex = 0;
  mark.setAttribute("role", "note");
  mark.setAttribute("aria-label", TERM_HELP[term] || "术语说明");
  mark.dataset.tooltip = TERM_HELP[term] || "术语说明";
  return mark;
}

function termLabel(label, term) {
  const wrapper = node("span", "term-label");
  wrapper.append(document.createTextNode(label), helpMark(term));
  return wrapper;
}

function setStatus(message, error = false) {
  elements.status.hidden = !message;
  elements.status.textContent = message || "";
  elements.status.classList.toggle("is-error", error);
}

async function fetchJson(path) {
  const response = await fetch(path, {headers: {Accept: "application/json"}, cache: "no-store"});
  let value;
  try { value = await response.json(); } catch (_error) { throw new Error(`无法解析本地响应（HTTP ${response.status}）`); }
  if (!response.ok) throw new Error(value?.error?.message || `HTTP ${response.status}`);
  return value;
}

function formatDate(value) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}).format(date);
}

function formatDuration(value) {
  if (typeof value !== "number") return "未记录";
  if (value < 1000) return `${value} ms`;
  const seconds = Math.round(value / 1000);
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}

function formatBytes(value) {
  if (typeof value !== "number") return "文件不存在";
  if (value < 1024) return `${value} B`;
  if (value < 1048576) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1048576).toFixed(1)} MB`;
}

function statusBadge(status) {
  const badge = node("span", "status-badge", statusLabel(status));
  badge.dataset.status = status || "unknown";
  return badge;
}

function metric(label, value, note, term = null) {
  const card = node("article", "metric-card");
  const labelNode = node("p", "metric-label");
  labelNode.append(term ? termLabel(label, term) : document.createTextNode(label));
  card.append(labelNode, node("p", "metric-value", value));
  if (note) card.append(node("p", "metric-note", note));
  return card;
}

function jsonBlock(value) {
  const details = node("details", "json-details");
  details.append(node("summary", "", "查看结构化原始数据"));
  const pre = node("pre", "json-block");
  pre.textContent = JSON.stringify(value, null, 2);
  details.append(pre);
  return details;
}

function textValue(value) {
  if (value === null || value === undefined || value === "") return "未声明";
  if (Array.isArray(value)) return value.length ? value.join("、") : "无";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  return VALUE_LABELS[value] || String(value);
}

function readRoute() {
  const match = location.hash.match(/^#\/skills\/([^/?]+)(?:\/executions\/([^/?]+))?(?:\?(.*))?$/);
  if (!match) return {};
  const query = new URLSearchParams(match[3] || "");
  const seq = Number(query.get("seq"));
  return {
    skillId: decodeURIComponent(match[1]),
    executionId: match[2] ? decodeURIComponent(match[2]) : null,
    skillTab: query.get("section") || (match[2] ? "executions" : "home"),
    detailTab: query.get("tab") || "overview",
    seq: Number.isInteger(seq) && seq > 0 ? seq : null,
  };
}

function writeRoute({replace = false} = {}) {
  if (!state.selectedSkillId) return;
  let route = `#/skills/${encodeURIComponent(state.selectedSkillId)}`;
  if (state.selectedExecutionId) route += `/executions/${encodeURIComponent(state.selectedExecutionId)}`;
  const query = new URLSearchParams();
  if (state.skillTab !== "home") query.set("section", state.skillTab);
  if (state.selectedExecutionId && state.detailTab !== "overview") query.set("tab", state.detailTab);
  if (state.detailTab === "trajectory" && state.routeSeq) query.set("seq", String(state.routeSeq));
  const queryText = query.toString();
  const target = `${route}${queryText ? `?${queryText}` : ""}`;
  if (location.hash === target) return;
  if (replace) history.replaceState(null, "", target); else history.pushState(null, "", target);
}

async function loadSkills(preserve = true) {
  const route = readRoute();
  const previous = preserve ? state.selectedSkillId : route.skillId;
  elements.refresh.disabled = true;
  setStatus("正在读取 Skill 目录…");
  try {
    const response = await fetchJson("/api/skills");
    state.skills = response.skills || [];
    state.executionCache.clear();
    elements.skillCount.textContent = String(state.skills.length);
    renderSkillList();
    if (!state.skills.length) {
      state.selectedSkillId = null;
      elements.skillHeader.hidden = true;
      elements.skillTabs.hidden = true;
      setStatus("新层级中还没有 Skill。若历史数据尚未迁移，请先查看迁移 dry-run。");
      return;
    }
    const next = state.skills.some((item) => item.skill_id === previous) ? previous : state.skills[0].skill_id;
    await selectSkill(next, {
      executionId: next === route.skillId ? route.executionId : null,
      skillTab: next === route.skillId ? route.skillTab : state.skillTab,
      detailTab: next === route.skillId ? route.detailTab : state.detailTab,
      seq: next === route.skillId ? route.seq : null,
      replaceRoute: true,
    });
    setStatus("");
  } catch (error) {
    setStatus(`无法读取 Skill：${error.message}`, true);
  } finally { elements.refresh.disabled = false; }
}

function renderSkillList() {
  const query = elements.skillSearch.value.trim().toLowerCase();
  elements.skillList.replaceChildren();
  for (const skill of state.skills.filter((item) => `${item.display_name} ${item.skill_id}`.toLowerCase().includes(query))) {
    const wrapper = node("div", "skill-tree-node");
    const button = node("button", "skill-item");
    button.type = "button";
    const selected = skill.skill_id === state.selectedSkillId;
    const expanded = selected && state.expandedSkillIds.has(skill.skill_id);
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-expanded", String(expanded));
    button.append(node("strong", "", skill.display_name || skill.skill_id));
    const facts = node(
      "span",
      "skill-facts",
      `${skill.execution_count} 次执行 · ${skill.revision_count} 个版本 · ${skill.single_analysis_count} 份单 trajectory 分析`,
    );
    button.append(facts);
    button.addEventListener("click", async () => {
      if (skill.skill_id !== state.selectedSkillId) {
        state.expandedSkillIds.add(skill.skill_id);
        await selectSkill(skill.skill_id);
        return;
      }
      if (state.expandedSkillIds.has(skill.skill_id)) state.expandedSkillIds.delete(skill.skill_id);
      else state.expandedSkillIds.add(skill.skill_id);
      renderSkillList();
    });
    wrapper.append(button);
    if (expanded) wrapper.append(renderTrajectoryMenu());
    elements.skillList.append(wrapper);
  }
}

function renderTrajectoryMenu() {
  const menu = node("ul", "trajectory-menu");
  menu.setAttribute("aria-label", "该 Skill 的 trajectory 与分析");
  if (!state.executions.length) {
    const empty = node("li", "trajectory-menu-empty", "正在读取 trajectory…");
    menu.append(empty);
    return menu;
  }
  state.executions.forEach((execution, index) => {
    const item = node("li", "trajectory-menu-group");
    const trajectoryButton = node("button", "trajectory-menu-item");
    trajectoryButton.type = "button";
    trajectoryButton.classList.toggle("is-selected", execution.execution_id === state.selectedExecutionId);
    trajectoryButton.append(
      node("span", "trajectory-menu-index", `trajectory ${String(index + 1).padStart(2, "0")}`),
      statusBadge(execution.status),
      node("time", "trajectory-menu-time", formatDate(execution.started_at)),
    );
    trajectoryButton.addEventListener("click", () => openExecutionFromTree(execution.execution_id, "trajectory"));
    item.append(trajectoryButton);
    if (execution.analysis_count) {
      const analysisButton = node("button", "trajectory-analysis-item", `单 trajectory 分析（${execution.analysis_count}）`);
      analysisButton.type = "button";
      analysisButton.addEventListener("click", () => openExecutionFromTree(execution.execution_id, "analysis"));
      item.append(analysisButton);
    }
    menu.append(item);
  });
  return menu;
}

async function openExecutionFromTree(executionId, detailTab) {
  state.skillTab = "executions";
  state.detailTab = detailTab;
  state.routeSeq = null;
  switchSkillTab("executions");
  await selectExecution(executionId);
}

async function selectSkill(skillId, options = {}) {
  const requestEpoch = ++state.skillRequestEpoch;
  state.selectedSkillId = skillId;
  state.selectedExecutionId = null;
  state.selectedExecution = null;
  state.revisions = [];
  state.executions = [];
  state.multi = [];
  state.improvements = [];
  state.skillTab = options.skillTab || "home";
  state.detailTab = options.detailTab || "overview";
  state.routeSeq = options.seq || null;
  state.expandedSkillIds.add(skillId);
  elements.executionDetail.replaceChildren(node("p", "empty-state", "选择一次执行，查看输入、输出、trajectory 和分析。"));
  elements.multiDetail.hidden = true;
  renderSkillList();
  setStatus("正在读取这个 Skill 的执行与分析…");
  try {
    const results = await Promise.allSettled([
      fetchJson(`/api/skills/${encodeURIComponent(skillId)}`),
      fetchJson(`/api/skills/${encodeURIComponent(skillId)}/revisions`),
      fetchJson(`/api/skills/${encodeURIComponent(skillId)}/executions`),
      fetchJson(`/api/skills/${encodeURIComponent(skillId)}/analyses/multi`),
      fetchJson(`/api/skills/${encodeURIComponent(skillId)}/improvements`),
    ]);
    if (requestEpoch !== state.skillRequestEpoch || skillId !== state.selectedSkillId) return;
    if (results[0].status === "rejected") throw results[0].reason;
    state.skill = results[0].value;
    state.revisions = results[1].status === "fulfilled" ? results[1].value.revisions || [] : [];
    state.executions = results[2].status === "fulfilled" ? results[2].value.executions || [] : [];
    state.multi = results[3].status === "fulfilled" ? results[3].value.analyses || [] : [];
    state.improvements = results[4].status === "fulfilled" ? results[4].value.candidates || [] : [];
    renderSkillHome();
    populateFilters();
    renderExecutions();
    renderSkillList();
    renderMultiAnalyses();
    renderImprovements();
    elements.skillHeader.hidden = false;
    elements.skillTabs.hidden = false;
    switchSkillTab(state.skillTab);
    writeRoute({replace: Boolean(options.replaceRoute)});
    const partialFailures = results.slice(1).filter((result) => result.status === "rejected").length;
    setStatus(partialFailures ? `${partialFailures} 个附属页面暂时无法读取，其余内容仍可使用。` : "", partialFailures > 0);
    if (options.executionId && state.executions.some((item) => item.execution_id === options.executionId)) {
      await selectExecution(options.executionId, {replaceRoute: true});
    }
  } catch (error) { setStatus(`无法读取 Skill：${error.message}`, true); }
}

function renderSkillHome() {
  const catalog = state.skills.find((item) => item.skill_id === state.selectedSkillId) || {};
  const pkg = state.skill.package;
  elements.skillKicker.textContent = `${catalog.execution_count || 0} 次执行 · ${catalog.single_analysis_count || 0} 份单 trajectory 分析`;
  elements.skillTitle.textContent = catalog.display_name || state.selectedSkillId;
  elements.skillMeta.textContent = "查看 Skill 内容、执行与分析";
  elements.skillSummary.replaceChildren(
    metric("执行", catalog.execution_count || 0, "每个数字对应一次完整运行", "execution"),
    metric("Skill 版本", catalog.revision_count || 0, "保留每次变化时的内容", "revision"),
    metric("单 trajectory 分析", catalog.single_analysis_count || 0, "归属于对应执行", "singleAnalysis"),
    metric("多 trajectory 分析", catalog.multi_analysis_count || 0, catalog.multi_analysis_count ? "已就绪" : "能力尚未实现", "multiAnalysis"),
  );
  elements.currentRevision.textContent = pkg?.revision_id ? "当前 Skill 版本" : "没有版本快照";
  elements.description.replaceChildren();
  if (pkg?.entrypoint) {
    const excerpt = pkg.entrypoint.replace(/^---[\s\S]*?---\s*/, "").trim();
    elements.description.append(node("pre", "skill-document", excerpt));
  } else {
    elements.description.append(node("p", "empty-state", "历史运行没有可展示的 Skill 入口快照。"));
  }
  const contract = pkg?.contract;
  const contractStatus = pkg?.contract_status || "missing_at_execution";
  elements.contractStatus.textContent = statusLabel(contractStatus);
  elements.contractStatus.dataset.status = contractStatus;
  elements.contractSummary.replaceChildren();
  const rows = contract ? [
    ["Contract 版本", contract.version, "contract"], ["工具", contract.runtime?.allowed_tools, "tool"],
    ["权限", contract.runtime?.allowed_permissions, "permission"], ["网络", contract.runtime?.network, "network"],
    ["依赖", contract.runtime?.dependencies, "dependency"], ["资源", contract.runtime?.assets, "asset"],
    ["评估方案", contract.evaluation?.suite_refs, "evaluation"],
  ] : [["历史状态", "执行当时没有 Skill Contract，未使用后来版本补写", "contract"]];
  for (const [label, value, term] of rows) {
    const title = node("dt");
    title.append(termLabel(label, term));
    elements.contractSummary.append(title, node("dd", "", textValue(value)));
  }
  elements.revisionList.replaceChildren();
  state.revisions.forEach((revision) => {
    const item = node("div", "revision-row");
    const name = revision.revision_id === pkg?.revision_id
      ? "当前 Skill 版本"
      : revisionLabel(revision.revision_id);
    const identity = node("div", "revision-name");
    identity.append(node("strong", "", name), termLabel("版本说明", "revision"));
    const technical = node("details", "technical-details");
    technical.append(node("summary", "", "查看记录编号"), node("code", "", revision.revision_id));
    item.append(identity, statusBadge(revision.lifecycle), technical);
    item.append(node("span", "muted", `Skill Contract：${statusLabel(revision.contract.status)}`));
    elements.revisionList.append(item);
  });
}

function replaceOptions(select, values, labeler = (value) => value) {
  const current = select.value;
  select.replaceChildren(new Option("全部", ""));
  for (const value of values) select.append(new Option(labeler(value), value));
  if (values.includes(current)) select.value = current;
}

function populateFilters() {
  replaceOptions(
    elements.revisionFilter,
    [...new Set(state.executions.map((item) => item.revision_id))],
    revisionLabel,
  );
  replaceOptions(
    elements.statusFilter,
    [...new Set(state.executions.map((item) => item.status))],
    statusLabel,
  );
  replaceOptions(
    elements.setFilter,
    [...new Set(state.executions.map((item) => item.execution_set_id).filter(Boolean))],
    executionSetLabel,
  );
}

function taskText(execution) {
  const task = execution.task || {};
  return String(task.task_case_id || task.id || task.name || task.source_path || "文档转换任务");
}

function baseName(path) {
  return String(path || "").split("/").filter(Boolean).pop() || "";
}

function executionSummary(execution) {
  const task = execution.task || {};
  const input = baseName(task.input) || baseName(execution.inputs?.[0]?.path);
  const output = baseName(task.expected_artifact) || baseName(execution.outputs?.[0]?.path);
  if (input && output) return `${input} 转换为 ${output}`;
  if (input) return `处理 ${input}`;
  if (output) return `生成 ${output}`;
  return "文档处理任务";
}

function promptText(execution) {
  return String(execution.task?.prompt || "这次执行没有保存完整任务 prompt。");
}

function revisionLabel(revisionId) {
  if (!revisionId) return "未记录 Skill 版本";
  if (revisionId === state.skill?.package?.revision_id) return "当前 Skill 版本";
  const historical = state.revisions
    .filter((revision) => revision.revision_id !== state.skill?.package?.revision_id)
    .findIndex((revision) => revision.revision_id === revisionId);
  return historical >= 0 ? `历史 Skill 版本 ${historical + 1}` : "历史 Skill 版本";
}

function executionSetLabel(setId) {
  if (!setId) return "单独执行";
  const ids = [...new Set(state.executions.map((item) => item.execution_set_id).filter(Boolean))];
  const index = ids.indexOf(setId);
  return `复测批次 ${index >= 0 ? index + 1 : ""}`.trim();
}

function filteredExecutions() {
  const query = elements.taskFilter.value.trim().toLowerCase();
  return state.executions.filter((item) =>
    (!elements.revisionFilter.value || item.revision_id === elements.revisionFilter.value) &&
    (!elements.statusFilter.value || item.status === elements.statusFilter.value) &&
    (!elements.setFilter.value || item.execution_set_id === elements.setFilter.value) &&
    (!query || taskText(item).toLowerCase().includes(query))
  );
}

function renderExecutions() {
  const executions = filteredExecutions();
  elements.executionCount.textContent = String(executions.length);
  elements.executionList.replaceChildren();
  if (!executions.length) {
    elements.executionList.append(node("p", "empty-state", "没有符合筛选条件的执行。"));
    return;
  }
  for (const execution of executions) {
    const card = node("article", "execution-item");
    const button = node("button", "execution-select");
    button.type = "button";
    card.classList.toggle("is-selected", execution.execution_id === state.selectedExecutionId);
    const top = node("span", "execution-item-top");
    top.append(statusBadge(execution.status), node("time", "", formatDate(execution.started_at)));
    button.append(top, node("strong", "", taskText(execution)), node("span", "execution-summary", executionSummary(execution)));
    const tags = node("span", "execution-tags");
    tags.append(node("span", "tag", revisionLabel(execution.revision_id)), node("span", "tag", ORIGIN_LABELS[execution.origin] || execution.origin));
    if (execution.execution_set_id) tags.append(node("span", "tag", executionSetLabel(execution.execution_set_id)));
    if (execution.analysis_count) tags.append(node("span", "tag", `${execution.analysis_count} 份分析`));
    button.append(tags);
    button.addEventListener("click", () => selectExecution(execution.execution_id));
    card.append(button);
    const prompt = node("details", "prompt-disclosure");
    prompt.append(node("summary", "", "展开任务 prompt"), node("pre", "prompt-text", promptText(execution)));
    const technical = node("details", "technical-details execution-technical");
    technical.append(node("summary", "", "查看记录编号"), node("code", "", execution.execution_id));
    card.append(prompt, technical);
    elements.executionList.append(card);
  }
  if (state.selectedExecutionId && !executions.some((item) => item.execution_id === state.selectedExecutionId)) {
    state.selectedExecutionId = null;
    elements.executionDetail.replaceChildren(node("p", "empty-state", "当前筛选隐藏了已选执行。"));
  }
}

async function selectExecution(executionId, options = {}) {
  const skillId = state.selectedSkillId;
  if (executionId !== state.selectedExecutionId) state.trajectoryMode = "key";
  const requestEpoch = ++state.executionRequestEpoch;
  state.selectedExecutionId = executionId;
  renderExecutions();
  renderSkillList();
  elements.executionDetail.replaceChildren(node("p", "empty-state", "正在读取这次执行…"));
  try {
    const cacheKey = `${skillId}/${executionId}`;
    let detail = state.executionCache.get(cacheKey);
    if (!detail) {
      detail = await fetchJson(`/api/skills/${encodeURIComponent(skillId)}/executions/${encodeURIComponent(executionId)}`);
      state.executionCache.set(cacheKey, detail);
    }
    if (requestEpoch !== state.executionRequestEpoch || skillId !== state.selectedSkillId || executionId !== state.selectedExecutionId) return;
    state.selectedExecution = detail;
    renderExecutionDetail();
    writeRoute({replace: Boolean(options.replaceRoute)});
  } catch (error) { elements.executionDetail.replaceChildren(node("p", "error-text", error.message)); }
}

function renderExecutionDetail() {
  const detail = state.selectedExecution;
  const fragment = elements.detailTemplate.content.cloneNode(true);
  fragment.querySelector('[data-field="execution-title"]').textContent = taskText(detail.execution);
  const badge = fragment.querySelector('[data-field="execution-status"]');
  badge.textContent = statusLabel(detail.execution.status);
  badge.dataset.status = detail.execution.status;
  fragment.querySelector('[data-field="execution-meta"]').textContent = `${formatDuration(detail.execution.duration_ms)} · ${revisionLabel(detail.execution.revision_id)} · ${executionSetLabel(detail.execution.execution_set_id)}`;
  for (const button of fragment.querySelectorAll("[data-detail-tab]")) {
    button.classList.toggle("is-active", button.dataset.detailTab === state.detailTab);
    button.addEventListener("click", () => {
      state.detailTab = button.dataset.detailTab;
      state.routeSeq = null;
      renderExecutionDetail();
      writeRoute();
    });
  }
  elements.executionDetail.replaceChildren(fragment);
  renderDetailContent(elements.executionDetail.querySelector('[data-field="detail-content"]'), detail);
}

function renderDetailContent(container, detail) {
  container.replaceChildren();
  if (state.detailTab === "overview") {
    const grid = node("div", "metric-grid compact");
    grid.append(
      metric("状态", statusLabel(detail.execution.status)),
      metric("耗时", formatDuration(detail.execution.duration_ms)),
      metric("trajectory", detail.execution.trajectory.sealed ? "已完整记录" : "记录不完整", null, "trajectory"),
      metric("单 trajectory 分析", detail.analyses.analyses.length, null, "singleAnalysis"),
    );
    container.append(grid);
  } else if (state.detailTab === "prompt") {
    container.append(sectionTitle("完整任务 prompt"), node("pre", "prompt-text detail-prompt", promptText(detail.execution)));
  } else if (state.detailTab === "input") {
    container.append(
      sectionTitle("任务输入"),
      node("p", "muted", "这次执行读取了以下输入文件。Markdown 和文本文件可直接在新页面只读预览。"),
    );
    renderArtifacts(container, detail.input.artifacts, "input");
  } else if (state.detailTab === "output") {
    container.append(sectionTitle("输出文件"));
    renderArtifacts(container, detail.output.artifacts, "output");
    if (detail.output.supporting_artifacts.length) { container.append(sectionTitle("辅助文件")); renderArtifacts(container, detail.output.supporting_artifacts, "辅助文件"); }
  } else if (state.detailTab === "trajectory") {
    renderTrajectory(container, detail.trajectory);
    if (state.routeSeq) requestAnimationFrame(() => document.getElementById(`trajectory-seq-${state.routeSeq}`)?.scrollIntoView({block: "center"}));
  } else if (state.detailTab === "analysis") {
    renderSingleAnalysis(container, detail.analyses);
  } else if (state.detailTab === "setup") {
    container.append(sectionTitle("运行设置与来源"), jsonBlock(detail.setup));
  }
}

function sectionTitle(text) { return node("h4", "subheading", text); }

function renderArtifacts(container, artifacts, role) {
  if (!artifacts.length) { container.append(node("p", "empty-state", "没有保存这一角色的文件。")); return; }
  for (const artifact of artifacts) {
    const row = node("div", "artifact-row");
    const identity = node("div", "");
    const roleLabels = {input: "输入", output: "输出", supporting: "辅助文件"};
    identity.append(node("strong", "", artifact.path.split("/").pop()), node("p", "muted", `${roleLabels[role] || role} · ${formatBytes(artifact.bytes)} · ${artifact.media_type || "未知类型"}`));
    const actions = node("div", "artifact-actions");
    const base = `/api/skills/${encodeURIComponent(state.selectedSkillId)}/executions/${encodeURIComponent(state.selectedExecutionId)}/files/${encodeURIComponent(artifact.artifact_id)}`;
    const extension = artifact.path.split(".").pop().toLowerCase();
    const previewable = artifact.media_type === "text/html" || artifact.media_type === "text/markdown" || artifact.media_type === "text/plain" || ["html", "htm", "md", "markdown", "txt"].includes(extension);
    if (previewable) {
      const preview = node("a", "button button-quiet", "网页预览"); preview.href = `${base}/preview`; preview.target = "_blank"; preview.rel = "noopener"; actions.append(preview);
    }
    const download = node("a", "button", "下载"); download.href = `${base}/download`; actions.append(download);
    row.append(identity, actions); container.append(row);
  }
}

function compactText(value, limit = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function serializedValue(value) {
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch (_error) { return String(value); }
}

function trajectoryStatus(step) {
  return step.status || step.payload?.status || step.payload?.outcome?.status || null;
}

function messageText(payload) {
  const content = payload?.message?.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n\n");
}

function containsProtectedReasoning(value) {
  return serializedValue(value).includes("[REDACTED: hidden reasoning]") ||
    (Array.isArray(value?.message?.content) && value.message.content.some((block) => block?.redacted));
}

function expandableValue(label, value, extraClass = "") {
  const text = serializedValue(value);
  const details = node("details", `trajectory-value ${extraClass}`.trim());
  details.append(
    node("summary", "", `${label}（${text.length.toLocaleString("zh-CN")} 字符）`),
    node("pre", "trajectory-value-content", text),
  );
  return details;
}

function appendToolAction(card, payload) {
  const name = String(payload.tool_name || "未知工具");
  card.append(node("p", "trajectory-action-summary", `调用 ${name}`));
  const argumentsList = node("div", "trajectory-arguments");
  const entries = Object.entries(payload.arguments || {});
  if (!entries.length) argumentsList.append(node("p", "muted", "这次调用没有保存参数。"));
  for (const [key, value] of entries) {
    const row = node("div", "trajectory-argument-row");
    row.append(node("strong", "trajectory-argument-name", key));
    const text = serializedValue(value);
    if (text.length <= 160 && !text.includes("\n")) row.append(node("code", "trajectory-inline-value", text));
    else row.append(expandableValue("查看完整参数", value));
    argumentsList.append(row);
  }
  card.append(argumentsList);
  if (payload.result !== undefined && payload.result !== null) card.append(expandableValue("查看工具返回", payload.result, "tool-result"));
}

function appendMessageAction(card, payload) {
  const roleLabels = {user: "用户输入", assistant: "AI 对外说明", toolResult: "工具返回"};
  const role = payload.message?.role;
  const text = messageText(payload);
  card.append(node("p", "trajectory-action-summary", text ? `${roleLabels[role] || "消息"}：${compactText(text)}` : (roleLabels[role] || "记录了一条消息")));
  if (text) card.append(expandableValue("展开完整消息", text));
  if (containsProtectedReasoning(payload)) card.append(node("p", "protected-note", "内部隐藏推理受保护，不在页面展示。"));
}

function genericTrajectorySummary(step) {
  const payload = step.payload || {};
  if (step.type === "artifact_registered") return `登记${payload.artifact_role === "input" ? "输入" : payload.artifact_role === "output" ? "输出" : "相关"}文件：${baseName(payload.artifact?.path) || "未命名文件"}`;
  if (step.type === "skill_resolved") return payload.loaded ? "Skill 已成功加载。" : "Skill 未能加载。";
  if (step.type === "runtime_observed") return `使用 ${payload.model?.name || payload.model?.id || "未记录模型"}，思考等级：${payload.thinking_level || "未记录"}。`;
  if (step.type === "trajectory_started") return "本次执行开始记录。";
  if (step.type === "trajectory_finished") return `本次执行结束，结果：${statusLabel(payload.outcome?.status)}。`;
  if (step.type === "trajectory_sealed") return `trajectory 已封存，共记录 ${payload.record_count ?? "未知"} 步。`;
  if (step.type === "turn_start") return "AI 开始新一轮处理。";
  if (step.type === "turn_end") return `本轮处理结束，包含 ${payload.tool_result_count ?? 0} 次工具返回。`;
  if (step.type === "pi_process_exited") return `运行进程退出，状态码：${payload.exit_code ?? "未记录"}。`;
  return `${TRAJECTORY_TYPE_LABELS[step.type] || step.type}。`;
}

function appendTrajectoryStep(timeline, step) {
  const payload = step.payload || {};
  const card = node("article", "trajectory-step"); card.id = `trajectory-seq-${step.seq}`;
  const top = node("div", "trajectory-step-top");
  const status = trajectoryStatus(step);
  top.append(
    node("code", "", `第 ${step.seq} 步`),
    node("strong", "", TRAJECTORY_TYPE_LABELS[step.type] || step.type),
    status ? statusBadge(status) : node("span", "tag", "运行记录"),
  );
  card.append(top);
  if (step.type === "tool_action") appendToolAction(card, payload);
  else if (step.type === "message_action") appendMessageAction(card, payload);
  else card.append(node("p", "trajectory-action-summary", genericTrajectorySummary(step)));
  const raw = node("details", "trajectory-raw");
  raw.append(node("summary", "", "查看这一步的完整记录"), node("pre", "trajectory-value-content", serializedValue({type: step.type, source: step.source, payload})));
  card.append(raw);
  timeline.append(card);
}

function renderTrajectory(container, trajectory) {
  const keySteps = trajectory.timeline || [];
  const allSteps = trajectory.records || [];
  if (state.routeSeq && !keySteps.some((step) => step.seq === state.routeSeq)) state.trajectoryMode = "all";
  const showingAll = state.trajectoryMode === "all";
  const steps = showingAll ? allSteps : keySteps;
  const heading = node("div", "trajectory-heading");
  heading.append(sectionTitle(`trajectory · ${showingAll ? "全部" : "关键"} ${steps.length} 步`), statusBadge(trajectory.metadata.sealed ? "sealed" : "unsealed"));
  const controls = node("div", "trajectory-controls");
  const toggle = node("button", "button button-quiet", showingAll ? `只看关键步骤（${keySteps.length}）` : `显示全部步骤（${allSteps.length}）`);
  toggle.type = "button";
  toggle.addEventListener("click", () => { state.trajectoryMode = showingAll ? "key" : "all"; state.routeSeq = null; renderExecutionDetail(); });
  controls.append(toggle, node("p", "trajectory-filter-note", "关键步骤由固定规则在页面读取时选出，不使用 LLM；原始 trajectory 没有被删改。"));
  container.append(heading, controls);
  if (trajectory.issues.length) for (const issue of trajectory.issues) container.append(node("p", "error-text", issue.message));
  const timeline = node("div", "timeline");
  for (const step of steps) appendTrajectoryStep(timeline, step);
  if (!steps.length) timeline.append(node("p", "empty-state", "没有可展示的 trajectory 步骤。"));
  container.append(timeline);
}

function renderSingleAnalysis(container, analyses) {
  const report = analyses.latest_valid_report;
  if (!report) {
    const records = (analyses.analyses || []).map((item) => item.record || {});
    const hasPrecheck = records.some((item) => item.kind === "precheck" && item.status === "accepted");
    const hasSemanticAttempt = records.some((item) => item.kind === "trajectory_error");
    const card = node("article", "analysis-hero");
    if (hasSemanticAttempt) {
      card.append(
        statusBadge("invalid_output"),
        node("h4", "", "语义分析未通过质量检查"),
        node("p", "", "基础检查已经完成，但语义结论的格式不合格，因此错误归因、恢复判断和 Skill 修改建议均未被接纳。"),
      );
    } else if (hasPrecheck) {
      card.append(
        statusBadge("accepted"),
        node("h4", "", "基础检查已完成"),
        node("p", "", "trajectory 的完整性和显式运行事实已经检查；语义分析尚未运行，因此目前没有错误归因、恢复判断或 Skill 修改建议。"),
      );
    } else {
      card.append(
        statusBadge("planned"),
        node("h4", "", "尚未分析"),
        node("p", "", "这次执行还没有进行单 trajectory 分析。"),
      );
    }
    container.append(card);
    return;
  }
  const hero = node("article", "analysis-hero");
  hero.append(statusBadge(report.analysis.status), node("h4", "", report.analysis.title), node("p", "", report.analysis.message));
  container.append(hero);
  const grid = node("div", "assessment-grid");
  for (const [key, value] of Object.entries(report.overview || {})) {
    const overviewLabels = {
      task_outcome: "任务结果", task_result: "任务结果",
      trajectory_quality: "trajectory 质量", trajectory_data: "trajectory 数据",
      skill_fit: "Skill 适配度", skill_recommendation: "Skill 修改建议",
      output_quality: "输出质量", error_assessment: "异常影响判断",
    };
    const card = node("article", "assessment-card"); card.append(node("p", "eyebrow", overviewLabels[key] || key.replaceAll("_", " ")), node("h5", "", value.label), node("p", "", value.detail), statusBadge(value.status)); grid.append(card);
  }
  container.append(grid, sectionTitle("发生了什么"), node("p", "narrative", report.narrative?.summary || "未提供摘要"));
  const timeline = node("div", "analysis-timeline");
  for (const item of report.narrative?.timeline || []) {
    const row = node("div", `analysis-event tone-${item.tone || "neutral"}`); row.append(node("strong", "", item.label), node("p", "", item.detail)); timeline.append(row);
  }
  container.append(timeline, sectionTitle("发现的问题"));
  if (!report.incidents?.length) {
    container.append(node("p", "empty-state", "当前没有通过质量检查的问题结论。"));
  }
  for (const incident of report.incidents || []) {
    const card = node("article", "incident-card");
    const top = node("div", "incident-card-top");
    top.append(node("h5", "", incident.title), statusBadge(incident.evidence_strength));
    card.append(top, node("p", "", incident.impact));
    const facts = node("dl", "definition-list compact");
    for (const [label, value] of [["恢复情况", incident.recovery], ["原因归属", incident.attribution], ["是否建议修改 Skill", statusLabel(incident.skill_change)]]) {
      facts.append(node("dt", "", label), node("dd", "", value));
    }
    card.append(facts);
    container.append(card);
  }
  container.append(sectionTitle("建议"), node("p", "recommendation", report.recommendation?.summary || "暂无建议"));
  const steps = node("ol", "next-steps"); for (const item of report.recommendation?.next_steps || []) steps.append(node("li", "", item)); container.append(steps);
  const evidence = node("details", "evidence-panel"); evidence.append(node("summary", "", `证据与下钻位置（${report.evidence?.length || 0}）`));
  for (const item of report.evidence || []) {
    const card = node("div", "evidence-row"); card.append(node("strong", "", item.title), node("p", "", item.summary));
    const seq = item.locator?.seq; if (seq) { const jump = node("button", "text-button", `跳到 trajectory 第 ${seq} 步`); jump.type = "button"; jump.addEventListener("click", () => { state.detailTab = "trajectory"; state.routeSeq = seq; renderExecutionDetail(); writeRoute(); requestAnimationFrame(() => document.getElementById(`trajectory-seq-${seq}`)?.scrollIntoView({behavior: "smooth", block: "center"})); }); card.append(jump); }
    evidence.append(card);
  }
  container.append(evidence);
}

function renderMultiAnalyses() {
  elements.multiList.replaceChildren();
  elements.multiDetail.hidden = true;
  if (!state.multi.length) {
    const empty = node("article", "surface empty-state-card");
    empty.append(
      node("h3", "", "这里还没有可展示的多 trajectory 分析"),
      node("p", "", "运行错误识别与分析（scripts/error_analysis.py run --publish-product）后，错误清单和逐错误报告会出现在这里。"),
    );
    elements.multiList.append(empty);
    return;
  }
  for (const analysis of state.multi) {
    const card = node("button", "analysis-card"); card.type = "button";
    card.append(statusBadge(analysis.status), node("h4", "", "多 trajectory 分析"), node("p", "muted", analysis.analysis_id + " · " + executionSetLabel(analysis.execution_set_id)));
    card.addEventListener("click", () => loadMultiAnalysis(analysis.analysis_id)); elements.multiList.append(card);
  }
}

function renderImprovements() {
  elements.improvementList.replaceChildren();
  if (!state.improvements.length) {
    const empty = node("article", "surface empty-state-card");
    empty.append(node("h3", "", "尚无改进方案"), node("p", "", "分析不会自动修改 Skill；只有明确提出并等待验证的方案才会进入这里。"));
    elements.improvementList.append(empty); return;
  }
  for (const candidate of state.improvements) {
    const card = node("article", "surface improvement-card");
    card.append(statusBadge(candidate.status), node("h4", "", "待验证的改进方案"));
    card.append(node("p", "", `${candidate.file_changes?.length || 0} 个文件变更 · ${candidate.comparison_ids?.length || 0} 次效果比较 · ${candidate.review_ids?.length || 0} 份人工复核`));
    card.append(jsonBlock(candidate.hypothesis)); elements.improvementList.append(card);
  }
}

function shortRun(runId) {
  const parts = String(runId || "").split("-");
  return parts.length > 1 ? parts[parts.length - 1] : String(runId || "");
}

function evidenceList(items) {
  const list = node("ul", "evidence-panel");
  for (const item of items || []) {
    const li = node("li", "");
    if (item && item.run_id && item.seq) li.textContent = shortRun(item.run_id) + " · seq " + item.seq;
    else if (item && item.report_path) li.textContent = "报告：" + item.report_path;
    else if (item && item.artifact_path) li.textContent = "产物：" + item.artifact_path;
    else li.textContent = textValue(item);
    list.append(li);
  }
  return list;
}

function renderErrorCentricReport(report) {
  elements.multiDetail.append(node("h3", "", "错误清单与四维分析"));
  const covered = report.scope?.eligible_trajectory_ids?.length || 0;
  elements.multiDetail.append(node("p", "muted", "覆盖 " + covered + " 次执行 · 识别 " + (report.errors?.length || 0) + " 个错误 · " + (report.reports?.length || 0) + " 份报告"));
  for (const error of report.errors || []) {
    const card = node("article", "finding-card");
    card.append(node("h5", "", (error.error_id || "错误") + " · " + (error.title || "未命名")));
    card.append(node("p", "", error.summary || "未提供说明"));
    card.append(node("p", "muted", "出现在 " + (error.observed_trajectory_ids?.length || 0) + " 条轨迹" + (error.suggested_dimensions?.length ? " · 建议维度：" + error.suggested_dimensions.join("、") : "")));
    if (error.anchor_evidence?.length) {
      card.append(node("p", "muted", "证据锚点：" + error.anchor_evidence.map((item) => shortRun(item.run_id) + "@seq" + item.seq).join("、")));
    }
    if (error.notes) card.append(node("p", "muted", "备注：" + error.notes));
    elements.multiDetail.append(card);

    const rep = (report.reports || []).find((item) => item.error_id === error.error_id);
    if (rep && rep.dimensions?.length) {
      elements.multiDetail.append(sectionTitle(error.error_id + " 的维度分析"));
      for (const dim of rep.dimensions) {
        const d = node("article", "finding-card");
        d.append(node("h5", "", DIMENSION_LABELS[dim.dimension] || dim.dimension || "维度"));
        d.append(node("p", "", dim.claim || "未提供说明"));
        d.append(node("p", "muted", "置信度 " + (dim.confidence ?? "未给出") + " · 观察到 " + (dim.observed_trajectory_ids?.length || 0) + " 条轨迹"));
        if (dim.evidence?.length) d.append(evidenceList(dim.evidence));
        if (dim.limitations?.length) d.append(node("p", "muted", "限制：" + dim.limitations.join("；")));
        elements.multiDetail.append(d);
      }
    }
  }
  if (report.limitations?.length) {
    elements.multiDetail.append(sectionTitle("整体限制"));
    elements.multiDetail.append(node("p", "muted", report.limitations.join("；")));
  }
  elements.multiDetail.append(jsonBlock(report));
}

async function loadMultiAnalysis(analysisId) {
  elements.multiDetail.hidden = false;
  elements.multiDetail.replaceChildren(node("p", "empty-state", "正在读取多 trajectory 分析…"));
  try {
    const detail = await fetchJson(`/api/skills/${encodeURIComponent(state.selectedSkillId)}/analyses/multi/${encodeURIComponent(analysisId)}`);
    elements.multiDetail.replaceChildren();
    if (!detail.report) {
      elements.multiDetail.append(node("h3", "", "用户结论暂不可用"), node("p", "muted", "保留了分析记录与确定性结果，但没有通过格式校验的多 trajectory 用户报告。"), jsonBlock(detail.record));
      return;
    }
    const report = detail.report;
    if (report.schema === "analysis.multi_trajectory_errors.v1") {
      renderErrorCentricReport(report);
      return;
    }
    elements.multiDetail.append(statusBadge(report.analysis.status), node("h3", "", report.overview.title || "多 trajectory 分析"), node("p", "narrative", report.overview.summary || ""));
    const covered = report.execution_set?.execution_ids || [];
    elements.multiDetail.append(node("p", "muted", `覆盖 ${covered.length} 次执行 · ${report.execution_set?.purpose || "未注明用途"}`));
    elements.multiDetail.append(sectionTitle("共同模式"));
    if (!report.patterns?.length) elements.multiDetail.append(node("p", "empty-state", "当前没有通过质量检查的共同模式。"));
    for (const pattern of report.patterns || []) {
      const card = node("article", "finding-card");
      card.append(node("h5", "", pattern.title || pattern.label || pattern.id || "共同模式"), node("p", "", pattern.summary || pattern.detail || textValue(pattern)));
      elements.multiDetail.append(card);
    }
    elements.multiDetail.append(sectionTitle("主要发现"));
    if (!report.findings?.length) elements.multiDetail.append(node("p", "empty-state", "当前没有可采纳的跨执行发现。"));
    for (const finding of report.findings || []) {
      const card = node("article", "finding-card");
      card.append(node("h5", "", finding.title || finding.label || finding.id || "发现"));
      card.append(node("p", "", finding.summary || finding.claim || finding.detail || "未提供说明"));
      if (finding.impact) card.append(node("p", "muted", `影响：${textValue(finding.impact)}`));
      elements.multiDetail.append(card);
    }
    elements.multiDetail.append(sectionTitle("证据"));
    if (!report.evidence?.length) elements.multiDetail.append(node("p", "empty-state", "没有可下钻的跨执行证据。"));
    for (const evidence of report.evidence || []) {
      const card = node("div", "evidence-row");
      card.append(node("strong", "", evidence.title || evidence.id || "证据"), node("p", "", evidence.summary || evidence.detail || ""));
      const executionId = evidence.execution_id || evidence.locator?.execution_id;
      const seq = evidence.seq || evidence.locator?.seq;
      if (executionId && state.executions.some((item) => item.execution_id === executionId)) {
        const jump = node("button", "text-button", seq ? `打开执行并跳到 trajectory 第 ${seq} 步` : "打开对应执行");
        jump.type = "button";
        jump.addEventListener("click", async () => {
          state.skillTab = "executions";
          state.detailTab = seq ? "trajectory" : "overview";
          state.routeSeq = seq || null;
          switchSkillTab("executions");
          await selectExecution(executionId);
        });
        card.append(jump);
      }
      elements.multiDetail.append(card);
    }
    elements.multiDetail.append(sectionTitle("建议与下一步"), node("p", "recommendation", report.recommendation.summary || ""));
    const steps = node("ol", "next-steps");
    for (const item of report.recommendation.next_steps || []) steps.append(node("li", "", item));
    elements.multiDetail.append(steps);
  } catch (error) { elements.multiDetail.replaceChildren(node("p", "error-text", error.message)); }
}

function switchSkillTab(tab) {
  state.skillTab = tab;
  for (const button of document.querySelectorAll("[data-skill-tab]")) {
    const active = button.dataset.skillTab === tab; button.classList.toggle("is-active", active); button.setAttribute("aria-pressed", String(active));
  }
  for (const name of ["home", "executions", "multi", "improvements"]) $( `panel-${name}` ).hidden = name !== tab;
}

elements.refresh.addEventListener("click", () => loadSkills(true));
elements.skillSearch.addEventListener("input", renderSkillList);
for (const button of document.querySelectorAll("[data-skill-tab]")) button.addEventListener("click", () => { switchSkillTab(button.dataset.skillTab); writeRoute(); });
for (const control of [elements.revisionFilter, elements.statusFilter, elements.setFilter]) control.addEventListener("change", renderExecutions);
elements.taskFilter.addEventListener("input", renderExecutions);

window.addEventListener("popstate", async () => {
  const route = readRoute();
  if (!route.skillId) return;
  if (route.skillId !== state.selectedSkillId) {
    await selectSkill(route.skillId, {executionId: route.executionId, skillTab: route.skillTab, detailTab: route.detailTab, seq: route.seq, replaceRoute: true});
    return;
  }
  state.skillTab = route.skillTab || "home";
  state.detailTab = route.detailTab || "overview";
  state.routeSeq = route.seq || null;
  switchSkillTab(state.skillTab);
  if (route.executionId) await selectExecution(route.executionId, {replaceRoute: true});
});

loadSkills(false);
