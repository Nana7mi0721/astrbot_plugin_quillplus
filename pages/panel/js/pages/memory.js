/* pages/memory.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, showErrorDetail, showToast } from "../components/feedback.js";
import { openModal } from "../components/modal.js";
import { updateBadge, switchTab } from "../pages/config.js";
import { getRAGConfig, providerRows } from "../pages/rag.js";
import { $, $$, downloadBlob, downloadJSON, esc, fmtTime, formatSessionId, withBusy } from "../utils.js";

export { MEM_PER_PAGE, archiveMemory, changeMemoryPage, createKnowledgePage, deleteEvalCase, deleteMemory, exportChatLogs, exportMemories, importMemories, loadChatLogs, loadEvalCases, loadEvalSnapshots, loadKnowledgePages, loadMemoryBrowser, loadMemoryOverview, loadMemorySessions, onMemorySessionChange, pruneMemories, refreshKnowledgePage, restoreMemory, runEval, saveEvalCase, saveKnowledgePinned, summarizeMemory, showMemoryDetail, showMemorySource, switchMemorySub, testMemoryVectorSearch, toggleMemoryPin, updateMemPager };



const MEM_PER_PAGE = 50;
let _kpEpoch = 0;
let _evalEpoch = 0;
let _snapEpoch = 0;
let _memEpoch = 0;
let _memModalEpoch = 0;

function switchMemorySub(sub) {
  $$("[data-memsub]").forEach(b => {
    const on = b.dataset.memsub === sub;
    b.classList.toggle("is-selected", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $$(".mem-sub").forEach(p => p.classList.toggle("is-active", p.id === "mem-" + sub));
  if (sub === "overview") loadMemoryOverview();
  if (sub === "browser") { S._memPage = 1; loadMemorySessions(); loadMemoryBrowser(); }
  if (sub === "pages") loadKnowledgePages();
  if (sub === "eval") { loadEvalCases(); loadEvalSnapshots(); }
}

async function loadMemoryOverview() {
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  try {
    const data = await api("/memory/stats");
    const d = data.data || data;
    const total = d.total_memories ?? 0;
    const sessions = d.total_sessions ?? 0;
    const today = d.today_count ?? 0;
    set("#dashTotal", total);
    set("#dashSessions", sessions);
    set("#dashToday", today);
    set("#dashIndex", total);
    updateBadge("badge-memory", total);
    // 进度条按各自维度归一化；延迟一帧以便过渡动画生效
    requestAnimationFrame(() => {
      const bar = (id, pct) => { const el = $(id); if (el) el.style.width = Math.max(4, Math.min(100, pct)) + "%"; };
      bar("#dashBarTotal", (total / Math.max(total, 100)) * 100);
      bar("#dashBarSessions", (sessions / Math.max(sessions, 50)) * 100);
      bar("#dashBarIndex", total > 0 ? 100 : 12);
      bar("#dashBarToday", (today / Math.max(today, 20)) * 100);
    });
    try {
      const cfg = await getRAGConfig();
      $("#memProvider").innerHTML = providerRows(cfg);
    } catch (_) {}
  } catch (e) {
    ["#dashTotal", "#dashSessions", "#dashToday", "#dashIndex"].forEach(id => set(id, "—"));
  }
}

async function loadMemorySessions() {
  const sel = $("#memSession");
  if (!sel) return;
  try {
    const resp = await api("/memory/sessions");
    const data = resp.data || resp;
    const sessions = data.sessions || [];
    sel.innerHTML = '<option value="">全部会话</option>' +
      sessions.map(s => `<option value="${esc(s.session_id)}">${esc(s.session_id)}（${s.mem_count} 条）</option>`).join("");
    // L9：对话日志那两个输入框此前是纯自由文本，用户只能靠记忆手打 Session ID。
    // 会话列表本来就在这次响应里，顺手填进 datalist 做候选（不额外发请求）。
    const dl = $("#logSessionList");
    if (dl) {
      dl.innerHTML = sessions
        .map(s => `<option value="${esc(s.session_id)}"></option>`).join("");
    }
  } catch (_) { /* 保留「全部会话」 */ }
}

