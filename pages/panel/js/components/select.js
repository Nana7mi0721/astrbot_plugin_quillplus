/* components/select.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { closeCtxMenu } from "../components/contextmenu.js";
import { $, $$, esc } from "../utils.js";

export { _selUid, applySelWidth, closeSelMenus, closeSelMenusImmediate, enhanceSelect, enhanceSelectAsSegmented, enhanceSelects, pruneSelMenus, selButtonLabel, selOptionList };

/* ── M. 自绘下拉（原生 select 增强） ────────────────────────────────── */
/* 契约：原生 <select> 仍是唯一的值存储与数据源，填充 / 读取代码（
   fillProviderSelect、modeSelect、loadWRCategories、loadMemorySessions、
   applySettings…）一律不改。增强层只做三件事：
     1. 用 .sel-wrap 包住 select 并插入触发器 / 浮层；
     2. 遮蔽实例上的 value（读写双向同步触发器文字）；
     3. 选中时写回 select.value 后派发 change，复用既有 change 委托。
   select 只在增强完全成功后 hidden，失败则回退原生外观。             */

let _selUid = 0;


/** 关闭已展开的下拉（except 为当前要打开的 .sel-wrap）。 */
function closeSelMenus(except) {
  if (S._selOpen && S._selOpen.wrap !== except) S._selOpen.close();
}

/** 立刻收起展开中的下拉，不做出场动画（滚动 / 尺寸变化等会移动锚点的场景）。 */
function closeSelMenusImmediate() {
  if (S._selOpen) S._selOpen.close(true);
}

/** 取出 select 的选项快照（value / 文本 / 禁用）。 */
function selOptionList(sel) {
  return $$("option", sel).map((o, i) => ({
    value: o.value,
    label: (o.textContent || "").replace(/\s+/g, " ").trim() || o.value,
    disabled: !!o.disabled,
    i,
  }));
}

/** 触发器上显示的文字：优先匹配当前值，否则退回原始值 / 首项。 */
function selButtonLabel(sel) {
  const opts = $$("option", sel);
  const hit = opts.find(o => o.value === sel.value);
  if (hit) return (hit.textContent || "").replace(/\s+/g, " ").trim() || hit.value;
  if (sel.value) return String(sel.value);
  return opts.length ? ((opts[0].textContent || "").trim() || opts[0].value) : "";
}

/** 把标注了 data-seg 的「少量固定选项」下拉改成分段控件。
 *  契约与 enhanceSelect 一致：原生 select 仍是唯一值存储，点击分段写
 *  sel.value 后派发 change，因此所有填充 / 读取代码无需改动。
 *  仅用于互斥、无禁用项、且选项数 2–3 的枚举（HIG：分段控件要求全部选项
 *  可见；选项多或含禁用项时下拉更合适，此时返回 null 交回 enhanceSelect）。 */
function enhanceSelectAsSegmented(sel) {
  if (!sel || sel._selApi || !sel.parentNode) return null;
  const opts = selOptionList(sel);
  if (opts.length < 2 || opts.length > 3 || opts.some(o => o.disabled)) return null;

  const wrap = document.createElement("span");
  wrap.className = "sel-wrap sel-seg";
  wrap.setAttribute("role", "radiogroup");
  const shortOf = (o, i) => {
    const el = $$("option", sel)[i];
    const s = el && el.dataset ? (el.dataset.short || "").trim() : "";
    return s || o.label;
  };
  wrap.innerHTML = opts.map((o, i) =>
    `<button type="button" role="radio" data-value="${esc(o.value)}"` +
    ` class="${o.value === sel.value ? "is-selected" : ""}"` +
    ` aria-checked="${o.value === sel.value ? "true" : "false"}"` +
    ` title="${esc(o.label)}">${esc(shortOf(o, i))}</button>`
  ).join("");

  const btns = $$("button", wrap);
  const sync = () => btns.forEach(b => {
    const on = b.dataset.value === sel.value;
    b.classList.toggle("is-selected", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });

  const parent = sel.parentNode;
  parent.insertBefore(wrap, sel);
  sel.hidden = true;
  sel.setAttribute("aria-hidden", "true");

  wrap.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.value === sel.value) return;
    sel.value = b.dataset.value;
    sync();
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });
  // 键盘：左右方向键在分段间移动（HIG 分段控件行为），Home/End 到首尾
  wrap.addEventListener("keydown", (e) => {
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    let j = -1;
    if (e.key === "ArrowRight") j = (i + 1) % btns.length;
    else if (e.key === "ArrowLeft") j = (i - 1 + btns.length) % btns.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = btns.length - 1;
    if (j < 0) return;
    e.preventDefault();
    btns[j].focus();
    btns[j].click();
  });

  const api = {
    sel, wrap, sync, dispose: () => { wrap.remove(); sel.hidden = false; },
  };

  // 与下拉同款：遮蔽实例 value，让 applySettings 等既有读写路径零改动同步选中态
  const protoDesc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(sel), "value") ||
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  if (protoDesc && protoDesc.get && protoDesc.set) {
    Object.defineProperty(sel, "value", {
      configurable: true,
      enumerable: true,
      get() { return protoDesc.get.call(this); },
      set(v) { protoDesc.set.call(this, v); sync(); },
    });
  }

  sel._selApi = api;
  sel.dataset.enhanced = "1";
  // 选项被外部改写（如后端下发新配置）时同步选中态，与下拉的 MutationObserver 对齐
  new MutationObserver(sync).observe(sel, { childList: true, subtree: true, attributes: true });
  return api;
}

