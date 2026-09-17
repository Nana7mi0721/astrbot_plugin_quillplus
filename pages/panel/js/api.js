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

// 首次探测失败后置负缓存，避免每次请求固定等待超时


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
  const bridge = S._bridgeAbsent ? null : (getBridge() || await waitForBridge(4000));

  if (bridge && !opts.isFormData && !opts.isBlob) {
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
  if (!opts.isFormData) headers["Content-Type"] = "application/json";
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
      if (opts.isBlob) return await res.blob();

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
