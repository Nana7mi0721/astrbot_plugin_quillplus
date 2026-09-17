/* pages/worldbook.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, sandboxPrompt, showErrorDetail, showToast } from "../components/feedback.js";
import { autoSizeModalTextareas, closeModal, openModal, renderEmptyState } from "../components/modal.js";
import { createTagInput } from "../components/taginput.js";
import { updateBadge } from "../pages/config.js";
import { reapplyWBEntryFilter, wbEntryHit } from "../pages/persona.js";
import { $, $$, downloadJSON, esc, withBusy } from "../utils.js";

export { _wbFilterQuery, deleteWB, deleteWBEntry, exportWBSt, importWBSt, loadWB, onWBSearchInput, openWBCreateModal, openWBEntryModal, reloadSingleWB, reloadWB, renderWBEntry, renderWBInner, renderWBList, saveWBCreate, saveWBEntry, setWBOnlyConst, toggleWB, updateWBStats };






/* 每本世界书内联搜索框当前的关键词（按世界书名索引）。
   条目列表会被整体重建，DOM 里的 <input> 也跟着换新，词若只存在节点上就会
   随重建一起消失；放在这里渲染期可直接取用。 */
const _wbFilterQuery = Object.create(null);

async function loadWB(forceOpenName) {
  const el = $("#wbList");
  let openName = forceOpenName || null;
  if (!openName && S._wbOpenItem) {
    const h = S._wbOpenItem.querySelector(".wb-head");
    if (h) openName = h.dataset.wbName;
  }
  try {
    const data = await api("/wb/list");
    const list = data.worldbooks || [];
    S.wbCache = {};
    S.wbAll = list;
    updateWBStats(list);
    renderWBList(list, openName);
  } catch (e) {
    el.innerHTML = renderEmptyState("worldbook", "加载失败", e.message);
  }
}

function updateWBStats(list) {
  const total = list.length;
  const entries = list.reduce((s, w) => s + ((w.entries && w.entries.length) || 0), 0);
  const constant = list.reduce((s, w) => s + ((w.entries || []).filter(e => e.is_constant).length), 0);
  const bar = $("#wbStats");
  if (bar) {
    bar.innerHTML =
      `<span>共 <span class="num">${total}</span> 本</span><span class="sep"></span>` +
      `<span>条目 <span class="num">${entries}</span></span><span class="sep"></span>` +
      `<span>常驻 <span class="num">${constant}</span></span>`;
  }
  const sub = $("#wbSub");
  if (sub) sub.textContent = total ? `· ${total} 本 · ${entries} 条` : "";
  updateBadge("badge-wb", total);
}

function renderWBList(list, openName) {
  const el = $("#wbList");
  if (!list.length) {
    el.innerHTML = renderEmptyState("worldbook", "还没有世界书", "点击上方「新建世界书」开始创建");
    S._wbOpenItem = null;
    return;
  }
  el.innerHTML = list.map(wb => {
    S.wbCache[wb.name] = wb;
    const n = (wb.entries && wb.entries.length) || 0;
    return `<section class="wb-item" data-wb="${esc(wb.name)}">
      <div class="wb-head" data-wb-name="${esc(wb.name)}" data-action="wb-toggle" role="button" tabindex="0">
        <svg class="icon chev" aria-hidden="true"><use href="#i-chevron-right"/></svg>
        <div class="grow">
          <div class="wb-name">${esc(wb.name)}</div>
          ${wb.description ? `<div class="wb-desc">${esc(wb.description)}</div>` : ""}
        </div>
        <span class="badge" data-wb-count>${n} 条</span>
        <button type="button" class="icon-btn icon-btn--danger" data-action="wb-delete" title="删除世界书" aria-label="删除世界书">
          <svg class="icon icon--sm" aria-hidden="true"><use href="#i-trash"/></svg>
        </button>
      </div>
      <div class="wb-body"><div class="wb-body-inner" data-wb-inner></div></div>
    </section>`;
  }).join("");
  S._wbOpenItem = null;
  if (openName && list.some(w => w.name === openName)) toggleWB(openName);
}

