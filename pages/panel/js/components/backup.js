/* components/backup.js — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */
import { api, apiPost } from "../api.js";
import { showErrorDetail, showToast } from "../components/feedback.js";
import { loadGlobalSettings, loadHealth } from "../pages/config.js";
import { switchMemorySub } from "../pages/memory.js";
import { loadPersonas } from "../pages/persona.js";
import { loadRAG, loadRAGModelInfo } from "../pages/rag.js";
import { loadWB } from "../pages/worldbook.js";
import { loadWR } from "../pages/writing.js";
import { $, b64ToBlob, downloadBlob, withBusy } from "../utils.js";

export { backupExport, backupRestore };

/* ── 全量备份 ─────────────────────────────────────────────────────── */

async function backupExport(btn) {
  await withBusy(btn, "打包中…", async () => {
    try {
      const res = await api("/backup/export_base64");
      if (!res || !res.b64_data) throw new Error("备份数据为空");
      const blob = b64ToBlob(res.b64_data, "application/zip");
      const ts = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16);
      downloadBlob(blob, res.filename || `quill_backup_${ts}.zip`);
      showToast("备份已下载", "success");
    } catch (e) { showToast("备份失败：" + e.message); }
  });
}

async function backupRestore(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const btn = input.closest(".file-btn");
  await withBusy(btn, "恢复中…", async () => {
    try {
      // 走 base64 + JSON：面板沙箱无法携带鉴权直连 fetch，只能经 bridge
      const buf = await file.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let bin = "";
      const CHUNK = 0x8000;
      for (let i = 0; i < bytes.length; i += CHUNK) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
      }
      const data = await apiPost("/backup/restore_base64", { b64_data: btoa(bin) });
      const msg = (data && (data.message || (data.data && data.data.message))) || "恢复完成";
      showToast(msg, "success");
      // 恢复后各模块内存态已过期，全部重新拉取
      loadWR(); loadWB(); loadPersonas(); loadRAG(); loadRAGModelInfo();
      switchMemorySub("overview");
      loadGlobalSettings(); loadHealth();
    } catch (e) { showErrorDetail("恢复失败", e.message); }
    finally { input.value = ""; }
  });
}

/* ══ K. 配置 ═════════════════════════════════════════════════════════ */
