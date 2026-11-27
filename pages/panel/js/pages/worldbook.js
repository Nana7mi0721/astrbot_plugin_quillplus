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
  const ruleTags = [];
  if (e.group) ruleTags.push("组:" + e.group + (Number(e.group_max) > 1 ? "/" + e.group_max : ""));
  const prio = Number(e.priority ?? e.order);
  if (prio && prio !== 100) ruleTags.push("优先级:" + prio);
  const sticky = Number(e.sticky ?? e.sticky_rounds);
  if (sticky > 0) ruleTags.push("粘性:" + sticky + "轮");
  const cooldown = Number(e.cooldown ?? e.cooldown_rounds);
  if (cooldown > 0) ruleTags.push("冷却:" + cooldown + "轮");
  const delay = Number(e.delay ?? e.delay_rounds);
  if (delay > 0) ruleTags.push("延迟:" + delay + "轮");
  if (e.exclude_recursion) ruleTags.push("免递归");
  if (e.prevent_recursion) ruleTags.push("禁递归");
  else if (e.recursive) ruleTags.push("递归");
  if (e.use_regex) ruleTags.push("正则");
  if (Number(e.inject_position) === 3) {
    const d = Number(e.depth);
    ruleTags.push("深度:" + (Number.isFinite(d) ? d : 4));
  }
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
    ${ruleTags.length ? `<div class="wb-entry-keys">${ruleTags.map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
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
  // M5.1：读取时同时兼容规范字段（sticky/cooldown/...）与 v5.3.5 过渡字段
  // （sticky_rounds/...），存量世界书无需迁移即可编辑。
  const e = entry || {};
  const legacyLogic = { primary_and_secondary: "AND_ANY", primary_or_secondary: "AND_ANY", all: "AND_ALL", secondary_only: "AND_ANY" };
  const r = {
    sticky: Number(e.sticky ?? e.sticky_rounds) || 0,
    cooldown: Number(e.cooldown ?? e.cooldown_rounds) || 0,
    delay: Number(e.delay ?? e.delay_rounds) || 0,
    group: e.group || "",
    groupMax: Number(e.group_max) > 0 ? Number(e.group_max) : 1,
    groupWeight: Number.isFinite(Number(e.group_weight)) ? Math.max(0, Number(e.group_weight)) : 100,
    priority: Number.isFinite(Number(e.priority ?? e.order)) ? Math.max(0, Number(e.priority ?? e.order)) : 100,
    scanDepth: e.scan_depth != null ? Number(e.scan_depth) : 0,
    excludeRecursion: !!e.exclude_recursion,
    preventRecursion: !!(e.prevent_recursion ?? e.recursive),
    useRegex: !!e.use_regex,
    injectPosition: [1, 2, 3].includes(Number(e.inject_position)) ? Number(e.inject_position) : 2,
    depth: Number.isFinite(Number(e.depth)) ? Math.max(0, Number(e.depth)) : 4,
    role: ["system", "user", "assistant"].includes(e.role) ? e.role : "system",
    selectiveLogic: e.selective_logic || legacyLogic[e.key_mode] || "AND_ANY",
    secondary: e.secondary_keys || e.keys_secondary || [],
  };
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
      <label>触发关键词（主）</label>
      <div id="weTagHost"></div>
    </div>
    <div class="form-group">
      <label>次关键词（可选，配合下方组合方式）</label>
      <div id="weSecondTagHost"></div>
    </div>
    <div class="form-group">
      <label for="weKeyMode">主次关键词组合方式</label>
      <select class="field field--wide" id="weKeyMode">
        <option value="AND_ANY">主命中 + 次任一（AND_ANY）</option>
        <option value="AND_ALL">主命中 + 次全部（AND_ALL）</option>
        <option value="NOT_ANY">主命中 + 次全不命中（NOT_ANY）</option>
        <option value="NOT_ALL">主命中 + 次不全命中（NOT_ALL）</option>
      </select>
      <span class="hint">次关键词为空时不参与匹配，退化为仅主关键词。</span>
    </div>
    <div class="form-group">
      <label for="weContent">内容</label>
      <textarea class="field field--wide" id="weContent" rows="6">${esc(entry?.content || "")}</textarea>
    </div>
    <div class="form-group">
      <label for="weCond">解锁条件（可选）</label>
      <input class="field field--wide" type="text" id="weCond" value="${esc(entry?.condition || "")}" placeholder="如：好感度>=70 或 剧情阶段 in 约会,婚礼（留空 = 无条件）">
    </div>
    <details class="form-group" ${(r.sticky || r.cooldown || r.delay || r.group || r.preventRecursion || r.useRegex || r.injectPosition === 3) ? "open" : ""}>
      <summary class="fs-12 t3">规则编排（可选：定时效应 / 互斥组 / 递归 / 正则 / 注入位置）</summary>
      <div class="row-wrap">
        <div class="form-group grow">
          <label for="weGroup">互斥组</label>
          <input class="field field--wide" type="text" id="weGroup" value="${esc(r.group)}" placeholder="同组只注入 group_max 条，留空 = 不分组">
        </div>
        <div class="form-group">
          <label for="weGroupMax">组内上限</label>
          <input class="field" type="number" min="1" max="999" id="weGroupMax" value="${r.groupMax}">
        </div>
        <div class="form-group">
          <label for="weGroupWeight">组内权重</label>
          <input class="field" type="number" min="0" max="9999" id="weGroupWeight" value="${r.groupWeight}">
        </div>
        <div class="form-group">
          <label for="wePriority">优先级</label>
          <input class="field" type="number" min="0" max="9999" id="wePriority" value="${r.priority}">
        </div>
      </div>
      <div class="row-wrap">
        <div class="form-group grow">
          <label for="weSticky">粘性轮数</label>
          <input class="field" type="number" min="0" max="999" id="weSticky" value="${r.sticky}">
        </div>
        <div class="form-group grow">
          <label for="weCooldown">冷却轮数</label>
          <input class="field" type="number" min="0" max="999" id="weCooldown" value="${r.cooldown}">
        </div>
        <div class="form-group grow">
          <label for="weDelay">延迟轮数</label>
          <input class="field" type="number" min="0" max="999" id="weDelay" value="${r.delay}">
        </div>
        <div class="form-group grow">
          <label for="weScan">扫描最近消息条数</label>
          <input class="field" type="number" min="0" max="999" id="weScan" value="${r.scanDepth}" placeholder="0 = 全文">
        </div>
      </div>
      <div class="row-wrap">
        <label class="check"><input type="checkbox" id="weExcludeRecursion" ${r.excludeRecursion ? "checked" : ""}><span>不被其他条目递归触发</span></label>
        <label class="check"><input type="checkbox" id="wePreventRecursion" ${r.preventRecursion ? "checked" : ""}><span>本条目不递归触发其他条目</span></label>
        <label class="check"><input type="checkbox" id="weRegex" ${r.useRegex ? "checked" : ""}><span>关键词按安全正则匹配</span></label>
      </div>
      <div class="row-wrap">
        <div class="form-group">
          <label for="weInjectPos">注入位置</label>
          <select class="field" id="weInjectPos">
            <option value="1" ${r.injectPosition === 1 ? "selected" : ""}>常驻位</option>
            <option value="2" ${r.injectPosition === 2 ? "selected" : ""}>按需位</option>
            <option value="3" ${r.injectPosition === 3 ? "selected" : ""}>深度位（按消息深度插入）</option>
          </select>
        </div>
        <div class="form-group">
          <label for="weDepth">深度（倒数第几条）</label>
          <input class="field" type="number" min="0" max="9999" id="weDepth" value="${r.depth}">
        </div>
        <div class="form-group">
          <label for="weRole">深度注入角色</label>
          <select class="field" id="weRole">
            <option value="system" ${r.role === "system" ? "selected" : ""}>system</option>
            <option value="user" ${r.role === "user" ? "selected" : ""}>user</option>
            <option value="assistant" ${r.role === "assistant" ? "selected" : ""}>assistant</option>
          </select>
        </div>
      </div>
    </details>
    <div class="row-wrap">
      <label class="check"><input type="checkbox" id="weConst" ${entry?.is_constant ? "checked" : ""}><span>常驻</span></label>
      <label class="check"><input type="checkbox" id="weEnabled" ${(entry ? entry.enabled : true) !== false ? "checked" : ""}><span>启用</span></label>
    </div>`;
  openModal();
  autoSizeModalTextareas($("#modalBody"));
  setTimeout(() => {
    S._wbEntryTagInput = createTagInput("weTagHost", entry?.keys || []);
    S._wbEntrySecondaryInput = createTagInput("weSecondTagHost", r.secondary || []);
    const keyModeSel = $("#weKeyMode");
    if (keyModeSel) keyModeSel.value = r.selectiveLogic;
  }, 40);
}

