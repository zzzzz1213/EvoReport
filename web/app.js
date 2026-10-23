const state = { projects: [], reports: [], memories: [], currentReport: null, savedDraft: "", selectedHistoryReport: null, backupPreview: null, reviewedText: null, reviewedReportId: null, replayResults: {} };
let draftSaveTimer = null;
let draftSaveQueue = Promise.resolve();
let reviewAutoClaims = [];
let reviewCompletionLines = [];
let reviewMissedClaims = [];
const titles = { overview: ["DAILY WORKSPACE", "今日总览"], projects: ["LOCAL REPOSITORIES", "项目管理"], daily: ["CREATE REPORT", "生成日报"], history: ["REPORT ARCHIVE", "报告历史"], evolution: ["PERSONAL DATASET", "写作进化"] };

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const error = new Error(typeof detail === "string" ? detail : detail?.message || data.error || `请求失败 (${response.status})`);
    error.review = typeof detail === "object" ? detail?.review : null;
    throw error;
  }
  return data;
}

function toast(message, error = false) {
  const element = document.querySelector("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.className = "toast", 2600);
}

function switchView(name) {
  document.querySelectorAll(".view").forEach(item => item.classList.toggle("active", item.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === name));
  document.querySelector("#view-eyebrow").textContent = titles[name][0];
  document.querySelector("#view-title").textContent = titles[name][1];
  if (name === "overview") loadDashboard();
  if (name === "projects" || name === "daily") loadProjects();
  if (name === "daily") { restoreLatestDraft(); refreshDailyQueue(); }
  if (name === "history") loadReports();
  if (name === "evolution") loadEvolution();
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
document.querySelectorAll("[data-goto]").forEach(button => button.addEventListener("click", () => switchView(button.dataset.goto)));

async function refreshDailyQueue() {
  const element = document.querySelector("#daily-queue-status");
  try {
    const queue = await api("/api/agents/daily-queue");
    element.textContent = `日报队列：执行中 ${queue.running} / ${queue.max_running}，等待 ${queue.waiting} / ${queue.max_waiting}`;
  } catch (_) {
    element.textContent = "日报队列状态暂不可用。";
  }
}

setInterval(() => {
  if (document.querySelector("#view-daily").classList.contains("active")) refreshDailyQueue();
}, 2000);

function showDraft(report, restored = false) {
  clearTimeout(draftSaveTimer);
  draftSaveTimer = null;
  state.currentReport = report;
  hideFinalReview();
  state.savedDraft = report.draft;
  document.querySelector("#report-editor").value = report.draft;
  document.querySelector("#report-editor").readOnly = false;
  document.querySelector("#confirm-report").disabled = false;
  document.querySelector("#draft-status").textContent = report.agent_trace?.status === "needs_review"
    ? "草稿待人工检查 · 自动保存已开启"
    : restored ? "已恢复未确认草稿 · 自动保存已开启" : "草稿 · 自动保存已开启";
}

async function restoreLatestDraft() {
  if (state.currentReport || document.querySelector("#report-editor").value) return;
  try {
    const reports = await api("/api/reports?kind=daily");
    if (state.currentReport || document.querySelector("#report-editor").value) return;
    const drafts = reports.filter(report => report.status === "draft" && report.origin === "generated");
    const latest = drafts.sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0];
    if (latest) showDraft(latest, true);
  } catch (error) { toast(error.message, true); }
}

function queueDraftSave() {
  const report = state.currentReport;
  if (!report || report.status !== "draft") return draftSaveQueue;
  const content = document.querySelector("#report-editor").value;
  draftSaveQueue = draftSaveQueue.catch(() => {}).then(async () => {
    if (state.currentReport?.id === report.id && state.savedDraft === content) return;
    const saved = await api(`/api/reports/${report.id}/draft`, { method: "PATCH", body: JSON.stringify({ draft: content }) });
    if (state.currentReport?.id === report.id) {
      state.savedDraft = saved.draft;
      if (document.querySelector("#report-editor").value === saved.draft) document.querySelector("#draft-status").textContent = "草稿已自动保存";
    }
  });
  return draftSaveQueue;
}

async function flushDraftSave() {
  clearTimeout(draftSaveTimer);
  draftSaveTimer = null;
  if (state.currentReport?.status === "draft" && document.querySelector("#report-editor").value !== state.savedDraft) {
    await queueDraftSave();
  } else {
    await draftSaveQueue;
  }
}

document.querySelector("#report-editor").addEventListener("input", () => {
  hideFinalReview();
  if (state.currentReport?.status !== "draft") return;
  const reportId = state.currentReport.id;
  document.querySelector("#draft-status").textContent = "草稿有未保存的修改";
  clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(() => {
    draftSaveTimer = null;
    queueDraftSave().catch(error => {
      if (state.currentReport?.id === reportId) document.querySelector("#draft-status").textContent = "自动保存失败 · 请重试";
      toast(error.message, true);
    });
  }, 600);
});

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard");
    document.querySelector("#stat-projects").textContent = data.projects;
    document.querySelector("#stat-reports").textContent = data.reports;
    document.querySelector("#stat-confirmed").textContent = data.confirmed_reports;
    document.querySelector("#stat-dataset").textContent = data.dataset.validation + data.dataset.holdout;
    renderReportList(document.querySelector("#recent-reports"), data.recent_reports, false);
    loadQuickWorkspace();
  } catch (error) { toast(error.message, true); }
}

async function loadQuickWorkspace() {
  try {
    const [projects, notes] = await Promise.all([api("/api/projects"), api(`/api/notes?note_date=${localDate}`)]);
    state.projects = projects;
    document.querySelector("#quick-note-project").innerHTML = `<option value="">不关联项目</option>${projects.map(project => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join("")}`;
    const list = document.querySelector("#today-notes");
    list.className = notes.length ? "stack-list" : "stack-list empty-state";
    list.innerHTML = notes.length ? notes.map(note => `<div class="stack-item"><div><strong>${escapeHtml(note.content)}</strong><small>${note.project_name ? escapeHtml(note.project_name) + " · " : ""}${new Date(note.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</small></div></div>`).join("") : "还没有快速记录。";
  } catch (error) { toast(error.message, true); }
}

async function loadProjects() {
  try {
    state.projects = await api("/api/projects");
    const list = document.querySelector("#project-list");
    list.innerHTML = state.projects.length ? state.projects.map(project => `<div class="stack-item"><div><strong>${escapeHtml(project.name)}</strong><small class="path" title="${escapeHtml(project.path)}">${escapeHtml(project.path)}</small><small>${project.project_type === "git" ? "Git 项目 · 自动采集提交" : "普通项目 · 仅手动记录"}</small></div><button class="delete-button" data-delete-project="${project.id}" title="删除">×</button></div>`).join("") : `<div class="empty-state">还没有项目。</div>`;
    list.querySelectorAll("[data-delete-project]").forEach(button => button.addEventListener("click", () => deleteProject(button.dataset.deleteProject)));
    const options = document.querySelector("#project-options");
    options.innerHTML = state.projects.length ? state.projects.map(project => `<label class="project-check"><input type="checkbox" value="${project.id}" checked>${escapeHtml(project.name)}</label>`).join("") : `<span class="muted">请先在“项目管理”中添加项目。</span>`;
  } catch (error) { toast(error.message, true); }
}

document.querySelector("#project-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/projects", { method: "POST", body: JSON.stringify({ name: document.querySelector("#project-name").value, path: document.querySelector("#project-path").value, project_type: document.querySelector("#project-type").value }) });
    event.target.reset();
    toast("项目已添加");
    loadProjects();
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#project-type").addEventListener("change", event => {
  document.querySelector("#project-type-hint").textContent = event.target.value === "git"
    ? "Git 项目需选择包含 .git 的根目录，系统会自动采集提交。"
    : "普通项目只需要存在的文件夹，日报仅使用你手动填写的内容。";
});

document.querySelector("#smart-parse").addEventListener("click", async event => {
  const content = document.querySelector("#smart-summary").value.trim();
  if (!content) return toast("请先输入一段工作描述", true);
  event.currentTarget.disabled = true;
  try {
    const parsed = await api("/api/reports/parse-work-summary", { method: "POST", body: JSON.stringify({ content }) });
    document.querySelector("#daily-completed").value = parsed.completed;
    document.querySelector("#daily-problems").value = parsed.problems;
    document.querySelector("#daily-tomorrow").value = parsed.tomorrow;
    toast("已拆分到日报表单，请检查后生成初稿");
  } catch (error) { toast(error.message, true); }
  finally { event.currentTarget.disabled = false; }
});

