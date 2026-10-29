/* M3 长任务面板：只轮询真实路由，离开页面即停止轮询。 */
import { api, apiPost } from "../api.js";
import { $, esc } from "../utils.js";
import { sandboxConfirm, showToast } from "../components/feedback.js";
let timer = null, serial = 0, storageLoading = false;
const statusNames = { queued:"等待中", running:"执行中", cancelling:"取消中", cancelled:"已取消", completed:"已完成", failed:"失败" };
const kindNames = { memory_summary:"增量总结", idle_reflection:"闲时反思", memory_import:"导入记忆", rag_reinitialize:"重建检索组件", memory_prune:"修剪记忆", chat_log_retention:"清理过期对话" };
const active = row => ["queued","running","cancelling"].includes(row.status);
const cardContent = (title, body) => `<div class="card-head"><h3>${esc(title)}</h3></div><div class="card-body"><div class="card-body-inner">${body}</div></div>`;
async function refreshStorage() {
  if (storageLoading) return;
  storageLoading = true;
  const host = $("#tasks-storage"); host.setAttribute("aria-busy","true");
  try {
    const storage = await api("storage/status");
    const rows = Object.entries(storage.stores || {}).map(([name,s]) => {
      const detail = `版本 ${s.version ?? s.schema_version ?? "未知"}${s.backup_verified ? " · 迁移备份已校验" : ""}${!s.available && s.error ? " · " + s.error : ""}`;
      return `<div class="cfg-row"><div class="cfg-row-text"><span class="cfg-row-title">${esc(({memory:"动态记忆",documents:"文档",writing:"素材"})[name] || name)}</span><span class="row-desc">${esc(detail)}</span></div><div class="cfg-row-control"><span class="badge ${s.available ? "badge--green" : "badge--red"}">${s.available ? "可用" : "不可用"}</span></div></div>`;
    }).join("");
    const pending=storage.pending_operations || [];
    const recovery=pending.length ? `<div class="note">有 ${pending.length} 项操作等待恢复。失败时保留阶段并暂停相关会话。</div><ul class="task-recovery">${pending.filter(r=>r.failure_count || r.error_class).map(r=>`<li>${esc(r.operation_id.slice(0,8))}：失败 ${esc(String(r.failure_count))} 次 · ${esc(r.error_class || "未知原因")}</li>`).join("")}</ul>` : "";
    host.innerHTML = cardContent("存储状态", rows + recovery + (storage.state_read_only ? '<p class="note">当前会话状态受版本保护，已拒绝写入原文件。</p>' : ""));
  } catch (error) { host.innerHTML = cardContent("存储状态", `<p class="row-desc">暂不可读：${esc(error.message)}</p>`); }
  finally { storageLoading = false; host.setAttribute("aria-busy","false"); }
}
export async function loadTasks() {
  clearTimeout(timer);
  const id = ++serial;
  const host = $("#tasks-list"); host.setAttribute("aria-busy","true");
  try {
    // 存储锁等待不能挡住任务列表与取消按钮。
    refreshStorage();
    const tasks = await api("tasks/list");
    if (id !== serial) return;
    const rows = tasks.items || [];
    host.innerHTML = cardContent("任务列表", `<div class="table-wrap"><table class="data" aria-label="后台任务"><thead><tr><th scope="col">任务</th><th scope="col">目标</th><th scope="col">状态</th><th scope="col">进度</th><th scope="col">结果</th><th scope="col">操作</th></tr></thead><tbody>${rows.length ? rows.map(row => {
      const result = row.result || {};
      const summary = Object.entries(result).filter(([key]) => ["processed","stored","imported","failed","duplicates"].includes(key)).map(([key,value]) => `${({processed:"处理",stored:"写入",imported:"新增",failed:"失败",duplicates:"重复"})[key]} ${typeof value === "boolean" ? Number(value) : value}`).join(" · ");
      return `<tr><td>${esc(kindNames[row.kind] || "后台处理")}<span class="task-id mono">${esc(row.task_id.slice(0,8))}</span></td><td><span class="cell-truncate task-target" title="${esc(row.key)}">${esc(row.key)}</span></td><td class="task-state"><span class="badge ${row.status==="failed" ? "badge--red" : row.status==="completed" ? "badge--green" : ""}">${esc(statusNames[row.status] || row.status)}</span></td><td>${row.progress} / ${row.total ?? "未知"}</td><td><span class="cell-truncate task-result" title="${esc(summary || row.message || "")}">${esc(summary || row.message || "暂无")}</span></td><td>${active(row) && row.status !== "cancelling" ? `<button type="button" class="btn btn--gray" data-action="tasks-cancel" data-task-id="${esc(row.task_id)}">取消</button>` : ""}</td></tr>`;
    }).join("") : '<tr><td colspan="6" class="t3" style="text-align:center">暂无后台任务</td></tr>'}</tbody></table></div>`);
    if (rows.some(active)) timer = setTimeout(() => {
      if (document.querySelector("#pane-tasks")?.classList.contains("is-active") && !document.hidden) loadTasks();
    }, 1500);
  } catch (error) {
    if (id === serial) host.innerHTML = cardContent("任务列表", `<p class="row-desc">任务查询失败：${esc(error.message)}</p>`);
  } finally { if (id===serial) host.setAttribute("aria-busy","false"); }
}
export function cancelTask(taskId) {
  sandboxConfirm("请求取消此任务？当前步骤结束后停止，已提交的结果会保留。", async () => {
    try { await apiPost("tasks/cancel",{task_id:taskId}); await loadTasks(); }
    catch (error) { showToast(error.message); }
  });
}
export async function inspectScope() {
  const umo = $("#scope-umo").value.trim(), persona = $("#scope-persona").value.trim();
  if (!umo) { showToast("请填写会话 UMO", "warning"); return; }
  const query = new URLSearchParams({umo});
  if (persona) query.set("persona_id",persona);
  try { $("#scope-result").textContent = JSON.stringify(await api("scope/resolve?" + query),null,2); }
  catch (error) { $("#scope-result").textContent = error.message; }
}
