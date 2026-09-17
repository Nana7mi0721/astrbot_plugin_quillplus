/* components/modal.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { closeCtxMenu } from "../components/contextmenu.js";
import { closeSelMenus } from "../components/select.js";
import { $, $$, esc } from "../utils.js";

export { EMPTY_ICON, autoSizeModalTextareas, closeAllMenus, closeModal, closeScrim, openModal, openScrim, renderEmptyState, toggleMoreMenu };

/* ── D. 模态框 ─────────────────────────────────────────────────────── */







function openScrim(scrim) {
  if (!scrim) return;
  // 焦点归还锚点：仅记录最外层弹层打开前的焦点，避免嵌套弹层互相覆盖
  if (!S._lastFocused) S._lastFocused = document.activeElement;
  // 锁内容区滚动（HIG：模态期间背景不应滚动）
  const c = $("#content");
  if (c) { S._scrollLockTop = c.scrollTop; c.style.overflow = "hidden"; }
  scrim.classList.remove("is-closing");
  scrim.classList.add("is-open");
  // 焦点移入弹窗：aria-modal 声明了行为就必须实现，否则 Tab 会走到遮罩后面
  const box = scrim.querySelector(".modal, .alert");
  if (box) {
    if (!box.hasAttribute("tabindex")) box.setAttribute("tabindex", "-1");
    // 让当前帧先完成布局，再落焦点，避免 Safari/Chromium 抢回焦点
    requestAnimationFrame(() => {
      if (!scrim.classList.contains("is-open")) return;
      const first = box.querySelector("input:not([type=hidden]):not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled])");
      (first || box).focus({ preventScroll: true });
    });
  }
}

function closeScrim(scrim) {
  if (!scrim || !scrim.classList.contains("is-open")) return;
  scrim.classList.add("is-closing");
  // 浮层挂在 body 下，模态关闭时其锚点会被销毁，必须一并收起
  closeSelMenus(null);
  closeCtxMenu(true);
  setTimeout(() => {
    scrim.classList.remove("is-open", "is-closing");
    // 仍有多层弹层打开时，不解锁滚动、不归还焦点
    const stillOpen = ["cropScrim", "alertScrim", "textScrim", "scrim"]
      .some(id => { const s = document.getElementById(id); return s && s.classList.contains("is-open"); });
    if (stillOpen) return;
    const c = $("#content");
    if (c) { c.style.overflow = ""; c.scrollTop = S._scrollLockTop; }
    if (S._lastFocused && S._lastFocused.isConnected) { try { S._lastFocused.focus({ preventScroll: true }); } catch (_) {} }
    S._lastFocused = null;
  }, 180);
}

function openModal() { openScrim($("#scrim")); }

function autoSizeModalTextareas(root) {
  (root || $("#modalBody"))?.querySelectorAll("textarea.field").forEach(ta => {
    ta.style.height = "auto";
    ta.style.height = Math.min(240, Math.max(64, ta.scrollHeight)) + "px";
  });
}

function closeModal() {
  closeScrim($("#scrim"));
  const saveBtn = $("#modalSaveBtn");
  if (saveBtn) saveBtn.hidden = false;
  S._modalState = null;
  S._modalMode = null;
}

/* ── E. 面板状态持久化 ───────────────────────────────────────────── */
/* 面板界面状态持久化已整体移除：配置页卡片折叠与侧栏分组折叠都不再存在，
   面板没有需要跨刷新记住的界面偏好。（localStorage 在面板沙箱内不可用，
   故「无需持久化」意味着这段逻辑可以直接删除，而不是换一种存储。）
   插件侧 /panel/ui_state 接口保留，供旧客户端平滑过渡。 */

/* ── 溢出菜单（⋯） ─────────────────────────────────────────────────── */
/* 同一时刻只允许一个菜单展开；开关由点击委托驱动，不逐菜单绑事件。 */

function closeAllMenus(except) {
  $$(".menu-wrap > .menu").forEach(menu => {
    if (menu === except) return;
    menu.hidden = true;
    const btn = menu.parentElement.querySelector("[data-action='more-menu']");
    if (btn) btn.setAttribute("aria-expanded", "false");
  });
}

function toggleMoreMenu(btn) {
  const menu = btn.parentElement.querySelector(".menu");
  if (!menu) return;
  const willOpen = menu.hidden;
  closeAllMenus(willOpen ? menu : null);
  menu.hidden = !willOpen;
  btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
}

/* ── 空状态 ────────────────────────────────────────────────────────── */

const EMPTY_ICON = {
  book: "i-book", worldbook: "i-books", persona: "i-person",
  doc: "i-doc", search: "i-search", memory: "i-brain", archive: "i-archive",
};

function renderEmptyState(iconType, title, description, extraHtml) {
  const icon = EMPTY_ICON[iconType] || EMPTY_ICON.book;
  return `<div class="empty-state">
    <svg class="icon icon--xl" aria-hidden="true"><use href="#${icon}"/></svg>
    <div class="empty-title">${esc(title)}</div>
    <div class="empty-desc">${esc(description)}</div>
    ${extraHtml ? `<div class="empty-extra">${extraHtml}</div>` : ""}
  </div>`;
}
