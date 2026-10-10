/* pages/rag.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, sandboxPrompt, showToast } from "../components/feedback.js";
import { renderEmptyState } from "../components/modal.js";
import { enhanceSelects } from "../components/select.js";
import { loadGlobalSettings, updateBadge } from "../pages/config.js";
import { $, $$, esc, fileToB64, withBusy } from "../utils.js";

export { deleteRAGDocument, deleteRAGGroup, getRAGConfig, loadRAG, loadRAGModelInfo, moveRAGDocument,
         newRAGGroup, processRAGFiles, providerRows, renameRAGGroup, setRAGKbEnabled, testRAGSearch,
         toggleRAGBig, toggleRAGGroup };

/* 二级结构：两个「大组」——插件文档库（可写）与 AstrBot 兼容（只读）。
   大组下面是子知识库组（插件的自定义分组 / AstrBot 的每个库），
   子组下面是文档行。组的 key 与角色卡绑定标识一致，便于排查：
   插件分组用 `grp:组名`，内置库用 `kb:库名`。 */
const PLUGIN_BIG = "__plugin_lib__";
const KB_BIG = "__astrbot_kb__";
/** 「不属于任何分组」的文档所在的虚拟组（不可改名/删除）。 */
const UNGROUPED_KEY = "__ungrouped__";
/** 组展开状态：模块级 Map，重渲染（上传/删除/切页）后保留用户的选择。 */
const _ragExpanded = new Map();
/** 大组展开状态（同样跨重渲染保留）。 */
const _ragBigExpanded = new Map();
/** 已拉取过的内置库文档：key → 文档数组（避免每次重渲染都打后端）。 */
const _ragKbDocs = new Map();
/** 最近一次拉到的插件分组名（渲染「移动文档」下拉用）。 */
let _ragGroups = [];
const isGroupExpanded = (key, dflt) => (_ragExpanded.has(key) ? _ragExpanded.get(key) : dflt);
const isBigExpanded = (key, dflt) => (_ragBigExpanded.has(key) ? _ragBigExpanded.get(key) : dflt);

/* ── 兼容开关 ─────────────────────────────────────────────────────── */

/** 兼容开关的当前值：直接读全局配置，未加载时按 schema 默认（开）处理。 */
function ragKbEnabled() {
  const g = (S._rawConfig || {}).rag || {};
  return g.astrbot_kb_enabled !== false;
}

/**
 * 落盘「兼容 AstrBot 知识库」开关。
 * 不走 saveAllChanges（那是配置页的批量保存），单独写这一个键，
 * 写完整体重读配置，顺带把配置页的 #c-rag-kb 同步到同一状态。
 */
async function setRAGKbEnabled(on, el) {
  const toggle = el || $("#ragKbToggle");
  if (toggle) toggle.disabled = true;
  try {
    await apiPost("/config/save_batch", { updates: [{ group: "rag", key: "astrbot_kb_enabled", value: !!on }] });
    await loadGlobalSettings();
    showToast(on ? "已开启 AstrBot 知识库兼容" : "已关闭 AstrBot 知识库兼容", "success");
  } catch (e) {
    showToast(e.message);
    if (toggle) toggle.checked = !on; // 失败回滚开关位置
  } finally {
    if (toggle) toggle.disabled = false;
    loadRAG();
  }
}

/* ── 子知识库组渲染 ───────────────────────────────────────────────── */

function groupHtml(o) {
  return `<div class="kb-group" data-key="${esc(o.key)}" data-kind="${o.kind}">
    <div class="kb-group-head">
      <button type="button" class="kb-group-caret" data-action="rag-group-toggle" aria-expanded="${o.expanded ? "true" : "false"}" aria-label="展开或收起文档">
        <svg class="icon icon--sm" aria-hidden="true"><use href="#i-chevron-right"/></svg>
      </button>
      <div class="kb-group-icon" aria-hidden="true">${o.icon}</div>
      <div class="grow">
        <div class="kb-group-name">${esc(o.title)}${o.badge || ""}</div>
        ${o.desc ? `<div class="kb-group-desc">${esc(o.desc)}</div>` : ""}
        <div class="kb-group-meta">${o.metaHtml || ""}</div>
      </div>
      ${o.actions ? `<div class="kb-group-actions">${o.actions}</div>` : ""}
    </div>
    <div class="kb-group-docs" ${o.expanded ? "" : "hidden"} data-loaded="${o.loaded ? "1" : "0"}">${o.docsHtml || ""}</div>
  </div>`;
}