function onWBSearchInput() {
  const q = ($("#wbSearch")?.value || "").trim().toLowerCase();
  if (!q) { renderWBList(S.wbAll, null); return; }
  renderWBList(S.wbAll.filter(wb =>
    (wb.name || "").toLowerCase().includes(q) || (wb.description || "").toLowerCase().includes(q)
  ), null);
}

async function reloadWB(btn) {
  await withBusy(btn, "", async () => {
    try {
      await api("/wb/reload", { method: "POST" });
      await loadWB();
      showToast("世界书已重新加载", "success");
    } catch (e) { showToast(e.message); }
  });
}

function setWBOnlyConst(on) {
  S._wbOnlyConst = !!on;
  if (S._wbOpenItem) {
    const h = S._wbOpenItem.querySelector(".wb-head");
    if (h) reloadSingleWB(h.dataset.wbName);
  }
  $$("[data-wb-inner]").forEach(inner => {
    if (!S._wbOpenItem || inner.closest(".wb-item") !== S._wbOpenItem) delete inner.dataset.loaded;
  });
}

/** 局部刷新单本世界书的条目列表，保留展开状态与滚动位置。 */
async function reloadSingleWB(name) {
  try {
    const wb = await apiPost("/wb/get", { name });
    if (!wb) return;
    S.wbCache[name] = wb;
    const head = document.querySelector(`.wb-head[data-wb-name="${CSS.escape(name)}"]`) ||
                 document.querySelector(`.wb-item[data-wb="${CSS.escape(name)}"] .wb-head`);
    const item = head && head.closest(".wb-item");
    if (!item) return;
    const inner = item.querySelector("[data-wb-inner]");
    const count = head.querySelector("[data-wb-count]");
    const entries = wb.entries || [];
    const shown = S._wbOnlyConst ? entries.filter(e => e.is_constant) : entries;
    if (count) count.textContent = S._wbOnlyConst ? `显示 ${shown.length} 条常驻` : `${entries.length} 条`;
    if (inner) {
      inner.innerHTML = renderWBInner(name, entries, shown);
      inner.dataset.loaded = "1";
      reapplyWBEntryFilter(name);
    }
  } catch (e) { showToast("刷新失败：" + e.message); }
}

function renderWBInner(name, entries, shown) {
  const n = esc(name);
  // 关键词存在模块级 map 里，而不是只活在 <input> 上：条目列表会被整体
  // 重渲染（切换「仅常驻」、增删条目后重建），重建时输入框是新节点、value
  // 为空，之前靠 reapplyWBEntryFilter 读 input.value 恢复会直接提前返回，
  // 表现成「一重渲染筛选就丢」。渲染时直接从 map 取词并把命中标记一起
  // 写进 HTML，重建后既不丢词也不闪一下全量。
  const q = (_wbFilterQuery[name] || "").toLowerCase().trim();
  const vis = q ? shown.filter(e => wbEntryHit(e, q)) : shown;
  const inner = vis.length
    ? vis.map(e => renderWBEntry(name, e)).join("")
    : renderEmptyState("book", "没有条目", "点击上方「新建条目」添加第一条设定");
  // 提示只在「本来有条目、但零命中」时出现；一册本就空着时由上面的空状态
  // 独占说明，两条提示叠着出现会互相打架。
  const tip = q && shown.length && !vis.length
    ? `<div class="wb-filter-empty">没有匹配「<b>${esc(_wbFilterQuery[name])}</b>」的条目</div>`
    : "";
  return `<div class="wb-toolbar">
      <button type="button" class="btn btn--sm btn--filled" data-action="wb-entry-new">
        <svg class="icon icon--sm" aria-hidden="true"><use href="#i-plus"/></svg>新建条目
      </button>
      <label class="search grow">
        <svg class="icon" aria-hidden="true"><use href="#i-search"/></svg>
        <input type="search" data-wb-filter placeholder="搜索本世界书的设定…" aria-label="搜索本世界书条目" value="${esc(_wbFilterQuery[name] || "")}">
        <button type="button" class="search-clear" data-action="clear-search" data-scope="wb-filter" aria-label="清除搜索">
          <svg class="icon" aria-hidden="true"><use href="#i-x"/></svg>
        </button>
      </label>
      <span class="fs-12 t3 nowrap">${S._wbOnlyConst ? `显示 ${shown.length} 条常驻 / ` : ""}共 ${entries.length} 条</span>
    </div>
    ${inner}${tip}`;
}

