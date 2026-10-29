/* 用量展示只适配全量聚合，不从分页明细推断总量；未知和已知零分开。 */
export const SOURCE = {reported: '供应商报告', price_snapshot: '价格推算', price_snapshot_partial: '部分推算（可能低估）'};
export const DIMENSIONS = {model: '模型', purpose: '用途', provider_id: '供应商或端点', persona_id: '角色'};
export const fmt = v => v == null ? '—' : Number(v).toLocaleString();
export const compact = v => v == null ? '—' : Intl.NumberFormat('zh-CN', {notation:'compact', maximumFractionDigits:1}).format(v);
export const percent = v => v == null ? '未报告' : (v * 100).toFixed(1) + '%';
export const bucketAmount = (row, bucket) => row.costs_by_source?.[bucket?.currency]?.[bucket?.source] ?? null;
export const amountText = v => v == null ? '无金额记录' : String(v);
export const valueOf = (row, metric, bucket) => metric === 'cost' ? bucketAmount(row, bucket) : row[metric] ?? null;
export function costBuckets(summary) {
  return Object.entries(summary.costs_by_source || {}).flatMap(([currency, sources]) => Object.keys(sources).map(source => ({currency, source})));
}
export function costLabel(bucket) { return bucket ? `${bucket.currency} · ${SOURCE[bucket.source] || bucket.source}` : '无金额记录'; }
export function decimalSum(values) {
  // 金额文本按十进制位对齐相加，Number 仅用于绘图几何，不回写账本。
  const strings = values.filter(v => v != null).map(String);
  if (!strings.length) return null;
  if (strings.some(v => !/^\d+(\.\d+)?$/.test(v))) return null;
  const scale = Math.max(...strings.map(v => (v.split('.')[1] || '').length));
  const sum = strings.reduce((n, v) => { const [a,b=''] = v.split('.'); return n + BigInt(a + b.padEnd(scale,'0')); },0n);
  const digits = String(sum).padStart(scale+1,'0');
  return scale ? digits.slice(0,-scale)+'.'+digits.slice(-scale) : digits;
}
export function ranking(groups, metric, bucket) {
  const rows = groups.map(r => ({...r, value:valueOf(r,metric,bucket)}));
  rows.sort((a,b) => (a.value == null)-(b.value == null) || Number(b.value)-Number(a.value) || a.key.localeCompare(b.key));
  const known = rows.filter(r=>r.value != null), rest = known.slice(7);
  const total = metric === 'cost' ? decimalSum(known.map(r=>r.value)) : known.reduce((n,r)=>n+Number(r.value),0);
  const other = rest.length ? {key:`其他（${rest.length} 项）`, other:true, calls:rest.reduce((n,r)=>n+r.calls,0), value:metric === 'cost' ? decimalSum(rest.map(r=>r.value)) : rest.reduce((n,r)=>n+Number(r.value),0)} : null;
  return {rows, top:known.slice(0,7), other, total, unknown:rows.filter(r=>r.value == null), leader:Math.max(0,...known.map(r=>Number(r.value)))};
}
export function dailySeries(rows) {
  if (!rows.length) return [];
  const byDay = new Map(rows.map(r=>[r.day,r]));
  const start = Date.parse(rows[0].day+'T00:00:00Z'), end = Date.parse(rows.at(-1).day+'T00:00:00Z');
  const result=[];
  for (let at=start;at<=end;at+=86400000) {
    const day=new Date(at).toISOString().slice(0,10);
    result.push(byDay.get(day) || {day,calls:0,input_total:null,output:null,field_coverage:{},noRecords:true});
  }
  return result;
}
