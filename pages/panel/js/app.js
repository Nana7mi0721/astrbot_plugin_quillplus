/* app.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "./state.js";
import { backupExport, backupRestore } from "./components/backup.js";
import { bindOverlayEvents, closeCtxMenu } from "./components/contextmenu.js";
import { closeCropDialog, handleAvatarFile, recropAvatar } from "./components/cropper.js";
import { closeAlert, copyText, dismissBanner } from "./components/feedback.js";
import { initListKeys } from "./components/listkeys.js";
import { closeAllMenus, closeModal, toggleMoreMenu } from "./components/modal.js";
import { closeSelMenus, enhanceSelects } from "./components/select.js";
import { discardChanges, filterConfig, initConfigNavObserver, loadGlobalSettings, loadHealth, markDirty, saveAllChanges, scrollToSection, setStreamModeAll, switchTab, syncSidebarBadges } from "./pages/config.js";
import { changeMemoryPage, deleteMemory, exportChatLogs, exportMemories, importMemories, loadChatLogs, loadMemoryBrowser, onMemorySessionChange, pruneMemories, showMemoryDetail, switchMemorySub, testMemoryVectorSearch, toggleMemoryPin } from "./pages/memory.js";
import { closeImportTextModal, deletePersona, exportPersona, filterCheckboxes, filterWBEntries, importPersona, importPersonaFromText, loadPersonas, openImportTextModal, openPersonaModal, renderPersonaPage, savePersona, switchPersonaView, syncExtList } from "./pages/persona.js";
import { deleteRAGDocument, processRAGFiles, testRAGSearch } from "./pages/rag.js";
import { deleteWB, deleteWBEntry, exportWBSt, importWBSt, onWBSearchInput, openWBCreateModal, openWBEntryModal, reloadWB, saveWBCreate, saveWBEntry, setWBOnlyConst, toggleWB } from "./pages/worldbook.js";
import { _wrSelected, calcPerPage, deleteWREntry, exportWR, goWRPage, importWR, loadWR, loadWRCategories, openWRModal, openWRTestModal, saveWREntry, toggleWREntry, updateWRBatchBar, wrBatchClear, wrBatchDelete, wrBatchToggle, wrState } from "./pages/writing.js";
import { $, debounce, withBusy } from "./utils.js";

export { ACTIONS, bindEvents, debouncedMemLoad, debouncedWBFilter, debouncedWRLoad, quillInit, saveModal };

/* ── 动作分发表 ────────────────────────────────────────────────────── */

