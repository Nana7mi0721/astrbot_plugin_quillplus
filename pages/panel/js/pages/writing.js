/* pages/writing.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, showErrorDetail, showToast, showToastWithAction } from "../components/feedback.js";
import { autoSizeModalTextareas, closeModal, openModal, openScrim, renderEmptyState } from "../components/modal.js";
import { createTagInput } from "../components/taginput.js";
import { updateBadge } from "../pages/config.js";
import { $, $$, catTone, downloadJSON, esc, withBusy } from "../utils.js";

export { WR_FALLBACK_GAP, WR_FALLBACK_ROW_H, WR_MIN_CARD_W, WR_MIN_PER_PAGE, WR_PAGER_BLOCK, _wrSelected, calcPerPage, deleteWREntry, exportWR, goWRPage, importWR, loadWR, loadWRCategories, openWRModal, openWRTestModal, renderWRGrid, renderWRPager, saveWREntry, toggleWREntry, updateWRBatchBar, updateWRStats, wrBatchClear, wrBatchDelete, wrBatchToggle, wrState };

const wrState = { entries: [], total: 0, page: 1, perPage: 9 };
const _wrSelected = new Set();
/* 卡片不再内联展开：单击卡片标题直接进编辑模态（openWRModal）。
   因而卡片高度恒定，网格不会被单张卡撑高，
   calcPerPage() 按真实几何算出的每页条数才站得住。 */


/** 每页条数 = 列数 × 行数，两侧都按真实几何反推。
    旧实现用 (innerHeight-260)/200 这类常数估算：窗口高度没变就不会重算，
    于是 1280×800 与 2560×1440 都得到同一组 rows，屏幕上明明还剩一大块
    空白却只渲染 9 张卡。这里改为量真实容器：列数由网格可用宽度对
    minmax(330px) 取整，行数由「内容区可滚动高度 - 头部 - 统计条 -
    分页栏」对实测卡片高度取整。 */
const WR_MIN_CARD_W = 330;      // 与 .wr-grid 的 minmax(330px, 1fr) 一致
const WR_FALLBACK_GAP = 16;     // --sp-4 兜底（columnGap 解析失败时用）
const WR_PAGER_BLOCK = 52;      // .pager-bar 高度（8 + 32 + 8）与 4px 余量
const WR_MIN_PER_PAGE = 4;
const WR_FALLBACK_ROW_H = 132;  // --wr-row-h 兜底（gridAutoRows 解析失败时用）

function calcPerPage() {
  const grid = $("#wrGrid");
  const content = $("#content");
  if (!grid || !content) return 9;

  // 列数：列宽下限 + 列间距决定一行能放几张。
  // 间距同样读 CSS（columnGap 会被解析成 px），避免改 gap 时漏改 JS。
  const gap = parseFloat(getComputedStyle(grid).columnGap) || WR_FALLBACK_GAP;
  const gridW = grid.clientWidth || content.clientWidth;
  const cols = Math.max(1, Math.floor((gridW + gap) / (WR_MIN_CARD_W + gap)));

  // 可用高度：直接量「网格顶边 → 内容区可视底边」的距离，而不是逐个
  // 累加头部 / 统计条的高度——累加会漏掉各自的 margin-bottom，任何一处
  // 改间距都要回来同步常数。两处 rect 相减后 scrollTop 自然抵消，
  // 结果与当前滚动位置无关（分页可能从任意滚动位置触发）。
  const padBottom = parseFloat(getComputedStyle(content).paddingBottom) || 0;
  // + scrollTop 把「视口坐标」还原成「内容坐标」：滚动到中途时网格顶边
  // 会跑到可视区上方，只相减会得到一个偏小的距离。
  const gridTop = grid.getBoundingClientRect().top
                - content.getBoundingClientRect().top
                + content.scrollTop;
  // 网格下方要留出分页栏（margin-top + min-height）
  const availH = Math.max(0, content.clientHeight - padBottom - gridTop) - WR_PAGER_BLOCK;

  // 行高唯一来源：直接读 CSS 的 grid-auto-rows（即 --wr-row-h）。
  // 量第一张卡会在空态 / 首屏骨架屏时量不到（那时 grid 里只有 skeleton），
  // 也会在将来卡片高度变化时与 CSS 脱节。读计算样式则始终与布局一致。
  const cardH = parseFloat(getComputedStyle(grid).gridAutoRows) || WR_FALLBACK_ROW_H;
  const rows = Math.max(1, Math.floor((availH + gap) / (cardH + gap)));

  return Math.min(60, Math.max(WR_MIN_PER_PAGE, cols * rows));
}

