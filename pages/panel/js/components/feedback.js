/* components/feedback.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { closeScrim, openScrim } from "../components/modal.js";
import { $, esc } from "../utils.js";

export { BANNER_ICON, BANNER_MAX, BANNER_MS, closeAlert, copyText, dismissBanner, showErrorDetail, showToast, showToastWithAction, sandboxConfirm, sandboxPrompt };

/* ── C. 提示横幅与对话框 ───────────────────────────────────────────── */

const BANNER_ICON = { success: "i-check-circle", error: "i-x-circle", warning: "i-warn", info: "i-info" };
// 分级时长：错误常驻由用户关闭；成功短暂停留；其余给足阅读时间
const BANNER_MS = { success: 2400, info: 3200, warning: 4200, error: 0 };
const BANNER_MAX = 4;

/**
 * 显示提示横幅。返回元素，调用方可直接 .remove()。
 * 语义与旧的 showToast 一致，便于全局替换。
 */
function showToast(msg, type = "error") {
  const host = $("#toaster");
  if (!host) return null;
  const kind = BANNER_ICON[type] ? type : "error";
  const el = document.createElement("div");
  el.className = `banner banner--${kind}`;
  el.innerHTML =
    `<svg class="icon banner-icon" aria-hidden="true"><use href="#${BANNER_ICON[kind]}"/></svg>` +
    `<span class="banner-msg">${esc(msg)}</span>` +
    (BANNER_MS[kind] > 0 ? "" : `<button type="button" class="banner-action" data-action="banner-close">关闭</button>`);
  host.appendChild(el);
  while (host.children.length > BANNER_MAX) host.firstElementChild.remove();

  requestAnimationFrame(() => el.classList.add("is-in"));

  const ms = BANNER_MS[kind];
  if (ms > 0) {
    // 进度条让自动消失可预期
    const bar = document.createElement("div");
    bar.className = "banner-progress";
    el.appendChild(bar);
    bar.animate([{ transform: "scaleX(1)" }, { transform: "scaleX(0)" }], { duration: ms, easing: "linear", fill: "forwards" });
    setTimeout(() => dismissBanner(el), ms);
  }
  return el;
}

function dismissBanner(el) {
  // 提前关闭（撤销 / 点 ✕）时清掉自动消失定时器，避免孤儿定时器空转
  if (el && el.__bannerTimer) { clearTimeout(el.__bannerTimer); el.__bannerTimer = null; }
  if (!el || !el.isConnected) return;
  el.classList.add("is-out");
  setTimeout(() => el.remove(), 200);
}

/**
 * 带撤销动作的横幅（删除类操作的反悔窗口）。
 * 保留旧签名 (msg, type, actionLabel, actionFn, ms)，便于调用点平滑迁移。
 */
/** 复制文本到剪贴板。面板可能运行在不透明源沙箱 iframe 中，
 *  此时 textarea + execCommand 是唯一可用路径，故保留回退分支。 */
function copyText(text) {
  const done = () => showToast("已复制：" + text, "success");
  const fallback = () => {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:-9999px;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      if (ok) done(); else showToast("复制失败，请手动选择复制", "warning");
    } catch (_) { showToast("复制失败，请手动选择复制", "warning"); }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(fallback);
  } else fallback();
}

function showToastWithAction(msg, type, actionLabel, actionFn, ms = 6000) {
  const host = $("#toaster");
  if (!host) return null;
  const kind = BANNER_ICON[type] ? type : "info";
  const el = document.createElement("div");
  el.className = `banner banner--${kind}`;
  el.innerHTML =
    `<svg class="icon banner-icon" aria-hidden="true"><use href="#${BANNER_ICON[kind]}"/></svg>` +
    `<span class="banner-msg">${esc(msg)}</span>` +
    `<button type="button" class="banner-action" data-banner-undo>${esc(actionLabel)}</button>` +
    `<button type="button" class="banner-action" data-action="banner-close" aria-label="关闭">✕</button>`;
  host.appendChild(el);
  while (host.children.length > BANNER_MAX) host.firstElementChild.remove();
  requestAnimationFrame(() => el.classList.add("is-in"));

  const bar = document.createElement("div");
  bar.className = "banner-progress";
  el.appendChild(bar);
  bar.animate([{ transform: "scaleX(1)" }, { transform: "scaleX(0)" }], { duration: ms, easing: "linear", fill: "forwards" });

  el.querySelector("[data-banner-undo]").addEventListener("click", () => {
    dismissBanner(el);
    try { actionFn(); } catch (e) { showToast("撤销失败: " + e.message); }
  });
  el.__bannerTimer = setTimeout(() => dismissBanner(el), ms);
  return el;
}

