/* pages/config.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, showToast } from "../components/feedback.js";
import { switchMemorySub } from "../pages/memory.js";
import { loadPersonas } from "../pages/persona.js";
import { loadRAG, loadRAGModelInfo } from "../pages/rag.js";
import { loadWB } from "../pages/worldbook.js";
import { loadWR } from "../pages/writing.js";
import { $, $$, compactNum, esc, withBusy } from "../utils.js";

export { CFG_DEFAULTS, CFG_FIELDS, applySettings, cfgGet, clearDirty, discardChanges, fillProviderSelect, filterConfig, initConfigNavObserver, loadGlobalSettings, loadHealth, markCfgNav, markDirty, saveAllChanges, scrollToSection, setStreamModeAll, switchTab, syncSidebarBadges, updateBadge, updateStreamStats };





/** 配置项 → DOM 的映射表。集中定义确保「读取」与「保存」永不脱节。 */
const CFG_FIELDS = [
  // [group, key, selector, kind]
  ["rag", "embedding_provider_id", "#c-rag-emb", "str"],
  ["rag", "rerank_provider_id", "#c-rag-rerank", "str"],
  ["rag", "llm_provider_id", "#c-rag-llm", "str"],
  ["rag", "enable_local_embedding", "#c-rag-local", "bool"],
  ["rag", "chunk_size", "#c-rag-chunk", "int"],
  ["rag", "chunk_overlap", "#c-rag-overlap", "int"],
  ["rag", "top_k", "#c-rag-topk", "int"],
  ["rag", "dense_top_k", "#c-rag-dense-topk", "int"],
  ["rag", "enable_memory", "#c-rag-memory", "bool"],
  ["rag", "enable_autonomous_reflection", "#c-rag-autonomous-reflection", "bool"],
  ["rag", "enable_chat_logging", "#c-rag-log", "bool"],
  ["rag", "chat_log_retention_days", "#c-rag-log-days", "int"],
  ["worldbook", "enabled", "#c-wb-enabled", "bool"],
  ["worldbook", "max_token_limit", "#c-wb-token", "int"],
  ["worldbook", "max_dynamic_entries", "#c-wb-max", "int"],
  ["worldbook", "injection_position", "#c-wb-pos", "str"],
  ["worldbook", "match_sensitivity", "#c-wb-sens", "float"],
  ["worldbook", "always_activate", "#c-wb-always", "bool"],
  ["worldbook", "show_trigger_log", "#c-wb-show-log", "bool"],
  ["writing_resource", "enabled", "#c-wr-enabled", "bool"],
  ["writing_resource", "max_entries", "#c-wr-max", "int"],
  ["writing_resource", "fallback_top_count", "#c-wr-fallback", "int"],
  ["writing_resource", "category_dedup_limit", "#c-wr-dedup", "int"],
  ["performance", "min_output_length", "#c-perf-min", "int"],
  ["performance", "max_output_length", "#c-perf-max", "int"],
  ["performance", "max_prompt_length", "#c-perf-max-prompt", "int"],
  ["status_bar", "enabled", "#c-sb-enabled", "bool"],
  ["status_bar", "default_placeholder", "#c-sb-placeholder", "placeholder"],
  ["status_bar", "show_delta", "#c-sb-delta", "bool"],
  ["status_bar", "fields", "#c-sb-fields", "str"],
  ["status_bar", "format_template", "#c-sb-tpl", "str"],
  // 这两项用 "str" 而非 "placeholder"：空值含义是「用内置默认」，
  // 不能被 placeholder 类型的「空 → 未设置」替换掉。
  ["status_bar", "format_template_plain", "#c-sb-tpl-plain", "str"],
  ["status_bar", "plain_platforms", "#c-sb-plain-plat", "str"],
  ["status_bar", "plot_paths", "#c-sb-plots", "str"],
  ["status_bar", "llm_extract", "#c-sb-llm-extract", "bool"],
  ["status_bar", "llm_provider_id", "#c-sb-llm", "str"],
  ["refusal", "enabled", "#c-ref-enabled", "bool"],
  ["refusal", "patterns", "#c-ref-pat", "str"],
  ["permissions", "admin_users", "#c-perm-admins", "str"],
  ["debug", "enabled", "#c-debug-enabled", "bool"],
  ["debug", "show_inject_report", "#c-debug-report", "bool"],
];

