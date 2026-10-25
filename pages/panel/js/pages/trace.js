/* pages/trace.js — M1a/M1b 注入诊断页：会话下拉 + 最近轮次列表 + 模块行展开
   + 三阶漏斗 + 裁剪事件 + 脱敏预览 + 模拟检索 + 静态体检 + SSE 实时视图。
   数据与 /quill debug 同源（后端 trace/recent 端点，同一 TraceStore）。
   展开渲染直接复用列表响应里的完整结构，不再按 id 二次请求——
   trace/detail 端点留给按 id 的精确查询（E2E / 外部工具 / 模拟锚点）。 */
import { api, apiPost, getBridge } from "../api.js";
import { $, esc, fmtTime, withBusy } from "../utils.js";

export {
  loadTracePage, onTraceSessionChange, refreshTrace, toggleTraceTurn,
  runSimulate, runLint, toggleTraceLive, copyPreviewText,
};

const TRACE_LIMIT = 20;

/* 模块名 → 展示名（与后端 TraceModuleResult.module 的取值一一对应；
   新增模块时两边同步改） */
const MODULE_LABEL = {
  worldbook: "世界书",
  wr: "素材库",
  rag_doc: "文档",
  memory: "记忆",
  core_memory: "核心记忆",
  persona: "角色卡",
  context_restore: "上下文恢复",
  statusbar: "状态栏",
  decision: "剧情决策",
  /* inject_stats 的短键（_remember_inject_report 合并后的标准键） */
  wb: "世界书",
  mem: "记忆",
  core_mem: "核心记忆",
  doc: "文档",
};

/* SSE 实时视图状态（页面级内存；离开页面不自动断开——面板是 SPA，
   切回本页仍能看到推入的新轮次） */
let _sseSubId = null;
let _pollTimer = null;

/* ── 加载 ─────────────────────────────────────────────────────────── */

async function loadTracePage() {
  const sel = $("#traceSession");
  if (!sel) return;
  // M1a-Fix 1.4：重建 option 列表前记住当前选中会话，刷新/重进不再被
  // 重置回第一项（该会话仍在列表中时恢复选中）。
  const prev = sel.value;
  _loadSimPersonas();
  try {
    const data = await api("/trace/recent");
    const sessions = data.sessions || [];
    const sub = $("#traceSub");
    if (sub) sub.textContent = data.enabled
      ? `${sessions.length} 个会话有记录`
      : "追踪已关闭";
    if (!sessions.length) {
      sel.innerHTML = '<option value="">暂无会话</option>';
      _renderEmpty(data.enabled);
      return;
    }
    sel.innerHTML = sessions.map(s =>
      `<option value="${esc(s.target_id_hash)}">${esc(_sessionLabel(s))}</option>`).join("");
    if (prev && sessions.some(s => s.target_id_hash === prev)) sel.value = prev;
    await _loadTurns();
  } catch (e) {
    $("#traceList").innerHTML =
      `<div class="note">加载失败：${esc(e.message || String(e))}</div>`;
  }
}

/* 模拟检索的角色卡下拉（决定 wb/wr/rag 绑定模式；一次拉全量列表，
   失败静默——下拉空着只是少一层绑定模拟，不阻断模拟本体） */
async function _loadSimPersonas() {
  const sel = $("#simPersona");
  if (!sel || sel.options.length > 1) return;
  try {
    const personas = await api("/persona/list");
    sel.innerHTML = '<option value="">不选角色卡（各系统按 disabled）</option>' +
      (personas || []).map(p =>
        `<option value="${esc(p.id || "")}">${esc(p.name || p.id || "?")}</option>`).join("");
  } catch (_) { /* 静默降级 */ }
}

function _sessionLabel(s) {
  const who = s.persona_name || s.persona_id || "未知角色";
  return `${String(s.target_id_hash || "").slice(0, 8)}… / ${who}（${s.count} 轮）`;
}

function _renderEmpty(enabled) {
  $("#traceList").innerHTML = enabled
    ? '<div class="t3" style="text-align:center;padding:var(--sp-5)">暂无追踪记录——发起一轮对话后点「刷新」。</div>'
    : '<div class="note">注入追踪已关闭（配置 → 系统与安全 → 调试与维护 → 启用注入追踪）。当前零采集零记录，开启后下一轮对话开始记录。</div>';
}

