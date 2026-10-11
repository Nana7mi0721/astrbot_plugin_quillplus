/* pages/persona.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, showErrorDetail, showToast } from "../components/feedback.js";
import { autoSizeModalTextareas, closeModal, closeScrim, openModal, openScrim, renderEmptyState } from "../components/modal.js";
import { enhanceSelects } from "../components/select.js";
import { updateBadge } from "../pages/config.js";
import { _wbFilterQuery } from "../pages/worldbook.js";
import { $, $$, b64ToBlob, downloadBlob, esc, fileToB64, withBusy } from "../utils.js";

export { MODE_LABEL, MODE_TONE, _personaRenderToken, applyWBEntryFilter, closeAstrbotImportModal, closeImportTextModal, deletePersona, exportPersona, filterCheckboxes, filterWBEntries, filteredPersonas, importFromAstrbot, importPersona, importPersonaFromText, loadAstrbotPersonas, loadPersonas, onRagGroupToggle, openAstrbotImportModal, openImportTextModal, openPersonaModal, reapplyWBEntryFilter, renderAstrbotPersonas, renderExtBlock, renderRagExtBlock, selectAstrbotPersona, setAstrbotStatus, syncRagGroup, toggleExtBig, toggleExtGroup, renderPersonaPage, savePersona, setWBTip, switchPersonaView, syncExtList, updatePersonaStats, wbEntryHit };





let _personaRenderToken = 0;




async function loadPersonas() {
  const host = $("#personaList");
  try {
    const list = await api("/persona/list");
    S._personas = list || [];
    updatePersonaStats();
    if (!S._personas.length) {
      host.innerHTML = renderEmptyState("persona", "还没有角色卡", "点击上方「新建角色卡」创建，或从 V2 角色卡导入",
        '<button type="button" class="btn btn--filled" data-action="persona-new">新建角色卡</button>');
      return;
    }
    S._personaShowCount = 20;
    renderPersonaPage();
  } catch (e) {
    host.innerHTML = renderEmptyState("persona", "加载失败", e.message);
  }
}

function updatePersonaStats() {
  const n = S._personas.length;
  const bar = $("#personaStats");
  if (bar) {
    bar.innerHTML =
      `<span>共 <span class="num">${n}</span> 张</span><span class="sep"></span>` +
      `<span>视图 <span class="num">${S._personaView === "grid" ? "网格" : "列表"}</span></span>`;
  }
  const sub = $("#personaSub");
  if (sub) sub.textContent = n ? `· ${n} 张` : "";
  updateBadge("badge-persona", n);
}

function filteredPersonas() {
  if (!S._personaQuery) return S._personas;
  return S._personas.filter(p => (p.name || "").toLowerCase().includes(S._personaQuery));
}

const MODE_LABEL = { auto: "全库", custom: "隔离", disabled: "关闭" };
const MODE_TONE = { auto: "badge--green", custom: "badge--accent", disabled: "" };

function renderPersonaPage() {
  const host = $("#personaList");
  const list = filteredPersonas();
  if (!list.length) {
    host.innerHTML = renderEmptyState("persona",
      S._personaQuery ? "没有匹配的角色卡" : "还没有角色卡",
      S._personaQuery ? "换个关键词，或清空搜索框" : "点击上方「新建角色卡」开始创建",
      S._personaQuery ? "" : '<button type="button" class="btn btn--filled" data-action="persona-new">新建角色卡</button>');
    return;
  }
  const visible = list.slice(0, S._personaShowCount);
  const hasMore = list.length > S._personaShowCount;
  window.__pids = window.__pids || {};
  const cls = S._personaView === "list" ? "persona-list" : "persona-grid";

  // 先渲染骨架（不含 base64 头像，避免超长字符串阻塞解析），头像再按需注入
  let html = `<div class="${cls}">` + visible.map((p, i) => {
    window.__pids[i] = p.id;
    const ext = p.quill_extensions || {};
    const mode = (m) => (["auto", "custom", "disabled"].includes(m) ? m : "disabled");
    const wbMode = mode(ext.wb_mode), ragMode = mode(ext.rag_mode), wrMode = mode(ext.wr_mode);
    const summary = (p.summary || "").slice(0, 88);
    return `<article class="persona-card">
      <div class="persona-avatar">
        <img data-avatar-id="${esc(p.id)}" alt="" hidden>
        <div class="persona-initial">${esc((p.name || "?").charAt(0))}</div>
      </div>
      <div class="persona-body">
        <div class="persona-name">${esc(p.name || p.id)}</div>
        ${summary ? `<div class="persona-summary">${esc(summary)}${(p.summary || "").length > 88 ? "…" : ""}</div>` : ""}
        <div class="persona-modes">
          <span class="badge ${MODE_TONE[wbMode]}" title="世界书：Auto 全库生效 / Custom 仅勾选 / Disabled 不加载">世界书 ${MODE_LABEL[wbMode]}</span>
          <span class="badge ${MODE_TONE[ragMode]}" title="文档库：Auto 全库检索 / Custom 仅勾选 / Disabled 不检索">文档 ${MODE_LABEL[ragMode]}</span>
          <span class="badge ${MODE_TONE[wrMode]}" title="素材库：Auto 全局匹配 / Custom 仅勾选分类 / Disabled 不检索">素材 ${MODE_LABEL[wrMode]}</span>
        </div>
      </div>
      <div class="persona-actions">
        <button type="button" class="btn btn--sm btn--tinted" data-action="persona-edit" data-idx="${i}">编辑</button>
        <button type="button" class="btn btn--sm btn--gray" data-action="persona-export" data-idx="${i}">导出</button>
        <button type="button" class="btn btn--sm btn--destructive-tinted" data-action="persona-delete" data-idx="${i}">删除</button>
      </div>
    </article>`;
  }).join("") + "</div>";

  if (hasMore) {
    html += `<div id="personaSentinel" class="fs-13 t3" style="text-align:center;padding:var(--sp-4)">
      滚动加载更多（剩余 ${list.length - S._personaShowCount} 张）</div>`;
  }
  host.innerHTML = html;

  if (hasMore) {
    const sentinel = $("#personaSentinel");
    if (sentinel && "IntersectionObserver" in window) {
      const io = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting) { io.disconnect(); S._personaShowCount += 20; renderPersonaPage(); }
      }, { rootMargin: "200px" });
      io.observe(sentinel);
    }
  }

  // 头像懒加载：令牌校验防止过期回调写进已重建的 DOM
  const token = ++_personaRenderToken;
  visible.forEach(p => {
    if (!p.has_avatar) return;
    const img = host.querySelector(`img[data-avatar-id="${CSS.escape(p.id)}"]`);
    if (!img) return;
    api("/persona/avatar?id=" + encodeURIComponent(p.id)).then(res => {
      if (token !== _personaRenderToken) return;
      if (res && res.avatar_url && img.isConnected) {
        img.src = res.avatar_url;
        img.hidden = false;
        const initial = img.nextElementSibling;
        if (initial) initial.style.display = "none";
      }
    }).catch(() => { /* 没有头像时保留首字母 */ });
  });
}

