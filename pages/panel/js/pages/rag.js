/* pages/rag.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { S } from "../state.js";
import { api, apiPost } from "../api.js";
import { sandboxConfirm, showToast } from "../components/feedback.js";
import { renderEmptyState } from "../components/modal.js";
import { updateBadge } from "../pages/config.js";
import { $, esc, fileToB64, withBusy } from "../utils.js";

export { deleteRAGDocument, getRAGConfig, loadRAG, loadRAGModelInfo, processRAGFiles, providerRows, testRAGSearch };




async function getRAGConfig() {
  if (S._ragConfig && Date.now() - S._ragConfigAt < 60000) return S._ragConfig;
  S._ragConfig = await api("/rag/config");
  S._ragConfigAt = Date.now();
  return S._ragConfig;
}

function providerRows(cfg) {
  const emb = cfg.embedding || {};
  const rr = cfg.rerank || {};
  return `<div class="prov-row"><span class="prov-key">Embedding</span><span class="prov-val">${esc(emb.type || "local")}（${esc(emb.provider_id || "BAAI/bge-small-zh-v1.5")}）</span></div>
    <div class="prov-row"><span class="prov-key">Rerank</span><span class="prov-val">${rr.has_rerank_provider ? esc(rr.rerank_provider_id) : "未配置"}</span></div>`;
}

async function loadRAG() {
  try {
    const data = await api("/rag/documents");
    const docs = data.documents || [];
    const host = $("#ragDocList");
    host.innerHTML = docs.length
      ? docs.map(d => `<div class="doc-row">
          <div class="grow">
            <div class="doc-name">${esc(d.source)}</div>
            <div class="doc-meta">${d.chunk_count || 0} 个分块</div>
          </div>
          <button type="button" class="btn btn--sm btn--destructive-tinted" data-action="rag-delete" data-value="${esc(d.source)}">删除</button>
        </div>`).join("")
      : renderEmptyState("doc", "还没有上传文档", "支持 .txt / .md / .pdf，拖拽或点击上方区域上传",
          '<button type="button" class="btn btn--gray" data-action="rag-pick">选择文件</button>');
    const totalDocs = docs.length;
    const totalChunks = docs.reduce((s, d) => s + (d.chunk_count || 0), 0);
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set("#ragStatDocs", totalDocs);
    set("#ragStatChunks", totalChunks);
    set("#ragStatVectors", totalChunks); // 向量数与分块数一一对应
    const sub = $("#ragSub");
    if (sub) sub.textContent = totalDocs ? `· ${totalDocs} 个文档 · ${totalChunks} 个分块` : "";
    updateBadge("badge-rag", totalDocs);
  } catch (e) {
    $("#ragDocList").innerHTML = renderEmptyState("doc", "加载失败", e.message);
  }
}

async function loadRAGModelInfo() {
  try {
    const cfg = await getRAGConfig();
    $("#ragModelInfo").innerHTML = providerRows(cfg);
  } catch (_) { /* 模型信息不可用时不阻塞页面 */ }
}

async function processRAGFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const status = $("#ragUploadStatus");
  let ok = 0, fail = 0, chunks = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    status.textContent = `正在上传并向量化（${i + 1}/${files.length}）：${f.name}`;
    try {
      const b64 = await fileToB64(f);
      const res = await api("/rag/upload_base64", { method: "POST", body: JSON.stringify({ source: f.name, b64_data: b64 }) });
      ok++;
      chunks += (res.chunk_count || 0);
    } catch (e) {
      fail++;
      console.error("[Quill] 文档上传失败:", f.name, e);
    }
  }
  status.innerHTML = fail === 0
    ? `<span style="color:var(--green-text)">✓ 已处理 ${ok} 个文件，生成 ${chunks} 个分块</span>`
    : `<span style="color:var(--orange-text)">完成：${ok} 成功，${fail} 失败（详见控制台）</span>`;
  loadRAG();
}

function deleteRAGDocument(source, btn) {
  sandboxConfirm(`确定删除文档「${source}」及其全部分块？`, async () => {
    await withBusy(btn, "", async () => {
      try {
        await apiPost("/rag/delete", { source });
        showToast("已删除", "success");
        loadRAG();
      } catch (e) { showToast(e.message); }
    });
  }, true);
}

async function testRAGSearch(btn) {
  const q = ($("#ragQuery")?.value || "").trim();
  const topK = parseInt($("#ragTopK")?.value, 10) || 5;
  const host = $("#ragResults");
  if (!q) { host.innerHTML = '<div class="fs-13 t3">请输入查询内容</div>'; return; }
  await withBusy(btn, "检索中…", async () => {
    host.innerHTML = '<div class="skeleton skeleton-row"></div>';
    try {
      const data = await apiPost("/rag/search", { query: q, top_k: topK });
      const items = data.results || [];
      host.innerHTML = items.length
        ? items.map((it, i) => `<div class="result-card">
            <div class="result-head">
              <span class="result-rank">${i + 1}</span>
              <span class="result-source">${esc(it.source)}</span>
              <span class="badge">块 #${it.chunk_index} · ${esc(it.content).length} 字符</span>
              <span class="result-score">相关度 ${Number(it.score || 0).toFixed(4)}${it.rerank_score !== undefined ? ` · 重排 ${Number(it.rerank_score).toFixed(4)}` : ""}</span>
            </div>
            <div class="result-body">${esc(it.content)}</div>
          </div>`).join("")
        : renderEmptyState("search", "没有匹配结果", "换个检索词，或调整返回结果数量");
    } catch (e) {
      host.innerHTML = `<div class="fs-13" style="color:var(--red-text)">${esc(e.message)}</div>`;
    }
  });
}

/* ══ J. 动态记忆 ═════════════════════════════════════════════════════ */