/** 各字段在渲染时使用的默认值（后端缺键时的兜底）。 */
const CFG_DEFAULTS = {
  "rag.chunk_size": 500, "rag.chunk_overlap": 50, "rag.top_k": 3,
  "rag.dense_top_k": 5, "rag.enable_local_embedding": true,
  "rag.enable_autonomous_reflection": true, "rag.enable_chat_logging": true,
  "rag.chat_log_retention_days": 30,
  "worldbook.enabled": true, "worldbook.max_token_limit": 4000, "worldbook.max_dynamic_entries": 4,
  "worldbook.injection_position": "user_prefix", "worldbook.match_sensitivity": 0.7,
  "writing_resource.enabled": true, "writing_resource.max_entries": 4,
  "writing_resource.fallback_top_count": 2, "writing_resource.category_dedup_limit": 3,
  "performance.min_output_length": 400, "performance.max_output_length": 0,
  "performance.max_prompt_length": 50000,
  "status_bar.fields": "好感度|关系阶段|心情|位置|穿着|当前想法",
  "status_bar.format_template": "**状态栏**\n```\n{content}\n```",
  "status_bar.format_template_plain": "───── 状态栏 ─────\n{content}\n────────────────",
  "status_bar.plain_platforms": "",
  "status_bar.plot_paths": "继续当前话题|转换场景|结束互动",
  "status_bar.default_placeholder": "未设置",
  "status_bar.show_delta": true,
  "debug.show_inject_report": false,
  "refusal.enabled": true,
  "refusal.patterns": "我不能\n我无法\n这违反\n我不应该\n这不合适\n我拒绝",
};

function cfgGet(group, key, fallback) {
  const g = S._rawConfig[group];
  return (g && g[key] !== undefined) ? g[key] : fallback;
}

function fillProviderSelect(sel, items, emptyLabel) {
  sel.innerHTML = `<option value="">${esc(emptyLabel)}</option>` +
    (items || []).map(x => `<option value="${esc(x.id)}">${esc(x.id)}${x.model ? " — " + esc(x.model) : ""}</option>`).join("");
}

/** 把服务端配置写进表单。 */
function applySettings(providers, cfg) {
  S._applyingSettings = true;
  S._rawConfig = cfg || {};
  fillProviderSelect($("#c-rag-emb"), providers.embedding, "（默认本地模型 bge-small）");
  fillProviderSelect($("#c-rag-rerank"), providers.rerank, "（不使用重排）");
  fillProviderSelect($("#c-rag-llm"), providers.llm, "（退化为文本截断）");
  fillProviderSelect($("#c-sb-llm"), providers.llm, "（回退到 RAG 摘要 LLM）");

  CFG_FIELDS.forEach(([group, key, sel, kind]) => {
    const el = $(sel);
    if (!el) return;
    const def = CFG_DEFAULTS[`${group}.${key}`];
    const v = cfgGet(group, key, def);
    if (kind === "bool") el.checked = !!v;
    else if (kind === "placeholder") el.value = (v === undefined || v === null || v === "") ? "未设置" : v;
    else el.value = v === undefined || v === null ? "" : v;
  });

  // 滑块旁的数值展示与后端值同步
  const sens = cfgGet("worldbook", "match_sensitivity", 0.7);
  const sensVal = $("#c-wb-sens-val");
  if (sensVal) sensVal.textContent = sens;
  // 程序化写表单也会派发 change；用微任务窗口忽略这次同步，
  // 否则初次加载配置就会被误判成“用户正在编辑”。
  queueMicrotask(() => { S._applyingSettings = false; });
}

async function loadGlobalSettings() {
  try {
    const [providers, cfg, stats] = await Promise.all([
      api("/provider/list"),
      api("/config/all"),
      api("/stream/stats").catch(() => null),
    ]);
    applySettings(providers, cfg);
    if (stats) updateStreamStats(stats);
  } catch (e) {
    showToast("读取配置失败：" + e.message);
  }
}

