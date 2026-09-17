/* components/contextmenu.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { closeSelMenus, closeSelMenusImmediate } from "../components/select.js";
import { $, $$, esc } from "../utils.js";

export { CTX_LETTERS, bindOverlayEvents, closeCtxMenu, onContextMenu, showCtxMenu };

/* ── N. 右键上下文菜单 ─────────────────────────────────────────────── */
/* 与下拉共用视觉语言；菜单项复用 data-action 分派，业务逻辑零重写。
   菜单在打开时才挂到命中元素内部（position: fixed 不受 overflow /
   层叠结构影响），这样 ACTIONS 中的 el.closest(".wr-card") 等写法
   无需任何改动即可解析到正确的条目。                                */





const CTX_LETTERS = {};       // 本次菜单的字母快捷键 → 元素

function closeCtxMenu(immediate) {
  if (!S._ctxOpen) return;
  const { el, keydown } = S._ctxOpen;
  document.removeEventListener("keydown", keydown, true);
  S._ctxOpen = null;
  S._ctxItems = [];
  Object.keys(CTX_LETTERS).forEach(k => delete CTX_LETTERS[k]);
  el.classList.remove("is-in");
  el.classList.add("is-out");
  const done = () => { if (el.parentNode) el.remove(); el.classList.remove("is-out"); };
  if (immediate) done();
  else setTimeout(() => { if (!el.classList.contains("is-in")) done(); }, 200);
}

/**
 * 显示上下文菜单。
 * items: [{ action, label, id?, idx?, value?, core?, kbd?, danger? } | { sep: true }]
 * mount 是菜单挂载点：默认挂在 targetEl 内部，这样 ACTIONS 里
 * el.closest(".wr-card") / el.closest(".wb-entry") 的既有写法无需改动；
 * 只靠 dataset 定位的动作（角色卡 idx、记忆 id）则挂到 body，
 * 避免把块级元素塞进 <tr> 这类非法嵌套。
 */
function showCtxMenu(x, y, targetEl, items, mount) {
  closeCtxMenu(true);
  closeSelMenus(null);

  if (!S._ctxHost) {
    S._ctxHost = document.createElement("div");
    S._ctxHost.className = "ctx-menu";
    S._ctxHost.setAttribute("role", "menu");
    S._ctxHost.tabIndex = -1;
  }
  const el = S._ctxHost;
  el.innerHTML = items.map(it => {
    if (it.sep) return '<div class="ctx-sep" role="separator"></div>';
    const attrs =
      ' data-action="' + esc(it.action) + '"' +
      (it.id !== undefined ? ' data-id="' + esc(it.id) + '"' : "") +
      (it.idx !== undefined ? ' data-idx="' + esc(it.idx) + '"' : "") +
      (it.value !== undefined ? ' data-value="' + esc(it.value) + '"' : "") +
      (it.core !== undefined ? ' data-core="' + esc(it.core) + '"' : "");
    return '<button type="button" class="ctx-item' + (it.danger ? " ctx-item--danger" : "") +
      '" role="menuitem" tabindex="-1"' + attrs + '>' +
      '<span class="ctx-label">' + esc(it.label) + "</span>" +
      (it.kbd ? '<span class="ctx-kbd">' + esc(it.kbd) + "</span>" : "") +
      "</button>";
  }).join("");

  // 挂载：默认挂在命中元素内部，让既有 ACTIONS 的 closest() 定位保持不变
  (mount || targetEl).appendChild(el);
  el.hidden = false;
  el.classList.remove("is-out");
  el.style.top = "-9999px";
  el.style.left = "-9999px";

  // 定位：鼠标位置，贴边自动翻转
  const mw = el.offsetWidth, mh = el.offsetHeight;
  const left = (x + mw > window.innerWidth - 8) ? Math.max(8, x - mw) : x;
  const up = (y + mh > window.innerHeight - 8);
  const top = up ? Math.max(8, y - mh) : y;
  el.style.left = Math.round(left) + "px";
  el.style.top = Math.round(top) + "px";
  el.classList.toggle("is-up", up);

  S._ctxItems = $$(".ctx-item", el);
  Object.keys(CTX_LETTERS).forEach(k => delete CTX_LETTERS[k]);
  S._ctxItems.forEach(item => {
    const kbd = item.querySelector(".ctx-kbd");
    if (kbd && kbd.textContent) CTX_LETTERS[kbd.textContent.toLowerCase()] = item;
  });

  const keydown = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      const back = targetEl;
      closeCtxMenu();
      // 归还焦点：卡片 / 表格行本身不可聚焦，补一个 -1 占位 tabindex
      if (back && back.isConnected) {
        if (!back.hasAttribute("tabindex")) back.setAttribute("tabindex", "-1");
        try { back.focus({ preventScroll: true }); } catch (_) {}
      }
      return;
    }
    if (e.key === "Tab") { closeCtxMenu(); return; }
    const idx = S._ctxItems.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!S._ctxItems.length) return;
      const dir = e.key === "ArrowDown" ? 1 : -1;
      const next = S._ctxItems[(idx + dir + S._ctxItems.length) % S._ctxItems.length];
      next.focus();
      return;
    }
    if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      const t = e.key === "Home" ? S._ctxItems[0] : S._ctxItems[S._ctxItems.length - 1];
      if (t) t.focus();
      return;
    }
    if ((e.key === "Enter" || e.key === " ") && idx >= 0) {
      e.preventDefault();
      document.activeElement.click();
      return;
    }
    if (/^[a-z0-9]$/i.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const hit = CTX_LETTERS[e.key.toLowerCase()];
      if (hit) { e.preventDefault(); hit.click(); }
    }
  };
  document.addEventListener("keydown", keydown, true);

  S._ctxOpen = { el, target: targetEl, keydown };
  el.classList.add("is-in");
  const first = S._ctxItems[0];
  if (first) first.focus();
  else el.focus();
}