/** 把原生 select 的宽度意图复制到包装元素（内联 width 优先）。
    配置页内不再写内联 maxWidth —— 宽度交给 .cfg-field .sel-wrap 的
    三档令牌（--w-ctl / --w-wide），否则 JS 与 CSS 两处定义会再次分叉。 */
function applySelWidth(sel, wrap) {
  const inlineW = (sel.style.width || "").trim();
  if (inlineW && inlineW !== "auto") { wrap.style.width = inlineW; return; }   // modeSelect: 150px
  if (inlineW === "auto") return;                                              // #wrCat：按内容收缩
  wrap.style.width = "100%";
}

/** 增强单个原生 select。返回控制器或 null（已增强 / 不可增强）。 */
function enhanceSelect(sel) {
  if (!sel || sel.tagName !== "SELECT" || !sel.parentNode) return null;
  if (sel._selApi) return sel._selApi;

  const parent = sel.parentNode;
  const wrap = document.createElement("span");
  wrap.className = "sel-wrap";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sel-btn";
  btn.setAttribute("role", "combobox");
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  btn.innerHTML = '<span class="sel-label"></span><svg class="icon" aria-hidden="true"><use href="#i-chevron"/></svg>';
  const labelEl = btn.querySelector(".sel-label");

  const menu = document.createElement("div");
  menu.className = "sel-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  const menuId = "selmenu-" + (_selUid++);
  menu.id = menuId;
  btn.setAttribute("aria-controls", menuId);

  let hlIdx = -1;
  let closeTimer = 0;
  let typeBuf = "";
  let typeTimer = 0;
  let mo = null;               // options 观察器；随锚点销毁一并 disconnect

  const items = () => $$(".sel-opt", menu);

  const syncBtn = () => {
    const text = selButtonLabel(sel);
    labelEl.textContent = text;
    btn.title = text;
    btn.disabled = !!sel.disabled;
  };

  const buildOptions = () => {
    menu.innerHTML = selOptionList(sel).map((it, idx) => {
      const on = !it.disabled && it.value === sel.value;
      return '<button type="button" class="sel-opt" role="option" id="' + menuId + '-o' + idx +
        '" data-value="' + esc(it.value) + '"' +
        ' aria-selected="' + (on ? "true" : "false") + '"' +
        (it.disabled ? ' aria-disabled="true"' : "") + '>' +
        '<span class="sel-opt-label">' + esc(it.label) + "</span>" +
        (on ? '<svg class="icon" aria-hidden="true"><use href="#i-check"/></svg>' : "") +
        "</button>";
    }).join("");
    hlIdx = -1;
  };

  const scrollHlIntoView = () => {
    const el = items()[hlIdx];
    if (!el) return;
    const t = el.offsetTop, h = el.offsetHeight;
    if (t < menu.scrollTop) menu.scrollTop = t - 4;
    else if (t + h > menu.scrollTop + menu.clientHeight) menu.scrollTop = t + h - menu.clientHeight + 4;
  };

  const setHl = (idx) => {
    const list = items();
    if (!list.length || idx < 0) return;
    hlIdx = (idx % list.length + list.length) % list.length;
    list.forEach((el, i) => el.classList.toggle("is-hl", i === hlIdx));
    const cur = list[hlIdx];
    btn.setAttribute("aria-activedescendant", cur.id);
    scrollHlIntoView();
  };

  /** 从 from 出发按 dir 找下一个可用项（跳过 disabled）。 */
  const step = (from, dir) => {
    const list = items();
    const n = list.length;
    if (!n) return -1;
    let j = from;
    for (let k = 0; k < n; k++) {
      j = (j + dir + n) % n;
      if (list[j].getAttribute("aria-disabled") !== "true") return j;
    }
    return from;
  };

  /** 首字母跳转（连续键入按前缀匹配，超时重置）。 */
  const jumpTo = (buf) => {
    const list = items();
    const n = list.length;
    if (!n) return;
    const from = hlIdx;
    const probe = (q) => {
      for (let k = 1; k <= n; k++) {
        const j = ((from + k) % n + n) % n;
        if (list[j].getAttribute("aria-disabled") === "true") continue;
        if ((list[j].textContent || "").trim().toLowerCase().startsWith(q)) return j;
      }
      return -1;
    };
    let j = probe(buf);
    if (j < 0 && buf.length > 1) j = probe(buf.slice(-1));
    if (j >= 0) setHl(j);
  };

  const place = () => {
    const r = btn.getBoundingClientRect();
    menu.style.minWidth = Math.round(r.width) + "px";
    // 窄触发器不至于把长文案压成省略号；上限避免长文案撑破视口
    menu.style.maxWidth = Math.round(Math.min(Math.max(r.width, 200), window.innerWidth - 16)) + "px";
    const mh = menu.offsetHeight;
    const mw = menu.offsetWidth;
    let top = r.bottom + 4;
    let up = false;
    if (top + mh > window.innerHeight - 8 && r.top - 4 - mh > 8) { up = true; top = r.top - 4 - mh; }
    let left = r.left;
    if (left + mw > window.innerWidth - 8) left = Math.max(8, r.right - mw);
    menu.style.top = Math.round(Math.max(8, top)) + "px";
    menu.style.left = Math.round(Math.max(8, left)) + "px";
    menu.classList.toggle("is-up", up);
  };

  const syncIfOpen = () => { if (isOpen()) place(); };

  // 出场动画期间的 .is-out 视为「已关闭」，否则快速二次点击会被当成关闭而不响应
  const isOpen = () => !menu.hidden && !menu.classList.contains("is-out");

  const open = () => {
    if (btn.disabled) return;
    if (!menu.hidden && !menu.classList.contains("is-out")) return;
    clearTimeout(closeTimer);
    menu.classList.remove("is-out");
    closeSelMenus(wrap);
    closeCtxMenu();
    buildOptions();
    menu.hidden = false;
    S._selOpen = { wrap, close };
    let hl = items().findIndex(el => el.getAttribute("aria-selected") === "true" && el.getAttribute("aria-disabled") !== "true");
    if (hl < 0) hl = items().findIndex(el => el.getAttribute("aria-disabled") !== "true");
    if (hl >= 0) setHl(hl);
    btn.setAttribute("aria-expanded", "true");
    place();
    if (hl >= 0) scrollHlIntoView();
    // 浮层挂在 body 下，锚点变化时自己跟（外部滚动则关闭，见 bindOverlayEvents）
    window.addEventListener("scroll", syncIfOpen, true);
    window.addEventListener("resize", syncIfOpen);
    requestAnimationFrame(() => { if (isOpen()) menu.classList.add("is-in"); });
  };

  const close = (immediate) => {
    if (menu.hidden || menu.classList.contains("is-out")) return;
    window.removeEventListener("scroll", syncIfOpen, true);
    window.removeEventListener("resize", syncIfOpen);
    menu.classList.remove("is-in");
    menu.classList.add("is-out");
    btn.setAttribute("aria-expanded", "false");
    btn.removeAttribute("aria-activedescendant");
    if (S._selOpen && S._selOpen.wrap === wrap) S._selOpen = null;
    hlIdx = -1;
    if (immediate) { clearTimeout(closeTimer); menu.hidden = true; menu.classList.remove("is-out"); return; }
    clearTimeout(closeTimer);
    closeTimer = setTimeout(() => {
      if (menu.classList.contains("is-out")) { menu.hidden = true; menu.classList.remove("is-out"); }
    }, 200);
  };

  const choose = (el) => {
    const v = el.dataset.value;
    const changed = sel.value !== v;
    close();
    if (changed) {
      sel.value = v;                                    // 被遮蔽的 setter：原型写入 + 触发器同步
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    btn.focus({ preventScroll: true });
  };

  // ── 事件 ──────────────────────────────────────────────────────────
  // 按下即响应（HIG），不等 click / mouseup
  btn.addEventListener("pointerdown", (e) => {
    if (btn.disabled) return;
    e.preventDefault();                                  // 阻止默认聚焦，手动接管，避免焦点闪到 <html>
    btn.focus({ preventScroll: true });                  // 鼠标唤起后键盘可继续操作
    if (isOpen()) close(); else open();
  });

  menu.addEventListener("pointerdown", (e) => {
    const opt = e.target.closest(".sel-opt");
    if (!opt || opt.getAttribute("aria-disabled") === "true") return;
    e.preventDefault();
    choose(opt);
  });

  btn.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const opened = isOpen();
    switch (e.key) {
      case "Enter":
      case " ":
        e.preventDefault();
        if (!opened) { open(); return; }
        { const cur = items()[hlIdx]; if (cur && cur.getAttribute("aria-disabled") !== "true") choose(cur); else close(); }
        return;
      case "ArrowDown":
        e.preventDefault();
        if (!opened) { open(); return; }
        setHl(step(hlIdx, 1)); return;
      case "ArrowUp":
        e.preventDefault();
        if (!opened) { open(); return; }
        setHl(step(hlIdx, -1)); return;
      case "Home":
        if (!opened) return;
        e.preventDefault();
        setHl(items().findIndex(el => el.getAttribute("aria-disabled") !== "true")); return;
      case "End":
        if (!opened) return;
        e.preventDefault();
        setHl(items().map(el => el.getAttribute("aria-disabled") === "true" ? -1 : 0).lastIndexOf(0)); return;
      case "Escape":
        if (!opened) return;
        e.preventDefault();
        e.stopPropagation();                             // 不要连带关掉外层弹窗
        close();
        return;
      case "Tab":
        close(true);
        return;
      default: break;
    }
    // 首字母 / 数字跳转（只认字母数字，避免抢走 "/" 聚焦搜索框等全局快捷键）
    if (/^[a-z0-9]$/i.test(e.key)) {
      typeBuf += e.key.toLowerCase();
      clearTimeout(typeTimer);
      typeTimer = setTimeout(() => { typeBuf = ""; }, 600);
      if (!opened) open();
      jumpTo(typeBuf);
    }
  });

  try {
    // 宽度意图 → 包装元素；DOM 重排完成后再隐藏原生控件
    parent.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    wrap.appendChild(btn);
    applySelWidth(sel, wrap);
    // 浮层挂到 body：#wrCat 位于带 backdrop-filter 的 .toolbar 内，那会成为
    // position:fixed 的包含块，坐标与裁剪都会出错；body 下则是纯视口坐标。
    menu.style.top = "-9999px";
    menu.style.left = "-9999px";
    document.body.appendChild(menu);
    buildOptions();
    syncBtn();

    // 遮蔽实例 value：applySettings / loadMemoryBrowser 等读写路径零改动即同步
    const protoDesc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(sel), "value") ||
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
    if (protoDesc && protoDesc.get && protoDesc.set) {
      Object.defineProperty(sel, "value", {
        configurable: true,
        enumerable: true,
        get() { return protoDesc.get.call(this); },
        set(v) { protoDesc.set.call(this, v); syncBtn(); },
      });
    }

    // options 被重建（fillProviderSelect / loadWRCategories / loadMemorySessions）时同步菜单与文字
    mo = new MutationObserver(() => { buildOptions(); syncBtn(); });
    mo.observe(sel, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["disabled"] });

    sel.dataset.enhanced = "1";
    sel.hidden = true;                                   // 增强成功后才隐藏 → 失败自动回退原生外观
  } catch (err) {
    sel.hidden = false;
    delete sel.dataset.enhanced;
    if (mo) mo.disconnect();
    if (menu.parentNode) menu.remove();
    if (wrap.parentNode) {
      wrap.parentNode.insertBefore(sel, wrap);
      wrap.remove();
    }
    return null;
  }

  const api = {
    open, close, wrap, sel, btn, menu,
    sync: () => { buildOptions(); syncBtn(); },
    dispose: () => {
      if (mo) { mo.disconnect(); mo = null; }
      clearTimeout(closeTimer);
      window.removeEventListener("scroll", syncIfOpen, true);
      window.removeEventListener("resize", syncIfOpen);
      menu.remove();
    },
  };
  sel._selApi = api;
  menu._selApi = api;          // 供 pruneSelMenus 回收孤儿浮层
  return api;
}

/** 回收锚点已被销毁的浮层。模态框每次打开都会重建 innerHTML，
 *  对应的 select 与包装元素一起消失，挂在 body 下的菜单需要显式清理。 */
function pruneSelMenus() {
  $$("body > .sel-menu").forEach(menu => {
    const api = menu._selApi;
    if (!api || !api.sel.isConnected) {
      if (api) api.dispose(); else menu.remove();
      if (S._selOpen && S._selOpen.wrap === (api && api.wrap)) S._selOpen = null;
    }
  });
}

/** 增强容器内所有原生下拉（幂等）。
 *  data-seg 标记者优先尝试分段控件；不满足条件时自动落回自绘下拉。 */
function enhanceSelects(root) {
  pruneSelMenus();
  $$("select.field", root || document).forEach(sel => {
    if (sel._selApi) return;
    if (sel.dataset.seg && enhanceSelectAsSegmented(sel)) return;
    enhanceSelect(sel);
  });
}
