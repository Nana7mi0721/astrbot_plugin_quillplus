/* components/listkeys.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";

export { initListKeys };

/* ── 列表方向键导航（roving tabindex） ─────────────────────────────── */

/** 列表方向键导航（roving tabindex）。
 *  进入列表后方向键移动焦点，Tab 离开时列表只占一个停留点。
 *  素材库/角色卡是网格布局，上下键跳一「行」（按实际列数）。
 *
 *  绑定用事件委托挂在 container 上：列表内容会被 renderWRGrid /
 *  renderPersonaPage / loadWB / loadMemoryBrowser 反复重建，而 container
 *  本身不换，因此重建后无需重新绑定。
 *  items() 过滤 offsetParent === null，天然跳过隐藏视图（非激活 tab、
 *  非激活记忆子页）里的项，网格列数也只在可见项上计算。 */
function initListKeys(container, itemSel, opts = {}) {
  if (!container) return;
  // 可见性判定：display:none 的祖先（未激活 tab / 记忆子页）下两者都为空，
  // 隐藏项就此排除，网格列数也只在可见项上计算。
  // <tr> 的 offsetParent 行为在浏览器间略有差异，故用 getClientRects 兜底。
  const visible = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
  const items = () => [...container.querySelectorAll(itemSel)].filter(visible);
  const cols = opts.grid ? () => {
    const list = items();
    if (list.length < 2) return 1;
    const firstTop = list[0].getBoundingClientRect().top;
    const n = list.findIndex(el => el.getBoundingClientRect().top > firstTop + 4);
    return n > 0 ? n : 1;
  } : () => 1;

  /** 把「当前项」标记搬到 el，并保证其余项都退回 -1（Tab 只停一次）。 */
  const setCurrent = (all, el) => {
    all.forEach(it => it.setAttribute("tabindex", "-1"));
    el.setAttribute("tabindex", "0");
  };

  container.addEventListener("keydown", (e) => {
    // 不抢输入控件的方向键（光标移动、select 选项切换、textarea 换行）
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    // 自绘下拉 / 右键菜单常挂在卡片内部（.ctx-menu 默认挂进 .wr-card），
    // 它们有自己的方向键逻辑，此时不该同时搬动列表焦点。
    if (S._selOpen || S._ctxOpen) return;
    if (t && t.closest && t.closest(".sel-menu, .ctx-menu")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    const list = items();
    if (!list.length) return;
    // 事件目标的所属项：焦点常落在卡片子元素（展开区 / 操作按钮）上，
    // 故先向上找最近的项，再退回「当前项」兜底。
    const owner = t && t.closest ? t.closest(itemSel) : null;
    let idx = owner ? list.indexOf(owner) : -1;
    if (idx < 0) idx = list.findIndex(el => el.getAttribute("tabindex") === "0");
    if (e.key === "Home") {
      e.preventDefault();
      const first = list[0];
      setCurrent(list, first);
      first.focus({ preventScroll: true });
      first.scrollIntoView({ block: "nearest" });
      return;
    }
    if (e.key === "End") {
      e.preventDefault();
      const last = list[list.length - 1];
      setCurrent(list, last);
      last.focus({ preventScroll: true });
      last.scrollIntoView({ block: "nearest" });
      return;
    }
    let step = 0;
    switch (e.key) {
      case "ArrowRight": step = 1; break;
      case "ArrowLeft": step = -1; break;
      case "ArrowDown": step = cols(); break;   // 列表视图 cols() 恒为 1
      case "ArrowUp": step = -cols(); break;
      default: return;
    }
    // 尚未进入列表（没有当前项）时，任一方向键都从首项起步
    if (idx < 0) idx = step > 0 ? -1 : list.length;
    const next = idx + step;
    if (next < 0 || next >= list.length) { e.preventDefault(); return; }   // 到边界就停住，不循环
    e.preventDefault();
    const el = list[next];
    setCurrent(list, el);
    el.focus({ preventScroll: true });
    el.scrollIntoView({ block: "nearest" });
  });

  // 默认全部 -1，只把「当前项」提为 0 —— Tab 进出整个列表只占一个停留点。
  // 列表此刻可能仍是骨架屏 / 空态，故用 MutationObserver 在内容重建后再补一次；
  // 即便补不上，方向键首次触发时也会把 tabindex 补齐（见上面的 idx<0 兜底）。
  const seed = () => {
    const list = items();
    if (!list.length) return;
    let cur = list.findIndex(el => el.getAttribute("tabindex") === "0");
    if (cur < 0) cur = 0;
    list.forEach((el, i) => el.setAttribute("tabindex", i === cur ? "0" : "-1"));
  };
  seed();
  if ("MutationObserver" in window) {
    const mo = new MutationObserver(() => { if (container.isConnected) seed(); else mo.disconnect(); });
    mo.observe(container, { childList: true });
  }
}