async function _loadTurns() {
  const target = $("#traceSession")?.value || "";
  const box = $("#traceList");
  if (!box) return;
  if (!target) { _renderEmpty(true); return; }
  box.innerHTML = '<div class="skeleton" style="width:300px"></div>';
  try {
    const data = await api(`/trace/recent?target=${encodeURIComponent(target)}&limit=${TRACE_LIMIT}`);
    _renderTurns(data.items || [], !!data.enabled);
  } catch (e) {
    box.innerHTML = `<div class="note">加载失败：${esc(e.message || String(e))}</div>`;
  }
}

async function onTraceSessionChange() {
  await _loadTurns();
}

async function refreshTrace(el) {
  await withBusy(el, "", loadTracePage);
}

/* ── 渲染 ─────────────────────────────────────────────────────────── */

function _renderTurns(turns, enabled) {
  const box = $("#traceList");
  if (!turns.length) { _renderEmpty(enabled); return; }
  box.innerHTML = turns.map(_turnCard).join("");
}

function _turnCard(t, opts = {}) {
  const sim = opts.simulate || t.simulate;
  const act = t.activated
    ? `<span class="badge badge--green">激活${t.activation_reason ? " · " + esc(t.activation_reason) : ""}</span>`
    : '<span class="badge">未激活</span>';
  const simBadge = sim
    ? '<span class="badge badge--accent" title="模拟检索沙盒产物——零写入，不在最近轮次列表里">模拟</span>'
    : "";
  const sb = t.statusbar_level
    ? `<span class="badge badge--accent" title="状态栏六级链本轮命中级别">${esc(t.statusbar_level)}</span>`
    : "";
  const warn = (t.warnings || []).length
    ? `<span class="badge badge--yellow" title="${esc((t.warnings || []).join("；"))}">${t.warnings.length} 警告</span>`
    : "";
  const hitParts = (t.module_results || [])
    .filter(m => (m.hits || 0) > 0)
    .map(m => `${MODULE_LABEL[m.module] || m.module}×${m.hits}`);
  return `
  <div class="trace-turn" data-trace-id="${esc(t.trace_id)}">
    <button type="button" class="trace-turn-head" data-action="trace-toggle" data-id="${esc(t.trace_id)}" aria-expanded="false">
      <svg class="icon icon--sm trace-caret" aria-hidden="true"><use href="#i-chevron-right"/></svg>
      <span class="mono fs-12 t3">#${t.seq ?? "—"}</span>
      ${act}${simBadge}
      <span class="fs-12">${esc(t.persona_name_at_turn || t.persona_id || "")}</span>
      <span class="fs-12 t3">${hitParts.length ? esc(hitParts.join(" · ")) : "无命中"}</span>
      <span class="grow"></span>
      ${sb}${warn}
      <span class="fs-12 t3" title="trace_id（与 /quill debug 的 Trace 行同源）">${esc(String(t.trace_id || "").slice(0, 8))}…</span>
      <span class="fs-12 t3">${esc(fmtTime(t.started_at))}</span>
      <span class="fs-12 t3">${t.duration_ms ? t.duration_ms + "ms" : "—"}</span>
    </button>
    <div class="trace-turn-body" hidden>
      ${_moduleTable(t)}
      ${_orderAndTokens(t)}
      ${_truncEvents(t)}
      ${_preview(t, opts)}
    </div>
  </div>`;
}

/* 漏斗列：candidates→hits（candidates=0 或缺省时只显示 hits——那是
   未透传漏斗的老轮次/降级行，不给假零） */
function _funnelCell(m) {
  const c = m.candidates ?? 0;
  const h = m.hits ?? 0;
  if (c > 0) {
    return `<span class="mono fs-12" title="候选 ${c} 条 → 最终注入 ${h} 条；差值见近失明细">${c}→${h}</span>`;
  }
  return `<span class="fs-12">${h}</span>`;
}