/** 「移动到分组」下拉的选项：空值 = 未分组。 */
function groupOptions(selected) {
  const cur = selected || "";
  const opts = [`<option value=""${cur ? "" : " selected"}>未分组</option>`];
  _ragGroups.forEach(g => {
    opts.push(`<option value="${esc(g)}"${cur === g ? " selected" : ""}>${esc(g)}</option>`);
  });
  // 分组已被删掉、文档的 group 字段还指向它（后端会 prune，这里只是兜底）
  if (cur && !_ragGroups.includes(cur)) {
    opts.push(`<option value="${esc(cur)}" selected>${esc(cur)}（已删除）</option>`);
  }
  return opts.join("");
}

/** 大组卡片（插件文档库 / AstrBot 兼容）。 */
function bigHtml(o) {
  return `<section class="kb-big" data-key="${esc(o.key)}" data-kind="${o.kind}">
    <div class="kb-big-head">
      <button type="button" class="kb-big-caret" data-action="rag-big-toggle" aria-expanded="${o.expanded ? "true" : "false"}" aria-label="展开或收起">
        <svg class="icon icon--sm" aria-hidden="true"><use href="#i-chevron-right"/></svg>
      </button>
      <div class="kb-big-icon" aria-hidden="true">${o.icon}</div>
      <div class="grow">
        <div class="kb-big-name">${esc(o.title)}${o.badge || ""}</div>
        ${o.desc ? `<div class="kb-big-desc">${esc(o.desc)}</div>` : ""}
        <div class="kb-big-meta">${o.metaHtml || ""}</div>
      </div>
      ${o.actions ? `<div class="kb-big-actions">${o.actions}</div>` : ""}
    </div>
    <div class="kb-big-body" ${o.expanded ? "" : "hidden"}>${o.bodyHtml || ""}</div>
  </section>`;
}

function pluginDocRow(d) {
  return `<div class="doc-row">
        <div class="grow">
          <div class="doc-name">${esc(d.source)}</div>
          <div class="doc-meta">${d.chunk_count || 0} 个分块</div>
        </div>
        <!-- 与「模型选择」同款自绘下拉：原生 select 只作值存储，外观见 .sel-wrap -->
        <select class="field doc-group-select" style="width:150px" data-source="${esc(d.source)}" aria-label="移动到分组" title="移动到分组">
          ${groupOptions(d.group || "")}
        </select>
        <button type="button" class="btn btn--sm btn--destructive-tinted" data-action="rag-delete" data-value="${esc(d.source)}">删除</button>
      </div>`;
}