async function loadMemoryBrowser() {
  const epoch = ++_memEpoch;
  const tbody = $("#memTbody");
  const sessionId = $("#memSession")?.value || "";
  const search = ($("#memFilter")?.value || "").trim();
  const status = ($("#memStatus")?.value || "").trim();
  tbody.innerHTML = '<tr><td colspan="4" class="t3" style="text-align:center">加载中…</td></tr>';
  try {
    // FUNC-P1-6：会话/搜索路径此前无分页参数，后端只回最新 50 条且不带
    // total，页脚把截断数冒充总数。现在与 list_all 同一契约带上分页。
    const url = sessionId
      ? `/memory/list?session_id=${encodeURIComponent(sessionId)}&page=${S._memPage}&per_page=${MEM_PER_PAGE}${status ? `&status=${encodeURIComponent(status)}` : ""}`
      : search
        ? `/memory/list?session_id=${encodeURIComponent(search)}&page=${S._memPage}&per_page=${MEM_PER_PAGE}${status ? `&status=${encodeURIComponent(status)}` : ""}`
        : `/memory/list_all?page=${S._memPage}&per_page=${MEM_PER_PAGE}${status ? `&status=${encodeURIComponent(status)}` : ""}`;
    const resp = await api(url);
    if (epoch !== _memEpoch) return;
    const data = resp.data || resp;
    const mems = data.memories || [];
    const total = data.total || mems.length;
    S._memPages = data.total_pages || Math.max(1, Math.ceil(total / MEM_PER_PAGE));
    S._memPage = data.page || S._memPage;

    if (!mems.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="t3" style="text-align:center">暂无记忆</td></tr>';
      updateMemPager(0, total);
      return;
    }
    tbody.innerHTML = mems.map(m => {
      const isCore = !!m.is_core;
      const low = (m.useful_score || 0) < 3 && !m.is_active;
      const scoreTone = low ? "badge--red" : "badge--orange";
      return `<tr>
        <td><span class="mono fs-12" title="${esc(m.session_id || "")}">${esc(formatSessionId(m.session_id))}</span></td>
        <td>
          <div class="row-desc" style="margin:0 0 6px">${esc((m.summary || "—").slice(0, 72))}</div>
          <div class="mem-tags">
            <span class="badge badge--accent" title="记忆留存强度">强度 ${m.strength || 10}</span>
            <span class="badge badge--green" title="被大模型召回引用的次数">引用 ${m.useful_count || 0} 次</span>
            <span class="badge ${scoreTone}" title="当前价值分">价值分 ${(m.useful_score || 0).toFixed(1)}</span>
            ${m.is_active ? '<span class="badge">主动记忆</span>' : ""}
            ${isCore ? '<span class="badge badge--yellow" title="核心记忆：无条件注入 prompt，不参与 Top-K 竞争">核心锚定</span>' : ""}
            ${m.status && m.status !== "active" ? `<span class="badge badge--orange">${esc(m.status)}</span>` : ""}
            ${m.near_duplicate_of ? `<span class="badge badge--red" title="近重复候选 ${esc(m.near_duplicate_of)}：仅标记，未自动合并">近重复</span>` : ""}
          </div>
        </td>
        <td class="fs-12 t3">${esc(fmtTime(m.timestamp))}</td>
        <td class="cell-actions">
          <button type="button" class="btn btn--sm ${isCore ? "btn--tinted" : "btn--gray"}" data-action="mem-pin" data-id="${m.id}" data-core="${isCore ? "1" : "0"}"
            title="${isCore ? "取消核心锚定" : "设为核心锚定（无条件注入）"}">${isCore ? "取消钉住" : "钉住"}</button>
          <button type="button" class="btn btn--sm btn--gray" data-action="mem-detail" data-id="${m.id}">详情</button>
          <button type="button" class="btn btn--sm btn--gray" data-action="mem-source" data-id="${m.id}">来源日志</button>
          ${!isCore && m.status !== "archived" ? `<button type="button" class="btn btn--sm btn--gray" data-action="mem-archive" data-id="${m.id}">归档</button>` : ""}
          ${!isCore && m.status !== "active" ? `<button type="button" class="btn btn--sm btn--gray" data-action="mem-restore" data-id="${m.id}">恢复</button>` : ""}
          <button type="button" class="btn btn--sm btn--destructive-tinted" data-action="mem-delete" data-id="${m.id}">删除</button>
        </td>
      </tr>`;
    }).join("");
    updateMemPager(mems.length, total);
  } catch (e) {
    if (epoch !== _memEpoch) return;
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--red-text)">${esc(e.message)}</td></tr>`;
  }
}