function _moduleTable(t) {
  const mods = t.module_results || [];
  if (!mods.length) {
    return '<div class="t3 fs-12" style="margin-bottom:var(--sp-2)">（无模块记录——未激活轮只经过上下文恢复）</div>';
  }
  const rows = mods.map(m => {
    const state = m.error
      ? `<span class="badge badge--red" title="错误类别">${esc(m.error)}</span>`
      : m.degraded
        ? `<span class="badge badge--yellow" title="降级状态">${esc(m.degraded)}</span>`
        : '<span class="badge badge--green">ok</span>';
    /* 命中明细 + 近失明细（M1b：filtered_reason 的条目是「被规则挤掉」
       的候选——设定漏注入排查的主线索） */
    const parts = (m.items || []).map(i => {
      const reason = i.filtered_reason || "";
      const score = i.score_or_kw ? `<span class="t3">（${esc(i.score_or_kw)}）</span>` : "";
      if (reason) {
        return `<span class="badge badge--yellow" title="被过滤的候选">${esc(i.source_ref || "?")} · ${esc(reason)}</span>`;
      }
      return `<span class="fs-12">${esc(i.source_ref || "")}${score}</span>`;
    });
    return `<tr>
      <td>${esc(MODULE_LABEL[m.module] || m.module)}</td>
      <td>${state}</td>
      <td>${_funnelCell(m)}</td>
      <td class="fs-12">${m.stage_ms ? m.stage_ms + "ms" : "—"}</td>
      <td class="fs-12">${parts.length ? parts.map(p => `<div>${p}</div>`).join("") : '<span class="t3">—</span>'}</td>
    </tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="data">
    <thead><tr><th>模块</th><th style="width:96px">状态</th><th style="width:76px">漏斗</th><th style="width:64px">耗时</th><th>命中 / 近失明细</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function _orderAndTokens(t) {
  const parts = [];
  const order = t.injection_order || [];
  if (order.length) {
    parts.push(`<div class="fs-12"><span class="t3">注入段序：</span>${order.map(esc).join(" → ")}</div>`);
  }
  const tk = t.token_estimate || {};
  if (tk.est_tokens !== undefined) {
    parts.push(`<div class="fs-12"><span class="t3">估算 Token：</span>约 ${tk.est_tokens}（稳定段 ${tk.stable_chars ?? 0} + 动态段 ${tk.dynamic_chars ?? 0} 字符，中文按 2 字符/token 粗估，仅量级参考）</div>`);
  }
  if (tk.simulated_chars !== undefined) {
    const over = tk.over_budget
      ? '<span class="badge badge--yellow">超预算</span>'
      : '<span class="badge badge--green">预算内</span>';
    parts.push(`<div class="fs-12"><span class="t3">预算画像：</span>模拟内容约 ${tk.simulated_chars} 字符 / 上限 ${tk.max_prompt_length} ${over}</div>`);
  }
  const stats = t.inject_stats || {};
  const statKeys = ["wb", "mem", "core_mem", "wr", "doc"];
  const statParts = statKeys.filter(k => stats[k] !== undefined)
    .map(k => `${MODULE_LABEL[k] || k}×${stats[k]}`);
  if (statParts.length) {
    parts.push(`<div class="fs-12"><span class="t3">注入构成：</span>${esc(statParts.join(" · "))}（与回复末尾报告行、/quill debug 同源）</div>`);
  }
  const warns = t.warnings || [];
  if (warns.length) {
    parts.push(`<div class="fs-12"><span class="t3">警告：</span>${warns.map(esc).join("；")}</div>`);
  }
  return parts.join("");
}

/* M1b 模块 3：裁剪事件（丢段/硬切/守卫撤回/预算画像） */
function _truncEvents(t) {
  const evs = t.truncation_events || [];
  if (!evs.length) return "";
  const KIND_LABEL = {
    section_dropped: "丢段", char_slice: "尾部硬切",
    trigger_log_withdrawn: "守卫撤回触发日志", rag_tail_trimmed: "守卫裁剪RAG尾部",
    budget_projection: "预算预警",
  };
  const rows = evs.map(e => {
    const kind = KIND_LABEL[e.kind] || e.kind;
    const sec = e.section ? `（段：${esc(e.section)}）` : "";
    const where = e.where ? `<span class="t3">[${esc(e.where)}]</span> ` : "";
    const extra = e.core_memory_protected ? "，核心记忆未受影响" : "";
    return `<div class="fs-12">${where}${esc(kind)}${sec} −${e.dropped_chars ?? "?"} 字符${extra}</div>`;
  }).join("");
  return `<div class="trace-trunc">
    <div class="fs-12" style="margin-bottom:var(--sp-1)"><span class="t3">裁剪事件（${evs.length}）：</span></div>
    ${rows}
  </div>`;
}

/* M1b 模块 7：脱敏注入预览（仅每会话最新一条保留；RAG 段只给长度） */
function _preview(t, opts = {}) {
  const sections = t.prompt_preview || [];
  if (!sections.length) return "";
  const copyTarget = opts.simulate ? "sim" : t.trace_id;
  const rows = sections.map(s => {
    const note = s.note ? `<div class="t3 fs-12">${esc(s.note)}</div>` : "";
    const text = s.text
      ? `<pre class="trace-preview-text">${esc(s.text)}</pre>`
      : '<div class="t3 fs-12">（内容略）</div>';
    return `<div class="trace-preview-sec">
      <div class="fs-12" style="margin-bottom:var(--sp-1)"><span class="mono">${esc(s.name)}</span> <span class="t3">${s.chars ?? 0} 字符</span></div>
      ${note}${text}
    </div>`;
  }).join("");
  return `<div class="trace-preview">
    <div class="row-wrap" style="margin-bottom:var(--sp-2)">
      <span class="fs-12"><span class="t3">脱敏预览（分段；RAG 检索段不展示原文）：</span></span>
      <span class="grow"></span>
      <button type="button" class="btn btn--sm btn--gray" data-action="trace-copy-preview" data-target="${esc(copyTarget)}">复制全部段落文本</button>
    </div>
    ${rows}
  </div>`;
}

/* ── 模拟检索（M1b 模块 4）────────────────────────────────────────── */

async function runSimulate(el) {
  const text = ($("#simText")?.value || "").trim();
  const box = $("#simResult");
  if (!box) return;
  if (!text) {
    box.hidden = false;
    box.innerHTML = '<div class="note">请输入要模拟的文本。</div>';
    return;
  }
  let svars;
  const rawVars = ($("#simVars")?.value || "").trim();
  if (rawVars) {
    try { svars = JSON.parse(rawVars); }
    catch (e) {
      box.hidden = false;
      box.innerHTML = `<div class="note">会话变量不是合法 JSON：${esc(e.message)}</div>`;
      return;
    }
  }
  await withBusy(el, "运行中…", async () => {
    try {
      const data = await apiPost("/trace/simulate", {
        text,
        persona_id: $("#simPersona")?.value || "",
        session_vars: svars || null,
      });
      box.hidden = false;
      box.innerHTML = _turnCard((data && data.data) || data, { simulate: true });
    } catch (e) {
      box.hidden = false;
      box.innerHTML = `<div class="note">模拟失败：${esc(e.message || String(e))}</div>`;
    }
  });
}

/* ── 静态体检（M1b 模块 5）────────────────────────────────────────── */

const LINT_CHECK_LABEL = {
  duplicate_keyword: "重复关键词", broad_keyword: "宽泛关键词",
  empty_keys: "死条目", unreachable_entry: "灵敏度不可达",
  condition_syntax: "条件语法错误", ghost_binding: "幽灵绑定",
  budget_ratio: "常驻预算水位",
};

async function runLint(el) {
  const box = $("#lintResult");
  if (!box) return;
  await withBusy(el, "体检中…", async () => {
    try {
      const data = await api("/worldbook/lint");
      const findings = (data && data.findings) || [];
      const summary = (data && data.summary) || {};
      if (!findings.length) {
        box.innerHTML = '<div class="badge badge--green">通过</div> <span class="fs-12 t3">未发现问题（重复/宽泛/不可达关键词、坏条件、幽灵绑定、预算水位均正常）。</span>';
        return;
      }
      const chips = Object.entries(summary)
        .map(([k, n]) => `<span class="badge badge--yellow">${esc(LINT_CHECK_LABEL[k] || k)}×${n}</span>`).join(" ");
      const rows = findings.map(f => `
        <div class="trace-lint-row">
          <span class="badge ${f.level === "warn" ? "badge--yellow" : ""}">${esc(LINT_CHECK_LABEL[f.check] || f.check)}</span>
          <span class="fs-12">${f.worldbook ? `<span class="mono t3">${esc(f.worldbook)}</span> / ` : ""}<b>${esc(f.title)}</b></span>
          <span class="fs-12 t3">${esc(f.detail)}</span>
        </div>`).join("");
      box.innerHTML = `<div style="margin-bottom:var(--sp-2)">${chips}</div>${rows}`;
    } catch (e) {
      box.innerHTML = `<div class="note">体检失败：${esc(e.message || String(e))}</div>`;
    }
  });
}

/* ── SSE 实时视图（M1b 模块 7）──────────────────────────────────────
   优先宿主桥 subscribeSSE（面板沙箱内唯一带鉴权的 EventSource 通道）；
   桥不可用（独立打开/直连模式）时降级为 15 秒轮询——面板最常见姿势是
   「开着看一眼」，轮询延迟可接受。 */

async function toggleTraceLive(el) {
  if (_sseSubId || _pollTimer) {
    await _stopLive();
    el.innerHTML = '<svg class="icon icon--sm" aria-hidden="true"><use href="#i-sparkle"/></svg>实时：关';
    return;
  }
  el.innerHTML = '<svg class="icon icon--sm" aria-hidden="true"><use href="#i-sparkle"/></svg>实时：连接中…';
  const bridge = getBridge();
  if (bridge && typeof bridge.subscribeSSE === "function") {
    try {
      _sseSubId = await bridge.subscribeSSE("trace/events", {
        onMessage: (msg) => _onLiveTrace(msg?.parsed),
        onError: () => { _sseSubId = null; _startPolling(); },
      });
      el.innerHTML = '<svg class="icon icon--sm" aria-hidden="true"><use href="#i-sparkle"/></svg>实时：开';
      return;
    } catch (_) { _sseSubId = null; /* 落到轮询 */ }
  }
  _startPolling();
  el.innerHTML = '<svg class="icon icon--sm" aria-hidden="true"><use href="#i-sparkle"/></svg>实时：轮询';
}

function _startPolling() {
  if (_pollTimer) return;
  _pollTimer = setInterval(async () => {
    if (!$("#traceList")) return;   // 页面已卸载
    try { await _loadTurns(); } catch (_) { /* 静默 */ }
  }, 15000);
}

async function _stopLive() {
  if (_sseSubId) {
    const bridge = getBridge();
    try { bridge?.unsubscribeSSE?.(_sseSubId); } catch (_) { /* 已断开 */ }
    _sseSubId = null;
  }
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

/* 实时推入：新轮次落库事件 → 会话匹配当前下拉时插到列表头 */
function _onLiveTrace(payload) {
  if (!payload || typeof payload !== "object") return;
  const box = $("#traceList");
  const sel = $("#traceSession");
  if (!box || !sel) return;
  if (sel.value && payload.target_id_hash && payload.target_id_hash !== sel.value) return;
  box.insertAdjacentHTML("afterbegin", _turnCard(payload));
  // 上限保护：列表超限砍尾部（与 TRACE_LIMIT 同口径）
  const cards = box.querySelectorAll(".trace-turn");
  if (cards.length > TRACE_LIMIT) cards[cards.length - 1].remove();
}

/* ── 交互 ─────────────────────────────────────────────────────────── */

function toggleTraceTurn(el) {
  const card = el.closest(".trace-turn");
  const body = card?.querySelector(".trace-turn-body");
  if (!body) return;
  const opening = body.hasAttribute("hidden");
  if (opening) body.removeAttribute("hidden");
  else body.setAttribute("hidden", "");
  el.setAttribute("aria-expanded", opening ? "true" : "false");
  card.classList.toggle("is-open", opening);
}

/* 预览复制：data-target="sim" 取模拟卡，否则按 trace_id 取对应卡片 */
async function copyPreviewText(el) {
  const key = el.dataset.target || "";
  const scope = key === "sim"
    ? $("#simResult")
    : document.querySelector(`.trace-turn[data-trace-id="${CSS.escape(key)}"]`);
  const text = [...(scope?.querySelectorAll(".trace-preview-sec pre.trace-preview-text") || [])]
    .map(pre => pre.textContent).join("\n\n");
  try {
    await navigator.clipboard.writeText(text);
    const orig = el.textContent;
    el.textContent = "已复制";
    setTimeout(() => { el.textContent = orig; }, 1500);
  } catch (e) {
    el.textContent = "复制失败（沙箱限制）";
    setTimeout(() => { el.textContent = "复制全部段落文本"; }, 2000);
  }
}