function kbDocRow(d) {
  const bytes = Number(d.file_size || 0);
  const size = bytes > 0 ? ` · ${bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`}` : "";
  return `<div class="doc-row">
        <div class="grow">
          <div class="doc-name">${esc(d.doc_name)}</div>
          <div class="doc-meta">${d.chunk_count || 0} 个分块${size}</div>
        </div>
        <span class="badge">只读</span>
      </div>`;
}

/** 插件文档库大组：自定义分组 + 未分组，可建组/改名/删组/移动文档/上传。 */
function pluginBigHtml(docs, groups) {
  const chunks = docs.reduce((s, d) => s + (d.chunk_count || 0), 0);
  const byGroup = new Map(groups.map(g => [g, []]));
  const ungrouped = [];
  docs.forEach(d => {
    const g = d.group || "";
    if (g && byGroup.has(g)) byGroup.get(g).push(d);
    else ungrouped.push(d); // 分组记录缺失（理论上有 prune 兜底）→ 按未分组展示
  });
  const body = groups.map(g => docGroupHtml(g, byGroup.get(g) || []))
    .concat([ungroupedHtml(ungrouped)]).join("");
  const target = `<select id="ragUploadTarget" class="field" style="width:150px" aria-label="上传到分组" title="上传到分组">
      <option value="">未分组</option>
      ${groups.map(g => `<option value="${esc(g)}">${esc(g)}</option>`).join("")}
    </select>`;
  return bigHtml({
    key: PLUGIN_BIG, kind: "plugin", icon: "🗂️",
    title: "插件文档库",
    badge: ' <span class="badge badge--accent">本插件</span>',
    desc: "上传到本插件的文档，共用插件自己的向量库与嵌入模型；分组只是归类，检索预算仍是全局的",
    metaHtml: `${docs.length} 个文档 · ${chunks} 个分块 · ${groups.length} 个分组`,
    actions: `<button type="button" class="btn btn--sm btn--gray" data-action="rag-group-new">新建分组</button>
      ${target}
      <button type="button" class="btn btn--sm" data-action="rag-pick">上传</button>`,
    bodyHtml: body,
    expanded: isBigExpanded(PLUGIN_BIG, true),
  });
}

/** 插件自定义分组（子知识库）：可改名/删除，组内文档可移动。 */
function docGroupHtml(name, docs) {
  const key = `grp:${name}`;
  const chunks = docs.reduce((s, d) => s + (d.chunk_count || 0), 0);
  return groupHtml({
    key, kind: "grp", icon: "📁",
    title: name,
    badge: "",
    desc: "",
    metaHtml: `${docs.length} 个文档 · ${chunks} 个分块`,
    actions: `<button type="button" class="btn btn--sm btn--gray" data-action="rag-group-rename" data-value="${esc(name)}">改名</button>
      <button type="button" class="btn btn--sm btn--destructive-tinted" data-action="rag-group-delete" data-value="${esc(name)}">删除</button>`,
    docsHtml: docs.length ? docs.map(pluginDocRow).join("") : '<div class="fs-13 t3">这个分组还没有文档（上传时把「上传到分组」选成它，或把已有文档移进来）</div>',
    expanded: isGroupExpanded(key, false),
    loaded: true,
  });
}

/** 「未分组」虚拟组：独立文档照样能绑定到角色卡。 */
function ungroupedHtml(docs) {
  const chunks = docs.reduce((s, d) => s + (d.chunk_count || 0), 0);
  return groupHtml({
    key: UNGROUPED_KEY, kind: "ungrouped", icon: "📄",
    title: "未分组",
    badge: "",
    desc: "不属于任何分组的独立文档，仍然可以单独绑定到角色卡",
    metaHtml: `${docs.length} 个文档 · ${chunks} 个分块`,
    actions: "",
    docsHtml: docs.length ? docs.map(pluginDocRow).join("") : '<div class="fs-13 t3">没有未分组的文档</div>',
    expanded: isGroupExpanded(UNGROUPED_KEY, true),
    loaded: true,
  });
}

/** AstrBot 兼容大组：收纳 AstrBot 面板里配置好的每个知识库（只读）。 */
function kbBigHtml(kbs) {
  const chunks = kbs.reduce((s, k) => s + (k.chunk_count || 0), 0);
  const docs = kbs.reduce((s, k) => s + (k.doc_count || 0), 0);
  return bigHtml({
    key: KB_BIG, kind: "kb", icon: "🔗",
    title: "AstrBot 兼容",
    badge: ' <span class="badge">只读</span>',
    desc: "AstrBot 面板「知识库」页里配置的库；每个库的嵌入模型可与插件不同，管理与上传在 AstrBot 面板里",
    metaHtml: kbs.length ? `${kbs.length} 个库 · ${docs} 个文档 · ${chunks} 个分块` : "还没有知识库",
    actions: "",
    bodyHtml: kbs.length
      ? kbs.map(kbGroupHtml).join("")
      : '<div class="fs-13 t3" style="padding:var(--sp-2) 0">AstrBot 面板里还没有配置知识库。去 AstrBot 面板的「知识库」页新建后回到这里刷新即可。</div>',
    expanded: isBigExpanded(KB_BIG, true),
  });
}

/** AstrBot 内置知识库：每组一个，文档懒加载，只读。 */
function kbGroupHtml(k) {
  const key = k.source; // `kb:库名`
  const cached = _ragKbDocs.get(key);
  const expanded = isGroupExpanded(key, false);
  const metaHtml = k.init_error
    ? `<span style="color:var(--orange-text)">⚠ ${esc(k.init_error)}</span>`
    : `${k.doc_count || 0} 个文档 · ${k.chunk_count || 0} 个分块`;
  return groupHtml({
    key, kind: "kb", icon: esc(k.emoji || "📚"),
    title: k.kb_name || key,
    badge: ' <span class="badge">内置库</span>',
    desc: k.description || "",
    metaHtml,
    actions: "",
    docsHtml: cached
      ? (cached.length ? cached.map(kbDocRow).join("") : '<div class="fs-13 t3">这个库还没有文档</div>')
      : '<div class="skeleton skeleton-row"></div>',
    expanded,
    loaded: !!cached,
  });
}

/** 展开内置库组时按需拉文档列表（只拉一次，结果进 _ragKbDocs 缓存）。 */
async function loadKBGroupDocs(group) {
  const host = group.querySelector(".kb-group-docs");
  if (!host || host.dataset.loaded === "1") return;
  const key = group.dataset.key || "";
  host.dataset.loaded = "1"; // 先占位防重入，失败再放开
  host.innerHTML = '<div class="skeleton skeleton-row"></div>';
  try {
    const kbName = key.replace(/^kb:/, "");
    const data = await api(`/rag/kb_documents?kb_name=${encodeURIComponent(kbName)}`);
    const docs = data.documents || [];
    _ragKbDocs.set(key, docs);
    host.innerHTML = docs.length
      ? docs.map(kbDocRow).join("")
      : `<div class="fs-13 t3">${data.available === false ? "AstrBot 内置知识库当前不可用" : "这个库还没有文档"}</div>`;
  } catch (e) {
    host.dataset.loaded = "0";
    host.innerHTML = `<div class="fs-13" style="color:var(--red-text)">${esc(e.message)}</div>`;
  }
}

/** 组头的小三角：展开/收起，内置库展开时顺带懒加载文档。 */
function toggleRAGGroup(btn) {
  const group = btn.closest(".kb-group");
  if (!group) return;
  const host = group.querySelector(".kb-group-docs");
  if (!host) return;
  const willOpen = host.hasAttribute("hidden");
  host.toggleAttribute("hidden", !willOpen);
  btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  _ragExpanded.set(group.dataset.key || "", willOpen);
  if (willOpen && group.dataset.kind === "kb" && host.dataset.loaded === "0") loadKBGroupDocs(group);
}

/** 大组头的小三角：展开/收起整个大组。 */
function toggleRAGBig(btn) {
  const sec = btn.closest(".kb-big");
  if (!sec) return;
  const body = sec.querySelector(".kb-big-body");
  if (!body) return;
  const willOpen = body.hasAttribute("hidden");
  body.toggleAttribute("hidden", !willOpen);
  btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  _ragBigExpanded.set(sec.dataset.key || "", willOpen);
}

/* ── 分组维护（仅插件内上传的文档） ─────────────────────────────── */

/** 分组增删改的统一出口：成功后重渲染列表（分组与文档数都会变）。 */
async function ragGroupAction(action, payload, okMsg) {
  try {
    await apiPost("/rag/groups", { action, ...payload });
    showToast(okMsg, "success");
  } catch (e) {
    showToast(e.message);
  }
  loadRAG();
}

function newRAGGroup() {
  sandboxPrompt("新建分组名称（归类插件内上传的文档，最多 40 个字符）", "", async (name) => {
    const clean = (name || "").trim();
    if (!clean) return;
    await ragGroupAction("create", { name: clean }, `已新建分组「${clean}」`);
  });
}

function renameRAGGroup(btn) {
  const old = btn.dataset.value || "";
  sandboxPrompt(`把分组「${old}」改名为（组内文档会跟着走）`, old, async (name) => {
    const clean = (name || "").trim();
    if (!clean || clean === old) return;
    await ragGroupAction("rename", { name: old, new_name: clean }, `已改名为「${clean}」`);
  });
}

function deleteRAGGroup(btn) {
  const name = btn.dataset.value || "";
  sandboxConfirm(`删除分组「${name}」？组内文档不会被删除，会回到「未分组」。`, async () => {
    await withBusy(btn, "", async () => {
      await ragGroupAction("delete", { name }, `已删除分组「${name}」`);
    });
  }, true);
}

/** 某个文档的「移动到分组」下拉：空值 = 移出到未分组。 */
async function moveRAGDocument(sel) {
  const source = sel.dataset.source || "";
  const group = sel.value || "";
  sel.disabled = true;
  try {
    await apiPost("/rag/groups", { action: "assign", sources: [source], group });
    showToast(group ? `已移动到「${group}」` : "已移出分组", "success");
  } catch (e) {
    showToast(e.message);
  } finally {
    sel.disabled = false;
    loadRAG();
  }
}

async function getRAGConfig() {
  if (S._ragConfig && Date.now() - S._ragConfigAt < 60000) return S._ragConfig;
  S._ragConfig = await api("/rag/config");
  S._ragConfigAt = Date.now();
  return S._ragConfig;
}

function providerRows(cfg) {
  const emb = cfg.embedding || {};
  const rr = cfg.rerank || {};
  return `<div class="prov-row"><span class="prov-key">Embedding</span><span class="prov-val">${esc(emb.type || "local")}（${esc(emb.provider_id || "BAAI/bge-small-zh-v1.5")}）</span></div>
    <div class="prov-row"><span class="prov-key">Rerank</span><span class="prov-val">${rr.has_rerank_provider ? esc(rr.rerank_provider_id) : "未配置"}</span></div>`;
}

async function loadRAG() {
  const toggle = $("#ragKbToggle");
  if (toggle) toggle.checked = ragKbEnabled();
  const host = $("#ragGroupList");
  try {
    const data = await api("/rag/documents");
    const docs = data.documents || [];
    // 插件文档分组（子知识库）：分组只是归类，所有文档共用同一套向量与嵌入模型
    const groups = data.groups || [];
    _ragGroups = groups;
    // AstrBot 内置知识库（只读展示；管理入口在 AstrBot 面板的「知识库」页）
    const kbs = data.astrbot_kbs || [];
    if (!host) return;
    // 两个平级大组：插件文档库（可写）+ AstrBot 兼容（只读，开关关掉时不渲染）
    const sections = [pluginBigHtml(docs, groups)];
    if (ragKbEnabled()) sections.push(kbBigHtml(kbs));
    host.innerHTML = sections.join("");
    // 分组下拉走「模型选择」同款自绘 UI（原生 select 仍是唯一值存储，
    // 因而 #ragUploadTarget 的读取与 doc-group-select 的 change 委托都不变）；
    // 每次重渲染都会新建 select，enhanceSelects 内部会先回收上一批挂在 body
    // 下的孤儿浮层，再增强本批。
    enhanceSelects(host);
    // 上次停留在展开态的内置库组：渲染后补一次懒加载
    $$(".kb-group-docs[data-loaded='0']", host).forEach(el => {
      const g = el.closest(".kb-group");
      if (g && !el.hidden) loadKBGroupDocs(g);
    });
    const totalDocs = docs.length;
    const totalChunks = docs.reduce((s, d) => s + (d.chunk_count || 0), 0);
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set("#ragStatDocs", totalDocs);
    set("#ragStatChunks", totalChunks);
    set("#ragStatVectors", totalChunks); // 向量数与分块数一一对应
    set("#ragStatGroups", groups.length);
    set("#ragStatKbs", kbs.length);
    const sub = $("#ragSub");
    const bits = [];
    if (totalDocs || groups.length) bits.push(`${totalDocs} 个文档 · ${totalChunks} 个分块 · ${groups.length} 个分组`);
    if (kbs.length) bits.push(`内置知识库 ${kbs.length} 个`);
    if (sub) sub.textContent = bits.length ? `· ${bits.join(" · ")}` : "";
    updateBadge("badge-rag", totalDocs + kbs.length);
  } catch (e) {
    if (host) host.innerHTML = renderEmptyState("doc", "加载失败", e.message);
  }
}

async function loadRAGModelInfo() {
  try {
    const cfg = await getRAGConfig();
    $("#ragModelInfo").innerHTML = providerRows(cfg);
  } catch (_) { /* 模型信息不可用时不阻塞页面 */ }
}

async function processRAGFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const status = $("#ragUploadStatus");
  // 上传目标分组：选「未分组」就是独立文档（照样能绑定到角色卡）
  const group = $("#ragUploadTarget")?.value || "";
  const where = group ? ` → 「${group}」` : "";
  let ok = 0, fail = 0, chunks = 0, groupFail = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    status.textContent = `正在上传并向量化（${i + 1}/${files.length}）：${f.name}${where}`;
    try {
      const b64 = await fileToB64(f);
      const res = await api("/rag/upload_base64", { method: "POST", body: JSON.stringify({ source: f.name, b64_data: b64, group }) });
      ok++;
      chunks += (res.chunk_count || 0);
      if (res.group_error) groupFail++;
    } catch (e) {
      fail++;
      console.error("[Quill] 文档上传失败:", f.name, e);
    }
  }
  if (fail === 0 && groupFail === 0) {
    status.innerHTML = `<span style="color:var(--green-text)">✓ 已处理 ${ok} 个文件${where}，生成 ${chunks} 个分块</span>`;
  } else if (fail === 0) {
    status.innerHTML = `<span style="color:var(--orange-text)">已上传 ${ok} 个文件，但有 ${groupFail} 个未能归入分组（分组可能已被删除），它们现在是「未分组」文档</span>`;
  } else {
    status.innerHTML = `<span style="color:var(--orange-text)">完成：${ok} 成功，${fail} 失败（详见控制台）</span>`;
  }
  loadRAG();
}

function deleteRAGDocument(source, btn) {
  sandboxConfirm(`确定删除文档「${source}」及其全部分块？`, async () => {
    await withBusy(btn, "", async () => {
      try {
        await apiPost("/rag/delete", { source });
        showToast("已删除", "success");
        loadRAG();
      } catch (e) { showToast(e.message); }
    });
  }, true);
}

async function testRAGSearch(btn) {
  const q = ($("#ragQuery")?.value || "").trim();
  const topK = parseInt($("#ragTopK")?.value, 10) || 5;
  const host = $("#ragResults");
  if (!q) { host.innerHTML = '<div class="fs-13 t3">请输入查询内容</div>'; return; }
  await withBusy(btn, "检索中…", async () => {
    host.innerHTML = '<div class="skeleton skeleton-row"></div>';
    try {
      const data = await apiPost("/rag/search", { query: q, top_k: topK });
      const items = data.results || [];
      host.innerHTML = items.length
        ? items.map((it, i) => {
            // 内置库命中追加文档名，便于定位到 AstrBot 面板里的源文档
            const src = it.source && it.source.startsWith("kb:") && it.doc_name
              ? `${it.source}/${it.doc_name}` : (it.source || "");
            return `<div class="result-card">
            <div class="result-head">
              <span class="result-rank">${i + 1}</span>
              <span class="result-source">${esc(src)}</span>
              <span class="badge">块 #${it.chunk_index} · ${esc(it.content).length} 字符</span>
              <span class="result-score">相关度 ${Number(it.score || 0).toFixed(4)}${it.rerank_score !== undefined ? ` · 重排 ${Number(it.rerank_score).toFixed(4)}` : ""}</span>
            </div>
            <div class="result-body">${esc(it.content)}</div>
          </div>`;
          }).join("")
        : renderEmptyState("search", "没有匹配结果", "换个检索词，或调整返回结果数量");
    } catch (e) {
      host.innerHTML = `<div class="fs-13" style="color:var(--red-text)">${esc(e.message)}</div>`;
    }
  });
}

/* ══ J. 动态记忆 ═════════════════════════════════════════════════════ */