async function loadWR(_retried) {
  const search = $("#wrSearch")?.value || "";
  const category = $("#wrCat")?.value || "";
  const onlyConst = $("#wrOnlyConst")?.checked || false;
  wrState.perPage = calcPerPage();
  // 重新拉取后选择集合作废，避免批量操作命中已不可见的条目
  if (_wrSelected.size) { _wrSelected.clear(); updateWRBatchBar(); }
  try {
    const params = new URLSearchParams({ page: wrState.page, per_page: wrState.perPage });
    if (search) params.set("search", search);
    if (category) params.set("category", category);
    if (onlyConst) params.set("is_constant", "true");
    const data = await api("/wr/list?" + params.toString());
    wrState.entries = data.items || [];
    wrState.total = data.total || 0;
    // 页码夹紧：窗口 resize / 筛选 / 删除 / 批量删除都会改变总页数，
    // 而 wrState.page 只在 goWRPage() 里受约束。若当前页已越界，
    // 后端会返回空列表，界面就停在「第 10 / 8 页 · 共 0 条」的空白页。
    // 夹紧后重取一次（_retried 防死循环：page 被夹到合法值后必然命中）。
    const pages = Math.max(1, Math.ceil(wrState.total / wrState.perPage));
    if (wrState.page > pages) {
      wrState.page = pages;
      if (!_retried) return loadWR(true);
    }
    if (wrState.page < 1) wrState.page = 1;
    renderWRGrid();
    renderWRPager();
    updateWRStats(data);
  } catch (e) { showToast(e.message); }
}

function updateWRStats(data) {
  const total = data.total ?? 0;
  const items = data.items || [];
  const enabled = items.filter(e => e.enabled !== false).length;
  const constant = items.filter(e => e.is_constant).length;
  const bar = $("#wrStats");
  if (bar) {
    bar.innerHTML =
      `<span>共 <span class="num">${total}</span> 条</span><span class="sep"></span>` +
      `<span>本页 <span class="num">${items.length}</span> 条</span><span class="sep"></span>` +
      `<span>启用 <span class="num">${enabled}</span></span><span class="sep"></span>` +
      `<span>常驻 <span class="num">${constant}</span></span>`;
  }
  const sub = $("#wrSub");
  if (sub) sub.textContent = total ? `· ${total} 条` : "";
  updateBadge("badge-wr", total);
}

function renderWRGrid() {
  const grid = $("#wrGrid");
  if (!wrState.entries.length) {
    grid.innerHTML = renderEmptyState("book", "还没有素材条目", "点击上方「新建条目」创建第一条写作素材");
    return;
  }
  grid.innerHTML = wrState.entries.map(e => {
    const eid = esc(e.entry_id);
    const kws = e.keywords || [];
    const shown = kws.slice(0, 6);
    const rest = kws.length - shown.length;
    const selected = _wrSelected.has(e.entry_id);
    const tagsHtml = shown.length
      ? `<div class="wr-card-tags">${shown.map(k => `<span class="tag">${esc(k)}</span>`).join("")}` +
        (rest > 0 ? `<span class="tag t3">+${rest}</span>` : "") + `</div>`
      : "";
    return `<article class="wr-card${selected ? " is-selected" : ""}" data-eid="${eid}">
      <div class="wr-card-head">
        <label class="check" title="选择此条目">
          <input type="checkbox" data-wr-select ${selected ? "checked" : ""} aria-label="选择 ${esc(e.entry_id)}">
        </label>
        <button type="button" class="wr-card-title" data-action="wr-edit">
          <div class="wr-card-name">${esc(e.name || e.entry_id)}</div>
          <div class="wr-card-id">${eid}</div>
        </button>
        <div class="wr-card-meta">
          <span class="badge badge--${catTone(e.category)}">${esc(e.category || "未分类")}</span>
          <span class="badge">P${e.priority != null ? e.priority : "-"}</span>
        </div>
      </div>
      ${tagsHtml}
      <div class="wr-card-foot">
        <label class="switch switch--sm" title="启用 / 停用">
          <input type="checkbox" data-wr-toggle ${e.enabled !== false ? "checked" : ""} aria-label="启用 ${esc(e.entry_id)}">
          <span class="switch-track"></span>
        </label>
        <div class="wr-actions">
          <button type="button" class="icon-btn" data-action="wr-edit" title="编辑条目" aria-label="编辑条目">
            <svg class="icon icon--sm" aria-hidden="true"><use href="#i-pencil"/></svg>
          </button>
          <button type="button" class="icon-btn icon-btn--danger" data-action="wr-delete" title="删除条目" aria-label="删除条目">
            <svg class="icon icon--sm" aria-hidden="true"><use href="#i-trash"/></svg>
          </button>
        </div>
      </div>
    </article>`;
  }).join("");
}