/**
 * 应用内确认框（替代原生 confirm —— 面板沙箱未授予 allow-modals，
 * 原生 confirm 恒返回 false，会导致「未保存提示」把用户锁死在配置页）。
 */
function sandboxConfirm(msg, callback, isDanger = false) {
  const scrim = $("#alertScrim");
  const okBtn = $("#alertOk");
  const cancelBtn = $("#alertCancel");
  const confirmWord = arguments[3];

  $("#alertTitle").textContent = isDanger ? "危险操作确认" : "操作确认";
  $("#alertMsg").textContent = msg || "";
  $("#alertBody").innerHTML = "";

  cancelBtn.hidden = false;
  okBtn.disabled = false;

  if (isDanger && confirmWord) {
    const expected = String(confirmWord).trim().split(/\s+/)[0].normalize("NFC");
    $("#alertBody").innerHTML =
      `<div class="danger-box" style="margin-bottom:var(--sp-3)">` +
      `此操作<b>不可逆</b>。请输入确认词的第一个单词 <b>${esc(expected)}</b> 以继续：</div>` +
      `<input class="field field--wide" id="dangerInput" placeholder="请输入" autocomplete="off">` +
      `<div class="hint" id="dangerHint" style="color:var(--red-text);display:none">输入不匹配</div>`;
    okBtn.className = "btn btn--destructive";
    okBtn.textContent = "执行不可逆操作";
    okBtn.disabled = true;
    const input = $("#dangerInput");
    input.addEventListener("input", () => {
      const v = (input.value || "").trim().split(/\s+/)[0].normalize("NFC");
      const matched = !!v && v === expected;
      okBtn.disabled = !matched;
      $("#dangerHint").style.display = matched ? "none" : "block";
    });
    okBtn.onclick = () => {
      const v = ($("#dangerInput").value || "").trim().split(/\s+/)[0].normalize("NFC");
      if (v === expected) { closeAlert(); if (callback) callback(); }
    };
    openScrim(scrim);
    setTimeout(() => input.focus(), 60);
    return;
  }

  okBtn.className = isDanger ? "btn btn--destructive" : "btn btn--filled";
  okBtn.textContent = isDanger ? "删除" : "确定";
  okBtn.onclick = () => { closeAlert(); if (callback) callback(); };
  openScrim(scrim);
  setTimeout(() => okBtn.focus(), 60);
};

/** 应用内输入框（替代原生 prompt） */
function sandboxPrompt(msg, def, callback) {
  const scrim = $("#alertScrim");
  $("#alertTitle").textContent = "请输入";
  $("#alertMsg").textContent = msg || "";
  $("#alertBody").innerHTML = `<input class="field field--wide" id="promptInput" value="${esc(def || "")}" autocomplete="off">`;

  const okBtn = $("#alertOk");
  const cancelBtn = $("#alertCancel");
  cancelBtn.hidden = false;
  okBtn.disabled = false;
  okBtn.className = "btn btn--filled";
  okBtn.textContent = "确定";
  okBtn.onclick = () => {
    const val = ($("#promptInput").value || "").trim();
    closeAlert();
    if (callback) callback(val);
  };
  openScrim(scrim);
  setTimeout(() => { const i = $("#promptInput"); if (i) { i.focus(); i.select(); } }, 60);
};

function closeAlert() { closeScrim($("#alertScrim")); }

/** 长错误详情：短消息用横幅，长消息用对话框完整展示。 */
function showErrorDetail(title, msg) {
  if (!msg) { showToast(title, "error"); return; }
  if (msg.length <= 90) { showToast(`${title}：${msg}`, "error"); return; }
  const scrim = $("#alertScrim");
  $("#alertTitle").textContent = title;
  $("#alertMsg").textContent = "";
  $("#alertBody").innerHTML =
    `<div class="danger-box" style="max-height:46vh;overflow:auto">` +
    `<pre class="mono fs-13" style="white-space:pre-wrap;word-break:break-word;margin:0">${esc(msg)}</pre></div>`;
  const okBtn = $("#alertOk");
  okBtn.disabled = false;
  okBtn.className = "btn btn--filled";
  okBtn.textContent = "知道了";
  okBtn.onclick = closeAlert;
  $("#alertCancel").hidden = true;
  openScrim(scrim);
}

window.sandboxConfirm = sandboxConfirm;
window.sandboxPrompt = sandboxPrompt;