async function showMemorySource(id) {
  const modalState = { type: "memory-source", id: String(id) };
  S._modalState = modalState;
  const epoch = ++_memModalEpoch;
  try {
    const resp = await api(`/memory/source_logs?id=${encodeURIComponent(id)}`);
    if (epoch !== _memModalEpoch || S._modalState !== modalState) return;
    const data = resp.data || resp;
    const logs = data.logs || [];
    $("#modalTitle").textContent = `记忆来源日志 #${id}`;
    $("#modalBody").innerHTML = logs.length
      ? logs.map(l => `<div class="form-group"><label>${esc(l.role)} · ${esc(l.timestamp || "")}</label><textarea class="field field--wide" rows="3" readonly>${esc(l.content || "")}</textarea></div>`).join("")
      : '<div class="fs-13 t3">该记忆没有可追溯的日志区间。</div>';
    $("#modalSaveBtn").hidden = true;
    openModal();
  } catch (e) {
    const current = S._modalState === modalState;
    if (current) S._modalState = null;
    if (epoch !== _memModalEpoch || !current) return;
    showToast("读取来源失败：" + e.message);
  }
}

async function archiveMemory(id) {
  sandboxConfirm("归档这条记忆？归档后不再参与检索，但可在状态筛选中恢复。", async () => {
    try {
      await apiPost("/memory/status", { id, status: "archived" });
      showToast("已归档", "success");
      loadMemoryBrowser();
    } catch (e) { showToast(e.message); }
  }, true);
}

async function restoreMemory(id) {
  try {
    await apiPost("/memory/status", { id, status: "active" });
    showToast("已恢复为 active", "success");
    loadMemoryBrowser();
  } catch (e) { showToast(e.message); }
}

function updateMemPager(shown, total) {
  const info = $("#memPageInfo");
  if (info) info.textContent = `共 ${total} 条 · 第 ${S._memPage}/${S._memPages} 页`;
  const prev = $("#memPrev"), next = $("#memNext");
  if (prev) prev.disabled = S._memPage <= 1;
  if (next) next.disabled = S._memPage >= S._memPages;
}

function changeMemoryPage(delta) {
  S._memPage = Math.max(1, Math.min(S._memPages, S._memPage + Number(delta)));
  loadMemoryBrowser();
}

function onMemorySessionChange() {
  S._memPage = 1;
  // 后端按 session_id 精确匹配，选定会话时禁用筛选框，避免用户误以为两个条件同时生效
  const filter = $("#memFilter");
  const locked = !!($("#memSession")?.value);
  if (filter) {
    filter.disabled = locked;
    if (locked) filter.value = "";
  }
  loadMemoryBrowser();
}

async function showMemoryDetail(id) {
  const modalState = { type: "memory-detail", id: String(id) };
  S._modalState = modalState;
  const epoch = ++_memModalEpoch;
  try {
    const resp = await api("/memory/get?id=" + id);
    if (epoch !== _memModalEpoch || S._modalState !== modalState) return;
    const m = resp.data || resp;
    if (!m || resp.status === "error") { showToast(resp.message || "获取详情失败"); return; }
    $("#modalTitle").textContent = `记忆详情 #${m.id}`;
    $("#modalBody").innerHTML = `
      <div class="form-group"><label>会话</label><input class="field field--wide" readonly value="${esc(formatSessionId(m.session_id))}"></div>
      <div class="form-group"><label>摘要</label><textarea class="field field--wide" rows="3" readonly>${esc(m.summary || "")}</textarea></div>
      <div class="form-group"><label>完整内容</label><textarea class="field field--wide" rows="10" readonly>${esc(m.chat_summary || "（无）")}</textarea></div>
      <div class="form-group"><label>创建时间</label><input class="field field--wide" readonly value="${esc(fmtTime(m.timestamp))}"></div>
      <div class="stat-grid">
        <div class="stat-cell"><div class="stat-cell-value">${m.strength || 10}</div><div class="stat-cell-label">强度</div></div>
        <div class="stat-cell"><div class="stat-cell-value">${m.useful_count || 0}</div><div class="stat-cell-label">引用次数</div></div>
        <div class="stat-cell"><div class="stat-cell-value">${(m.useful_score || 0).toFixed(1)}</div><div class="stat-cell-label">价值分</div></div>
        <div class="stat-cell"><div class="stat-cell-value">${m.is_core ? "是" : "否"}</div><div class="stat-cell-label">核心锚定</div></div>
      </div>`;
    $("#modalSaveBtn").hidden = true;
    openModal();
  } catch (e) {
    const current = S._modalState === modalState;
    if (current) S._modalState = null;
    if (epoch !== _memModalEpoch || !current) return;
    showToast("获取详情失败：" + e.message);
  }
}