function renderWRPager() {
  const el = $("#wrPager");
  const { page, perPage, total } = wrState;
  const pages = Math.ceil(total / perPage) || 1;
  el.innerHTML =
    `<span class="pager-info">第 ${page} / ${pages} 页 · 共 ${total} 条</span>
     <span class="pager-nav">
       <button type="button" class="btn btn--sm btn--gray" data-action="wr-page" data-value="1" ${page <= 1 ? "disabled" : ""}>首页</button>
       <button type="button" class="btn btn--sm btn--gray" data-action="wr-page" data-value="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
       <button type="button" class="btn btn--sm btn--gray" data-action="wr-page" data-value="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button>
       <button type="button" class="btn btn--sm btn--gray" data-action="wr-page" data-value="${pages}" ${page >= pages ? "disabled" : ""}>末页</button>
     </span>`;
}

function goWRPage(p) {
  const pages = Math.ceil(wrState.total / wrState.perPage) || 1;
  let target = parseInt(p, 10) || 1;
  target = Math.min(pages, Math.max(1, target));
  if (target === wrState.page) return;
  wrState.page = target;
  // 先置顶再取数：calcPerPage() 读的是内容坐标，置顶后环境与首次进入一致。
  // 用直接赋值而非 smooth —— 连续点「下一页」时平滑滚动会把每页的动画
  // 串成一条，页面一直在飘；分页栏已钉在底栏，用户视线不受影响。
  const content = $("#content");
  if (content) content.scrollTop = 0;
  loadWR();
}

// ── 批量选择 ──
/* 选中态驱动：计数胶囊显示「已选 N」，批量操作作为同级按钮在选中后出现。 */
function updateWRBatchBar() {
  const n = _wrSelected.size;
  const pill = $("#wrSelectPill");
  if (pill) {
    pill.hidden = n === 0;
    pill.textContent = `已选 ${n}`;
  }
  $$("[data-batch-action]").forEach(el => {
    el.disabled = n === 0;
  });
}

function wrBatchClear() {
  _wrSelected.clear();
  updateWRBatchBar();
  renderWRGrid();
}

async function wrBatchToggle(enabled) {
  const ids = [..._wrSelected];
  if (!ids.length) return;
  // 菜单在执行前已收起，忙态反馈落在标题带的选中胶囊上
  await withBusy($("#wrSelectPill"), "处理中…", async () => {
    try {
      const res = await apiPost("/wr/batch_toggle", { entry_ids: ids, enabled });
      const failed = (res && res.failed) || 0;
      showToast(failed ? `批量操作：${res.toggled} 成功，${failed} 失败` : "批量操作完成", failed ? "warning" : "success");
      _wrSelected.clear();
      updateWRBatchBar();
      loadWR();
    } catch (e) { showToast("批量操作失败：" + e.message); }
  });
}

async function wrBatchDelete() {
  const ids = [..._wrSelected];
  if (!ids.length) return;
  sandboxConfirm(`确定批量删除 ${ids.length} 个条目？`, async () => {
    await withBusy($("#wrSelectPill"), "删除中…", async () => {
      try {
        const snapshot = wrState.entries.filter(e => _wrSelected.has(e.entry_id));
        const res = await apiPost("/wr/batch_delete", { entry_ids: ids });
        const failed = (res && res.failed) || 0;
        showToastWithAction(
          failed ? `批量删除：${res.deleted} 成功，${failed} 失败` : `已删除 ${ids.length} 个条目`,
          failed ? "warning" : "success", "撤销",
          async () => {
            let ok = 0, bad = 0;
            for (const e of snapshot) {
              try { await apiPost("/wr/create", e); ok++; } catch (_) { bad++; }
            }
            showToast(`撤销完成：恢复 ${ok} 条${bad ? `，${bad} 条失败` : ""}`, bad ? "warning" : "success");
            loadWR();
          }
        );
        _wrSelected.clear();
        updateWRBatchBar();
        loadWR();
      } catch (e) { showToast("批量删除失败：" + e.message); }
    });
  }, true);
}

