/* pages/memory.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, showErrorDetail, showToast } from "../components/feedback.js";
import { openModal } from "../components/modal.js";
import { updateBadge } from "../pages/config.js";
import { getRAGConfig, providerRows } from "../pages/rag.js";
import { $, $$, downloadBlob, downloadJSON, esc, fmtTime, formatSessionId, withBusy } from "../utils.js";

export { MEM_PER_PAGE, changeMemoryPage, deleteMemory, exportChatLogs, exportMemories, importMemories, loadChatLogs, loadMemoryBrowser, loadMemoryOverview, loadMemorySessions, onMemorySessionChange, pruneMemories, showMemoryDetail, switchMemorySub, testMemoryVectorSearch, toggleMemoryPin, updateMemPager };



const MEM_PER_PAGE = 50;

function switchMemorySub(sub) {
  $$("[data-memsub]").forEach(b => {
    const on = b.dataset.memsub === sub;
    b.classList.toggle("is-selected", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $$(".mem-sub").forEach(p => p.classList.toggle("is-active", p.id === "mem-" + sub));
  if (sub === "overview") loadMemoryOverview();
  if (sub === "browser") { S._memPage = 1; loadMemorySessions(); loadMemoryBrowser(); }
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
  } catch (_) { /* 保留「全部会话」 */ }
}

async function loadMemoryBrowser() {
  const tbody = $("#memTbody");
  const sessionId = $("#memSession")?.value || "";
  const search = ($("#memFilter")?.value || "").trim();
  tbody.innerHTML = '<tr><td colspan="4" class="t3" style="text-align:center">加载中…</td></tr>';
  try {
    const url = sessionId
      ? "/memory/list?session_id=" + encodeURIComponent(sessionId)
      : search
        ? "/memory/list?session_id=" + encodeURIComponent(search)
        : `/memory/list_all?page=${S._memPage}&per_page=${MEM_PER_PAGE}`;
    const resp = await api(url);
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
          </div>
        </td>
        <td class="fs-12 t3">${esc(fmtTime(m.timestamp))}</td>
        <td class="cell-actions">
          <button type="button" class="btn btn--sm ${isCore ? "btn--tinted" : "btn--gray"}" data-action="mem-pin" data-id="${m.id}" data-core="${isCore ? "1" : "0"}"
            title="${isCore ? "取消核心锚定" : "设为核心锚定（无条件注入）"}">${isCore ? "取消钉住" : "钉住"}</button>
          <button type="button" class="btn btn--sm btn--gray" data-action="mem-detail" data-id="${m.id}">详情</button>
          <button type="button" class="btn btn--sm btn--destructive-tinted" data-action="mem-delete" data-id="${m.id}">删除</button>
        </td>
      </tr>`;
    }).join("");
    updateMemPager(mems.length, total);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--red-text)">${esc(e.message)}</td></tr>`;
  }
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
  try {
    const resp = await api("/memory/get?id=" + id);
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
  } catch (e) { showToast("获取详情失败：" + e.message); }
}

function deleteMemory(id) {
  sandboxConfirm("确定删除这条记忆？", async () => {
    try {
      await apiPost("/memory/delete", { memory_id: id });
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
  sandboxConfirm("确定执行修剪？系统将按有用性分档自动清理过期记忆，此操作不可撤销。", async () => {
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
      showToast("正在批量向量化，请稍候…", "info");
      const res = await apiPost("/memory/import", data);
      showToast("导入完成：" + (res.message || ""), "success");
      switchMemorySub("browser");
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