document.querySelector("#quick-note-form").addEventListener("submit", async event => {
  event.preventDefault();
  const content = document.querySelector("#quick-note-content");
  try {
    await api("/api/notes", { method: "POST", body: JSON.stringify({ note_date: localDate, content: content.value, project_id: document.querySelector("#quick-note-project").value || null }) });
    content.value = "";
    toast("今日记录已保存");
    loadQuickWorkspace();
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#quick-daily").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await flushDraftSave();
    const report = await api("/api/reports/daily/quick", { method: "POST", body: JSON.stringify({ report_date: localDate }) });
    switchView("daily");
    showDraft(report);
    toast("今日工作已汇总，可直接编辑确认");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

async function deleteProject(id) {
  if (!confirm("只从 EvoReport 移除该项目，不会删除本地文件。继续吗？")) return;
  try { await api(`/api/projects/${id}`, { method: "DELETE" }); toast("项目已移除"); loadProjects(); } catch (error) { toast(error.message, true); }
}

document.querySelector("#daily-form").addEventListener("submit", async event => {
  event.preventDefault();
  const projectIds = [...document.querySelectorAll("#project-options input:checked")].map(item => item.value);
  const button = event.submitter;
  button.disabled = true; button.textContent = "正在读取 Git 活动…";
  try {
    await flushDraftSave();
    const report = await api("/api/reports/daily", { method: "POST", body: JSON.stringify({ report_date: document.querySelector("#report-date").value, project_ids: projectIds, completed: document.querySelector("#daily-completed").value, problems: document.querySelector("#daily-problems").value, tomorrow: document.querySelector("#daily-tomorrow").value }) });
    showDraft(report);
    toast("日报初稿已生成");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "根据事实生成初稿"; refreshDailyQueue(); }
});

function hideFinalReview() {
  document.querySelector("#final-review").hidden = true;
  state.reviewedText = null;
  state.reviewedReportId = null;
}

function showFinalReview(review, finalText) {
  const panel = document.querySelector("#final-review");
  const list = document.querySelector("#final-review-issues");
  list.replaceChildren();
  for (const issue of review.issues || []) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = issue.summary;
    item.append(title);
    const structure = issue.structure?.action || issue.structure?.object
      ? `结构：${issue.structure.polarity === "pending" ? "未完成/计划" : "已完成"} · 动作 ${issue.structure.action || "未识别"} · 对象 ${issue.structure.object || "未识别"}` : "";
    const components = issue.score_components && Object.keys(issue.score_components).length
      ? `评分组成：顺序 ${Math.round(issue.score_components.sequence * 100)}% · 字符 ${Math.round(issue.score_components.ngram * 100)}% · 结构语义 ${Math.round(issue.score_components.semantic * 100)}%` : "";
    const details = [issue.evidence_id ? `证据编号：${issue.evidence_id}` : "", issue.evidence,
      issue.support_status === "uncertain" ? "判断：待核实" : issue.support_status === "unsupported" ? "判断：无证据" : "",
      structure, issue.match_score === null ? "" : `综合匹配度：${Math.round(issue.match_score * 100)}%`, components,
      issue.suggestion];
    for (const detail of details.filter(Boolean)) {
      const line = document.createElement("small");
      line.textContent = detail;
      item.append(line);
    }
    list.append(item);
  }
  document.querySelector("#review-acknowledge").hidden = review.unsafe;
  state.reviewedText = finalText;
  state.reviewedReportId = state.currentReport?.id;
  panel.hidden = false;
}

async function confirmCurrentReport(acknowledgeWarnings = false) {
  if (!state.currentReport) return;
  const button = document.querySelector("#confirm-report");
  const acknowledgeButton = document.querySelector("#review-acknowledge");
  const editor = document.querySelector("#report-editor");
  button.disabled = true;
  acknowledgeButton.disabled = true;
  try {
    await flushDraftSave();
    editor.readOnly = true;
    const finalText = editor.value;
    const path = `/api/reports/${state.currentReport.id}/confirm`;
    const confirmed = await api(path, { method: "POST", body: JSON.stringify({ final_text: finalText, acknowledge_warnings: acknowledgeWarnings }) });
    hideFinalReview();
    state.currentReport = confirmed;
    document.querySelector("#draft-status").textContent = "已确认 · 已加入个人数据集";
    const check = confirmed.post_activation_check;
    toast(check?.decision === "rolled_back" ? "终稿已保存；新策略连续退化，已自动回滚" :
      check?.decision === "error" ? "终稿已保存；策略监测暂时失败" : "终稿已保存", check?.decision === "error");
  } catch (error) {
    if (error.review) {
      showFinalReview(error.review, editor.value);
      document.querySelector("#draft-status").textContent = error.review.unsafe ? "终稿含敏感信息 · 请修改后重试" : "终稿待检查 · 尚未确认";
    }
    toast(error.message, true);
  }
  finally {
    acknowledgeButton.disabled = false;
    if (state.currentReport?.status === "draft") {
      button.disabled = false;
      editor.readOnly = false;
    }
  }
}

document.querySelector("#confirm-report").addEventListener("click", () => confirmCurrentReport());
document.querySelector("#review-edit").addEventListener("click", () => {
  hideFinalReview();
  document.querySelector("#report-editor").focus();
});
document.querySelector("#review-acknowledge").addEventListener("click", () => {
  if (state.reviewedReportId !== state.currentReport?.id || state.reviewedText !== document.querySelector("#report-editor").value) {
    hideFinalReview();
    return confirmCurrentReport();
  }
  return confirmCurrentReport(true);
});

document.querySelector("#copy-report").addEventListener("click", () => copyText(document.querySelector("#report-editor").value));
document.querySelector("#copy-history").addEventListener("click", () => copyText(document.querySelector("#history-preview").textContent));
document.querySelector("#export-history").addEventListener("click", () => {
  if (!state.selectedHistoryReport) return toast("请先选择一篇报告", true);
  window.location.assign(`/api/reports/${state.selectedHistoryReport.id}/export`);
});
document.querySelector("#export-history-word").addEventListener("click", () => {
  if (!state.selectedHistoryReport) return toast("请先选择一篇报告", true);
  window.location.assign(`/api/reports/${state.selectedHistoryReport.id}/export/docx`);
});
document.querySelector("#export-confirmed").addEventListener("click", () => window.location.assign("/api/reports/export/confirmed"));
document.querySelector("#create-backup").addEventListener("click", async () => {
  try { const backup = await api("/api/backups", { method: "POST" }); toast(`备份已创建：${backup.filename}`); }
  catch (error) { toast(error.message, true); }
});

document.querySelector("#restore-file").addEventListener("change", () => {
  state.backupPreview = null;
  document.querySelector("#restore-backup").disabled = true;
  document.querySelector("#restore-preview").textContent = "文件已选择，请先检查备份。";
});
document.querySelector("#preview-backup").addEventListener("click", async event => {
  const file = document.querySelector("#restore-file").files[0];
  if (!file) return toast("请先选择 .db 备份文件", true);
  event.currentTarget.disabled = true;
  state.backupPreview = null;
  document.querySelector("#restore-backup").disabled = true;
  try {
    const response = await fetch("/api/backups/preview", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "备份检查失败");
    state.backupPreview = result;
    const counts = result.counts;
    document.querySelector("#restore-preview").textContent = `备份有效：${counts.projects} 个项目、${counts.reports} 篇报告（其中已确认 ${counts.confirmed_reports} 篇）、${counts.work_notes} 条随手记录。`;
    document.querySelector("#restore-backup").disabled = false;
  } catch (error) {
    document.querySelector("#restore-preview").textContent = error.message;
    toast(error.message, true);
  } finally { event.currentTarget.disabled = false; }
});
document.querySelector("#restore-backup").addEventListener("click", async event => {
  if (!state.backupPreview) return;
  if (!confirm("恢复会用备份中的数据替换当前日报数据。系统会先自动备份当前数据库。确定继续吗？")) return;
  event.currentTarget.disabled = true;
  try {
    const result = await api("/api/backups/restore", { method: "POST", body: JSON.stringify({ preview_id: state.backupPreview.preview_id, confirmation: "RESTORE" }) });
    state.backupPreview = null;
    state.selectedHistoryReport = null;
    state.currentReport = null;
    state.replayResults = {};
    state.savedDraft = "";
    clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
    document.querySelector("#report-editor").value = "";
    document.querySelector("#report-editor").readOnly = false;
    document.querySelector("#confirm-report").disabled = true;
    document.querySelector("#draft-status").textContent = "尚未生成";
    document.querySelector("#resume-history").hidden = true;
    document.querySelector("#history-preview").textContent = "选择一份报告查看详情。";
    document.querySelector("#history-facts").textContent = "选择一份日报查看归档事实。";
    resetReviewLabels();
    document.querySelector("#history-filter-form").reset();
    document.querySelector("#restore-preview").textContent = `恢复完成。恢复前的数据已保存为 ${result.safety_backup}。`;
    document.querySelector("#restore-file").value = "";
    await loadReports();
    toast("数据已恢复，请刷新其他页面查看最新内容");
  } catch (error) {
    event.currentTarget.disabled = false;
    toast(error.message, true);
  }
});

async function loadReports() {
  try {
    const projectSelect = document.querySelector("#history-project");
    const selectedProject = projectSelect.value;
    const projects = await api("/api/projects");
    projectSelect.innerHTML = `<option value="">全部项目</option>${projects.map(project => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)}</option>`).join("")}`;
    projectSelect.value = projects.some(project => project.id === selectedProject) ? selectedProject : "";
    const start = document.querySelector("#history-start").value;
    const end = document.querySelector("#history-end").value;
    if (start && end && start > end) throw new Error("开始日期不能晚于结束日期");
    const params = new URLSearchParams();
    if (projectSelect.value) params.set("project_id", projectSelect.value);
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    state.reports = await api(`/api/reports${params.size ? `?${params}` : ""}`);
    const list = document.querySelector("#history-list");
    renderReportList(list, state.reports, true);
    const selected = state.reports.find(report => report.id === state.selectedHistoryReport?.id);
    if (selected) selectReport(selected.id, list.querySelector(`[data-report-id="${selected.id}"]`));
    else {
      state.selectedHistoryReport = null;
      document.querySelector("#history-preview").textContent = "选择一份报告查看详情。";
      document.querySelector("#history-facts").textContent = "选择一份日报查看归档事实。";
      document.querySelector("#resume-history").hidden = true;
      resetReviewLabels();
    }
    loadReviewBenchmark();
  }
  catch (error) { toast(error.message, true); }
}

document.querySelector("#history-filter-form").addEventListener("submit", event => { event.preventDefault(); loadReports(); });
document.querySelector("#history-filter-clear").addEventListener("click", () => {
  document.querySelector("#history-filter-form").reset();
  loadReports();
});

function renderReportList(element, reports, selectable) {
  if (!reports.length) { element.className = "report-list empty-state"; element.textContent = "还没有报告。"; return; }
  element.className = "report-list";
  element.innerHTML = reports.map(report => `<div class="report-item" data-report-id="${report.id}"><div><strong>${report.kind === "weekly" ? "工作周报" : "工作日报"} · ${report.report_date}</strong><small>${report.origin === "historical" ? "历史导入·" : report.origin === "synthetic" ? "体验数据·不参与进化" : "策略 v" + report.strategy_version}</small></div><span class="tag ${report.status}">${report.origin === "historical" ? "历史样本" : report.origin === "synthetic" ? "模拟样本" : report.status === "confirmed" ? "已确认" : "草稿"}</span></div>`).join("");
  if (selectable) element.querySelectorAll("[data-report-id]").forEach(item => item.addEventListener("click", () => selectReport(item.dataset.reportId, item)));
}

function selectReport(id, element) {
  const report = state.reports.find(item => item.id === id);
  if (!report) return;
  state.selectedHistoryReport = report;
  document.querySelectorAll(".report-item").forEach(item => item.classList.remove("selected"));
  element.classList.add("selected");
  document.querySelector("#history-preview").textContent = report.final_text || report.draft;
  const facts = report.source?.facts || [];
  const sources = report.source?.fact_sources || [];
  const archive = document.querySelector("#history-facts");
  archive.className = facts.length ? "stack-list" : "stack-list empty-state";
  archive.innerHTML = facts.length ? facts.map((fact, index) => {
    const source = sources[index] || {};
    const location = source.source_type === "git_commit"
      ? `${source.project_name || "项目"} · Git 提交 ${String(source.commit_hash || "").slice(0, 8) || "未知"}`
      : source.source_type === "manual" ? `手工记录第 ${source.line || "?"} 行 · 未指定项目` : "来源未记录";
    return `<div class="stack-item"><div><strong>${escapeHtml(fact)}</strong><small>${escapeHtml(location)}</small></div></div>`;
  }).join("") : "这份报告没有可追溯的任务事实。";
  if (report.kind === "daily" && report.origin === "generated" && report.status === "confirmed") loadReviewLabels(report.id);
  else resetReviewLabels("此报告不在可标注的 Validation 系统日报范围内。");
  document.querySelector("#resume-history").hidden = !(report.kind === "daily" && report.status === "draft" && report.origin === "generated");
}

function resetReviewLabels(message = "选择一份日报查看可标注事项。") {
  reviewAutoClaims = [];
  reviewCompletionLines = [];
  reviewMissedClaims = [];
  const list = document.querySelector("#review-label-list");
  list.className = "stack-list empty-state";
  list.textContent = message;
  document.querySelector("#review-missed-section").hidden = true;
  document.querySelector("#save-review-labels").hidden = true;
}

const reviewScenarioNames = { compound: "复合事项", paraphrase: "同义改写", implicit: "隐含事项" };
const benchmarkScenarioNames = {
  "origin:generated": "系统生成", "origin:historical": "历史导入",
  "source_types:manual": "手工来源", "source_types:git": "Git 来源", "source_types:mixed": "混合来源", "source_types:unattributed": "来源未标注",
  "project_count:zero": "无项目", "project_count:one": "单项目", "project_count:multiple": "多项目",
  "length:short": "短篇", "length:medium": "中篇", "length:long": "长篇",
  "edits:changed": "终稿有修改", "edits:unchanged": "终稿无修改", "edits:unknown": "修改情况未知",
  "review:compound": "复合事项", "review:paraphrase": "同义改写", "review:implicit": "隐含事项",
};

function reviewScenarioControls(kind, index, selected = []) {
  return `<div class="review-scenarios"><small>场景（可多选）：</small>${Object.entries(reviewScenarioNames).map(([key, label]) =>
    `<label><input type="checkbox" data-review-scenario="${kind}" data-review-scenario-index="${index}" value="${key}" ${selected.includes(key) ? "checked" : ""}>${label}</label>`).join(" ")}</div>`;
}

function renderMissedClaims() {
  const list = document.querySelector("#review-missed-list");
  list.className = reviewMissedClaims.length ? "stack-list" : "stack-list empty-state";
  list.innerHTML = reviewMissedClaims.length ? reviewMissedClaims.map((claim, index) => {
    const line = reviewCompletionLines.find(item => item.line === claim.line)?.text || "";
    const excerpt = line.slice(claim.column_start, claim.column_end);
    const parent = reviewAutoClaims[claim.parent_claim_index];
    const relation = parent ? `属于已识别事项 ${parent.index + 1}：${escapeHtml(parent.text)} · 按父项计一次`
      : "独立漏报 · 单独计一次";
    return `<div class="stack-item"><div><strong>第 ${claim.line} 行 · ${escapeHtml(excerpt)}</strong><small>${relation}</small>${reviewScenarioControls("missed", index, claim.scenarios)}</div><button class="ghost" type="button" data-remove-missed="${index}">移除</button></div>`;
  }).join("") : "尚未补标漏检事项。";
  document.querySelectorAll("[data-review-parent-note]").forEach(note => {
    const count = reviewMissedClaims.filter(item => item.parent_claim_index === Number(note.dataset.reviewParentNote)).length;
    note.textContent = count ? `包含 ${count} 条人工补标子句；基准按本事项计一次` : "";
  });
  document.querySelector("#save-review-labels").hidden = !reviewAutoClaims.length && !reviewMissedClaims.length;
}

async function loadReviewLabels(reportId) {
  resetReviewLabels("正在读取标注事项。");
  try {
    const data = await api(`/api/reports/${reportId}/review-labels`);
    if (state.selectedHistoryReport?.id !== reportId) return;
    reviewAutoClaims = data.claims;
    reviewCompletionLines = data.completion_lines;
    reviewMissedClaims = data.missed_claims.map(({ line, column_start, column_end, parent_claim_index, scenarios }) =>
      ({ line, column_start, column_end, parent_claim_index, scenarios }));
    const list = document.querySelector("#review-label-list");
    list.className = data.claims.length ? "stack-list" : "stack-list empty-state";
    list.innerHTML = data.claims.length ? data.claims.map(claim => `<div class="stack-item"><div><strong>第 ${claim.line} 行 · ${escapeHtml(claim.text)}</strong><small>结构：${claim.structure.polarity === "pending" ? "未完成/计划" : "已完成"} · 动作 ${escapeHtml(claim.structure.action || "未识别")} · 对象 ${escapeHtml(claim.structure.object || "未识别")}</small><small>Critic：${claim.support_status === "uncertain" ? "待核实" : claim.support_status === "unsupported" ? "无证据" : "有证据"}${data.stale ? " · 原标注已失效，请重新核对" : ""}</small><small data-review-parent-note="${claim.index}"></small>${reviewScenarioControls("claim", claim.index, claim.scenarios)}</div><label>人工判断<select data-review-label-index="${claim.index}"><option value="">未标注</option><option value="supported" ${claim.label === "supported" ? "selected" : ""}>有来源</option><option value="unsupported" ${claim.label === "unsupported" ? "selected" : ""}>无来源</option></select></label></div>`).join("") : data.stale ? "原标注已失效，请重新核对。" : "没有自动识别的完成事项，可在下方补标。";
    const section = document.querySelector("#review-missed-section");
    section.hidden = !data.completion_lines.length;
    document.querySelector("#review-missed-line").innerHTML = data.completion_lines.map(item => `<option value="${item.line}">第 ${item.line} 行 · ${escapeHtml(item.text)}</option>`).join("");
    renderMissedClaims();
  } catch (error) {
    if (state.selectedHistoryReport?.id === reportId) resetReviewLabels(error.message);
  }
}

document.querySelector("#review-missed-form").addEventListener("submit", event => {
  event.preventDefault();
  const lineNumber = Number(document.querySelector("#review-missed-line").value);
  const line = reviewCompletionLines.find(item => item.line === lineNumber)?.text || "";
  const fragment = document.querySelector("#review-missed-text").value.trim();
  const start = line.indexOf(fragment);
  if (!fragment || start < 0 || line.indexOf(fragment, start + 1) >= 0) return toast("请从所选行复制唯一的完整片段", true);
  const end = start + fragment.length;
  const overlapping = reviewAutoClaims.filter(claim => claim.line === lineNumber
    && start < claim.column_end && claim.column_start < end);
  if (overlapping.length && (overlapping.length !== 1 || start < overlapping[0].column_start
    || end > overlapping[0].column_end || (start === overlapping[0].column_start && end === overlapping[0].column_end))) {
    return toast("子句须完整位于一个已识别事项内", true);
  }
  if (reviewMissedClaims.some(claim => claim.line === lineNumber && start < claim.column_end && claim.column_start < end)) {
    return toast("片段与已有补标重叠，请调整选择", true);
  }
  const parent = overlapping[0] || null;
  reviewMissedClaims.push({ line: lineNumber, column_start: start, column_end: end,
    parent_claim_index: parent?.index ?? null, scenarios: [] });
  if (parent) document.querySelector(`[data-review-label-index="${parent.index}"]`).value = "unsupported";
  document.querySelector("#review-missed-text").value = "";
  renderMissedClaims();
});

document.querySelector("#review-missed-list").addEventListener("click", event => {
  const button = event.target.closest("[data-remove-missed]");
  if (!button) return;
  reviewMissedClaims.splice(Number(button.dataset.removeMissed), 1);
  renderMissedClaims();
});

document.querySelector("#review-missed-list").addEventListener("change", event => {
  const input = event.target;
  if (input.dataset.reviewScenario !== "missed") return;
  const index = Number(input.dataset.reviewScenarioIndex);
  reviewMissedClaims[index].scenarios = [...document.querySelectorAll(`[data-review-scenario="missed"][data-review-scenario-index="${index}"]:checked`)].map(item => item.value);
});

async function loadReviewBenchmark() {
  try {
    const data = await api("/api/review-benchmark");
    const list = document.querySelector("#review-benchmark");
    const counts = data.counts;
    list.className = data.labeled_claims ? "stack-list" : "stack-list empty-state";
    const precision = data.precision === null ? "暂无可计算结果" : `${Math.round(data.precision * 100)}%`;
    const recall = data.recall === null ? "暂无可计算结果" : `${Math.round(data.recall * 100)}%`;
    const decisions = data.decision_counts;
    const scenarios = Object.entries(reviewScenarioNames).map(([key, name]) => {
      const item = data.scenario_coverage.scenarios[key];
      return `${name} ${item.items} 项（误报 ${item.false_positive}、漏报 ${item.false_negative}）`;
    }).join("；");
    list.innerHTML = data.labeled_claims ? `<div class="stack-item"><div><strong>${data.status === "ready" ? `精确率 ${precision} · 召回率 ${recall}` : "样本不足，暂不显示精确率和召回率"}</strong><small>已标注 ${data.report_count} 份、${data.labeled_claims} 项、${data.project_count} 个项目；其中复合子句 ${data.nested_claims} 条按父项计数；命中 ${counts.true_positive}，误报 ${counts.false_positive}，漏报 ${counts.false_negative}，正确放行 ${counts.true_negative}；失效报告 ${data.stale_reports} 份。至少需要 3 份报告、10 项标注。</small><small>自动判断：有证据 ${decisions.supported} 项、待核实 ${decisions.uncertain} 项、无证据 ${decisions.unsupported} 项。</small><small>场景覆盖：${scenarios}；未标场景 ${data.scenario_coverage.untagged} 项。场景可重叠，由人工标注。</small></div></div>${data.mismatches.map(item => `<div class="stack-item"><div><strong>${item.type === "false_positive" ? "误报" : "漏报"} · 第 ${item.line} 行：${escapeHtml(item.text)}</strong><small>${item.child_texts?.length ? `补标子句：${item.child_texts.map(escapeHtml).join("、")} · ` : ""}报告 ${escapeHtml(item.report_id.slice(0, 8))}</small></div></div>`).join("")}` : data.stale_reports ? `有 ${data.stale_reports} 份标注已失效，请重新核对。` : "尚无有效人工标注。";
  } catch (error) { toast(error.message, true); }
}

document.querySelector("#save-review-labels").addEventListener("click", async event => {
  const reportId = state.selectedHistoryReport?.id;
  if (!reportId) return;
  const labels = [...document.querySelectorAll("[data-review-label-index]")].map(select => ({ index: Number(select.dataset.reviewLabelIndex), label: select.value,
    scenarios: [...document.querySelectorAll(`[data-review-scenario="claim"][data-review-scenario-index="${select.dataset.reviewLabelIndex}"]:checked`)].map(item => item.value) }));
  if (labels.some(item => !item.label)) return toast("请标注这份终稿的每个完成事项", true);
  if (reviewMissedClaims.some(item => item.parent_claim_index !== null && labels[item.parent_claim_index].label !== "unsupported")) {
    return toast("含补标子句的父事项须标为无来源", true);
  }
  event.currentTarget.disabled = true;
  try {
    const missedClaims = reviewMissedClaims.map(({ line, column_start, column_end, scenarios }) => ({ line, column_start, column_end, scenarios }));
    await api(`/api/reports/${reportId}/review-labels`, { method: "PUT", body: JSON.stringify({ labels, missed_claims: missedClaims }) });
    toast("整篇标注已保存");
    await loadReviewLabels(reportId);
    await loadReviewBenchmark();
  } catch (error) { toast(error.message, true); }
  finally { event.currentTarget.disabled = false; }
});

document.querySelector("#resume-history").addEventListener("click", async () => {
  if (!state.selectedHistoryReport || state.selectedHistoryReport.status !== "draft") return;
  try {
    await flushDraftSave();
    showDraft(state.selectedHistoryReport, true);
    switchView("daily");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#weekly-form").addEventListener("submit", async event => {
  event.preventDefault();
  try { await api("/api/reports/weekly", { method: "POST", body: JSON.stringify({ week_start: document.querySelector("#week-start").value }) }); toast("周报初稿已生成"); loadReports(); }
  catch (error) { toast(error.message, true); }
});

document.querySelector("#historical-import-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/reports/historical", { method: "POST", body: JSON.stringify({ report_date: document.querySelector("#historical-date").value, content: document.querySelector("#historical-content").value }) });
    event.target.reset();
    toast("历史日报已导入为个人样本");
    loadReports();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

function renderCriticRules(data) {
  const list = document.querySelector("#critic-rule-list");
  const monitor = document.querySelector("#critic-rule-monitor");
  const frozen = data.frozen_benchmark;
  document.querySelector("#create-critic-benchmark").disabled = frozen.state === "collecting";
  const counts = data.benchmark.counts;
  const positive = counts.true_positive + counts.false_negative;
  const negative = counts.false_positive + counts.true_negative;
  const ready = data.benchmark.status === "ready" && positive >= 2 && negative >= 2;
  const sampleStatus = ready ? `可评测：${data.benchmark.report_count} 份、${data.benchmark.labeled_claims} 项、${data.benchmark.project_count} 个项目；无来源 ${positive} 项，有来源 ${negative} 项。冻结还需至少 2 个项目。`
    : `标注不足：现有 ${data.benchmark.report_count} 份、${data.benchmark.labeled_claims} 项、${data.benchmark.project_count} 个项目；无来源 ${positive} 项，有来源 ${negative} 项。冻结需至少 3 份、10 项、2 个项目，且两类各 2 项。`;
  const typeName = { true_positive: "命中", false_positive: "误报", false_negative: "漏报", true_negative: "正确放行" };
  const candidates = data.versions.filter(item => item.source !== "builtin");
  list.className = "stack-list";
  const holdoutStatus = frozen.state === "not_frozen" ? "尚未冻结审查 Validation；当前离线结果仅供探索。"
    : frozen.state === "used" ? `审查基准 v${frozen.version} 的 Holdout 已使用：${frozen.result.decision === "passed" ? "通过" : "未通过"}，候选 v${frozen.result.candidate_version}。如需再次门禁，请建立新基准并收集新标注。`
    : `审查基准 v${frozen.version}：Holdout 已收集 ${frozen.holdout_reports} 份、${frozen.holdout_claims} 项、${frozen.holdout_projects} 个项目；至少需 2 份、6 项、2 个项目，且两类各 2 项。${frozen.validation_stale ? "Validation 标注已变化，需新建基准。" : ""}`;
  const backToBuiltin = data.active_version !== 1 ? '<button class="ghost" data-activate-critic="1">切回内置 v1</button>' : '';
  list.innerHTML = `<div class="stack-item"><div><strong>内置规则 60% · ${data.active_version === 1 ? "当前使用" : "可回滚"}</strong><small>${sampleStatus}</small><small>标注集指纹 ${escapeHtml(data.corpus_fingerprint.slice(0, 12))}；${escapeHtml(holdoutStatus)}</small></div>${backToBuiltin}</div>`
    + (candidates.length ? candidates.map(item => {
      const latest = data.evaluations.find(run => run.candidate_version === item.version);
      const current = data.evaluations.find(run => run.candidate_version === item.version && !run.stale);
      const summary = latest ? `<small>${latest.stale ? "旧标注集结果，需重评" : "当前标注集"} · 误报 ${latest.baseline.counts.false_positive} → ${latest.candidate.counts.false_positive}，漏报 ${latest.baseline.counts.false_negative} → ${latest.candidate.counts.false_negative} · ${latest.decision === "improved" ? "两类错误未增加，至少一类下降" : "未满足双项不退化条件"}</small>` : "<small>尚未评测</small>";
      const changes = latest?.changes.slice(0, 20).map(change => `<small>报告 ${escapeHtml(change.report_id.slice(0, 8))} · 第 ${change.line} 行 · ${typeName[change.before]} → ${typeName[change.after]}${change.nested_count ? ` · 含 ${change.nested_count} 条补标子句` : ""}</small>`).join("") || "";
      const canGate = frozen.state === "collecting" && frozen.holdout_ready && !frozen.validation_stale && current?.decision === "improved";
      const wasActive = data.switches.some(change => change.active_version === item.version);
      const gatePassed = frozen.state === "used" && frozen.result.decision === "passed" && frozen.result.candidate_version === item.version;
      const canActivate = data.active_version !== item.version && (wasActive || gatePassed && data.active_version === 1);
      const match = item.skill.claim_match;
      const proposal = item.source === "validation_proposal" ? `<small>Validation 自动候选 · 搜索 ${item.proposal.searched_candidates || 0} 组参数 · ${escapeHtml(item.proposal.reason || "")}</small>` : "<small>手动候选</small>";
      const semantic = item.skill.schema_version >= 2 ? ` · 结构语义权重 ${match.semantic_weight_percent}% · 待核实区间 ${match.uncertainty_margin_percent}%` : "";
      return `<div class="stack-item"><div><strong>Skill v${item.version} · 阈值 ${match.threshold_percent}% · 字符重合权重 ${match.ngram_overlap_weight_percent}%${semantic} ${data.active_version === item.version ? "· 当前使用" : ""}</strong><small>schema v${item.skill.schema_version} · 强制检查 ${item.skill.required_checks.map(escapeHtml).join("、")} · 指纹 ${escapeHtml(item.fingerprint.slice(0, 12))}</small>${proposal}${summary}${changes}${latest?.changes.length > 20 ? `<small>另有 ${latest.changes.length - 20} 项变化</small>` : ""}</div><div><button class="ghost" data-evaluate-critic="${item.version}" ${ready && !current ? "" : "disabled"}>${current ? "当前标注集已评测" : "离线评测"}</button><button class="ghost" data-gate-critic="${item.version}" ${canGate ? "" : "disabled"}>一次性 Holdout 门禁</button><button class="ghost" data-activate-critic="${item.version}" ${canActivate ? "" : "disabled"}>${data.active_version === item.version ? "当前使用" : wasActive ? "切回此版" : "手动启用"}</button></div></div>`;
    }).join("") : '<div class="stack-item">尚无候选规则。</div>');
  list.querySelectorAll("[data-evaluate-critic]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api(`/api/agents/critic-rules/${button.dataset.evaluateCritic}/evaluate`, { method: "POST" });
      toast(result.decision === "improved" ? "候选在当前标注集减少了错误" : "候选没有通过离线对照");
      loadEvolution();
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  list.querySelectorAll("[data-gate-critic]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api(`/api/agents/critic-rules/${button.dataset.gateCritic}/gate`, { method: "POST" });
      toast(result.decision === "passed" ? "审查 Holdout 门禁通过" : "审查 Holdout 门禁未通过");
      loadEvolution();
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  list.querySelectorAll("[data-activate-critic]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const rule = await api(`/api/agents/critic-rules/${button.dataset.activateCritic}/activate`, { method: "POST" });
      toast(`Critic 规则已切换到 v${rule.version}`);
      loadEvolution();
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }));
  const observed = data.monitor;
  monitor.className = observed.status === "inactive" ? "stack-list empty-state" : "stack-list";
  monitor.innerHTML = observed.status === "inactive" ? "当前使用内置规则，暂无候选上线监测。"
    : `<div class="stack-item"><div><strong>v${observed.active_version} · ${observed.status === "ready" ? "已达到展示门槛" : "标注样本不足"}</strong><small>启用后新确认并标注 ${observed.report_count} 份、${observed.labeled_claims} 项；失效 ${observed.stale_reports} 份。至少需 3 份、10 项。</small>${observed.status === "ready" ? `<small>误报 ${observed.previous_counts.false_positive} → ${observed.counts.false_positive}；漏报 ${observed.previous_counts.false_negative} → ${observed.counts.false_negative}（前版 → 当前版）</small>` : ""}</div></div>`;
}

document.querySelector("#create-critic-benchmark").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  try {
    const result = await api("/api/agents/critic-benchmarks", { method: "POST" });
    toast(`审查 Validation 已冻结为 v${result.version}；请继续标注新报告以收集 Holdout`);
    loadEvolution();
  } catch (error) { toast(error.message, true); event.currentTarget.disabled = false; }
});

document.querySelector("#propose-critic-skill").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  try {
    const result = await api("/api/agents/critic-skills/propose", { method: "POST" });
    toast(`已从 Validation 生成并评测 Critic Skill v${result.version.version}`);
    loadEvolution();
  } catch (error) { toast(error.message, true); event.currentTarget.disabled = false; }
});

document.querySelector("#critic-rule-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    const value = Number(document.querySelector("#critic-rule-threshold").value);
    const weight = Number(document.querySelector("#critic-rule-ngram-weight").value);
    const result = await api("/api/agents/critic-rules", { method: "POST", body: JSON.stringify({ claim_threshold_percent: value, ngram_overlap_weight_percent: weight }) });
    toast(`Critic Skill v${result.version} 已保存`);
    loadEvolution();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

async function loadEvolution() {
  try {
    const [data, agentData, llm, recoverable, memoryData, projects, promptData, suggestionData, criticData] = await Promise.all([api("/api/evolution/status"), api("/api/agents/overview"), api("/api/llm/status"), api("/api/agents/recoverable"), api("/api/memories?include_expired=true"), api("/api/projects"), api("/api/agents/writer-prompts"), api("/api/memories/suggestions"), api("/api/agents/critic-rules")]);
    const dataset = data.dataset;
    const benchmark = data.benchmark;
    const targets = data.data_targets;
    const missingScenarios = Object.entries(reviewScenarioNames).filter(([key]) => !targets.review_scenarios.scenarios[key].items).map(([, name]) => name);
    document.querySelector("#create-benchmark").disabled = !benchmark.can_create_next;
    document.querySelector("#dataset-gate").innerHTML = `<div><strong>${benchmark.version ? `固定基准 v${benchmark.version}` : "尚未建立固定基准"}</strong><small>${benchmark.version ? `指纹 ${escapeHtml(benchmark.fingerprint.slice(0, 12))} · Holdout ${benchmark.holdout_used ? "已使用，需新版本" : "尚未使用"}` : "样本足够时可建立首个基准"}</small><small>待纳入新版本：Validation ${benchmark.new_validation} 份、全新 Holdout ${benchmark.new_holdout} 份</small></div><div class="metric-row"><span class="metric">Validation ${dataset.validation}/${dataset.required_validation}</span><span class="metric">Holdout ${dataset.holdout}/${dataset.required_holdout}</span></div>`;
    const coverage = benchmark.coverage || data.validation_coverage;
    const coverageLabel = benchmark.version && benchmark.coverage ? `固定基准 v${benchmark.version}`
      : benchmark.version ? "旧基准未记录覆盖摘要；以下为当前 Validation" : "当前可用 Validation";
    const warnings = (coverage.warnings || []).map(item => `<small>提示：${escapeHtml(item)}</small>`).join("");
    document.querySelector("#benchmark-coverage").innerHTML = `<div class="stack-item"><div><strong>${coverageLabel} · ${coverage.sample_count} 份真实日报</strong><small>报告来源：系统生成 ${coverage.origin.generated} · 历史导入 ${coverage.origin.historical}</small><small>事实来源：手工 ${coverage.source_types.manual} · Git ${coverage.source_types.git} · 混合 ${coverage.source_types.mixed} · 未标注 ${coverage.source_types.unattributed}</small><small>所选项目：0 个 ${coverage.project_count.zero} · 1 个 ${coverage.project_count.one} · 至少 2 个 ${coverage.project_count.multiple}</small><small>终稿篇幅：少于 200 字 ${coverage.length.short} · 200–599 字 ${coverage.length.medium} · 至少 600 字 ${coverage.length.long}</small><small>相对初稿：有修改 ${coverage.edits.changed} · 未修改 ${coverage.edits.unchanged} · 无法比较 ${coverage.edits.unknown}</small>${warnings}</div></div><div class="stack-item"><div><strong>数据积累目标（不作为门禁）</strong><small>Validation ${targets.validation_current}/${targets.validation} · Holdout ${targets.holdout_current}/${targets.holdout} · 有效人工审查事项 ${targets.review_items_current}/${targets.review_items}</small><small>人工场景待补充：${missingScenarios.length ? missingScenarios.join("、") : "已有标注，仍需扩充"}。样本不足时评测结论仅适用于实际选取的场景。</small></div></div>`;
    renderCriticRules(criticData);
    const monitor = data.post_activation_monitor || { status: "unavailable" };
    const monitorPanel = document.querySelector("#post-activation-monitor");
    monitorPanel.className = monitor.status === "unavailable" ? "stack-list empty-state" : "stack-list";
    if (monitor.status === "unavailable") {
      monitorPanel.textContent = "尚无策略切换记录；激活新策略后开始收集本地验证样本。";
    } else {
      const reportIds = monitor.report_ids.slice(0, 20).map(id => escapeHtml(id.slice(0, 8))).join("、") || "无";
      const more = monitor.report_ids.length > 20 ? ` 等 ${monitor.report_ids.length} 份` : "";
      const result = monitor.status === "ready"
        ? `<div class="metric-row"><span class="metric">综合分 ${metricScore(monitor.previous_metrics)} → ${metricScore(monitor.active_metrics)}</span><span class="metric">安全性 ${Math.round(monitor.previous_metrics.safety * 100)}% → ${Math.round(monitor.active_metrics.safety * 100)}%</span></div>`
        : `<small>样本不足：${monitor.sample_count}/${monitor.required_samples}，暂不判断策略效果。</small>`;
      const sourceLabel = monitor.activation_source === "evaluation" ? "评测激活" : monitor.activation_source === "auto_rollback" ? "自动回滚" : "手动启用";
      monitorPanel.innerHTML = `<div class="stack-item"><div><strong>v${monitor.previous_version} → v${monitor.active_version} · ${sourceLabel}</strong><small>仅使用策略切换后新生成并确认的 Validation 日报，共 ${monitor.sample_count} 份。</small><small>报告编号：${reportIds}${more}</small>${result}</div></div>`;
    }
    const latestCheck = data.latest_monitor_check;
    if (latestCheck) {
      const groups = latestCheck.evidence.windows.map((group, index) => {
        const ids = group.report_ids.map(id => escapeHtml(id.slice(0, 8))).join("、");
        return `<small>第 ${index + 1} 组：${ids} · 综合分 ${metricScore(group.previous_metrics)} → ${metricScore(group.active_metrics)} · 安全性 ${Math.round(group.previous_metrics.safety * 100)}% → ${Math.round(group.active_metrics.safety * 100)}%</small>`;
      }).join("");
      monitorPanel.className = "stack-list";
      monitorPanel.innerHTML += `<div class="stack-item"><div><strong>最近判断：${latestCheck.decision === "rolled_back" ? "已自动回滚" : "继续监测"} · v${latestCheck.evidence.active_version} 对比 v${latestCheck.evidence.previous_version}</strong><small>${escapeHtml(latestCheck.reason)}</small>${groups}</div></div>`;
    }
    const activePrompt = promptData.versions.find(item => item.status === "active");
    document.querySelector("#writer-prompt-list").className = "stack-list";
    document.querySelector("#writer-prompt-list").innerHTML = promptData.versions.map(item => {
      const latest = promptData.evaluations.find(run => run.candidate_version === item.version);
      const currentRun = promptData.evaluations.find(run => run.candidate_version === item.version && run.benchmark_version === benchmark.version);
      const wasActive = Boolean(item.was_active);
      const canActivate = wasActive || latest?.decision === "passed" && latest.baseline_version === activePrompt?.version && latest.benchmark_version === benchmark.version;
      const canEvaluate = item.status !== "active" && promptData.model_enabled && benchmark.version && !benchmark.holdout_used && !currentRun;
      const decision = !latest ? "未评测" : ({ passed: "评测通过", failed: "评测未通过", deferred: "评测未完成", error: "评测失败", stale: "评测已失效", running: "评测中" })[latest.decision] || "未评测";
      const scores = latest?.metrics?.validation ? `<small>Validation ${metricScore(latest.metrics.validation.baseline)} → ${metricScore(latest.metrics.validation.candidate)}；Holdout ${metricScore(latest.metrics.holdout?.baseline)} → ${metricScore(latest.metrics.holdout?.candidate)}</small>` : "";
      const reportIds = [...(latest?.evidence?.validation_cases || []), ...(latest?.evidence?.holdout_cases || [])].map(entry => escapeHtml(entry.report_id.slice(0, 8))).join("、");
      const sceneCoverage = latest?.evidence?.validation_scenario_coverage;
      const sceneNote = sceneCoverage && !sceneCoverage.legacy ? `<small>Validation 已选 ${sceneCoverage.selected.length}/${sceneCoverage.available.length} 类可用标签${sceneCoverage.uncovered.length ? `；未选 ${sceneCoverage.uncovered.map(tag => escapeHtml(benchmarkScenarioNames[tag] || tag)).join("、")}` : ""}。结果仅反映抽取样本。</small>` : "";
      return `<div class="stack-item"><div><strong>v${item.version} · ${item.status === "active" ? "当前使用" : decision}</strong><small>${item.source === "builtin" ? "内置版本" : "手动创建"} · 指纹 ${escapeHtml(item.sha256.slice(0, 12))}</small><small>${escapeHtml(item.addendum || "默认写作指令")}</small>${latest ? `<small>基准 v${latest.benchmark_version} · ${escapeHtml(latest.reason)}</small>${scores}${sceneNote}<small>样本编号：${reportIds || "无"}</small>` : ""}</div><div>${item.status === "active" ? `<span class="tag">活动</span>` : `<button class="ghost" data-activate-prompt="${item.version}" ${canActivate ? "" : "disabled"}>启用</button>`}${item.status !== "active" && !wasActive ? `<button class="ghost" data-evaluate-prompt="${item.version}" ${canEvaluate ? "" : "disabled"}>${promptData.model_enabled ? "运行评测" : "模型未配置"}</button>` : ""}</div></div>`;
    }).join("");
    document.querySelectorAll("[data-activate-prompt]").forEach(button => button.addEventListener("click", () => activateWriterPrompt(button.dataset.activatePrompt)));
    document.querySelectorAll("[data-evaluate-prompt]").forEach(button => button.addEventListener("click", () => evaluateWriterPrompt(button.dataset.evaluatePrompt)));
    const promptSwitch = promptData.switches[0];
    const promptSource = { evaluation: "评测通过后启用", manual: "人工切换", auto_rollback: "自动回滚" };
    document.querySelector("#writer-prompt-switches").textContent = promptSwitch ? `最近切换：v${promptSwitch.previous_version} → v${promptSwitch.active_version} · ${promptSource[promptSwitch.source] || "人工切换"}` : "尚无指令切换记录。";
    const promptMonitor = document.querySelector("#writer-prompt-monitor");
    const promptMonitoring = promptData.post_activation_monitor;
    if (!promptMonitoring || promptMonitoring.status === "unavailable") {
      promptMonitor.className = "stack-list empty-state";
      promptMonitor.textContent = "尚无可监测的指令切换。";
    } else {
      const readyText = promptMonitoring.status === "ready"
        ? `最近 5 份综合分 ${metricScore(promptMonitoring.baseline_metrics)} → ${metricScore(promptMonitoring.active_metrics)}；安全性 ${Math.round(promptMonitoring.baseline_metrics.safety * 100)}% → ${Math.round(promptMonitoring.active_metrics.safety * 100)}%`
        : "样本达到门槛后才计算指标。";
      promptMonitor.className = "stack-list";
      promptMonitor.innerHTML = `<div class="stack-item"><div><strong>v${promptMonitoring.active_version} 对比 v${promptMonitoring.previous_version} · ${promptMonitoring.status === "ready" ? "正在监测" : "样本积累中"}</strong><small>上线前基线 ${promptMonitoring.baseline_sample_count}/${promptMonitoring.required_baseline_samples}；上线后 ${promptMonitoring.active_sample_count}/${promptMonitoring.required_active_samples}。仅统计实际使用模型的 Validation 日报。</small><small>${readyText}</small></div></div>`;
    }
    const latestPromptCheck = promptData.latest_monitor_check;
    if (latestPromptCheck) {
      const groups = latestPromptCheck.evidence.windows.map((group, index) => `<small>第 ${index + 1} 组：${group.report_ids.map(id => escapeHtml(id.slice(0, 8))).join("、")} · 综合分 ${metricScore(group.metrics)} · 安全性 ${Math.round(group.metrics.safety * 100)}%</small>`).join("");
      promptMonitor.className = "stack-list";
      promptMonitor.innerHTML += `<div class="stack-item"><div><strong>最近判断：${latestPromptCheck.decision === "rolled_back" ? "已自动回滚" : "继续监测"}</strong><small>${escapeHtml(latestPromptCheck.reason)}</small>${groups}</div></div>`;
    }
    const profile = data.writing_profile;
    const terms = profile.project_terms.length ? profile.project_terms.join("、") : "尚未识别到稳定术语";
    document.querySelector("#writing-profile").className = "stack-list";
    document.querySelector("#writing-profile").innerHTML = `<div class="stack-item"><div><strong>${profile.ready ? "画像已生效" : "画像样本不足"}</strong><small>仅统计 ${profile.sample_count} 篇 Validation 真实日报；已记录 ${data.validation_feedback_count} 条可用于后续学习的终稿修改反馈。</small></div></div><div class="stack-item"><div><strong>${profile.preferred_list} · 单条约 ${profile.average_item_length || "—"} 字</strong><small>列表偏好置信度 ${Math.round(profile.list_confidence * 100)}% · 开发统计：${profile.statistics_preference}</small></div></div><div class="stack-item"><div><strong>常用术语</strong><small>${escapeHtml(terms)}</small></div></div>`;
    const memoryLabels = { numbered: "编号列表", include_statistics: "开发统计", max_item_length: "单条字数", note: "项目背景" };
    const explanation = data.latest_explanation;
    document.querySelector("#evaluation-explanation").className = explanation ? "stack-list" : "stack-list empty-state";
    document.querySelector("#evaluation-explanation").innerHTML = explanation ? `<div class="stack-item"><div><strong>${explanation.decision === "activated" ? "候选策略已激活" : "候选策略未激活"}</strong><small>${escapeHtml(explanation.reason)}</small><small>基准 v${explanation.evidence?.benchmark_version || "—"} · 依据：${explanation.evidence?.feedback_report_ids?.length || 0} 份终稿修改、${explanation.evidence?.critic_run_ids?.length || 0} 次审查问题；${(explanation.evidence?.rules || []).map(rule => `${memoryLabels[rule.key] || "规则"} ${escapeHtml(rule.before)} → ${escapeHtml(rule.after)}（${escapeHtml(rule.source)}）`).join("；") || "无规则变化"}</small></div></div><div class="metric-row"><span class="metric">Validation ${metricScore(explanation.metrics.current_validation)} → ${metricScore(explanation.metrics.candidate_validation)}</span><span class="metric">Holdout ${metricScore(explanation.metrics.current_holdout)} → ${metricScore(explanation.metrics.candidate_holdout)}</span></div>` : "还没有评测记录。";
    state.memories = memoryData.memories;
    document.querySelector("#memory-project").innerHTML = projects.map(project => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)}</option>`).join("");
    document.querySelector("#project-memory-form button").disabled = !projects.length;
    document.querySelector("#memory-list").className = state.memories.length ? "stack-list" : "stack-list empty-state";
    document.querySelector("#memory-list").innerHTML = state.memories.length ? state.memories.map(item => `<div class="stack-item"><div><strong>${memoryLabels[item.memory_key] || "记忆"} · ${item.scope === "project" ? escapeHtml(projects.find(project => project.id === item.project_id)?.name || "项目") : "个人偏好"}${item.is_expired ? " · 已过期" : ""}</strong><small>${escapeHtml(item.value === "true" ? "启用" : item.value === "false" ? "不启用" : item.value)} · ${item.source_type === "evaluation" ? "评测激活记录" : item.source_type === "archive_candidate" ? "归档建议采纳" : item.source_type === "user_correction" ? "用户纠正" : "用户设置"}${item.scope === "project" ? ` · ${item.expires_on ? `有效至 ${escapeHtml(item.expires_on)}` : "长期有效"}` : ""}</small></div><div><button class="ghost" data-correct-memory="${escapeHtml(item.id)}">纠正</button><button class="ghost" data-remove-memory="${escapeHtml(item.id)}">清理</button></div></div>`).join("") : "还没有记忆。";
    document.querySelectorAll("[data-correct-memory]").forEach(button => button.addEventListener("click", () => correctMemory(button.dataset.correctMemory)));
    document.querySelectorAll("[data-remove-memory]").forEach(button => button.addEventListener("click", () => removeMemory(button.dataset.removeMemory)));
    const suggestions = suggestionData.suggestions;
    const suggestionList = document.querySelector("#memory-suggestion-list");
    suggestionList.className = suggestions.length ? "stack-list" : "stack-list empty-state";
    suggestionList.innerHTML = suggestions.length ? suggestions.map(item => {
      const projectName = projects.find(project => project.id === item.project_id)?.name || "项目";
      const locations = item.scope === "personal"
        ? item.evidence.map(source => `${source.report_date} · ${String(source.before)} → ${String(source.after)} · 报告 ${source.report_id.slice(0, 8)}`).join("；")
        : item.evidence.map(source => `${source.report_date} · ${({ manual_completed: "今日完成", manual_problems: "遇到的问题", manual_tomorrow: "明日计划" })[source.field]}第 ${source.line} 行 · 报告 ${source.report_id.slice(0, 8)}`).join("；");
      const value = item.value === "true" ? "启用" : item.value === "false" ? "不启用" : item.value;
      const relations = item.conflicts_with?.length ? `冲突：${item.conflicts_with.map(memory => memory.value).join("、")}`
        : item.merge_with?.length ? `将合并：${item.merge_with.map(memory => memory.value).join("、")}`
          : item.expired_matches?.length ? `历史过期项：${item.expired_matches.map(memory => memory.value).join("、")}` : "";
      const title = item.scope === "personal" ? `个人偏好 · ${memoryLabels[item.memory_key] || item.memory_key}` : `${projectName} · 项目背景`;
      return `<div class="stack-item"><div><strong>${escapeHtml(title)} · ${escapeHtml(value)}</strong><small>${escapeHtml(item.reason)}</small><small>${escapeHtml(locations)}</small>${relations ? `<small>${escapeHtml(relations)}</small>` : ""}</div><div><button class="ghost" data-accept-suggestion="${escapeHtml(item.id)}" data-suggestion-scope="${escapeHtml(item.scope)}">采纳</button><button class="ghost" data-ignore-suggestion="${escapeHtml(item.id)}">忽略</button></div></div>`;
    }).join("") : "暂无待确认候选；候选至少需要两个不同日期的已确认 Validation 日报提供一致证据。";
    suggestionList.querySelectorAll("[data-accept-suggestion]").forEach(button => button.addEventListener("click", () => decideMemorySuggestion(button.dataset.acceptSuggestion, "accept", button.dataset.suggestionScope)));
    suggestionList.querySelectorAll("[data-ignore-suggestion]").forEach(button => button.addEventListener("click", () => decideMemorySuggestion(button.dataset.ignoreSuggestion, "ignore")));
    document.querySelector("#strategy-list").innerHTML = data.strategies.map(item => `<div class="stack-item"><div><strong>v${item.version} · ${item.status === "active" ? "当前使用" : item.status}</strong><small>编号列表 ${item.config.numbered ? "开" : "关"} · 统计 ${item.config.include_statistics ? "开" : "关"} · 单条 ${item.config.max_item_length} 字</small></div>${item.status === "inactive" || item.status === "activated" ? `<button class="ghost" data-activate="${item.version}">启用</button>` : item.status === "active" ? `<span class="tag">活动</span>` : `<span class="tag draft">未通过</span>`}</div>`).join("");
    document.querySelectorAll("[data-activate]").forEach(button => button.addEventListener("click", () => activateStrategy(button.dataset.activate)));
    document.querySelector("#run-list").innerHTML = data.recent_runs.length ? data.recent_runs.map(run => `<div class="stack-item"><div><strong>${run.decision === "activated" ? "已激活" : "未激活"} · v${run.candidate_version} · 基准 ${run.evidence?.benchmark_version || "—"}</strong><small>${escapeHtml(run.reason)}</small><small>Validation ${metricScore(run.metrics.current_validation)} → ${metricScore(run.metrics.candidate_validation)}；Holdout ${metricScore(run.metrics.current_holdout)} → ${metricScore(run.metrics.candidate_holdout)}</small><small>${(run.evidence?.rules || []).map(rule => `${memoryLabels[rule.key] || "规则"}：${escapeHtml(rule.source)}`).join("；") || "无规则变化"}</small><small>修改来源：${(run.evidence?.feedback_report_ids || []).map(id => escapeHtml(id.slice(0, 8))).join("、") || "无"}；审查运行：${(run.evidence?.critic_run_ids || []).map(id => escapeHtml(id.slice(0, 8))).join("、") || "无"}</small></div></div>`).join("") : `<div class="empty-state">还没有评测记录。</div>`;
    const summary = agentData.summary;
    document.querySelector("#llm-status").textContent = llm.enabled ? `豆包已启用 · ${llm.model}` : "模型未配置 · 使用本地 Writer";
    document.querySelector("#agent-summary").textContent = summary.run_count ? `${summary.run_count} 次运行 · 平均 ${summary.average_duration_ms}ms` : "暂无记录";
    const evaluationStages = { prepared: "准备完成", validation_ready: "Validation 已完成", holdout_claimed: "Holdout 已领取", decision_ready: "结果待提交" };
    const failureReason = code => code === "ModelCallTimeout" ? "模型调用超时"
      : code === "RoleProcessTimeout" ? "角色执行超时" : (code || "未知");
    const recoveryItems = recoverable.runs.map(run => run.workflow === "strategy_evaluation"
      ? `<div class="stack-item"><div><strong>策略评测 · 可恢复</strong><small>${run.status === "failed" ? `失败原因：${escapeHtml(run.error_code || "未知")}` : "运行中断"} · ${evaluationStages[run.stage] || "阶段待核对"} · 已恢复 ${run.recovery_count}/2 次</small></div><button class="ghost" data-resume-run="${escapeHtml(run.id)}" data-resume-workflow="strategy_evaluation">继续评测</button></div>`
      : run.workflow === "writer_prompt_evaluation"
        ? `<div class="stack-item"><div><strong>Writer 指令 v${run.candidate_version} · 可恢复</strong><small>${run.stage === "holdout_claimed" ? "Holdout 已领取" : "Validation 评测中"} · 调用 ${run.model_calls}/${run.max_model_calls} 次 · 已恢复 ${run.recovery_count}/2 次${promptData.model_enabled ? "" : " · 需配置模型"}</small></div><button class="ghost" data-resume-run="${escapeHtml(run.id)}" data-resume-workflow="writer_prompt_evaluation" ${promptData.model_enabled ? "" : "disabled"}>继续评测</button></div>`
      : run.workflow === "final_review"
        ? `<div class="stack-item"><div><strong>终稿审查 · 可恢复</strong><small>${run.status === "failed" ? `失败原因：${escapeHtml(run.error_code || "未知")}` : "运行中断"} · ${run.stage === "critic_ready" ? "审查已完成，等待确认" : "等待审查"} · 已恢复 ${run.recovery_count}/2 次${run.draft_matches ? "" : " · 需填回原终稿"}</small></div><button class="ghost" data-resume-run="${escapeHtml(run.id)}" data-resume-workflow="final_review" data-report-id="${escapeHtml(run.report_id)}">继续审查</button></div>`
        : `<div class="stack-item"><div><strong>日报生成 · ${run.remaining_budget_ms > 0 && run.remaining_tokens > 0 ? "可恢复" : "总预算已耗尽"}</strong><small>${run.status === "failed" ? `失败原因：${escapeHtml(failureReason(run.error_code))}` : "运行中断"} · 已恢复 ${run.recovery_count}/2 次 · 剩余预算 ${Math.ceil(run.remaining_budget_ms / 1000)} 秒、${run.remaining_tokens} Token</small></div><button class="ghost" data-resume-run="${escapeHtml(run.id)}">${run.remaining_budget_ms > 0 && run.remaining_tokens > 0 ? "继续运行" : "结束运行"}</button></div>`);
    const stepSummary = step => {
      const detail = step.agent_name === "Planner Agent" && step.output.task_ids ? `（任务 ${step.output.task_ids.length} 项：事实覆盖 ${step.output.task_actions?.filter(action => action === "cover_facts" || action === "verify_source").length || 0}，章节保留 ${step.output.task_actions?.filter(action => action === "preserve_section").length || 0}，记忆关注 ${step.output.focused_sections?.length || 0}）`
        : step.agent_name === "Planner Agent" && step.output.added_task_ids ? `（新增返工 ${step.output.added_task_ids.length} 项：${step.output.task_actions.map(escapeHtml).join("、")}${step.output.superseded_task_ids?.length ? `；替换 ${step.output.superseded_task_ids.map(escapeHtml).join("、")}` : ""}）`
        : step.agent_name === "Fact Agent" && step.output.task_id ? `（任务 ${escapeHtml(step.output.task_id)} · ${escapeHtml(step.output.action)} · 证据 ${step.output.evidence_ids?.length || 0} 条）`
        : step.agent_name === "Writer Agent" && step.input.repair_task_ids?.length ? `（执行返工 ${step.input.repair_task_ids.map(escapeHtml).join("、")}：${step.input.repair_actions.map(escapeHtml).join("、")}）`
        : step.agent_name === "Writer Agent" && step.input.feedback_task_ids?.length ? `（返工任务 ${step.input.feedback_task_ids.map(escapeHtml).join("、")}）`
        : step.agent_name === "Critic Agent" && step.status !== "passed" ? `（问题 ${step.output.issue_types?.map(escapeHtml).join("、") || "待核对"}；关联任务 ${step.output.affected_task_ids?.map(escapeHtml).join("、") || "无"}）` : "";
      return `${escapeHtml(step.agent_name)}${Number.isInteger(step.input.attempt) ? ` 第 ${step.input.attempt + 1} 轮` : ""} ${step.status === "passed" ? "✓" : "!"}${detail}`;
    };
    const runItems = agentData.runs.map(run => {
      const snapshot = run.instruction_snapshot || {};
      const versions = [snapshot.writer_prompt ? `Writer 指令 v${snapshot.writer_prompt.version} · ${escapeHtml(snapshot.writer_prompt.sha256.slice(0, 12))}` : "", snapshot.critic_rules ? `Critic 规则 v${snapshot.critic_rules.version}` : "", snapshot.evaluation_rules ? `评测规则 v${snapshot.evaluation_rules.version}` : ""].filter(Boolean).join(" · ");
      const canReplay = ["daily_report", "final_review"].includes(run.workflow) ||
        (run.workflow === "strategy_evaluation" && ["activated", "deferred"].includes(run.status));
      const isolated = run.steps.some(step => step.output?.execution?.mode === "isolated_process")
        ? " · 角色独立进程" : "";
      return `<div class="stack-item"><div><strong>${run.workflow === "strategy_evaluation" ? "策略评测" : run.workflow === "final_review" ? "终稿审查" : "日报生成"} · ${run.status === "passed" ? "通过" : run.status === "needs_review" ? "待人工检查" : run.status === "blocked" ? "已拦截" : run.status === "budget_exhausted" ? "预算耗尽" : run.status === "invalidated" ? "已失效" : run.status === "failed" ? "失败" : run.status === "running" ? "运行中" : run.status}</strong><small>${run.steps.map(stepSummary).join(" · ")} · ${run.duration_ms}ms${isolated}${run.workflow === "daily_report" ? ` · Token ${run.token_used}/${summary.token_limit}${run.token_estimated ? `（含估算 ${run.token_estimated}）` : ""}` : ""}</small>${versions ? `<small>${versions}</small>` : ""}<small data-replay-result="${escapeHtml(run.id)}">${escapeHtml(state.replayResults[run.id] || "")}</small></div>${canReplay ? `<button class="ghost" data-replay-run="${escapeHtml(run.id)}">核验回放</button>` : ""}</div>`;
    });
    document.querySelector("#agent-run-list").className = recoveryItems.length || runItems.length ? "stack-list" : "stack-list empty-state";
    document.querySelector("#agent-run-list").innerHTML = [...recoveryItems, ...runItems].join("") || "暂无 Agent 执行记录。";
    document.querySelectorAll("[data-resume-run]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      let reviewReport = null;
      let submittedFinalText = "";
      try {
        let options = { method: "POST" };
        if (button.dataset.resumeWorkflow === "final_review") {
          await flushDraftSave();
          const reportId = button.dataset.reportId;
          reviewReport = await api(`/api/reports/${encodeURIComponent(reportId)}`);
          submittedFinalText = state.currentReport?.id === reportId
            ? document.querySelector("#report-editor").value : reviewReport.draft;
          options = { method: "POST", body: JSON.stringify({ final_text: submittedFinalText }) };
        }
        const result = button.dataset.resumeWorkflow === "writer_prompt_evaluation"
          ? await api(`/api/agents/writer-prompts/evaluations/${button.dataset.resumeRun}/resume`, options)
          : await api(`/api/agents/${button.dataset.resumeRun}/resume`, options);
        if (button.dataset.resumeWorkflow === "strategy_evaluation") {
          toast(result.decision === "activated" ? "策略评测已恢复并激活" : result.reason);
          loadEvolution();
        } else if (button.dataset.resumeWorkflow === "writer_prompt_evaluation") {
          toast(result.decision === "passed" ? "Writer 指令评测通过，可手动启用" : result.reason);
          loadEvolution();
        } else if (button.dataset.resumeWorkflow === "final_review") {
          if (state.currentReport?.id === result.id) {
            state.currentReport = result;
            document.querySelector("#report-editor").readOnly = true;
            document.querySelector("#confirm-report").disabled = true;
            document.querySelector("#draft-status").textContent = "已确认 · 已加入个人数据集";
          }
          toast("终稿审查已恢复，报告已确认");
          loadEvolution();
        } else {
          showDraft(result, true);
          switchView("daily");
          toast("日报已从检查点恢复");
        }
      } catch (error) {
        if (button.dataset.resumeWorkflow === "final_review" && error.review && reviewReport) {
          showDraft(reviewReport, true);
          document.querySelector("#report-editor").value = submittedFinalText;
          showFinalReview(error.review, submittedFinalText);
          switchView("daily");
        }
        toast(error.message, true);
        loadEvolution();
      }
      finally { button.disabled = false; }
    }));
    document.querySelectorAll("[data-replay-run]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const replay = await api(`/api/agents/${button.dataset.replayRun}/replay`);
        const target = document.querySelector(`[data-replay-result="${button.dataset.replayRun}"]`);
        const mismatch = replay.steps.find(step => step.result === "diverged");
        const message = replay.status === "verified" ? `回放核验通过 · ${replay.steps.length} 个步骤`
          : replay.status === "unavailable" ? `无法回放：${replay.reason}`
          : `回放发现差异：${mismatch?.agent_name || "运行"} · ${mismatch?.reason || replay.reason}`;
        state.replayResults[button.dataset.replayRun] = message;
        if (target) target.textContent = message;
      } catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    }));
  } catch (error) { toast(error.message, true); }
}