function switchPersonaView(mode) {
  S._personaView = mode;
  $("#personaGrid")?.classList.toggle("is-selected", mode === "grid");
  $("#personaListBtn")?.classList.toggle("is-selected", mode === "list");
  updatePersonaStats();
  renderPersonaPage();
}

/** 三态模式切换时，禁用态置灰并锁定勾选列表。 */
function syncExtList(sys) {
  const modeSel = $(`#pf-${sys}-mode`);
  const list = $(`#pf-${sys}-list`);
  if (!modeSel || !list) return;
  const mode = modeSel.value;
  const off = mode === "disabled";
  list.classList.toggle("is-disabled", off);
  list.style.opacity = "";
  list.style.pointerEvents = "";
  list.querySelectorAll("input[type='checkbox']").forEach(cb => {
    cb.disabled = off;
  });
}

function filterCheckboxes(input, listId) {
  const q = (input.value || "").toLowerCase().trim();
  const list = document.getElementById(listId);
  if (!list) return;
  // 文档知识库是「大组 → 子知识库组 → 文档」三级结构（见 renderRagExtBlock）：
  // 组名命中→整组留下；只有文档名命中→自动展开该组与所在大组并只留命中项。
  // 未展开过的内置库组还没拉文档，只能按组名匹配。
  const groups = $$(".ext-group", list);
  if (groups.length) {
    groups.forEach(g => {
      const head = g.querySelector(".ext-group-head");
      const nameHit = (head?.textContent || "").toLowerCase().includes(q);
      let kidHit = 0;
      $$("label", g.querySelector(".ext-group-docs") || g).forEach(lbl => {
        const hit = !q || nameHit || lbl.textContent.toLowerCase().includes(q);
        lbl.style.display = hit ? "" : "none";
        if (hit && q) kidHit++;
      });
      g.style.display = (!q || nameHit || kidHit > 0) ? "" : "none";
      if (q && !nameHit && kidHit > 0) openExtGroup(g);
    });
    // 大组级：内部一个子组都没留下就整块隐藏；大组名自己命中则整块照原样展开
    $$(".ext-big", list).forEach(b => {
      const head = b.querySelector(".ext-big-head");
      const bigHit = !q || (head?.textContent || "").toLowerCase().includes(q);
      if (bigHit && q) {
        $$(".ext-group", b).forEach(g => {
          g.style.display = "";
          $$("label", g.querySelector(".ext-group-docs") || g).forEach(l => { l.style.display = ""; });
        });
      }
      const anyVisible = bigHit || $$(".ext-group", b).some(g => g.style.display !== "none");
      b.style.display = anyVisible ? "" : "none";
      if (q && anyVisible && !bigHit) openExtBig(b);
    });
    return;
  }
  $$("label", list).forEach(lbl => {
    lbl.style.display = lbl.textContent.toLowerCase().includes(q) ? "" : "none";
  });
}

/* ── 文档知识库：大组 → 子知识库组 → 文档 三级勾选 ────────────────── */

const RAG_BIG_PLUGIN = "__plugin_lib__";
const RAG_BIG_KB = "__astrbot_kb__";
/** 会写进 bound_rag_docs 的组勾选框类型：kb:库名（AstrBot 整库）/ grp:组名（插件分组）。 */
const RAG_GROUP_KINDS = new Set(["kb", "grp"]);

const RAG_GROUP_CARET = `<button type="button" class="ext-group-caret" data-action="ext-group-toggle" aria-expanded="false" aria-label="展开或收起文档">
  <svg class="icon icon--sm" aria-hidden="true"><use href="#i-chevron-right"/></svg>
</button>`;

const RAG_BIG_CARET = `<button type="button" class="ext-big-caret" data-action="ext-big-toggle" aria-expanded="true" aria-label="展开或收起知识库大组">
  <svg class="icon icon--sm" aria-hidden="true"><use href="#i-chevron-right"/></svg>
</button>`;

/* 图标：统一取 index.html 的 #icon-sprite，不用 emoji 当图标
   （emoji 的渲染随字体/平台而变，也不是 macOS 的图标语言）。 */
const ICON_INBOX = '<svg class="icon" aria-hidden="true"><use href="#i-inbox"/></svg>';
const ICON_DOC = '<svg class="icon" aria-hidden="true"><use href="#i-doc"/></svg>';
const ICON_BOOKS = '<svg class="icon" aria-hidden="true"><use href="#i-books"/></svg>';
const ICON_LAYERS = '<svg class="icon" aria-hidden="true"><use href="#i-layers"/></svg>';
const ICON_LINK = '<svg class="icon" aria-hidden="true"><use href="#i-link"/></svg>';
const ICON_WARN = '<svg class="icon icon--sm" aria-hidden="true"><use href="#i-warn"/></svg>';