function updateStreamStats(stats) {
  const hint = $("#streamStats");
  if (!hint) return;
  const total = stats.total || 0;
  hint.textContent = total === 0
    ? "暂无活跃会话"
    : `共 ${total} 个会话 · 自动 ${stats.auto || 0} · 流式 ${stats.on || 0} · 关闭 ${stats.off || 0}`;
}

async function setStreamModeAll(mode, btn) {
  await withBusy(btn, "", async () => {
    try {
      const res = await apiPost("/stream/all", { mode });
      showToast((res && res.message) || "流式模式已批量设置", "success");
      const stats = await api("/stream/stats").catch(() => null);
      if (stats) updateStreamStats(stats);
    } catch (e) { showToast("设置失败：" + e.message); }
  });
}

/** 标记有未保存更改（显示底部操作条）。 */
function markDirty() {
  if (S._applyingSettings) return;
  if (S._dirty) return;
  S._dirty = true;
  $("#actionBar")?.classList.add("is-visible");
}

function clearDirty() {
  S._dirty = false;
  $("#actionBar")?.classList.remove("is-visible");
}

/** 放弃修改：重新从服务端拉取配置覆盖本地编辑。 */
async function discardChanges() {
  await loadGlobalSettings();
  clearDirty();
  showToast("已放弃未保存的修改", "info");
}

/** 差异保存：只提交与服务器不同的项，避免无谓写盘与重载。 */
async function saveAllChanges(btn) {
  const bar = $("#actionBar");
  await withBusy(btn, "", async () => {
    try {
      const updates = [];
      const add = (group, key, value) => {
        const old = (S._rawConfig[group] && S._rawConfig[group][key] !== undefined) ? S._rawConfig[group][key] : null;
        if (JSON.stringify(old) !== JSON.stringify(value)) updates.push({ group, key, value });
      };
      CFG_FIELDS.forEach(([group, key, sel, kind]) => {
        const el = $(sel);
        if (!el) return;
        if (kind === "bool") add(group, key, el.checked);
        else if (kind === "int") add(group, key, parseInt(el.value, 10) || CFG_DEFAULTS[`${group}.${key}`] || 0);
        else if (kind === "float") add(group, key, parseFloat(el.value) || CFG_DEFAULTS[`${group}.${key}`] || 0);
        else if (kind === "placeholder") add(group, key, (el.value || "").trim() || "未设置");
        else add(group, key, el.value);
      });

      if (!updates.length) {
        showToast("没有需要保存的修改", "success");
        clearDirty();
        return;
      }

      // 保存与回读必须分开 catch：写已经落盘后再回读失败，若与保存共用一个
      // try，会把回读的异常报成「保存失败」，用户以为没存上而重复提交。
      await apiPost("/config/save_batch", { updates });
      try {
        await loadGlobalSettings();
      } catch (e) {
        console.warn("[Quill] 保存已成功，但回读配置失败：", e);
      }
      showToast(`已保存 ${updates.length} 项配置（部分设置需重启对话生效）`, "success");
      clearDirty();
    } catch (e) {
      showToast("保存失败：" + e.message);
    }
  });
}