function renderWBEntry(wbName, e) {
  const keys = e.keys || [];
  return `<article class="wb-entry" data-wb="${esc(wbName)}" data-eid="${esc(e.id || "")}">
    <div class="wb-entry-head">
      <div class="grow">
        <div class="wb-entry-title">${esc(e.title || e.id || "(无标题)")}</div>
        <div class="wb-entry-id">${esc(e.id || "")}</div>
      </div>
      <div class="wb-entry-aside">
        ${e.is_constant ? '<span class="badge badge--const">常驻</span>' : ""}
        ${e.enabled === false ? '<span class="badge badge--off">已停用</span>' : ""}
        <div class="wb-entry-actions">
          <button type="button" class="icon-btn" data-action="wb-entry-edit" title="编辑条目" aria-label="编辑条目">
            <svg class="icon icon--sm" aria-hidden="true"><use href="#i-pencil"/></svg>
          </button>
          <button type="button" class="icon-btn icon-btn--danger" data-action="wb-entry-delete" title="删除条目" aria-label="删除条目">
            <svg class="icon icon--sm" aria-hidden="true"><use href="#i-trash"/></svg>
          </button>
        </div>
      </div>
    </div>
    ${keys.length ? `<div class="wb-entry-keys">${keys.map(k => `<span class="tag">${esc(k)}</span>`).join("")}</div>` : ""}
    ${e.content ? `<div class="wb-entry-content">${esc(String(e.content).slice(0, 240))}${String(e.content).length > 240 ? "…" : ""}</div>` : ""}
  </article>`;
}

function toggleWB(name) {
  const item = document.querySelector(`.wb-item[data-wb="${CSS.escape(name)}"]`);
  if (!item) return;
  if (S._wbOpenItem && S._wbOpenItem !== item) S._wbOpenItem.classList.remove("is-open");
  const isOpen = item.classList.contains("is-open");
  if (isOpen) { item.classList.remove("is-open"); S._wbOpenItem = null; return; }
  item.classList.add("is-open");
  S._wbOpenItem = item;
  const wb = S.wbCache[name];
  const inner = item.querySelector("[data-wb-inner]");
  if (wb && inner && !inner.dataset.loaded) {
    const entries = wb.entries || [];
    const shown = S._wbOnlyConst ? entries.filter(e => e.is_constant) : entries;
    inner.innerHTML = renderWBInner(name, entries, shown);
    inner.dataset.loaded = "1";
    reapplyWBEntryFilter(name);
  }
}

function deleteWB(name, btn) {
  sandboxConfirm(`确定删除世界书「${name}」？此操作不可逆，所有绑定它的角色卡将自动解绑。`, async () => {
    await withBusy(btn, "", async () => {
      try {
        await apiPost("/wb/delete", { name });
        showToast("已删除", "success");
        await loadWB();
      } catch (e) { showToast(e.message); }
    });
  }, true, name);
}

function importWBSt(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const defaultName = file.name.replace(/\.json$/i, "");
  sandboxPrompt("请输入世界书名称（建议使用英文与下划线）", defaultName, async (name) => {
    if (!name) { input.value = ""; return; }
    const btn = input.closest(".file-btn");
    await withBusy(btn, "导入中…", async () => {
      try {
        // 读为文本走 JSON 传输，绕开沙箱对 FormData 的限制
        const text = await file.text();
        await apiPost("/wb/import_json", { name, data: text });
        showToast("导入成功", "success");
        await loadWB(name);
      } catch (e) { showErrorDetail("导入失败", e.message); }
      finally { input.value = ""; }
    });
  });
}

async function exportWBSt() {
  sandboxPrompt("请输入要导出的世界书名称", "", async (name) => {
    if (!name) return;
    try {
      const data = await api("/wb/export_st?name=" + encodeURIComponent(name));
      downloadJSON(data, `${name}_st.json`);
      showToast("已导出 ST 格式", "success");
    } catch (e) { showToast("导出失败：" + e.message); }
  });
}