/** 一篇插件文档的勾选行。 */
function ragDocLabel(d, bound) {
  return `<label class="check"><input type="checkbox" class="cb-rag-bind" value="${esc(d.source)}" ${bound.includes(d.source) ? "checked" : ""}><span class="nowrap">${esc(d.source)}</span><span class="ext-doc-meta">${d.chunk_count || 0} 段</span></label>`;
}

/** 一个插件分组（name 为空即「未分组」，它没有组勾选框）的子知识库组卡片。 */
function ragPluginGroupHtml(name, docs, bound) {
  const key = name ? `grp:${name}` : "__ungrouped__";
  const chunks = docs.reduce((s, d) => s + (d.chunk_count || 0), 0);
  const subs = docs.filter(d => bound.includes(d.source)).length;
  const head = name
    ? `<label class="check"><input type="checkbox" class="cb-rag-grp" data-kind="grp" value="${esc(key)}" ${bound.includes(key) ? "checked" : ""}><span class="nowrap">${ICON_INBOX} ${esc(name)}</span></label>`
    : `<span class="check" style="cursor:default"><span class="nowrap">${ICON_DOC} 未分组</span></span>`;
  return `<div class="ext-group" data-key="${esc(key)}" data-kind="${name ? "grp" : "ungrouped"}">
    <div class="ext-group-head">
      ${RAG_GROUP_CARET}
      ${head}
      <span class="ext-group-meta">${docs.length} 个文档 · ${chunks} 个分块${subs ? ` · 已选 ${subs} 篇` : ""}</span>
    </div>
    <div class="ext-group-docs" ${name ? "hidden" : ""} data-loaded="1">
      ${docs.length ? docs.map(d => ragDocLabel(d, bound)).join("") : '<div class="fs-13 t3">这个分组还没有文档</div>'}
    </div>
  </div>`;
}

/**
 * 知识库绑定块的组态渲染：与「文档知识库」页同构的三级结构。
 * 大组（.ext-big）＝「插件文档库」/「AstrBot 兼容」，各带一个纯 UI 的全选开关
 * （cb-rag-all，value 固定为空、不参与提交）；子知识库组（.ext-group）里的
 * 组勾选框 cb-rag-grp 才是可提交项：插件分组写 grp:组名，内置库写 kb:库名；
 * 文档级 cb-rag-bind：插件文档写 source，内置库文档写 kb:库名/文档名。
 */
function renderRagExtBlock(plugins, groups, kbs, bound, selectHtml, hint, emptyText) {
  const pChunks = plugins.reduce((s, d) => s + (d.chunk_count || 0), 0);
  const kbChunks = kbs.reduce((s, k) => s + (k.chunk_count || 0), 0);
  const ungrouped = plugins.filter(d => !d.group || !groups.includes(d.group));

  const pluginBody = groups.map(name =>
    ragPluginGroupHtml(name, plugins.filter(d => d.group === name), bound)
  ).concat(ragPluginGroupHtml("", ungrouped, bound)).join("");

  const kbBody = kbs.length
    ? kbs.map(k => {
        const whole = bound.includes(k.source);
        const subs = bound.filter(b => b.startsWith(`${k.source}/`)).length;
        const seg = (k.chunk_count || 0) ? `${k.chunk_count} 个分块` : "空库";
        return `<div class="ext-group" data-key="${esc(k.source)}" data-kind="kb">
          <div class="ext-group-head">
            ${RAG_GROUP_CARET}
            <label class="check"><input type="checkbox" class="cb-rag-grp" data-kind="kb" value="${esc(k.source)}" ${whole ? "checked" : ""}><span class="nowrap">${ICON_BOOKS} ${esc(k.kb_name || k.source)}</span></label>
            <span class="ext-group-meta">${k.doc_count || 0} 个文档 · ${seg}${subs ? ` · 已选 ${subs} 篇` : ""}</span>
          </div>
          <div class="ext-group-docs" hidden data-loaded="0" data-kb="${esc(k.kb_name || "")}"><div class="skeleton skeleton-row"></div></div>
        </div>`;
      }).join("")
    : '<div class="fs-13 t3">AstrBot 面板里还没有配置知识库</div>';

  const big = (key, kind, title, meta, inner) => `<div class="ext-big" data-key="${key}">
    <div class="ext-big-head">
      ${RAG_BIG_CARET}
      <label class="check"><input type="checkbox" class="cb-rag-all" data-big="${kind}" value=""><span class="nowrap">${title}</span></label>
      <span class="ext-big-meta">${meta}</span>
    </div>
    <div class="ext-big-body">${inner}</div>
  </div>`;

  const body = (plugins.length || kbs.length)
    ? big(RAG_BIG_PLUGIN, "plugin", `${ICON_LAYERS} 插件文档库`, `${plugins.length} 个文档 · ${pChunks} 个分块 · ${groups.length} 个分组`, pluginBody)
      + big(RAG_BIG_KB, "kb", `${ICON_LINK} AstrBot 兼容`, `${kbs.length} 个库 · ${kbChunks} 个分块`, kbBody)
    : `<div class="fs-13 t3">${esc(emptyText)}</div>`;

  return `<div class="form-group">
    <div class="row" style="justify-content:space-between;margin-bottom:6px">
      <label style="margin:0">文档知识库</label>
      ${selectHtml}
    </div>
    <label class="search" style="width:100%;margin-bottom:6px">
      <svg class="icon" aria-hidden="true"><use href="#i-search"/></svg>
      <input type="search" placeholder="搜索知识库、分组或文档…" data-ext-filter="pf-rag-list" aria-label="搜索知识库、分组或文档">
    </label>
    <div class="ext-list" id="pf-rag-list">${body}</div>
    <span class="hint">${esc(hint)}</span>
  </div>`;
}