const ACTIONS = {
  // 通用
  "tab": (el) => switchTab(el.dataset.tab),
  "to-top": () => $("#content")?.scrollTo({ top: 0, behavior: "smooth" }),
  "more-menu": (el) => toggleMoreMenu(el),
  "clear-search": (el) => {
    // 世界书内联搜索框每册一个，没法给唯一 id；用作用域就近取，
    // 否则 getElementById 只会命中展开列表里的第一个，清错别人的框。
    const input = el.dataset.scope === "wb-filter"
      ? el.closest(".wb-item")?.querySelector("[data-wb-filter]")
      : document.getElementById(el.dataset.target);
    if (!input) return;
    input.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  },
  "banner-close": (el) => dismissBanner(el.closest(".banner")),
  "scrim-close": (el, e) => { if (e.target === el) closeModal(); },
  "modal-close": () => closeModal(),
  "modal-save": () => saveModal(),
  "text-close": () => closeImportTextModal(),
  "text-import": (el) => importPersonaFromText(el),
  "alert-cancel": () => closeAlert(),
  "crop-close": () => closeCropDialog(),

  // 写作素材库
  "wr-new": () => openWRModal(null),
  "wr-edit": (el) => openWRModal(el.closest(".wr-card").dataset.eid),
  "wr-delete": (el) => deleteWREntry(el.closest(".wr-card").dataset.eid, el),
  "wr-test": () => openWRTestModal(),
  "wr-export": (el) => exportWR(el),
  "wr-batch-toggle": (el) => wrBatchToggle(el.dataset.value === "1"),
  "wr-batch-delete": () => wrBatchDelete(),
  "wr-batch-clear": () => wrBatchClear(),
  "wr-page": (el) => goWRPage(el.dataset.value),
  // 右键菜单专用：只补「复制 ID」「切换启用」两个缺失动作，
  // 编辑 / 删除直接复用上面的 wr-edit / wr-delete（菜单项挂在 .wr-card 内）。
  "wr-copy-id": (el) => {
    const card = el.closest(".wr-card");
    if (card) copyText(card.dataset.eid);
  },
  "wr-ctx-toggle": (el) => {
    const card = el.closest(".wr-card");
    if (!card) return;
    const sw = card.querySelector("[data-wr-toggle]");
    const next = !(sw && sw.checked);
    if (sw) sw.checked = next;             // 切完重渲染，这里只是给用户即时反馈
    toggleWREntry(card.dataset.eid, next, sw);
  },

  // 世界书
  "wb-new": () => openWBCreateModal(),
  "wb-toggle": (el) => toggleWB(el.closest(".wb-item").dataset.wb),
  "wb-delete": (el) => deleteWB(el.closest(".wb-item").dataset.wb, el),
  "wb-reload": (el) => reloadWB(el),
  "wb-export-st": () => exportWBSt(),
  "wb-entry-new": (el) => openWBEntryModal(el.closest(".wb-item").dataset.wb, null),
  "wb-entry-edit": (el) => {
    const card = el.closest(".wb-entry");
    openWBEntryModal(card.dataset.wb, card.dataset.eid);
  },
  "wb-entry-delete": (el) => {
    const card = el.closest(".wb-entry");
    deleteWBEntry(card.dataset.wb, card.dataset.eid, el);
  },

  // 角色卡
  "persona-new": () => openPersonaModal(null),
  "persona-edit": (el) => openPersonaModal(window.__pids[Number(el.dataset.idx)]),
  "persona-export": (el) => exportPersona(window.__pids[Number(el.dataset.idx)]),
  "persona-delete": (el) => deletePersona(window.__pids[Number(el.dataset.idx)], el),
  "persona-refresh": (el) => withBusy(el, "", loadPersonas),
  "persona-view": (el) => switchPersonaView(el.dataset.value),
  "persona-import-text": () => openImportTextModal(),
  "persona-recrop": () => recropAvatar(),

  // 文档知识库
  "rag-pick": () => $("#ragFile")?.click(),
  "rag-search": (el) => testRAGSearch(el),
  "rag-delete": (el) => deleteRAGDocument(el.dataset.value, el),

  // 动态记忆
  "mem-page": (el) => changeMemoryPage(el.dataset.value),
  "mem-detail": (el) => showMemoryDetail(el.dataset.id),
  "mem-delete": (el) => deleteMemory(el.dataset.id),
  "mem-pin": (el) => toggleMemoryPin(el.dataset.id, el.dataset.core === "1"),
  "mem-prune": (el) => pruneMemories(el),
  "mem-export": (el) => exportMemories(el),
  "log-load": (el) => loadChatLogs(el),
  "log-export": (el) => exportChatLogs(el.dataset.value, el),
  "vec-search": (el) => testMemoryVectorSearch(el),

  // 配置
  "config-save": (el) => saveAllChanges(el),
  "config-discard": () => discardChanges(),
  "stream-all": (el) => setStreamModeAll(el.dataset.value, el),
  "backup-export": (el) => backupExport(el),
  "cfg-nav": (el) => scrollToSection(el.dataset.target),
};

/* 统一的保存入口（供模态框分派与全局快捷键复用） */
async function saveModal() {
  const st = S._modalState;
  if (st && st.type === "persona") return savePersona();
  if (st && st.type === "wb-entry") return saveWBEntry();
  if (st && st.type === "wb-create") return saveWBCreate();
  if (S._modalMode === "create" || S._modalMode === "edit") return saveWREntry();
  return;   // 无模态状态时不保存（原实现会误调 saveWREntry）
}

