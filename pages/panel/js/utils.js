/* utils.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */

export { $, $$, CAT_TONES, PLUGIN_NAME, b64ToBlob, catTone, compactNum, debounce, downloadBlob, downloadJSON, esc, fileToB64, fmtTime, formatSessionId, store, withBusy };

const PLUGIN_NAME = "astrbot_plugin_quillplus";
const $  = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

/* ── A. 工具函数 ───────────────────────────────────────────────────── */

/** HTML 转义。所有插入模板字符串的动态内容都必须经过它。 */
function esc(s) {
  return s == null ? "" : String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/**
 * 沙箱安全的存储。
 * 面板 iframe 为不透明源（sandbox 无 allow-same-origin），直接访问
 * localStorage 会抛 SecurityError；即便不抛，其内容也不会跨刷新留存。
 * 因此这里只作为会话内的降级缓存（会话结束即失效，不再写回插件侧）。
 */
const store = (() => {
  let mem = Object.create(null);
  let ok = false;
  try { const k = "__quill_probe__"; localStorage.setItem(k, "1"); localStorage.removeItem(k); ok = true; } catch (_) { ok = false; }
  return {
    available: ok,
    get(k) { try { return ok ? localStorage.getItem(k) : (k in mem ? mem[k] : null); } catch (_) { return k in mem ? mem[k] : null; } },
    set(k, v) { try { if (ok) localStorage.setItem(k, v); else mem[k] = v; } catch (_) { mem[k] = v; } },
    del(k) { try { if (ok) localStorage.removeItem(k); else delete mem[k]; } catch (_) { delete mem[k]; } },
  };
})();

/** 数字转紧凑显示（超过 1 万用 k 结尾），用于统计与徽章。 */
function compactNum(n) {
  const v = Number(n) || 0;
  return v >= 10000 ? (v / 1000).toFixed(v >= 100000 ? 0 : 1) + "k" : String(v);
}

/** 分类色：由名称散列出稳定索引，映射到固定 8 色调色板。
    不用任意色相 —— 白字对比度不可控，且会与语义色（绿=成功/红=危险）撞车。 */
const CAT_TONES = ["accent", "green", "orange", "yellow", "indigo", "purple", "teal", "pink"];
function catTone(name) {
  if (!name) return "accent";
  let h = 0;
  for (let i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
  return CAT_TONES[Math.abs(h) % CAT_TONES.length];
}

/** 会话 ID 美化：aiocqhttp:GroupMessage:123::persona → 群 123 / persona */
function formatSessionId(sid) {
  if (!sid) return "—";
  const [target = "", persona = ""] = String(sid).split("::");
  const parts = target.split(":");
  let scope = target;
  if (parts.length >= 3) {
    const type = parts[parts.length - 2];
    const id = parts[parts.length - 1];
    if (type === "GroupMessage") scope = `群 ${id}`;
    else if (type === "PrivateMessage") scope = `私聊 ${id}`;
    else scope = `${type} ${id}`;
  }
  return persona ? `${scope} / ${persona}` : scope;
}

/** ISO 时间戳 → 可读文本 */
function fmtTime(ts) {
  return (ts || "").split(".")[0].replace("T", " ") || "—";
}

/** 下载 JSON 文件 */
function downloadJSON(data, filename) {
  downloadBlob(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }), filename);
}

/** 下载二进制内容。沙箱允许 downloads，用临时 <a> 触发。 */
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** base64 → Blob（用于从 base64 端点下载文件） */
function b64ToBlob(b64, mime) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], mime ? { type: mime } : undefined);
}

/** File → base64（不含 data URL 前缀）。分块避免超长参数栈溢出。 */
function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });
}

/** 按钮忙碌态：禁用 + 转圈，结束后恢复原内容。 */
async function withBusy(el, busyText, fn) {
  if (!el) return fn();
  // 非表单控件（如 .menu-wrap 容器）不能禁用也不该换内容：
  // 替换 innerHTML 会让容器高度塌陷，整页跟着跳。改用 aria-busy 标记。
  const isFormControl = typeof el.disabled === "boolean";
  if (!isFormControl) {
    el.setAttribute("aria-busy", "true");
    el.classList.add("is-busy");
    try { return await fn(); }
    finally { el.removeAttribute("aria-busy"); el.classList.remove("is-busy"); }
  }
  const orig = el.innerHTML;
  const wasDisabled = el.disabled;
  el.disabled = true;
  if (busyText) el.innerHTML = `<span class="spinner"></span><span>${esc(busyText)}</span>`;
  try { return await fn(); }
  finally {
    el.disabled = wasDisabled;
    el.innerHTML = orig;
  }
}