/** 组勾选态与子项勾选态互相同步：勾整库/整组＝覆盖组内全部文档。 */
function syncRagGroup(group) {
  if (!group) return;
  const grpCb = group.querySelector(".cb-rag-grp");
  if (!grpCb) return;
  const kids = $$(".cb-rag-bind", group);
  const on = kids.filter(cb => cb.checked).length;
  if (grpCb.checked && RAG_GROUP_KINDS.has(grpCb.dataset.kind)) {
    // 整库/整组已勾选：清掉单独勾的文档，避免出现「整库 + 其中几篇」这种
    // 后端不支持的表达（split_bound_ex 会把它们并成整库/整组）
    kids.forEach(cb => { cb.checked = false; });
    grpCb.indeterminate = false;
    return;
  }
  grpCb.indeterminate = on > 0 && on < kids.length;
}

/** 勾选框变化：大组＝全选（插件侧勾到每篇文档，AstrBot 侧勾到每个库）；组＝整组开关。 */
function onRagGroupToggle(cb) {
  if (cb.classList.contains("cb-rag-all")) {
    const big = cb.closest(".ext-big");
    if (!big) return;
    if (cb.dataset.big === "kb") {
      // 「AstrBot 兼容」全选＝每个库整库（kb:库名）：比逐篇勾文档更省事，
      // 也避免把「整库」和「库内几篇」混在一条绑定里
      $$(".cb-rag-grp", big).forEach(k => { k.checked = cb.checked; });
    } else {
      $$(".cb-rag-bind", big).forEach(k => { k.checked = cb.checked; });
    }
    $$(".ext-group", big).forEach(syncRagGroup);
    return;
  }
  const group = cb.closest(".ext-group");
  if (!group) return;
  syncRagGroup(group);
}

function openExtGroup(group) {
  const host = group?.querySelector(".ext-group-docs");
  if (!host) return;
  host.removeAttribute("hidden");
  group.querySelector(".ext-group-caret")?.setAttribute("aria-expanded", "true");
  if (host.dataset.loaded === "0" && host.dataset.kb) loadExtKbDocs(group);
}

function toggleExtGroup(btn) {
  const group = btn.closest(".ext-group");
  if (!group) return;
  const host = group.querySelector(".ext-group-docs");
  if (!host) return;
  if (host.hasAttribute("hidden")) openExtGroup(group);
  else {
    host.setAttribute("hidden", "");
    group.querySelector(".ext-group-caret")?.setAttribute("aria-expanded", "false");
  }
}

function openExtBig(big) {
  const body = big?.querySelector(".ext-big-body");
  if (!body) return;
  body.removeAttribute("hidden");
  big.querySelector(".ext-big-caret")?.setAttribute("aria-expanded", "true");
}

function toggleExtBig(btn) {
  const big = btn.closest(".ext-big");
  const body = big?.querySelector(".ext-big-body");
  if (!body) return;
  if (body.hasAttribute("hidden")) openExtBig(big);
  else {
    body.setAttribute("hidden", "");
    big.querySelector(".ext-big-caret")?.setAttribute("aria-expanded", "false");
  }
}

/** 展开内置库组时按需拉文档；勾选态用打开弹窗时捕获的 bound 列表判定。 */
async function loadExtKbDocs(group) {
  const host = group.querySelector(".ext-group-docs");
  if (!host || host.dataset.loaded === "1") return;
  host.dataset.loaded = "1";
  host.innerHTML = '<div class="skeleton skeleton-row"></div>';
  const kbName = host.dataset.kb || "";
  const bound = S._ragBound || [];
  try {
    const data = await api(`/rag/kb_documents?kb_name=${encodeURIComponent(kbName)}`);
    const docs = data.documents || [];
    host.innerHTML = docs.length
      ? docs.map(d => {
          const val = `kb:${kbName}/${d.doc_name}`;
          return `<label class="check"><input type="checkbox" class="cb-rag-bind" value="${esc(val)}" ${bound.includes(val) ? "checked" : ""}><span class="nowrap">${esc(d.doc_name)}</span><span class="ext-doc-meta">${d.chunk_count || 0} 段</span></label>`;
        }).join("")
      : `<div class="fs-13 t3">${data.available === false ? "AstrBot 内置知识库当前不可用" : "这个库还没有文档"}</div>`;
    syncRagGroup(group);
  } catch (e) {
    host.dataset.loaded = "0";
    host.innerHTML = `<div class="fs-13" style="color:var(--red-text)">${esc(e.message)}</div>`;
  }
}

/** 世界书「本册条目」内联搜索。
 *  过滤的是当前展开这一本里的 .wb-entry，用 data-hidden 而不是直接改
 *  style.display：条目列表由 renderWBInner 整体重渲染（切换「仅常驻」、
 *  增删条目后端返回后都会重建 DOM），内联样式会随重建一起丢失，
 *  只留下一个还写着关键词、却什么也没过滤的输入框。
 *  data-hidden 由 CSS 统一翻译成 display:none，重建后只要输入框里还有词，
 *  就立刻重新应用一次（见 reloadSingleWB / toggleWB 的调用点）。 */
function filterWBEntries(input) {
  const item = input.closest(".wb-item");
  if (!item) return;
  const name = item.dataset.wb;
  const raw = input.value || "";
  const q = raw.toLowerCase().trim();
  if (q) _wbFilterQuery[name] = raw; else delete _wbFilterQuery[name];
  applyWBEntryFilter(item, q, raw);
}

/** 命中判定：标题、id、关键词、正文拼起来做子串匹配。
 *  单独拆出来是因为渲染期（renderWBInner）也要用同一套规则，两处若各写
 *  一份，重建后的结果会和逐字输入时的结果对不上。 */
function wbEntryHit(e, q) {
  const hay = [e.title || "", e.id || "", (e.keys || []).join(" "), e.content || ""]
    .join(" ").toLowerCase();
  return hay.includes(q);
}