/** 右键命中分派：只有下列容器才拦截，其余交给浏览器原生菜单。 */
function onContextMenu(e) {
  const t = e.target;
  if (!t || !t.closest) return;

  const card = t.closest(".wr-card");
  if (card) {
    const eid = card.dataset.eid;
    const sw = card.querySelector("[data-wr-toggle]");
    const on = !!(sw && sw.checked);
    e.preventDefault();
    showCtxMenu(e.clientX, e.clientY, card, [
      { action: "wr-edit", label: "编辑条目", kbd: "E" },
      { action: "wr-copy-id", label: "复制条目 ID", kbd: "C" },
      { sep: true },
      { action: "wr-ctx-toggle", label: on ? "停用条目" : "启用条目", kbd: "T" },
      { sep: true },
      { action: "wr-delete", label: "删除条目", kbd: "D", danger: true },
    ]);
    return;
  }

  const wbEntry = t.closest(".wb-entry");
  if (wbEntry) {
    e.preventDefault();
    showCtxMenu(e.clientX, e.clientY, wbEntry, [
      { action: "wb-entry-edit", label: "编辑条目", kbd: "E" },
      { sep: true },
      { action: "wb-entry-delete", label: "删除条目", kbd: "D", danger: true },
    ]);
    return;
  }

  const pcard = t.closest(".persona-card");
  if (pcard) {
    const src = pcard.querySelector("[data-action='persona-edit']");
    if (!src) return;
    const idx = src.dataset.idx;
    e.preventDefault();
    showCtxMenu(e.clientX, e.clientY, pcard, [
      { action: "persona-edit", label: "编辑角色卡", idx, kbd: "E" },
      { action: "persona-export", label: "导出角色卡", idx, kbd: "X" },
      { sep: true },
      { action: "persona-delete", label: "删除角色卡", idx, kbd: "D", danger: true },
    ], document.body);                                    // 动作只读 data-idx，无需挂在卡片内
    return;
  }

  const row = t.closest("table.data tbody tr");
  if (row) {
    const detail = row.querySelector("[data-action='mem-detail']");
    const pin = row.querySelector("[data-action='mem-pin']");
    if (!detail) return;                                  // 日志表等非记忆行不拦截
    e.preventDefault();
    const id = detail.dataset.id;
    const isCore = pin && pin.dataset.core === "1";
    showCtxMenu(e.clientX, e.clientY, row, [
      { action: "mem-detail", label: "查看详情", id, kbd: "I" },
      { action: "mem-pin", label: isCore ? "取消核心锚定" : "置为核心锚定", id, core: isCore ? "1" : "0", kbd: "P" },
      { sep: true },
      { action: "mem-delete", label: "删除记忆", id, kbd: "D", danger: true },
    ], document.body);                                    // <tr> 内不能塞块级浮层
  }
}

/** 浮层的全局关闭时机：点击外部 / 滚动 / 窗口尺寸变化。 */
function bindOverlayEvents() {
  document.addEventListener("pointerdown", (e) => {
    // 浮层挂在 body 下，click 的 target 不在 .sel-wrap 内，须单独放行 .sel-menu
    if (S._selOpen && !e.target.closest(".sel-wrap") && !e.target.closest(".sel-menu")) closeSelMenus(null);
    if (S._ctxOpen && !e.target.closest(".ctx-menu")) closeCtxMenu();
  }, true);

  document.addEventListener("click", (e) => {
    // 上下文菜单项交由既有 data-action 委托执行，执行后再收起
    if (S._ctxOpen && e.target.closest(".ctx-menu")) { const t = S._ctxOpen; setTimeout(() => { if (S._ctxOpen === t) closeCtxMenu(); }, 0); }
  });

  document.addEventListener("contextmenu", onContextMenu);

  // 原生 label[for] 指向已被隐藏的 select → 转交触发器开合，标签仍可点。
  // 必须限定在标签所在弹窗内查找：#wrCat 在工具栏与新建条目弹窗里同名，
  // 全局 getElementById 会命中工具栏那个下拉。
  document.addEventListener("click", (e) => {
    const lab = e.target.closest("label[for]");
    if (!lab || e.target.closest(".sel-wrap")) return;
    const scope = lab.closest("#modalBody, .modal-body, .scrim");
    const sel = (scope && scope.querySelector("#" + CSS.escape(lab.htmlFor))) || document.getElementById(lab.htmlFor);
    if (!sel || !sel._selApi || sel._selApi.btn.disabled) return;
    e.preventDefault();
    sel._selApi.btn.focus({ preventScroll: true });
    if (sel._selApi.menu.hidden) sel._selApi.open(); else sel._selApi.close();
  });

  // 滚动就收起（锚点已经移走，留一个淡出中的浮层反而不跟手）
  $("#content")?.addEventListener("scroll", () => { closeSelMenusImmediate(); closeCtxMenu(true); }, { passive: true });
  window.addEventListener("resize", () => { closeSelMenusImmediate(); closeCtxMenu(true); });
}