/* ── 全局事件绑定 ──────────────────────────────────────────────────── */

function bindEvents() {
  // 1) 点击委托：所有 data-action 与折叠头、标签页
  document.addEventListener("click", (e) => {
    const actEl = e.target.closest("[data-action]");
    if (actEl) {
      const action = actEl.dataset.action;
      // 菜单项执行后自动收起；触发器自身由 toggleMoreMenu 管理开合
      if (action !== "more-menu" && e.target.closest(".menu")) closeAllMenus();
      const fn = ACTIONS[action];
      if (fn) { fn(actEl, e); return; }
    }
    // 点击菜单之外任何位置 → 关闭所有溢出菜单
    if (!e.target.closest(".menu-wrap")) closeAllMenus();
    const tabEl = e.target.closest("[data-tab]");
    if (tabEl && tabEl.classList.contains("sl-item")) {
      switchTab(tabEl.dataset.tab);
      return;
    }
    const navBtn = e.target.closest("#cfgNav > button");
    if (navBtn) { scrollToSection(navBtn.dataset.target); return; }
    const memBtn = e.target.closest("[data-memsub]");
    if (memBtn) { switchMemorySub(memBtn.dataset.memsub); return; }
    const del = e.target.closest(".tag-del[data-idx]");
    if (del) return; // 由标签输入内部处理
  });

  // 1b) 双击委托：PC 肌肉记忆 —— 双击卡片主体直接编辑。
  // 素材卡在单击层已由标题按钮直接进模态（无延迟语义），故这里只剩
  // 角色卡与记忆表两处双击加速。
  document.addEventListener("dblclick", (e) => {
    // 双击行内按钮 / 开关 / 输入控件时不触发编辑：它们已有各自的单击语义，
    // 例如卡片 footer 的启停开关、删除按钮，双击不应弹编辑窗。
    if (e.target.closest("button, input, select, textarea, a, label, .switch, .check, .menu, .ctx-menu, .sel-menu")) return;
    // 有浮层时（模态 / 自绘下拉 / 右键菜单）不接管双击
    if ($("#scrim")?.classList.contains("is-open") || S._ctxOpen || S._selOpen) return;

    // 按 action 名分派，沿用 ACTIONS 里既有的 (el, e) 签名：
    // wr-edit 取 el.closest(".wr-card").dataset.eid；
    // persona-edit 取 el.dataset.idx；mem-detail 取 el.dataset.id。
    // 三个 handler 都从真实元素上取值，故直接复用列表里已有的
    // [data-action] 按钮元素（真元素，无需伪造 dataset，最不容易猜错）。
    const run = (action, el) => { const fn = ACTIONS[action]; if (fn) fn(el, e); };

    const pcard = e.target.closest(".persona-card");
    if (pcard) {
      e.preventDefault();
      const editBtn = pcard.querySelector("[data-action='persona-edit']");
      if (editBtn) run("persona-edit", editBtn);
      return;
    }

    const row = e.target.closest("#memTbody tr");
    if (row) {
      const detail = row.querySelector("[data-action='mem-detail']");
      if (!detail) return;                          // 加载中 / 空态行不响应
      e.preventDefault();
      window.getSelection()?.removeAllRanges();     // 清掉双击产生的文字选区
      run("mem-detail", detail);
    }
  });

  // 2) 变更委托：素材库选择/启停
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches("#pf-wb-mode, #pf-rag-mode, #pf-wr-mode")) {
      const sys = { "pf-wb-mode": "wb", "pf-rag-mode": "rag", "pf-wr-mode": "wr" }[t.id];
      if (sys) syncExtList(sys);
      return;
    }
    if (t.matches("[data-wr-select]")) {
      const card = t.closest(".wr-card");
      if (!card) return;
      if (t.checked) _wrSelected.add(card.dataset.eid);
      else _wrSelected.delete(card.dataset.eid);
      card.classList.toggle("is-selected", t.checked);
      updateWRBatchBar();
      return;
    }
    if (t.matches("[data-wr-toggle]")) {
      toggleWREntry(t.closest(".wr-card").dataset.eid, t.checked, t);
      return;
    }
    if (t.id === "wrOnlyConst") { wrState.page = 1; loadWR(); return; }
    // 分类筛选：必须限定 SELECT —— 新建/编辑条目弹窗里也有一个同名的 #wrCat 文本输入框，
    // 若不加判断，在弹窗里改分类会误触发列表重载并把正在编辑的分页重置掉。
    if (t.id === "wrCat" && t.tagName === "SELECT") { wrState.page = 1; loadWR(); return; }
    if (t.id === "wbOnlyConst") { setWBOnlyConst(t.checked); return; }
    if (t.id === "memSession") { onMemorySessionChange(); return; }
    // 配置表单变化 → 标记未保存
    if (t.closest("#pane-config") && t.matches("input, select, textarea")) markDirty();
  });

  // 3) 输入委托：搜索框即时过滤 + 滑块联动 + 标签输入回车
  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t.matches("#modalBody textarea.field")) {
      t.style.height = "auto";
      t.style.height = Math.min(240, Math.max(64, t.scrollHeight)) + "px";
    }
    if (t.id === "wrSearch") { wrState.page = 1; debouncedWRLoad(); return; }
    if (t.id === "wbSearch") { debouncedWBFilter(); return; }
    if (t.id === "personaSearch") {
      S._personaQuery = (t.value || "").trim().toLowerCase();
      S._personaShowCount = 20;
      renderPersonaPage();
      return;
    }
    if (t.id === "memFilter") { debouncedMemLoad(); return; }
    if (t.id === "configSearchInput") { filterConfig(t.value); return; }
    if (t.id === "c-wb-sens") {
      const v = $("#c-wb-sens-val");
      if (v) v.textContent = t.value;
      markDirty();
      return;
    }
    if (t.matches("[data-ext-filter]")) {
      filterCheckboxes(t, t.dataset.extFilter);
      return;
    }
    if (t.matches("[data-wb-filter]")) { filterWBEntries(t); return; }
    if (t.closest("#pane-config") && t.matches("input, select, textarea")) markDirty();
  });

  // 搜索清除按钮的可见性
  document.addEventListener("input", (e) => {
    const wrap = e.target.closest && e.target.closest(".search");
    if (wrap && e.target.tagName === "INPUT") wrap.classList.toggle("has-value", !!e.target.value);
  });

  // 4) 键盘委托：回车触发、Esc 关闭、快捷键
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    // Esc：自顶向下关闭最上层弹层
    if (e.key === "Escape") {
      // 自绘浮层优先于模态：它们挂得最靠前，Esc 应先收起它们
      if (S._ctxOpen) { closeCtxMenu(); return; }
      if (S._selOpen) { const b = S._selOpen.wrap.querySelector(".sel-btn"); closeSelMenus(null); if (b) b.focus({ preventScroll: true }); return; }
      if ($(".menu-wrap > .menu:not([hidden])")) { closeAllMenus(); return; }
      if ($("#cropScrim")?.classList.contains("is-open")) { closeCropDialog(); return; }
      if ($("#alertScrim")?.classList.contains("is-open")) { closeAlert(); return; }
      if ($("#textScrim")?.classList.contains("is-open")) { closeImportTextModal(); return; }
      if ($("#scrim")?.classList.contains("is-open")) { closeModal(); return; }
      return;
    }
    // Tab 循环：有弹层打开时把焦点限制在最上层弹窗内（focus trap）。
    // 必须放在 typing 早返回之前，否则输入框内 Tab 不受约束。
    if (e.key === "Tab") {
      const scrim = ["cropScrim", "alertScrim", "textScrim", "scrim"]
        .map(id => document.getElementById(id))
        .find(s => s && s.classList.contains("is-open"));
      if (scrim) {
        const box = scrim.querySelector(".modal, .alert");
        if (box) {
          const f = [...box.querySelectorAll("input:not([type=hidden]):not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])")]
            .filter(el => el.offsetParent !== null);
          if (f.length) {
            const first = f[0], last = f[f.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
          }
        }
      }
    }
    // Alt+1..6 切换标签页。必须放在 typing 早返回之前：面板里几乎总有输入框持有焦点
    // （搜索框、配置项），否则用户在任意输入框内都无法用快捷键切页。
    if (e.altKey && /^[1-6]$/.test(e.key)) {
      e.preventDefault();
      const tabs = ["wr", "wb", "persona", "rag", "memory", "config"];
      switchTab(tabs[Number(e.key) - 1]);
      return;
    }
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    if (typing) {
      // 输入框内：Ctrl/Cmd+Enter 提交
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        if ($("#scrim")?.classList.contains("is-open")) { e.preventDefault(); saveModal(); }
        return;
      }
      // 确认框内回车提交：输入法组合中（isComposing）不触发，避免中文候选词回车误提交
      if (e.key === "Enter" && !e.isComposing) {
        if (t.id === "promptInput") { e.preventDefault(); $("#alertOk")?.click(); return; }
        if (t.id === "dangerInput") {
          e.preventDefault();
          const ok = $("#alertOk");
          if (ok && !ok.disabled) ok.click();
          return;
        }
      }
      // 世界书条目内联搜索：回车不提交
      if (e.key === "Enter" && t.matches("[data-wb-filter]")) { e.preventDefault(); return; }
      if (e.key === "Enter" && t.id === "ragQuery") { e.preventDefault(); testRAGSearch($("[data-action='rag-search']")); return; }
      if (e.key === "Enter" && t.id === "logSession") { e.preventDefault(); loadChatLogs($("[data-action='log-load']")); return; }
      if (e.key === "Enter" && t.id === "vecQuery") { e.preventDefault(); testMemoryVectorSearch($("[data-action='vec-search']")); return; }
      return;
    }
    if (e.ctrlKey || e.metaKey) {
      if (e.key.toLowerCase() === "s") {
        e.preventDefault();
        if ($("#pane-config")?.classList.contains("is-active")) saveAllChanges($("[data-action='config-save']"));
        return;
      }
      return;
    }
    // "/" 聚焦当前页搜索框
    if (e.key === "/") {
      e.preventDefault();
      const pane = $(".tab-pane.is-active");
      const box = pane && pane.querySelector("input[type='search']");
      if (box) { box.focus(); box.select && box.select(); }
    }
  });

  // 5) 文件选择委托
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.tagName !== "INPUT" || t.type !== "file") return;
    // 选文件即等于选定该项操作，菜单可以收起
    closeAllMenus();
    switch (t.dataset.file) {
      case "wr-import": importWR(t); break;
      case "wb-import": importWBSt(t); break;
      case "persona-import": importPersona(t); break;
      case "mem-import": importMemories(t); break;
      case "backup-restore": backupRestore(t); break;
      default: break;
    }
  });

  // 6) 头像相关（file / 点击 / 键盘）
  // #pf-avatar-zone 由 openPersonaModal 的模板字符串动态生成，绑定时并不存在，
  // 故改为委托到 document（与下面 pf-avatar-file 的 change 委托同处）。
  document.addEventListener("click", (e) => {
    if (e.target.closest("#pf-avatar-zone")) { $("#pf-avatar-file")?.click(); return; }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.closest("#pf-avatar-zone")) {
      e.preventDefault();
      $("#pf-avatar-file")?.click();
    }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "pf-avatar-file") handleAvatarFile(e.target);
  });

  // 7) RAG 上传区（点击 / 拖拽 / 键盘）
  const drop = $("#ragDrop");
  if (drop) {
    drop.addEventListener("click", () => $("#ragFile")?.click());
    drop.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#ragFile")?.click(); }
    });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
    drop.addEventListener("drop", async (e) => {
      e.preventDefault();
      drop.classList.remove("is-over");
      if (e.dataTransfer && e.dataTransfer.files.length) await processRAGFiles(e.dataTransfer.files);
    });
  }
  document.addEventListener("change", (e) => {
    if (e.target.id === "ragFile" && e.target.files.length) {
      processRAGFiles(e.target.files);
      e.target.value = "";
    }
  });

  // 8) 回到顶部按钮随滚动显隐
  $("#content")?.addEventListener("scroll", () => {
    const btn = $("#toTop");
    if (btn) btn.classList.toggle("is-visible", ($("#content").scrollTop || 0) > 320);
  }, { passive: true });

  // 9) 弹层遮罩关闭（点击空白处）
  $("#scrim")?.addEventListener("click", (e) => { if (e.target.id === "scrim") closeModal(); });
  $("#textScrim")?.addEventListener("click", (e) => { if (e.target.id === "textScrim") closeImportTextModal(); });
  $("#alertScrim")?.addEventListener("click", (e) => { if (e.target.id === "alertScrim") closeAlert(); });
  $("#cropScrim")?.addEventListener("click", (e) => { if (e.target.id === "cropScrim") closeCropDialog(); });

  // 10) 窗口尺寸变化：网格分页重算
  window.addEventListener("resize", debounce(() => {
    if ($("#pane-wr")?.classList.contains("is-active")) {
      const next = calcPerPage();
      if (next !== wrState.perPage) { wrState.perPage = next; loadWR(); }
    }
  }, 320));
}