function applyWBEntryFilter(item, q, raw) {
  let hit = 0;
  const entries = $$(".wb-entry", item);
  entries.forEach(e => {
    const show = !q || (e.textContent || "").toLowerCase().includes(q);
    if (show) hit++;
    e.toggleAttribute("data-hidden", !show);
  });
  // 零命中的提示按需新建，而不是只在 HTML 里预留一个再切 hidden：
  // 提示只在「输入了词且一条都没命中」时才该存在，而首次渲染时输入框
  // 是空的、不会有这个节点，后续靠切 hidden 就永远切不出提示来。
  setWBTip(item, q && entries.length && !hit ? (raw || q) : null);
}

/** 挂载 / 更新 / 移除「没有匹配…」提示。传 null 表示移除。 */
function setWBTip(item, text) {
  let tip = $(".wb-filter-empty", item);
  if (text == null) { if (tip) tip.remove(); return; }
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "wb-filter-empty";
    const anchor = item.querySelector("[data-wb-inner]") || item;
    anchor.appendChild(tip);
  }
  tip.innerHTML = `没有匹配「<b>${esc(text)}</b>」的条目`;
  tip.hidden = false;
}

/** 重渲染后按当前关键词恢复过滤。
 *  现在 renderWBInner 已经把词和命中结果直接写进 HTML，正常路径下这里
 *  不需要再做什么；保留它只用于兜底「渲染期没吃到的词」（例如列表由别的
 *  路径塞进来）。仍读 input.value 是刻意的：那条路径下 map 与 DOM 可能
 *  已经不同步，以界面为准才不会出现「框里有词、条目全在」的错位。 */
function reapplyWBEntryFilter(name) {
  const item = document.querySelector('.wb-item[data-wb="' + CSS.escape(name) + '"]');
  if (!item) return;
  const input = $("[data-wb-filter]", item);
  if (!input || !input.value) return;
  applyWBEntryFilter(item, input.value.toLowerCase().trim(), input.value);
}