async function deleteMemory(id) {
  let shared;
  try { shared = (await api("/memory/get?id=" + id)).shared_scope; }
  catch (error) { showToast("无法核对记忆归属：" + error.message); return; }
  const message = shared ? `删除会话组「${shared.name}」的这条记忆？以下入口都会受影响：\n${shared.affected.join("\n")}` : "确定删除这条记忆？";
  sandboxConfirm(message, async () => {
    try {
      await apiPost("/memory/delete", { memory_id: id, ...(shared ? {scope_revision:shared.revision,confirm_scope:shared.scope_id} : {}) });
      showToast("已删除", "success");
      loadMemoryBrowser();
    } catch (e) { showToast(e.message); }
  }, true);
}

async function toggleMemoryPin(id, isCore) {
  const next = !isCore;
  try {
    await apiPost("/memory/pin", { memory_id: id, is_core: next });
    showToast(next ? "已设为核心锚定，将无条件注入 prompt" : "已取消核心锚定", "success");
    loadMemoryBrowser();
  } catch (e) { showToast("设置失败：" + e.message); }
}

function pruneMemories(btn) {
  sandboxConfirm("确定执行修剪？低价值记忆会先标为过期并可在状态筛选中恢复；超过 180 天的已归档/已过期条目才会物理清理。", async () => {
    await withBusy(btn, "", async () => {
      try {
        const res = await api("/memory/prune", { method: "POST" });
        showToast(res.message || "修剪完成", "success");
        loadMemoryBrowser();
      } catch (e) { showToast(e.message); }
    });
  }, true, "修剪");
}

async function exportMemories(btn) {
  await withBusy(btn, "导出中…", async () => {
    try {
      const data = await api("/memory/export");
      downloadJSON(data, "quill_memories_export.json");
      showToast("已导出记忆库", "success");
    } catch (e) { showToast("导出失败：" + e.message); }
  });
}

async function summarizeMemory() {
  const session = $("#memSession")?.value || "";
  if (!session) { showToast("请先选择一个会话", "warning"); return; }
  try {
    const task = await apiPost("memory/summarize_task", { session_id:session });
    showToast(task.reused ? "正在查看已有总结任务" : "总结任务已创建", "info");
    switchTab("tasks");
  } catch (error) { showToast(error.message); }
}

async function importMemories(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const btn = input.closest(".file-btn");
  await withBusy(btn, "导入中…", async () => {
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      if (!data || typeof data !== "object") throw new Error("JSON 格式无效");
      if (!Array.isArray(data.memories)) throw new Error("JSON 缺少 memories 数组");
      const res = await apiPost("/memory/import_task", data);
      showToast("导入任务已创建：" + res.task_id.slice(0,8), "info");
      switchTab("tasks");
    } catch (e) { showErrorDetail("导入失败", e.message); }
    finally { input.value = ""; }
  });
}