document.querySelector("#memory-key").addEventListener("change", event => {
  const isLength = event.target.value === "max_item_length";
  document.querySelector("#memory-boolean-label").hidden = isLength;
  document.querySelector("#memory-length-label").hidden = !isLength;
});

document.querySelector("#personal-memory-form").addEventListener("submit", async event => {
  event.preventDefault();
  const key = document.querySelector("#memory-key").value;
  const value = key === "max_item_length" ? document.querySelector("#memory-length").value : document.querySelector("#memory-boolean").value;
  try {
    await api("/api/memories", { method: "POST", body: JSON.stringify({ scope: "personal", memory_key: key, value }) });
    toast("写作偏好已保存，新日报将使用该设置");
    loadEvolution();
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#project-memory-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/memories", { method: "POST", body: JSON.stringify({ scope: "project", memory_key: "note", project_id: document.querySelector("#memory-project").value, value: document.querySelector("#memory-note").value, expires_on: document.querySelector("#memory-expires-on").value || null }) });
    document.querySelector("#memory-note").value = "";
    document.querySelector("#memory-expires-on").value = "";
    toast("项目背景已保存");
    loadEvolution();
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#writer-prompt-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    const result = await api("/api/agents/writer-prompts", { method: "POST", body: JSON.stringify({ addendum: document.querySelector("#writer-prompt-addendum").value }) });
    document.querySelector("#writer-prompt-addendum").value = "";
    toast(`Writer 指令 v${result.version} 已保存，可手动启用`);
    loadEvolution();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

async function activateWriterPrompt(version) {
  try {
    await api(`/api/agents/writer-prompts/${version}/activate`, { method: "POST" });
    toast(`已切换到 Writer 指令 v${version}`);
    loadEvolution();
  } catch (error) { toast(error.message, true); }
}

async function evaluateWriterPrompt(version) {
  const button = document.querySelector(`[data-evaluate-prompt="${version}"]`);
  button.disabled = true;
  try {
    const result = await api(`/api/agents/writer-prompts/${version}/evaluate`, { method: "POST" });
    toast(result.reason, result.decision !== "passed");
  } catch (error) { toast(error.message, true); }
  finally { loadEvolution(); }
}

async function correctMemory(memoryId) {
  const item = state.memories.find(memory => memory.id === memoryId);
  if (!item) return;
  const hint = item.memory_key === "max_item_length" ? "输入 40 到 160 之间的数字" : item.memory_key === "note" ? "输入修正后的项目备注" : "输入“启用”或“关闭”";
  const current = item.value === "true" ? "启用" : item.value === "false" ? "关闭" : item.value;
  const answer = window.prompt(hint, current);
  if (answer === null) return;
  const value = item.memory_key === "note" || item.memory_key === "max_item_length" ? answer.trim() : answer.trim() === "启用" ? "true" : answer.trim() === "关闭" ? "false" : "";
  const expiry = item.scope === "project" ? window.prompt("有效至（YYYY-MM-DD；留空表示长期有效）", item.expires_on || "") : undefined;
  if (expiry === null) return;
  try {
    await api(`/api/memories/${memoryId}`, { method: "PATCH", body: JSON.stringify(item.scope === "project" ? { value, expires_on: expiry.trim() || null } : { value }) });
    toast("记忆已纠正，原记录保留来源");
    loadEvolution();
  } catch (error) { toast(error.message, true); }
}

async function removeMemory(memoryId) {
  try {
    await api(`/api/memories/${memoryId}`, { method: "DELETE" });
    toast("记忆已清理");
    loadEvolution();
  } catch (error) { toast(error.message, true); }
}

async function decideMemorySuggestion(suggestionId, action, scope = "project") {
  const expiry = action === "accept" && scope === "project" ? window.prompt("有效至（YYYY-MM-DD；留空表示长期有效）", "") : "";
  if (expiry === null) return;
  try {
    await api(`/api/memories/suggestions/${suggestionId}/decision`, {
      method: "POST", body: JSON.stringify({ action, expires_on: expiry.trim() || null }),
    });
    toast(action === "accept" ? "候选已加入项目记忆" : "候选已忽略");
    loadEvolution();
  } catch (error) { toast(error.message, true); }
}

document.querySelector("#run-evolution").addEventListener("click", async event => {
  event.target.disabled = true;
  try { const result = await api("/api/evolution/propose", { method: "POST" }); toast(result.decision === "activated" ? "新策略已通过门禁并激活" : result.reason); loadEvolution(); }
  catch (error) { toast(error.message, true); }
  finally { event.target.disabled = false; }
});

document.querySelector("#create-benchmark").addEventListener("click", async event => {
  event.target.disabled = true;
  try {
    const result = await api("/api/evolution/benchmarks", { method: "POST" });
    toast(`固定基准 v${result.version} 已建立`);
    loadEvolution();
  } catch (error) { toast(error.message, true); loadEvolution(); }
});

async function activateStrategy(version) {
  try { await api(`/api/evolution/strategies/${version}/activate`, { method: "POST" }); toast(`已切换到策略 v${version}`); loadEvolution(); }
  catch (error) { toast(error.message, true); }
}

async function copyText(value) { if (!value) return; await navigator.clipboard.writeText(value); toast("已复制到剪贴板"); }
function metricScore(value) { return value?.score ?? "未运行"; }
function escapeHtml(value) { const div = document.createElement("div"); div.textContent = value ?? ""; return div.innerHTML; }

const now = new Date();
const localDate = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
document.querySelector("#today-chip").textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "short" }).format(now);
document.querySelector("#report-date").value = localDate;
const monday = new Date(now); monday.setDate(now.getDate() - ((now.getDay() + 6) % 7));
document.querySelector("#week-start").value = new Date(monday.getTime() - monday.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
document.querySelector("#historical-date").value = localDate;

api("/health").then(() => { document.querySelector("#service-status").textContent = "运行正常"; }).catch(() => { document.querySelector("#service-status").textContent = "连接失败"; });
loadDashboard();
