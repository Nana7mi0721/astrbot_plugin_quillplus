/* M2：汇总与分页独立；未知不填零，费用按计价来源展示。 */
import { api, apiPost } from "../api.js";
import { $, esc } from "../utils.js";
import { enhanceSelects } from "../components/select.js";
import { sandboxConfirm, showToast } from "../components/feedback.js";

let offset = 0, total = 0, serial = 0;
const LIMIT = 50;
const fmt = value => value === null || value === undefined ? "未知" : Number(value).toLocaleString();
const percent = value => value === null || value === undefined ? "未知" : (value * 100).toFixed(1) + "%";
const SOURCE = { reported: "供应商报告", price_snapshot: "价格推算", price_snapshot_partial: "部分推算（可能低估）" };
const costs = values => Object.entries(values || {}).flatMap(([currency, buckets]) =>
  Object.entries(buckets).map(([source, amount]) => `${SOURCE[source] || source}：${currency} ${amount}`)).join(" · ") || "未知";
const card = (title, body) => `<div class="card"><div class="card-head"><h3>${esc(title)}</h3></div><div class="card-body"><div class="card-body-inner">${body}</div></div></div>`;
const table = (title, headers, rows) => `<div class="table-wrap"><table class="data" aria-label="${esc(title)}"><thead><tr>${headers.map(h => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.map(row => `<tr>${row.map(v => `<td><span class="cell-truncate" title="${esc(String(v))}">${esc(String(v))}</span></td>`).join("")}</tr>`).join("") : `<tr><td colspan="${headers.length}" class="t3 usage-empty">暂无调用记录</td></tr>`}</tbody></table></div>`;
const metric = (label, value) => `<div class="dash-card"><div class="dash-value">${esc(String(value))}</div><div class="dash-label">${esc(label)}</div></div>`;

function pagination(loading = false) {
  $('[data-action="usage-prev"]').disabled = loading || offset <= 0;
  $('[data-action="usage-next"]').disabled = loading || offset + LIMIT >= total;
  $("#usage-pagination").textContent = `${total ? offset + 1 : 0}–${Math.min(offset + LIMIT, total)} / ${total}`;
}