async function loadChatLogs(btn) {
  const sid = ($("#logSession")?.value || "").trim();
  const tbody = $("#logTbody");
  if (!sid) { showToast("请输入 Session ID", "warning"); return; }
  await withBusy(btn, "", async () => {
    tbody.innerHTML = '<tr><td colspan="3" class="t3" style="text-align:center">加载中…</td></tr>';
    try {
      const data = await api(`/chatlog/list?session_id=${encodeURIComponent(sid)}&limit=200`);
      const logs = (data && (data.logs || (data.data && data.data.logs))) || [];
      if (!logs.length) {
        tbody.innerHTML = '<tr><td colspan="3" class="t3" style="text-align:center">该会话暂无日志（需先开启「保存原始对话日志」并产生 RP 对话）</td></tr>';
        return;
      }
      tbody.innerHTML = logs.map(l => `<tr>
        <td>${l.role === "user" ? '<span class="badge">用户</span>' : '<span class="badge badge--accent">AI</span>'}</td>
        <td class="cell-pre">${esc(String(l.content || "").slice(0, 500))}${String(l.content || "").length > 500 ? "…" : ""}</td>
        <td class="fs-12 t3">${esc(l.timestamp || "")}</td>
      </tr>`).join("");
      showToast(`已加载 ${logs.length} 条日志`, "success");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:var(--red-text)">${esc(e.message)}</td></tr>`;
    }
  });
}

async function exportChatLogs(fmt, btn) {
  const sid = ($("#logSession")?.value || "").trim();
  if (!sid) { showToast("请输入 Session ID", "warning"); return; }
  await withBusy(btn, "导出中…", async () => {
    try {
      const data = await api(`/chatlog/export?session_id=${encodeURIComponent(sid)}&format=${fmt}`);
      const content = (data && (data.content || (data.data && data.data.content))) || "";
      if (!content) { showToast("该会话暂无可导出的日志", "warning"); return; }
      const safe = sid.replace(/[^a-zA-Z0-9_:.-]/g, "_");
      downloadBlob(new Blob([content], { type: "text/plain;charset=utf-8" }), `quill_chatlog_${safe}.${fmt === "txt" ? "txt" : "md"}`);
      showToast("已导出日志", "success");
    } catch (e) { showToast("导出失败：" + e.message); }
  });
}

async function testMemoryVectorSearch(btn) {
  const q = ($("#vecQuery")?.value || "").trim();
  const host = $("#vecResults");
  const raw = $("#vecRaw");
  if (!q) { host.innerHTML = '<div class="fs-13 t3">请输入查询文本</div>'; return; }
  await withBusy(btn, "检索中…", async () => {
    try {
      const data = await apiPost("/memory/vector_search", { query: q, top_k: 5 });
      if (raw) { raw.hidden = false; raw.textContent = JSON.stringify(data, null, 2); }
      const items = data.results || [];
      host.innerHTML = items.length
        ? items.map((r, i) => `<div class="result-card">
            <div class="result-head">
              <span class="result-rank">${i + 1}</span>
              <span class="result-source">${esc(String(r.summary || "").slice(0, 70))}</span>
              <span class="result-score">score ${Number(r.score || 0).toFixed(4)}</span>
            </div>
          </div>`).join("")
        : '<div class="fs-13 t3">没有结果</div>';
    } catch (e) { host.innerHTML = `<div class="fs-13" style="color:var(--red-text)">${esc(e.message)}</div>`; }
  });
}

/* ── M6.2 角色知识页 ─────────────────────────────────────────── */