async function loadHealth() {
  const set = (sel, v) => { const el = $(sel); if (el) el.textContent = v; };
  const tone = (el, rate) => {
    if (!el) return;
    el.classList.remove("is-good", "is-mid", "is-bad");
    if (rate === null || rate === undefined) return;
    el.classList.add(rate >= 80 ? "is-good" : rate >= 50 ? "is-mid" : "is-bad");
  };
  try {
    const info = await api("/info");
    const ver = $("#appVersion");
    if (ver && info && info.version) ver.textContent = String(info.version).replace(/^Quill\s*v?/i, "v");

    // 写作素材库索引状态：FTS 失效时后端会静默退化成全表扫描（结果变慢、排序
    // 变差），此前没有任何可见面。这里把降级状态显式标红。
    const ix = info && info.wr_index;
    if (ix && ix.fts_ok === false) {
      set("#health-wr-index", "降级");
      set("#health-wr-index-detail", "全表扫描");
      tone($("#health-wr-index"), 0);
    } else if (ix && ix.fts_ok === true) {
      set("#health-wr-index", "正常");
      set("#health-wr-index-detail", `${ix.entries || 0} 条`);
      tone($("#health-wr-index"), 100);
    } else {
      set("#health-wr-index", "—");
      set("#health-wr-index-detail", "—");
      tone($("#health-wr-index"), null);
    }

    const h = info && info.health;
    if (!h) {
      set("#health-rag-rate", "N/A");
      set("#health-status-rate", "N/A");
      return;
    }
    const ragRate = h.rag && h.rag.rate;
    set("#health-rag-rate", ragRate !== null && ragRate !== undefined ? ragRate + "%" : "—");
    set("#health-rag-detail", `${(h.rag && h.rag.success) || 0} / ${(h.rag && h.rag.total) || 0}`);
    tone($("#health-rag-rate"), ragRate);

    const stRate = h.status_bar && h.status_bar.rate;
    set("#health-status-rate", stRate !== null && stRate !== undefined ? stRate + "%" : "—");
    set("#health-status-detail", `${(h.status_bar && h.status_bar.success) || 0} / ${(h.status_bar && h.status_bar.total) || 0}`);
    tone($("#health-status-rate"), stRate);
  } catch (e) {
    set("#health-rag-rate", "N/A");
    set("#health-status-rate", "N/A");
    set("#health-wr-index", "N/A");
    set("#health-wr-index-detail", "—");
  }
}

/** 配置页搜索：按文本匹配显示/隐藏字段行，并联动卡片与分区。 */
function filterConfig(query) {
  const q = (query || "").toLowerCase().trim();
  const rows = $$("#pane-config .cfg-row, #pane-config .cfg-field, #pane-config .health-row");
  const cards = $$("#pane-config .card");
  const sections = $$("#pane-config .cfg-section");

  if (!q) {
    rows.forEach(r => { r.style.display = ""; });
    cards.forEach(c => { c.style.display = ""; });
    sections.forEach(s => { s.style.display = ""; });
    return;
  }
  rows.forEach(r => { r.style.display = (r.textContent || "").toLowerCase().includes(q) ? "" : "none"; });
  cards.forEach(c => {
    const inner = $$(".cfg-row, .cfg-field, .health-row", c);
    const anyVisible = inner.some(r => r.style.display !== "none");
    const title = c.querySelector("h3");
    const titleHit = title && title.textContent.toLowerCase().includes(q);
    c.style.display = (anyVisible || titleHit) ? "" : "none";
  });
  sections.forEach(s => {
    s.style.display = $$(".card", s).some(c => c.style.display !== "none") ? "" : "none";
  });
}

/* ══ L. 事件委托与初始化 ═════════════════════════════════════════════ */
/* 全部交互收敛到单一委托入口（data-action + 映射表），彻底消除模板字符串
   里拼接 onclick 带来的转义与注入隐患。 */



function switchTab(tab) {
  // 仅当「离开」配置页且存在未保存修改时才拦截。
  // 面板 iframe 未授予 allow-modals，原生 confirm 恒返回 false 会把用户
  // 永久锁在配置页，因此这里用自绘对话框；用户确认后清除脏标记再跳转。
  if (tab !== "config" && S._dirty && S._lastCfgTab === "config") {
    sandboxConfirm("配置页有未保存的修改，确定要离开吗？", async () => {
      // 丢弃修改时必须回滚表单值，否则再次进入配置页会看到残留的编辑
      await discardChanges();
      switchTab(tab);
    });
    return;
  }
  $$("[data-tab]").forEach(b => {
    const on = b.dataset.tab === tab;
    b.classList.toggle("is-selected", on);
    // 侧栏是应用导航，不是标签页：当前页用 aria-current="page" 表达。
    if (b.classList.contains("sl-item")) {
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    }
  });
  $$(".tab-pane").forEach(p => p.classList.toggle("is-active", p.id === "pane-" + tab));
  S._lastCfgTab = tab;
  $("#content")?.scrollTo({ top: 0 });

  if (tab === "wr") loadWR();
  else if (tab === "wb") loadWB();
  else if (tab === "persona") loadPersonas();
  else if (tab === "rag") { loadRAG(); loadRAGModelInfo(); }
  else if (tab === "memory") switchMemorySub("overview");
  else if (tab === "config") { loadGlobalSettings(); loadHealth(); }
}