// ── 单条增删改 ──
function deleteWREntry(entryId, btn) {
  sandboxConfirm(`确定删除「${entryId}」？`, async () => {
    await withBusy(btn, "", async () => {
      try {
        const entry = wrState.entries.find(e => e.entry_id === entryId);
        await apiPost("/wr/delete", { entry_id: entryId });
        showToastWithAction(`已删除 ${entryId}`, "success", "撤销", async () => {
          if (!entry) { showToast("原条目数据已不可用，无法撤销"); return; }
          try {
            await apiPost("/wr/create", entry);
            showToast("已撤销删除", "success");
            loadWR();
          } catch (e) { showToast("撤销失败：" + e.message); }
        });
        loadWR();
      } catch (e) { showToast(e.message); }
    });
  }, true);
}

async function toggleWREntry(entryId, enabled, cbEl) {
  if (cbEl) cbEl.disabled = true;
  try {
    await apiPost("/wr/toggle", { entry_id: entryId, enabled });
    loadWR();
  } catch (e) {
    showToast(e.message);
    if (cbEl) cbEl.checked = !enabled;
  } finally { if (cbEl) cbEl.disabled = false; }
}

async function loadWRCategories() {
  try {
    const data = await api("/wr/categories");
    const sel = $("#wrCat");
    if (!sel) return;
    sel.innerHTML = '<option value="">全部分类</option>' +
      (data.categories || []).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  } catch (_) { /* 分类列表不可用时保留「全部分类」 */ }
}

async function openWRModal(entryId) {
  S._modalMode = entryId ? "edit" : "create";
  let data = { category: "", entry_id: "", name: "", keywords: [], content: "", priority: 5, is_constant: false, enabled: true };
  if (entryId) {
    try {
      data = await apiPost("/wr/get", { entry_id: entryId });
      if (typeof data.keywords === "string") data.keywords = data.keywords.split(/[,，\s]+/).filter(Boolean);
    } catch (e) { showToast(e.message); return; }
  }
  $("#modalTitle").textContent = entryId ? "编辑条目" : "新建条目";
  $("#modalBody").innerHTML = `
    <div class="form-row">
      <div class="form-group" style="flex:2">
        <label for="wrEid">条目 ID</label>
        <input class="field" type="text" id="wrEid" value="${esc(data.entry_id || "")}" ${entryId ? "readonly" : ""} placeholder="唯一标识，如 forest_scene">
      </div>
      <div class="form-group" style="flex:1">
        <label for="wrCat">分类</label>
        <input class="field" type="text" id="wrCat" value="${esc(data.category || "")}" placeholder="如 场景">
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="wrName">名称（可选）</label>
        <input class="field" type="text" id="wrName" value="${esc(data.name || "")}">
      </div>
      <div class="form-group">
        <label for="wrPrio">权重</label>
        <div class="slider">
          <input type="range" id="wrPrio" min="1" max="10" value="${data.priority || 5}">
          <span class="slider-value" id="wrPrioVal">${data.priority || 5}</span>
        </div>
      </div>
    </div>
    <div class="form-group">
      <label>关键词（命中后注入）</label>
      <div id="wrTagHost"></div>
    </div>
    <div class="form-group">
      <label for="wrContent">内容</label>
      <textarea class="field field--wide" id="wrContent" rows="6">${esc(data.content || "")}</textarea>
    </div>
    <div class="row-wrap">
      <label class="check"><input type="checkbox" id="wrConst" ${data.is_constant ? "checked" : ""}><span>常驻（无需关键词）</span></label>
      <label class="check"><input type="checkbox" id="wrEnabled" ${data.enabled !== false ? "checked" : ""}><span>启用</span></label>
    </div>`;
  // 表单 id 与页面搜索框 id 撞名（wrCat），此处用局部查询避免误伤
  $("#modalBody #wrCat").value = data.category || "";
  openModal();
  autoSizeModalTextareas($("#modalBody"));
  setTimeout(() => { S._wrTagInput = createTagInput("wrTagHost", data.keywords || []); }, 40);
}