async function loadKnowledgePages() {
  const epoch = ++_kpEpoch;
  const sid = ($("#memPageSession")?.value || $("#memSession")?.value || "").trim();
  const host = $("#memPagesBody");
  if (!host) return;
  if (!sid) { host.innerHTML = '<div class="fs-13 t3">输入会话后加载知识页。</div>'; return; }
  host.innerHTML = '<div class="fs-13 t3">加载中…</div>';
  try {
    const resp = await api(`/memory/pages?session_id=${encodeURIComponent(sid)}`);
    if (epoch !== _kpEpoch) return;
    const data = resp.data || resp;
    const pages = data.pages || [];
    const templates = data.templates || [];
    const tplHost = $("#memPageTemplates");
    if (tplHost) {
      tplHost.innerHTML = templates.map(t =>
        `<button type="button" class="btn btn--sm btn--gray" data-action="mem-page-create" data-key="${esc(t.key)}" data-title="${esc(t.title)}" data-question="${esc(t.question || "")}">新建：${esc(t.title)}</button>`
      ).join("");
    }
    if (!pages.length) {
      host.innerHTML = '<div class="fs-13 t3">该会话暂无知识页。可先保存固定设定，或从后台创建模板页。</div>';
      return;
    }
    host.innerHTML = pages.map(p => `<div class="card kp-card" data-key="${esc(p.template_key)}" data-title="${esc(p.title)}" style="margin-bottom:var(--sp-3)">
      <div class="card-head">
        <h4>${esc(p.title || p.template_key)}</h4>
        <span class="badge ${p.freshness_status === "fresh" ? "badge--green" : "badge--orange"}">${esc(p.freshness_status || "fresh")}</span>
        <span class="grow"></span>
        <button type="button" class="btn btn--sm btn--gray" data-action="mem-page-refresh">刷新</button>
      </div>
      <div class="card-body"><div class="card-body-inner">
        <div class="form-group"><label>固定设定（后台刷新不会覆盖）</label>
          <textarea class="field field--wide" rows="4" data-kp-pinned>${esc(p.pinned_section || "")}</textarea>
        </div>
        <button type="button" class="btn btn--sm btn--gray" data-action="mem-page-save">保存固定设定</button>
        <div class="form-group" style="margin-top:var(--sp-3)"><label>整理内容 v${Number(p.generation_version || 1)}</label>
          <pre class="cell-pre" style="white-space:pre-wrap">${esc(p.generated_body || "（尚未生成）")}</pre>
        </div>
      </div></div>
    </div>`).join("");
  } catch (e) {
    if (epoch !== _kpEpoch) return;
    host.innerHTML = `<div class="fs-13" style="color:var(--red-text)">${esc(e.message)}</div>`;
  }
}

async function createKnowledgePage(btn) {
  const sid = ($("#memPageSession")?.value || $("#memSession")?.value || "").trim();
  if (!sid) { showToast("请先输入会话", "warning"); return; }
  await withBusy(btn, "创建中…", async () => {
    try {
      const task = await apiPost("/memory/pages/refresh_task", {
        session_id: sid,
        template_key: btn.dataset.key,
        title: btn.dataset.title,
        question: btn.dataset.question || "",
        force: true,
      });
      showToast(task.reused ? "正在使用已有任务" : "知识页创建任务已提交", "info");
      switchTab("tasks");
    } catch (e) { showToast(e.message); }
  });
}

async function saveKnowledgePinned(btn) {
  const card = btn.closest(".kp-card");
  const sid = ($("#memPageSession")?.value || $("#memSession")?.value || "").trim();
  if (!card || !sid) { showToast("请先选择会话", "warning"); return; }
  const pinned = card.querySelector("[data-kp-pinned]")?.value || "";
  await withBusy(btn, "保存中…", async () => {
    try {
      await apiPost("/memory/pages/save_pinned", {
        session_id: sid,
        template_key: card.dataset.key,
        title: card.dataset.title,
        pinned_section: pinned,
      });
      showToast("固定设定已保存", "success");
      loadKnowledgePages();
    } catch (e) { showToast(e.message); }
  });
}

async function refreshKnowledgePage(btn) {
  const card = btn.closest(".kp-card");
  const sid = ($("#memPageSession")?.value || $("#memSession")?.value || "").trim();
  if (!card || !sid) { showToast("请先选择会话", "warning"); return; }
  await withBusy(btn, "创建中…", async () => {
    try {
      const task = await apiPost("/memory/pages/refresh_task", {
        session_id: sid,
        template_key: card.dataset.key,
        title: card.dataset.title,
        force: false,
      });
      showToast(task.reused ? "正在使用已有刷新任务" : "知识页刷新任务已创建", "info");
      switchTab("tasks");
    } catch (e) { showToast(e.message); }
  });
}

/* ── M6.4 检索评估 ───────────────────────────────────────────── */

async function loadEvalCases() {
  const epoch = ++_evalEpoch;
  const tbody = $("#evalCasesBody");
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="4" class="t3">加载中…</td></tr>';
  try {
    const resp = await api("/eval/cases");
    if (epoch !== _evalEpoch) return;
    const data = resp.data || resp;
    const cases = data.cases || [];
    if (!cases.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="t3">暂无用例</td></tr>';
      return;
    }
    tbody.innerHTML = cases.map(c => `<tr>
      <td class="mono fs-12">${esc(c.case_id)}</td>
      <td>${esc(c.question)}<div class="fs-12 t3">${esc((c.tags || []).join(" · "))}</div></td>
      <td class="fs-12">${esc((c.expected_sources || []).join(", "))}</td>
      <td><button type="button" class="btn btn--sm btn--destructive-tinted" data-action="eval-delete" data-id="${esc(c.case_id)}">删除</button></td>
    </tr>`).join("");
  } catch (e) {
    if (epoch !== _evalEpoch) return;
    tbody.innerHTML = `<tr><td colspan="4" style="color:var(--red-text)">${esc(e.message)}</td></tr>`;
  }
}