// 防抖包装（在 bindEvents 之外定义，供多处复用）
const debouncedWRLoad = debounce(loadWR, 300);
const debouncedWBFilter = debounce(onWBSearchInput, 200);
const debouncedMemLoad = debounce(loadMemoryBrowser, 300);

/* ── 初始化 ────────────────────────────────────────────────────────── */

async function quillInit() {
  bindEvents();
  bindOverlayEvents();
  // 静态原生下拉 → 自绘 listbox（select 仍留在 DOM 里作为值存储）
  enhanceSelects();
  initConfigNavObserver();

  // 列表方向键导航：容器常驻，内容重建不影响委托绑定。
  // 角色卡有网格 / 列表两种视图，grid 的列数由实际渲染结果实时计算，两者通用。
  initListKeys($("#wrGrid"), ".wr-card", { grid: true });
  initListKeys($("#wbList"), ".wb-item", {});
  initListKeys($("#personaList"), ".persona-card", { grid: true });
  // 记忆表用显式 id：面板里有两个 table.data（记忆 / 日志），
  // 按标签序取第一个会在将来插入新表格时静默指错。
  initListKeys($("#memTbody"), "tr", {});

  // 侧栏分组与配置卡片都不再有可折叠项，无需恢复界面状态。

  // 主题：服务端已在 <html data-theme> 上写好；bridge 亦会推送 isDark。
  // 锁住系统偏好的自动跟随，避免覆盖面板主题。
  if (window.AstrBotPluginPage) {
    window.__quillThemeLocked = true;
    try {
      const ctx = window.AstrBotPluginPage.getContext && window.AstrBotPluginPage.getContext();
      if (ctx && typeof ctx.isDark === "boolean") {
        document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
      }
      if (window.AstrBotPluginPage.onContext) {
        window.AstrBotPluginPage.onContext((next) => {
          if (next && typeof next.isDark === "boolean") {
            document.documentElement.setAttribute("data-theme", next.isDark ? "dark" : "light");
          }
        });
      }
    } catch (_) { /* 旧版 bridge 无 getContext 时忽略 */ }
  }

  // 版本徽章与健康度（顺带首屏拿到版本号）
  loadHealth();

  // 深链接：#tab=memory&sub=logs
  try {
    const hm = (location.hash || "").match(/tab=([a-zA-Z]+)/);
    if (hm) {
      const tab = hm[1].toLowerCase();
      if (["wr", "wb", "persona", "rag", "memory", "config"].includes(tab)) switchTab(tab);
      const sm = (location.hash || "").match(/sub=([a-zA-Z]+)/);
      if (sm && tab === "memory") switchMemorySub(sm[1].toLowerCase());
    } else {
      loadWR();
    }
  } catch (_) { loadWR(); }

  loadWRCategories();
  loadGlobalSettings();
  syncSidebarBadges();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", quillInit);
else quillInit();