async function saveWBEntry() {
  const st = S._modalState;
  if (!st) return;
  const name = st.wbName;
  const title = $("#weTitle")?.value.trim() || "";
  const id = $("#weId")?.value.trim() || Math.random().toString(36).slice(2, 10);
  const keys = S._wbEntryTagInput ? S._wbEntryTagInput.getTags() : [];
  const secondary_keys = S._wbEntrySecondaryInput ? S._wbEntrySecondaryInput.getTags() : [];
  const content = $("#weContent")?.value || "";
  const is_constant = !!$("#weConst")?.checked;
  const enabled = $("#weEnabled")?.checked !== false;
  // M1b-Fix P1-d：条件字段恒随表单提交（空串 = 清除条件，后端
  // update_entry_in_worldbook 对空串 pop 键——不留删不掉的旧条件）。
  const condition = $("#weCond")?.value.trim() || "";
  // M5.1：规则字段恒提交；0 / 空串 = 关闭，后端按缺省等价旧行为。
  const num = (sel, def = 0) => {
    const raw = parseInt($(sel)?.value, 10);
    return Number.isFinite(raw) ? Math.max(0, raw) : def;
  };
  const selective_logic = $("#weKeyMode")?.value || "AND_ANY";
  const group = $("#weGroup")?.value.trim() || "";
  const group_max = Math.max(1, num("#weGroupMax", 1));
  const group_weight = num("#weGroupWeight", 100);
  const priority = num("#wePriority", 100);
  const sticky = num("#weSticky", 0);
  const cooldown = num("#weCooldown", 0);
  const delay = num("#weDelay", 0);
  const scan_depth = num("#weScan", 0);
  const exclude_recursion = !!$("#weExcludeRecursion")?.checked;
  const prevent_recursion = !!$("#wePreventRecursion")?.checked;
  const use_regex = !!$("#weRegex")?.checked;
  const inject_position = parseInt($("#weInjectPos")?.value, 10) || 2;
  const depth = num("#weDepth", 4);
  const role = $("#weRole")?.value || "system";
  const entry = {
    id, title, keys, secondary_keys, selective: secondary_keys.length > 0,
    selective_logic, content, is_constant, enabled, condition,
    group, group_max, group_weight, priority, sticky, cooldown, delay, scan_depth,
    exclude_recursion, prevent_recursion, use_regex, inject_position, depth, role,
  };
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