function updateBadge(id, count) {
  const el = document.getElementById(id);
  if (!el) return;
  if (count > 0) { el.textContent = compactNum(count); el.hidden = false; }
  else el.hidden = true;
}

/** 首屏填充侧栏徽章：六个页面中尚未访问过的页面，其计数也应立即显示。
    各请求独立降级，任一后端接口不可用不影响其余徽章。 */
async function syncSidebarBadges() {
  const jobs = [];

  // /info 一次覆盖素材库 / 世界书 / 角色卡
  jobs.push(api("/info").then(info => {
    if (!info) return;
    if (typeof info.wr_count === "number") updateBadge("badge-wr", info.wr_count);
    if (typeof info.wb_count === "number") updateBadge("badge-wb", info.wb_count);
    if (typeof info.persona_count === "number") updateBadge("badge-persona", info.persona_count);
  }).catch(() => {}));

  jobs.push(api("/memory/stats").then(res => {
    const d = (res && res.data) || res;
    if (d && typeof d.total_memories === "number") updateBadge("badge-memory", d.total_memories);
  }).catch(() => {}));

  jobs.push(api("/rag/documents").then(res => {
    const docs = (res && res.documents) || [];
    updateBadge("badge-rag", docs.length);
  }).catch(() => {}));

  await Promise.all(jobs);
}

/** 配置分区导航：滚动到对应分区并高亮。 */
function scrollToSection(id) {
  const target = document.getElementById(id);
  if (!target) return;
  // 吸顶带是整条 .page-head-bar（页头 + 分区导航），不是其中的导航行：
  // 拿导航行的高度当偏移会让分区标题停在带子内部被挡住。
  const bar = $("#pane-config .page-head-bar");
  const offset = (bar ? bar.getBoundingClientRect().height : 0) + 12;
  const top = target.getBoundingClientRect().top + ($("#content")?.scrollTop || 0) - offset;
  $("#content")?.scrollTo({ top, behavior: "smooth" });
  markCfgNav(id);
}

/** 标出当前所处的配置分区。
    aria-current 而非 aria-selected：这是页内导航，不是标签页选择。 */
function markCfgNav(id) {
  $$("#cfgNav > button").forEach(b => {
    const on = b.dataset.target === id;
    b.classList.toggle("is-selected", on);
    if (on) b.setAttribute("aria-current", "true");
    else b.removeAttribute("aria-current");
  });
}

function initConfigNavObserver() {
  const sections = $$("#pane-config .cfg-section");
  const scroller = $("#content");
  if (!sections.length || !scroller) return;

  // 这里不用 IntersectionObserver。五个分区在 DOM 里不是平级兄弟——后四个
  // 嵌在 #sec-inject 里，前者的边界把它们整段包住，于是「谁正在视野里」在
  // 交集模型下无法区分；而且 observer 只给来去事件，向上滚时只有离开事件，
  // 高亮会停在最后一次进入的分区上不动。改成直接算：取「分区顶边已经越过
  // 吸顶带下沿」中最靠下的那一个，上下滚都对称，也不依赖 DOM 是否嵌套。
  let raf = 0;
  const measure = () => {
    raf = 0;
    const bar = $("#pane-config .page-head-bar");
    // 带高随视口变化（窄屏导航折行后会从 ~105 涨到 ~196），每次测量都重取。
    const barBottom = bar ? bar.getBoundingClientRect().bottom : 0;
    let current = sections[0].id;
    for (const s of sections) {
      // 留 1px 容差，避免恰好贴线时的抖动。
      if (s.getBoundingClientRect().top - barBottom <= 1) current = s.id;
      else break;
    }
    // 滚到最底部时最后一段可能永远够不到判定线，此时直接选它，否则底部
    // 会一直停在上一个分区。
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) {
      current = sections[sections.length - 1].id;
    }
    markCfgNav(current);
  };
  const onScroll = () => { if (!raf) raf = requestAnimationFrame(measure); };

  scroller.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  // 切到配置页时容器尺寸才确定（此前 display:none 下几何全是 0），
  // 因此初始化后补一帧测量。
  onScroll();
}