function deleteWBEntry(wbName, entryId, btn) {
  sandboxConfirm("确定删除该条设定？", async () => {
    await withBusy(btn, "", async () => {
      try {
        await apiPost("/wb/entry/delete", { name: wbName, entry_id: entryId });
        showToast("已删除", "success");
        await reloadSingleWB(wbName);
      } catch (e) { showToast(e.message); }
    });
  }, true);
}

function openWBCreateModal() {
  S._modalState = { type: "wb-create" };
  $("#modalTitle").textContent = "新建世界书";
  $("#modalBody").innerHTML = `
    <div class="form-group">
      <label for="wbNewName">名称</label>
      <input class="field field--wide" type="text" id="wbNewName" placeholder="建议使用英文、数字与下划线，如 modern_city">
    </div>
    <div class="form-group">
      <label for="wbNewDesc">描述（可选）</label>
      <input class="field field--wide" type="text" id="wbNewDesc" placeholder="一句话说明这本世界书的用途">
    </div>`;
  openModal();
}

function openWBEntryModal(wbName, entryId) {
  const wb = S.wbCache[wbName];
  if (!wb) return;
  const entry = entryId ? (wb.entries || []).find(e => e.id === entryId) : null;
  const isNew = !entry;
  S._modalState = { type: "wb-entry", mode: isNew ? "create" : "edit", wbName };
  $("#modalTitle").textContent = `${isNew ? "新建" : "编辑"}条目 · ${wbName}`;
  $("#modalBody").innerHTML = `
    <div class="form-group">
      <label for="weTitle">标题</label>
      <input class="field field--wide" type="text" id="weTitle" value="${esc(entry?.title || "")}">
    </div>
    <div class="form-group">
      <label for="weId">ID${isNew ? "（留空自动生成）" : ""}</label>
      <input class="field field--wide" type="text" id="weId" value="${esc(entry?.id || "")}" ${isNew ? "" : "readonly"} placeholder="唯一标识">
    </div>
    <div class="form-group">
      <label>触发关键词</label>
      <div id="weTagHost"></div>
    </div>
    <div class="form-group">
      <label for="weContent">内容</label>
      <textarea class="field field--wide" id="weContent" rows="6">${esc(entry?.content || "")}</textarea>
    </div>
    <div class="row-wrap">
      <label class="check"><input type="checkbox" id="weConst" ${entry?.is_constant ? "checked" : ""}><span>常驻</span></label>
      <label class="check"><input type="checkbox" id="weEnabled" ${(entry ? entry.enabled : true) !== false ? "checked" : ""}><span>启用</span></label>
    </div>`;
  openModal();
  autoSizeModalTextareas($("#modalBody"));
  setTimeout(() => { S._wbEntryTagInput = createTagInput("weTagHost", entry?.keys || []); }, 40);
}

async function saveWBEntry() {
  const st = S._modalState;
  if (!st) return;
  const name = st.wbName;
  const title = $("#weTitle")?.value.trim() || "";
  const id = $("#weId")?.value.trim() || Math.random().toString(36).slice(2, 10);
  const keys = S._wbEntryTagInput ? S._wbEntryTagInput.getTags() : [];
  const content = $("#weContent")?.value || "";
  const is_constant = !!$("#weConst")?.checked;
  const enabled = $("#weEnabled")?.checked !== false;
  const entry = { id, title, keys, content, is_constant, enabled };
  try {
    if (st.mode === "create") await apiPost("/wb/entry/create", { name, entry });
    else await apiPost("/wb/entry/update", { name, entry_id: id, entry });
    showToast("条目已保存", "success");
    closeModal();
    await reloadSingleWB(name);
  } catch (e) { showToast(e.message); }
}

async function saveWBCreate() {
  const name = $("#wbNewName")?.value.trim() || "";
  const description = $("#wbNewDesc")?.value.trim() || "";
  if (!name) { showToast("请输入世界书名称", "warning"); return; }
  try {
    await apiPost("/wb/create", { name, description });
    showToast("世界书已创建", "success");
    closeModal();
    await loadWB(name);
  } catch (e) { showToast(e.message); }
}

/* ══ H. 角色卡 ═══════════════════════════════════════════════════════ */