async function openPersonaModal(personaId) {
  S._personaMode = personaId ? "edit" : "create";
  S._personaEditing = null;
  S._originalAvatarDataUrl = null;
  let data = {
    id: "", name: "", avatar_path: "", summary: "",
    core_prompts: { personality: "", scenario: "", examples_of_dialogue: "", first_message: "" },
    quill_extensions: {
      wb_mode: "disabled", bound_worldbooks: [],
      rag_mode: "disabled", bound_rag_docs: [],
      wr_mode: "disabled", bound_writing_resource: [],
    },
  };
  if (personaId) {
    try {
      const all = await api("/persona/list");
      data = (all || []).find(p => p.id === personaId) || data;
      S._personaEditing = { ...data };
    } catch (e) { showToast(e.message); return; }
  }

  // 扩展绑定所需的候选列表，任一失败都不阻塞表单渲染
  const info = await api("/info").catch(() => ({}));
  const allWbs = info.available_worldbooks || [];
  const ragData = await api("/rag/documents").catch(() => ({ documents: [], groups: [], astrbot_kbs: [] }));
  const ragDocs = ragData.documents || [];
  const ragGroups = ragData.groups || [];
  const astrbotKbs = ragData.astrbot_kbs || [];
  const cats = await api("/wr/categories").catch(() => ({ categories: [] }));

  const ext = data.quill_extensions || {};
  const cp = data.core_prompts || {};
  const wbMode = ext.wb_mode || "disabled";
  const ragMode = ext.rag_mode || "disabled";
  const wrMode = ext.wr_mode || "disabled";
  const boundWb = ext.bound_worldbooks || [];
  const boundRag = ext.bound_rag_docs || [];
  const boundWr = ext.bound_writing_resource || [];

  const modeSelect = (id, current, labels) => `<select class="field" id="${id}" style="width:150px">
    ${labels.map(([v, t]) => `<option value="${v}" ${current === v ? "selected" : ""}>${t}</option>`).join("")}
  </select>`;

  $("#modalTitle").textContent = personaId ? "编辑角色卡" : "新建角色卡";
  $("#modalBody").innerHTML = `
    <div class="segmented" id="pModalTabs" style="margin-bottom:var(--sp-4)" role="tablist">
      <button type="button" class="is-selected" data-ptab="basic" role="tab" aria-selected="true" aria-controls="ptab-basic" id="ptabbtn-basic">基础信息</button>
      <button type="button" data-ptab="core" role="tab" aria-selected="false" aria-controls="ptab-core" id="ptabbtn-core">核心设定</button>
      <button type="button" data-ptab="ext" role="tab" aria-selected="false" aria-controls="ptab-ext" id="ptabbtn-ext">高级扩展</button>
    </div>

    <div class="ptab is-active" id="ptab-basic" role="tabpanel" aria-labelledby="ptabbtn-basic">
      <div class="form-group">
        <label for="pf-name">角色名称</label>
        <input class="field field--wide" type="text" id="pf-name" value="${esc(data.name || "")}" placeholder="必填，如：白芷">
      </div>
      <div class="form-group">
        <label>头像</label>
        <div class="avatar-zone" id="pf-avatar-zone" role="button" tabindex="0">
          <input type="file" id="pf-avatar-file" accept="image/*" hidden aria-label="选择头像图片">
          <div id="avatarPlaceholder">
            <svg class="icon icon--lg" aria-hidden="true"><use href="#i-camera"/></svg>
            <div>点击上传头像</div>
          </div>
          <img id="avatarPreview" alt="头像预览" hidden>
        </div>
        <input type="hidden" id="pf-avatar" value="${esc(data.avatar_path || "")}">
        <div id="avatarRecrop" style="margin-top:var(--sp-2)" ${(data.avatar_path && personaId) ? "" : "hidden"}>
          <button type="button" class="btn btn--sm btn--gray" data-action="persona-recrop">
            <svg class="icon icon--sm" aria-hidden="true"><use href="#i-crop"/></svg>重新裁剪当前头像
          </button>
        </div>
      </div>
      <div class="form-group">
        <label for="pf-summary">简介</label>
        <textarea class="field field--wide" id="pf-summary" rows="3" placeholder="显示在角色卡上的简短介绍">${esc(data.summary || "")}</textarea>
      </div>
    </div>

    <div class="ptab" id="ptab-core" role="tabpanel" aria-labelledby="ptabbtn-core">
      <div class="form-group">
        <label for="pf-personality">人格 / 外貌设定</label>
        <textarea class="field field--wide" id="pf-personality" rows="8" placeholder="性格、外貌、语气等核心设定">${esc(cp.personality || "")}</textarea>
      </div>
      <div class="form-group">
        <label for="pf-scenario">世界观 / 场景</label>
        <textarea class="field field--wide" id="pf-scenario" rows="6" placeholder="当前场景与世界背景">${esc(cp.scenario || "")}</textarea>
      </div>
      <div class="form-group">
        <label for="pf-examples">对话范例</label>
        <textarea class="field field--wide" id="pf-examples" rows="6" placeholder="几组对话示例，帮助模型掌握角色语气">${esc(cp.examples_of_dialogue || "")}</textarea>
      </div>
      <div class="form-group">
        <label for="pf-first">开场白</label>
        <textarea class="field field--wide" id="pf-first" rows="4" placeholder="切换该角色后自动注入对话历史的第一条">${esc(cp.first_message || "")}</textarea>
      </div>
    </div>

    <div class="ptab" id="ptab-ext" role="tabpanel" aria-labelledby="ptabbtn-ext">
      ${renderExtBlock("wb", "世界书", "搜索世界书…", allWbs, boundWb,
        modeSelect("pf-wb-mode", wbMode, [["auto", "全库激活"], ["custom", "隔离模式"], ["disabled", "完全禁用"]]),
        "全库激活＝所有世界书生效；隔离模式＝仅勾选的生效；完全禁用＝不加载世界书。", "暂无可用的世界书")}
      ${renderRagExtBlock(ragDocs, ragGroups, astrbotKbs, boundRag,
        modeSelect("pf-rag-mode", ragMode, [["auto", "全库检索"], ["custom", "隔离模式"], ["disabled", "完全禁用"]]),
        "全库检索＝检索插件全部文档与非空内置知识库；隔离模式＝仅检索勾选项；完全禁用＝不检索。勾分组＝该分组的全部文档，勾库＝整库，展开后可只勾其中几篇。分组与上传在本插件「文档知识库」页管理，内置库在 AstrBot 面板管理。",
        "暂无已上传文档与可用内置知识库")}
      ${renderExtBlock("wr", "写作素材库", "搜索分类…", cats.categories || [], boundWr,
        modeSelect("pf-wr-mode", wrMode, [["auto", "全局匹配"], ["custom", "隔离模式"], ["disabled", "完全禁用"]]),
        "全局匹配＝匹配所有素材；隔离模式＝仅匹配勾选分类；完全禁用＝不检索素材。", "暂无可用的素材分类")}
    </div>`;

  $("#modalBody").querySelectorAll("[data-ptab]").forEach(btn => {
    btn.addEventListener("click", () => {
      $$("#pModalTabs > button").forEach(b => { b.classList.remove("is-selected"); b.setAttribute("aria-selected", "false"); });
      $$("#modalBody .ptab").forEach(p => p.classList.remove("is-active"));
      btn.classList.add("is-selected");
      btn.setAttribute("aria-selected", "true");
      $(`#ptab-${btn.dataset.ptab}`).classList.add("is-active");
    });
  });
  ["wb", "rag", "wr"].forEach(syncExtList);
  // 知识库组的「整库 / 部分选中」初始态（子项懒加载后会在 loadExtKbDocs 里再同步一次）
  S._ragBound = boundRag.slice();
  $$("#pf-rag-list .ext-group").forEach(syncRagGroup);
  // modeSelect 生成的三处平台模式下拉同样走自绘（模板字符串本身不改）
  enhanceSelects($("#modalBody"));

  if (personaId && data.has_avatar) {
    const img = $("#avatarPreview");
    const ph = $("#avatarPlaceholder");
    if (img && ph) {
      api("/persona/avatar?id=" + encodeURIComponent(data.id)).then(res => {
        if (res && res.avatar_url) {
          img.src = res.avatar_url;
          img.hidden = false;
          ph.hidden = true;
        }
      }).catch(() => {});
    }
  }

  openModal();
  autoSizeModalTextareas($("#modalBody"));
  S._modalState = { type: "persona" };
}

function renderExtBlock(sys, title, ph, options, bound, selectHtml, hint, emptyText) {
  // options 支持字符串或 {value, label}：kb: 内置库源复选框 value 仍是
  // 绑定标识（kb:库名），label 额外带上分段数
  const norm = options.map(o => (typeof o === "string" ? { value: o, label: o } : o));
  const body = norm.length
    ? norm.map(o => `<label class="check"><input type="checkbox" class="cb-${sys}-bind" value="${esc(o.value)}" ${bound.includes(o.value) ? "checked" : ""}><span class="nowrap">${esc(o.label)}</span></label>`).join("")
    : `<div class="fs-13 t3">${esc(emptyText)}</div>`;
  return `<div class="form-group">
    <div class="row" style="justify-content:space-between;margin-bottom:6px">
      <label style="margin:0">${esc(title)}</label>
      ${selectHtml}
    </div>
    <label class="search" style="width:100%;margin-bottom:6px">
      <svg class="icon" aria-hidden="true"><use href="#i-search"/></svg>
      <input type="search" placeholder="${esc(ph)}" data-ext-filter="pf-${sys}-list" aria-label="${esc(ph)}">
    </label>
    <div class="ext-list" id="pf-${sys}-list">${body}</div>
    <span class="hint">${esc(hint)}</span>
  </div>`;
}