async function saveWREntry() {
  const entry_id = $("#wrEid")?.value.trim() || "";
  const category = $("#modalBody #wrCat")?.value.trim() || "";
  const name = $("#wrName")?.value.trim() || undefined;
  const content = $("#wrContent")?.value.trim() || "";
  const priority = parseInt($("#wrPrio")?.value, 10) || 5;
  const is_constant = !!$("#wrConst")?.checked;
  const enabled = $("#wrEnabled")?.checked !== false;
  const keywords = S._wrTagInput ? S._wrTagInput.getTags() : [];
  if (!entry_id || !category || !content) { showToast("请填写条目 ID、分类和内容", "warning"); return; }
  try {
    const body = { entry_id, category, name, keywords, content, priority, is_constant, enabled };
    if (S._modalMode === "create") { await apiPost("/wr/create", body); showToast("条目已创建", "success"); }
    else { await apiPost("/wr/update", body); showToast("条目已更新", "success"); }
    closeModal();
    loadWR();
  } catch (e) { showToast(e.message); }
}

async function exportWR(btn) {
  await withBusy(btn, "导出中…", async () => {
    try {
      const data = await api("/wr/export");
      downloadJSON(data, "quill_wr_export.json");
      showToast("已导出素材库", "success");
    } catch (e) { showToast("导出失败：" + e.message); }
  });
}

async function importWR(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const btn = input.closest(".file-btn");
  await withBusy(btn, "导入中…", async () => {
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const entries = data.entries || (data.data && data.data.entries) ||
        (Array.isArray(data) ? data : (data.data ? [data.data] : [data]));
      if (!entries || !entries.length || typeof entries[0] !== "object" || !entries[0].content) {
        throw new Error("JSON 格式不正确：缺少有效条目（必须含 content 字段）");
      }
      entries.forEach((e, i) => { if (!e.entry_id) e.entry_id = `import_${Date.now()}_${i}`; });
      const res = await apiPost("/wr/import", { entries });
      showToast(`导入完成：${res.imported} 条成功${res.failed ? `，${res.failed} 条失败` : ""}`, res.failed ? "warning" : "success");
      loadWR();
    } catch (e) { showErrorDetail("导入失败", e.message); }
    finally { input.value = ""; }
  });
}

function openWRTestModal() {
  const scrim = $("#alertScrim");
  $("#alertTitle").textContent = "素材库匹配测试";
  $("#alertMsg").textContent = "输入一段文本，查看会命中哪些素材条目。";
  $("#alertBody").innerHTML =
    `<textarea class="field field--wide" id="wrTestInput" rows="4" placeholder="在此粘贴一段正文……"></textarea>
     <div id="wrTestResult" style="margin-top:var(--sp-3);max-height:280px;overflow:auto"></div>`;
  const okBtn = $("#alertOk");
  const cancelBtn = $("#alertCancel");
  cancelBtn.hidden = false;
  okBtn.disabled = false;
  okBtn.className = "btn btn--filled";
  okBtn.textContent = "测试匹配";
  okBtn.onclick = async () => {
    const text = ($("#wrTestInput").value || "").trim();
    if (!text) { showToast("请输入测试文本", "warning"); return; }
    const box = $("#wrTestResult");
    box.innerHTML = '<div class="fs-13 t3">匹配中…</div>';
    okBtn.disabled = true;
    try {
      const res = await apiPost("/wr/test", { text });
      const items = (res && res.results) || [];
      box.innerHTML = items.length
        ? items.map((r, i) => `<div class="result-card">
            <div class="result-head">
              <span class="result-rank">${i + 1}</span>
              <span class="result-source">${esc(r.name || "?")}</span>
              <span class="result-score">匹配度 ${(r.match_score || 0).toFixed(2)}</span>
            </div>
            <div class="result-body">${esc(r.keywords || "—")}</div>
          </div>`).join("")
        : '<div class="fs-13 t3">未匹配到任何条目</div>';
    } catch (e) {
      box.innerHTML = `<div class="fs-13" style="color:var(--red-text)">匹配失败：${esc(e.message)}</div>`;
    } finally { okBtn.disabled = false; }
  };
  openScrim(scrim);
  setTimeout(() => $("#wrTestInput")?.focus(), 60);
}

/* ══ G. 世界书 ═══════════════════════════════════════════════════════ */