export async function loadUsage(reset = true) {
  if (reset) offset = 0;
  const id = ++serial;
  const params = new URLSearchParams({ days: $("#usage-days").value, limit: LIMIT, offset });
  for (const [key, selector] of [["scope_id", "#usage-scope"], ["purpose", "#usage-purpose"], ["model", "#usage-model"], ["persona_id", "#usage-persona"], ["provider_id", "#usage-provider"]]) {
    const value = $(selector).value.trim();
    if (value) params.set(key, value);
  }
  const host = $("#usage-results");
  host.textContent = "正在查询…";
  pagination(true);
  try {
    const data = await api("usage/stats?" + params);
    if (id !== serial) return;
    total = data.total;
    if (offset && offset >= total) {
      offset = total ? Math.floor((total - 1) / LIMIT) * LIMIT : 0;
      return loadUsage(false);
    }
    const s = data.summary;
    const selected = $("#usage-scope").value;
    $("#usage-scope").innerHTML = '<option value="">全部会话</option>' + (data.sessions || []).map(row => `<option value="${esc(row.scope_id)}">${esc(row.scope_id || "未归属会话")} (${esc(String(row.calls))})</option>`).join("");
    if (selected && !Array.from($("#usage-scope").options).some(o => o.value === selected)) {
      const option = document.createElement("option"); option.value = selected; option.textContent = selected; $("#usage-scope").appendChild(option);
    }
    $("#usage-scope").value = selected;
    enhanceSelects($("#pane-usage"));
    const counters = `<div class="dash-grid">${metric("用户轮", fmt(s.turns))}${metric("可见调用", fmt(s.calls))}${metric(`输入 Token（${s.field_coverage.input_total}/${s.calls} 次）`, fmt(s.input_total))}${metric(`输出 Token（${s.field_coverage.output}/${s.calls} 次）`, fmt(s.output))}</div>`;
    const summaryRows = [
      ["记录状态", data.enabled ? "正在记录" : "已关闭，仍可查询旧记录"],
      ["用量至少部分已知", `${s.known_usage_calls}/${s.calls}`],
      ["缓存读取 / 写入", `${fmt(s.input_cached)} / ${fmt(s.cache_write)}`],
      ["缓存适用调用中的报告覆盖", `${percent(s.cache_coverage)}（${s.cache_reported_calls}/${s.cache_applicable_calls} 次）`],
      ["有报告样本的加权命中率", `${percent(s.cache_hit_rate)}（${s.cache_reported_calls} 次，输入 ${fmt(s.cache_eligible_input)} Token）`],
      ["费用（按币种与来源）", costs(s.costs_by_source)],
      ["费用未知的调用", fmt(s.unpriced_calls)],
      ["宿主未暴露完整调用链 / 非用户调用", `${s.partial_calls} / ${s.non_user_calls}`],
    ];
    host.innerHTML = card("观测范围内合计", counters + table("汇总指标", ["指标", "数值"], summaryRows) +
      `<div class="note">${esc(data.coverage_note)} 宿主默认零不代表真实零命中；未知缓存样本不进入命中率分母。台账未写入或未确认的记录：${esc(fmt(data.dropped_records))}；已隔离损坏记录：${esc(fmt(data.quarantined_records))}。</div>`);
    for (const [key, title] of [["purpose", "按用途"], ["model", "按模型"], ["persona_id", "按角色"], ["provider_id", "按选定供应商或独立端点"]]) {
      host.innerHTML += card(title, table(title, ["分组", "调用", "轮次", "有用量", "缓存报告覆盖", "费用来源"], (data.groups[key] || []).map(r => [r.key, r.calls, r.turns, `${r.known_usage_calls}/${r.calls}`, percent(r.cache_coverage), costs(r.costs_by_source)])));
    }
    host.innerHTML += card("每日统计（UTC）", table("每日统计", ["日期", "调用", "用量已知", "输入", "缓存读", "输出", "费用来源"], data.daily.map(r => [r.day, r.calls, `${r.known_usage_calls}/${r.calls}`, fmt(r.input_total), fmt(r.input_cached), fmt(r.output), costs(r.costs_by_source)])));
    host.innerHTML += card("调用明细", table("调用明细", ["时间（本地）", "入口标识", "角色", "用途", "模型", "状态/覆盖", "输入", "缓存读", "缓存写", "输出", "费用/来源/价格版本"], data.items.map(r => [new Date(r.created_at * 1000).toLocaleString(), r.source_umo || "未归属", r.persona_id || "未知", r.purpose, r.model, `${r.status} / ${r.billing_coverage}`, fmt(r.usage.input_total), fmt(r.usage.input_cached), fmt(r.usage.cache_write), fmt(r.usage.output), (r.cost_amount === null ? "未知" : `${r.currency} ${r.cost_amount} / ${SOURCE[r.cost_source] || r.cost_source}${r.price_snapshot ? " / " + r.price_snapshot.version : ""}`) + (r.cost_note ? " / " + r.cost_note : "")])));
    pagination();
  } catch (error) {
    if (id !== serial) return;
    host.textContent = "用量查询失败：" + error.message;
    showToast("用量查询失败：" + error.message);
  }
}
export function usagePage(direction) {
  const next = offset + direction * LIMIT;
  if (next < 0 || next >= total) return;
  offset = next;
  return loadUsage(false);
}
export function clearUsage() {
  sandboxConfirm("清空全部会话的用量台账？此操作不能通过剧情读档恢复。", async () => {
    try { await apiPost("usage/clear", { confirm: "CLEAR_USAGE" }); await loadUsage(); showToast("用量台账已清空", "success"); }
    catch (error) { showToast(error.message); }
  }, true, "CLEAR_USAGE");
}