async function savePersona() {
  const name = $("#pf-name")?.value.trim() || "";
  if (!name) { showToast("角色名称不能为空", "warning"); return; }
  const checked = (cls) => $$(`.${cls}:checked`).map(cb => cb.value);
  // 文档知识库的粒度可以是整库（cb-rag-grp[data-kind=kb]，值＝kb:库名）、
  // 整个插件分组（cb-rag-grp[data-kind=grp]，值＝grp:组名）或单个文档
  // （cb-rag-bind，值＝kb:库名/文档名 或 插件文档的 source）。
  // 两个大组的全选开关（cb-rag-all）只是 UI 全选，value=""，不参与提交。
  const boundRagDocs = [...new Set([
    ...checked("cb-rag-bind"),
    ...$$(".cb-rag-grp:checked").filter(cb => RAG_GROUP_KINDS.has(cb.dataset.kind)).map(cb => cb.value),
  ])];
  const body = {
    id: S._personaMode === "edit" && S._personaEditing ? S._personaEditing.id : name,
    name,
    avatar_path: $("#pf-avatar")?.value.trim() || "",
    summary: $("#pf-summary")?.value.trim() || "",
    core_prompts: {
      personality: $("#pf-personality")?.value.trim() || "",
      scenario: $("#pf-scenario")?.value.trim() || "",
      examples_of_dialogue: $("#pf-examples")?.value.trim() || "",
      first_message: $("#pf-first")?.value.trim() || "",
    },
    quill_extensions: {
      wb_mode: $("#pf-wb-mode")?.value || "disabled",
      bound_worldbooks: checked("cb-wb-bind"),
      rag_mode: $("#pf-rag-mode")?.value || "disabled",
      bound_rag_docs: boundRagDocs,
      wr_mode: $("#pf-wr-mode")?.value || "disabled",
      bound_writing_resource: checked("cb-wr-bind"),
    },
  };
  try {
    if (S._personaMode === "create") { await apiPost("/persona/create", body); showToast("角色卡已创建", "success"); }
    else { await apiPost("/persona/update", body); showToast("角色卡已更新", "success"); }
    closeModal();
    loadPersonas();
  } catch (e) { showToast(e.message); }
}

function deletePersona(personaId, btn) {
  sandboxConfirm(`确定删除角色卡「${personaId}」？此操作不可逆，其头像与世界书绑定将一并清除。`, async () => {
    await withBusy(btn, "", async () => {
      try {
        await apiPost("/persona/delete", { id: personaId });
        showToast("已删除", "success");
        loadPersonas();
      } catch (e) { showToast(e.message); }
    });
  }, true, personaId);
}

async function importPersona(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (!["png", "jpg", "jpeg", "webp", "json"].includes(ext)) { showToast("仅支持 PNG / JPG / WebP / JSON", "warning"); return; }
  if (file.size > 5 * 1024 * 1024) { showToast("文件过大（上限 5MB）", "warning"); return; }
  const btn = input.closest(".file-btn");
  await withBusy(btn, "导入中…", async () => {
    try {
      const b64 = await fileToB64(file);
      const res = await apiPost("/persona/import_base64", { filename: file.name, b64_data: b64 });
      showToast(`角色卡「${res.name}」导入成功`, "success");
      loadPersonas();
    } catch (e) { showErrorDetail("角色卡导入失败", e.message); loadPersonas(); }
    finally { input.value = ""; }
  });
}

async function exportPersona(personaId) {
  try {
    const p = S._personas.find(x => x.id === personaId);
    const fallback = p ? (p.avatar_path ? `${p.name}_v2.png` : `${p.name}_v2.json`) : `${personaId}_v2.json`;
    const res = await api("/persona/export_base64", { method: "POST", body: JSON.stringify({ id: personaId }) });
    downloadBlob(b64ToBlob(res.b64_data), res.filename || fallback);
    showToast("已导出角色卡", "success");
  } catch (e) { showToast("导出失败：" + e.message); }
}

function openImportTextModal() {
  const scrim = $("#textScrim");
  openScrim(scrim);
  setTimeout(() => $("#importText")?.focus(), 60);
}

function closeImportTextModal() {
  closeScrim($("#textScrim"));
  const ta = $("#importText");
  if (ta) ta.value = "";
}

async function importPersonaFromText(btn) {
  const text = ($("#importText")?.value || "").trim();
  if (!text) { showToast("请粘贴角色设定文本", "warning"); return; }
  await withBusy(btn, "解析中…", async () => {
    try {
      // base64 编码后传输，避免沙箱与编码问题
      const b64 = btoa(unescape(encodeURIComponent(text)));
      const res = await apiPost("/persona/import_text_base64", { b64_text: b64 });
      closeImportTextModal();
      showToast(`角色卡「${res.name}」导入成功`, "success");
      loadPersonas();
    } catch (e) { showErrorDetail("文本导入失败", e.message); }
  });
}

/* ── 由 AstrBot 人格导入 ────────────────────────────────────────────
   数据面：/persona/astrbot_list 只读 AstrBot 的 persona_manager，
   /persona/astrbot_import 用「摘要模型」（配置里的 LLM 提供商）把人格正文
   整理成角色卡四段式后落卡。
   转换要等一次真实 LLM 调用（长人格十几秒），所以：
   · 期间把「导入」按钮切成 spinner + 「转换中…」并禁用，避免重复提交造出多张卡；
   · 失败信息同时写进弹窗内的状态块（弹窗不关，用户可以直接改名字重试）。 */
let _abPersonas = [];
let _abMeta = null;
let _abSelected = "";

function setAstrbotStatus(text, tone = "") {
  const box = $("#abStatus");
  if (!box) return;
  box.hidden = !text;
  box.className = "ab-status note" + (tone ? ` note--${tone}` : "");
  box.innerHTML = text
    ? `<svg class="icon" aria-hidden="true"><use href="#i-${tone === "warn" ? "warn" : "info"}"/></svg><span>${esc(text)}</span>`
    : "";
}

