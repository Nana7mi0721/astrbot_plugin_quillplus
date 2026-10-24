/* api.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "./state.js";
import { $, PLUGIN_NAME } from "./utils.js";

export { api, apiPost, getBridge, waitForBridge };

/* ── B. API 通信层 ─────────────────────────────────────────────────── */

/**
 * 取桥接器。独立打开（非 iframe）时 bridge 虽被注入但无人应答，
 * 会让所有请求永久挂起 —— 此时返回 null 走原生 fetch。
 */
function getBridge() {
  if (window.parent === window) return null;
  return window.AstrBotPluginPage || null;
}

async function waitForBridge(timeoutMs = 4000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const b = getBridge();
    if (b) return b;
    await new Promise(r => setTimeout(r, 16));
  }
  return null;
}

/**
 * L6 修复：探测失败必须置**持续的**负缓存。
 *
 * 原实现只在「独立打开」（`window.parent === window`）时置 S._bridgeAbsent；
 * 而在 iframe 内 bridge 始终不注入（宿主版本不匹配 / SDK 脚本被拦 / 注入时机
 * 早于 SDK 就绪）时，每个请求都要把 4000ms 等满，面板表现为「点任何按钮先转
 * 4 秒，再全部报 API 失败」，首页并发十几个请求时更明显。
 * 探测是幂等的：一次拿不到就是拿不到。页面重新加载会重置该内存标记，
 * 不会掩盖「宿主后来才就绪」的情况。
 */
async function resolveBridge() {
  if (S._bridgeAbsent) return null;
  const b = getBridge() || await waitForBridge(4000);
  if (!b && !S._bridgeAbsent) {
    S._bridgeAbsent = true;
    console.warn("[Quill] 未检测到面板桥接器，本页后续请求改用直连 fetch");
  }
  return b;
}

/**
 * 统一请求入口。
 * 1) bridge 可用 → apiGet / apiPost（面板沙箱内唯一能携带鉴权的通道）
 * 2) 否则 → 原生 fetch，含多候选 URL 降级与 AstrBot envelope 解包
 */
async function api(path, opts = {}) {
  const np = String(path).replace(/^\/+/, "");
  const [bp, query] = np.split("?");
  // 独立打开（非 iframe）时 bridge 虽被注入但无人应答，等待只会白等满超时，
  // 首个请求会因此延迟数秒 —— 此时直接判定不可用，置负缓存走 fetch。
  if (!S._bridgeAbsent && window.parent === window) {
    S._bridgeAbsent = true;
    console.info("[Quill] 独立面板模式：bridge 不可用，后续请求直连 fetch");
  }
  // L8：bridge 分支此前还挂着 `!opts.isFormData && !opts.isBlob` 两个条件，
  // 但全仓（含所有页面与组件）没有任何调用点传过这两个开关：面板上传一律先
  // 转 base64 再走 JSON（沙箱不让跨源读 File 流），下载一律走 `*_export_base64`
  // 拿 base64 后自行组 Blob。这两个条件是永不生效的死分支，且会让读代码的人
  // 误以为存在 multipart 通道。下方 fetch 降级路径中的同名判断保留——那是真正
  // 会用到的路径（直连模式下的表单/二进制响应）。
  const bridge = await resolveBridge();

  if (bridge) {
    const method = (opts.method || "GET").toUpperCase();
    let result;
    if (method === "GET") {
      const params = { _t: Date.now() };
      if (query) new URLSearchParams(query).forEach((v, k) => { params[k] = v; });
      result = await bridge.apiGet(bp, params);
    } else {
      let bodyObj = {};
      if (opts.body) { try { bodyObj = JSON.parse(opts.body); } catch (_) { bodyObj = {}; } }
      result = await bridge.apiPost(bp, bodyObj);
    }
    if (result && typeof result === "object" && "status" in result) {
      if (result.status === "error") throw new Error(result.message || "API 返回错误");
      return result.data;
    }
    return result;
  }

  const cfg = window.__QUILL_CONFIG__ || {};
  const pageOrigin = window.location?.origin || "";
  let origin = (cfg.astrbotOrigin || (pageOrigin && pageOrigin !== "null" ? pageOrigin : "")).replace(/\/+$/, "");
  if (!origin && document.referrer) {
    try { origin = new URL(document.referrer).origin; } catch (_) { /* 沙箱内 referrer 通常为空，忽略 */ }
  }

  const isPost = (opts.method || "GET").toUpperCase() === "POST";
  const headers = { ...opts.headers };
  // L8：`api()` 是纯 JSON 客户端——面板所有上传先转 base64、所有下载走
  // `*_export_base64` 后自行组 Blob，因此这里固定声明 JSON，不再维护
  // 永不生效的 multipart / blob 开关。
  headers["Content-Type"] = "application/json";
  try {
    const token = localStorage.getItem("token");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  } catch (_) { /* 不透明源下无 token，依赖 Cookie 鉴权 */ }

  const sep = np.includes("?") ? "&" : "?";
  const bust = isPost ? "" : `${sep}_t=${Date.now()}`;
  const arg = `${PLUGIN_NAME}/${np}`;
  const candidates = [
    `${origin}/api/plug/${arg}${bust}`,
    `${origin}/api/v1/plugins/extensions/${arg}${bust}`,
  ];
  if (cfg.desktopServer) candidates.push(`${origin}/api/${np}`);

  const errors = [];
  for (const url of candidates) {
    try {
      const res = await fetch(url, {
        method: isPost ? "POST" : "GET",
        headers,
        body: opts.body || undefined,
        credentials: "include",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // L8：同前——不再保留 `opts.isBlob` 分支；二进制导出统一由服务端
      // `*_export_base64` 返回 base64，页面自行 `b64ToBlob` 组合下载。
      let json = await res.json();
      // 剥离 AstrBot 外层 envelope
      if (json && json.code === 200 && json.data !== undefined) json = json.data;
      if (json && typeof json === "object" && "status" in json) {
        if (json.status === "error") throw new Error(json.message || "API 返回错误");
        return json.data !== undefined ? json.data : json;
      }
      return json;
    } catch (e) { errors.push(e.message); }
  }
  throw new Error(`[Quill] API 失败: ${errors.join("; ")}`);
}

function apiPost(url, body) {
  return api(url, { method: "POST", body: JSON.stringify(body) });
}