async function saveEvalCase(btn) {
  const caseId = ($("#evalCaseId")?.value || "").trim();
  const question = ($("#evalQuestion")?.value || "").trim();
  if (!caseId || !question) { showToast("需要用例 ID 与问题", "warning"); return; }
  const split = (value) => String(value || "").split(/[,，]/).map(x => x.trim()).filter(Boolean);
  await withBusy(btn, "保存中…", async () => {
    try {
      await apiPost("/eval/cases/save", {
        case_id: caseId,
        question,
        scope_id: ($("#evalScope")?.value || "").trim(),
        persona_id: ($("#evalPersona")?.value || "").trim(),
        expected_sources: split($("#evalExpected")?.value),
        tags: split($("#evalTags")?.value),
      });
      showToast("评估用例已保存", "success");
      loadEvalCases();
    } catch (e) { showToast(e.message); }
  });
}

async function deleteEvalCase(caseId) {
  sandboxConfirm(`删除评估用例「${caseId}」？`, async () => {
    try {
      await apiPost("/eval/cases/delete", { case_id: caseId });
      loadEvalCases();
    } catch (e) { showToast(e.message); }
  }, true);
}

async function runEval(btn) {
  await withBusy(btn, "创建中…", async () => {
    try {
      const task = await apiPost("/eval/run_task", {
        include_llm_rerank: !!$("#evalUseLLM")?.checked,
      });
      showToast(task.reused ? "正在使用已有评估任务" : "评估任务已创建", "info");
      switchTab("tasks");
    } catch (e) { showToast(e.message); }
  });
}

async function loadEvalSnapshots() {
  const epoch = ++_snapEpoch;
  const host = $("#evalSnapshotsBody");
  if (!host) return;
  try {
    const resp = await api("/eval/snapshots");
    if (epoch !== _snapEpoch) return;
    const data = resp.data || resp;
    const snapshots = data.snapshots || [];
    if (!snapshots.length) {
      host.innerHTML = '<div class="fs-13 t3">尚未运行评估。</div>';
      return;
    }
    host.innerHTML = snapshots.slice().reverse().map(s => {
      const m = s.metrics || {};
      const cfg = s.settings || {};
      const diffCases = s.diff && Array.isArray(s.diff.cases) ? s.diff.cases : [];
      const line = ["doc", "memory", "worldbook"].map(route => {
        const item = m[route] || {};
        return `${route}: ${item.hit_rate == null ? "—" : (item.hit_rate * 100).toFixed(0) + "%"} / MRR ${item.mrr == null ? "—" : Number(item.mrr).toFixed(3)}`;
      }).join(" · ");
      const diffText = diffCases.slice(0, 6).map(item => {
        const routes = Object.keys(item.routes || {});
        return item.change === "new"
          ? `${item.case_id}（新增）`
          : item.change === "removed"
            ? `${item.case_id}（移除）`
          : `${item.case_id}（${routes.join("/") || "结果变化"}）`;
      }).join("；");
      return `<div class="result-card"><div class="result-head">
        <span class="result-source">${esc(s.created_at || "")} · ${Number(s.case_count || 0)} 条 · top_k ${Number(cfg.top_k || 0)} / dense ${Number(cfg.dense_top_k || 0)}${cfg.allow_llm_fallback ? " · 含 LLM 重排" : ""}</span>
        <span class="result-score">${esc(line)}${diffCases.length ? ` · 变化 ${diffCases.length} 例` : ""}</span>
      </div>${diffText ? `<div class="fs-12 t3" style="margin-top:var(--sp-2)">${esc(diffText)}</div>` : ""}</div>`;
    }).join("");
  } catch (e) {
    if (epoch !== _snapEpoch) return;
    host.innerHTML = `<div class="fs-13" style="color:var(--red-text)">${esc(e.message)}</div>`;
  }
}