function setAstrbotImportEnabled(on) {
  const btn = $("#abImportBtn");
  if (btn) btn.disabled = !on;
}

async function openAstrbotImportModal() {
  const scrim = $("#astrbotScrim");
  if (!scrim) return;
  _abSelected = "";
  _abPersonas = [];
  _abMeta = null;
  const nameInput = $("#abName");
  if (nameInput) nameInput.value = "";
  setAstrbotStatus("");
  setAstrbotImportEnabled(false);
  openScrim(scrim);
  await loadAstrbotPersonas();
}

function closeAstrbotImportModal() {
  closeScrim($("#astrbotScrim"));
  _abPersonas = [];
  _abMeta = null;
  _abSelected = "";
  const list = $("#abList");
  if (list) list.innerHTML = "";
  const nameInput = $("#abName");
  if (nameInput) nameInput.value = "";
  setAstrbotStatus("");
}

async function loadAstrbotPersonas() {
  const list = $("#abList");
  if (list) list.innerHTML = '<div class="ab-empty">正在读取 AstrBot 人格…</div>';
  try {
    const res = await api("/persona/astrbot_list");
    _abMeta = res || {};
    _abPersonas = (_abMeta && _abMeta.personas) || [];
  } catch (e) {
    _abMeta = null;
    _abPersonas = [];
    setAstrbotStatus(`读取人格失败：${e.message}`, "warn");
  }
  renderAstrbotPersonas();
}

function astrbotPersonaRow(p) {
  const pid = String(p.persona_id || "");
  const selected = pid === _abSelected;
  const bits = [];
  if (p.folder) bits.push(esc(p.folder));
  bits.push(`${Number(p.prompt_chars) || 0} 字`);
  if (Number(p.begin_dialog_count) > 0) bits.push(`${Number(p.begin_dialog_count)} 条开场对话`);
  const tag = p.name_taken ? '<span class="ab-tag">已有同名角色卡</span>' : "";
  return `<button type="button" class="ab-item${selected ? " is-selected" : ""}" role="option"
    aria-selected="${selected ? "true" : "false"}" data-action="astrbot-pick" data-pid="${esc(pid)}">
    <span class="ab-item-main">
      <span class="ab-item-name">${esc(pid)}${tag}</span>
      <span class="ab-item-sub">${bits.join(" · ")}</span>
    </span>
    <svg class="icon icon--sm ab-item-check" aria-hidden="true"><use href="#i-check"/></svg>
  </button>`;
}

function renderAstrbotPersonas() {
  const list = $("#abList");
  if (!list) return;
  const meta = _abMeta || {};
  if (!meta.available) {
    list.innerHTML = "";
    setAstrbotStatus(meta.reason || "当前 AstrBot 未向插件提供人格管理器，无法读取人格", "warn");
    setAstrbotImportEnabled(false);
    return;
  }
  // 摘要模型没配好就别让用户白点一次（后端也会拒，但提示在这里更早）
  if (!meta.provider_ready) {
    setAstrbotStatus(meta.provider_id
      ? `摘要模型 ${meta.provider_id} 不可用：请到「配置 → RAG 与动态记忆」检查 LLM 提供商`
      : "尚未配置用于生成摘要的模型：请到「配置 → RAG 与动态记忆」选择 LLM 提供商", "warn");
  } else {
    setAstrbotStatus("");
  }
  if (!_abPersonas.length) {
    list.innerHTML = '<div class="ab-empty">AstrBot 里还没有创建过人格</div>';
    setAstrbotImportEnabled(false);
    return;
  }
  list.innerHTML = _abPersonas.map(astrbotPersonaRow).join("");
  setAstrbotImportEnabled(Boolean(meta.provider_ready));
  // 只有一个候选时直接选中，省一次点击
  if (_abPersonas.length === 1) selectAstrbotPersona(_abPersonas[0].persona_id);
}

function selectAstrbotPersona(pid) {
  _abSelected = String(pid || "");
  $$("#abList .ab-item").forEach(btn => {
    const on = btn.dataset.pid === _abSelected;
    btn.classList.toggle("is-selected", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  const item = _abPersonas.find(p => String(p.persona_id) === _abSelected);
  const nameInput = $("#abName");
  // AstrBot 人格名就是 persona_id，直接当默认角色卡名；已重名的留空让摘要模型
  // 自己取名（后端在撞名时会加「（导入）」后缀），免得用户先撞一次 400。
  if (nameInput) nameInput.value = item && item.name_taken ? "" : _abSelected;
  if (item && item.name_taken) {
    setAstrbotStatus(`已有一张同名角色卡「${_abSelected}」：名称留空则由摘要模型起名（若仍然重名会自动加「（导入）」后缀），也可以自己填一个`, "warn");
  } else if (_abMeta && _abMeta.provider_ready) {
    setAstrbotStatus("");
  }
  setAstrbotImportEnabled(Boolean(_abMeta && _abMeta.provider_ready) && Boolean(_abSelected));
}

async function importFromAstrbot(btn) {
  if (!_abSelected) { showToast("请先选择一个人格", "warning"); return; }
  const name = ($("#abName")?.value || "").trim();
  setAstrbotStatus("正在用摘要模型整理人格设定，请稍候…（长人格可能需要十几秒）");
  await withBusy(btn || $("#abImportBtn"), "转换中…", async () => {
    try {
      const res = await apiPost("/persona/astrbot_import", { persona_id: _abSelected, name });
      const card = (res && res.persona) || {};
      const renamed = (res && res.renamed_from) || "";
      closeAstrbotImportModal();
      showToast(renamed
        ? `已导入为「${card.name}」：「${renamed}」重名，已自动加后缀`
        : `角色卡「${card.name}」导入成功`, "success");
      loadPersonas();
    } catch (e) {
      setAstrbotStatus(`导入失败：${e.message}`, "warn");
      showErrorDetail("由人格导入失败", e.message);
    }
  });
}
