/* U1：全量聚合可视化，分页只更新明细；桥接请求串行并仅保留最后一个待办。 */
import {api, apiPost} from '../api.js';
import {$, esc} from '../utils.js';
import {enhanceSelects} from '../components/select.js';
import {sandboxConfirm, showToast} from '../components/feedback.js';
import {SOURCE, DIMENSIONS, fmt, compact, percent, costBuckets, costLabel, bucketAmount, ranking, dailySeries} from './usage_viewmodel.js';
import {mountTrend} from './usage_charts.js';
let offset=0,total=0,snapshot=null,flight=null,pending=null,activeParams='',wanted='',disposeChart=()=>{};
let metric='io',dimension='model',rankMetric='calls',bucketIndex=0,showAll=false;
const expanded=new Set(), LIMIT=50;
const FILTERS={scope_id:'#usage-scope',source_umo:'#usage-origin',purpose:'#usage-purpose',model:'#usage-model',provider_id:'#usage-provider',persona_id:'#usage-persona'};
const labels={scope_id:'逻辑会话',source_umo:'物理入口',purpose:'用途',model:'模型',provider_id:'供应商',persona_id:'角色'};
const table=(title,headers,rows)=>`<div class="table-wrap"><table class="data" aria-label="${esc(title)}"><thead><tr>${headers.map(h=>`<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${row.map(v=>`<td>${esc(String(v))}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${headers.length}" class="t3" style="text-align:center">当前筛选无记录</td></tr>`}</tbody></table></div>`;
const card=(title,body)=>`<section class="card"><div class="card-head"><h3>${title}</h3></div><div class="card-body"><div class="card-body-inner">${body}</div></div></section>`;
const options=(values,selected)=>Object.entries(values).map(([key,name])=>`<option value="${esc(key)}" ${key===selected?'selected':''}>${esc(name)}</option>`).join('');
const amount=(value)=>value==null?'无金额记录':esc(String(value));
function bucket(){return costBuckets(snapshot?.data.summary||{})[bucketIndex];}
function pager(busy=false){
  $('[data-action="usage-prev"]').disabled=busy||offset<=0;
  $('[data-action="usage-next"]').disabled=busy||offset+LIMIT>=total;
  $('#usage-pagination').textContent=`${total?offset+1:0}–${Math.min(offset+LIMIT,total)} / ${total}`;
}
function filters(){const f={days:$('#usage-days').value};for(const [key,sel] of Object.entries(FILTERS)){const value=$(sel).value.trim();if(value)f[key]=value;}return f;}
function chips(f){$('#usage-chips').innerHTML=Object.entries(f).filter(([key])=>key!=='days').map(([key,value])=>`<button class="btn btn--gray" type="button" data-usage-remove="${key}">${labels[key]}：${esc(value)} ×</button>`).join('');}
function renderOverview(){
  const d=snapshot.data,s=d.summary;
  const kpi=(name,value,note)=>`<div class="usage-kpi"><div class="usage-kpi-name">${name}</div><strong>${value}</strong><div class="usage-caption">${note}</div></div>`;
  const coverage=(n,m)=>m?`${n}/${m} 次（${(n/m*100).toFixed(1)}%）`:'0 次';
  const money=Object.entries(s.display_costs_by_source??s.costs_by_source??{}).flatMap(([currency,sources])=>Object.entries(sources).map(([source,value])=>`<div><b>${esc(currency)} ${amount(value)}</b><span class="usage-caption">${esc(SOURCE[source]||source)}</span></div>`)).join('');
  $('#usage-overview').innerHTML=`<div class="usage-kpis">${kpi('可见调用',fmt(s.calls),`${fmt(s.turns)} 个用户轮 · ${fmt(s.non_user_calls)} 次非用户调用 · 用量覆盖 ${percent(s.usage_coverage)}（${s.known_usage_calls}/${s.calls}）`)}${kpi('已报告输入',compact(s.input_total),`输入用量 ${coverage(s.field_coverage.input_total,s.calls)} · 合计 ${fmt(s.input_total)} Token`)}${kpi('已报告输出',compact(s.output),`输出用量 ${coverage(s.field_coverage.output,s.calls)} · 合计 ${fmt(s.output)} Token`)}${kpi('费用按币种与来源',money||'—',`${fmt(s.unpriced_calls)} 次费用未知${s.unconverted_currencies?.length?' · 未换算：'+esc(s.unconverted_currencies.join('、')):''}`)}</div>`;
  $('#usage-snapshot').textContent=`${d.enabled?'正在记录':'记录已关闭，仍可查看历史'} · 已更新 ${new Date(snapshot.at).toLocaleTimeString()} · 当前快照：${Object.entries(snapshot.filters).map(([k,v])=>`${labels[k]||'天数'}=${v}`).join('，')}`;
  $('#usage-snapshot').textContent+=d.currency_view?.currency!=='original'?` · 换算展示 ${d.currency_view.currency}（手动汇率 1 USD=${d.currency_view.usd_cny_rate} CNY，原始金额保留）`:'';
  const currencies=Object.keys(s.display_costs_by_source??s.costs_by_source??{});
  const currencyHint=$('#usage-currency-hint');
  if(currencyHint)currencyHint.textContent=currencies.length?`台账中实际出现的费用币种：${currencies.join('、')}。这里只改变换算展示，原始金额与来源保留。`:'台账暂时没有金额记录。';
  const compression=d.context_compression;
  if(compression){
    const relevant=compression.recent.filter(r=>!snapshot.filters.scope_id||r.scope_hash===snapshot.filters.scope_id).sort((a,b)=>b.updated_at-a.updated_at)[0];
    $('#usage-context-status').textContent=`自动压缩${compression.enabled?'已开启':'已关闭'} · 文本估算上限 ${fmt(compression.limit)} Token，85% 触发。`+(relevant?` 最近观测：${fmt(relevant.estimated_tokens)} Token（逻辑会话 ${relevant.scope_hash}）。`:' 尚无本轮上下文观测。');
    if(relevant){
      const checkpoint={none:'尚无检查点',prepared:'摘要已准备，等待后续匹配请求',projected:'摘要已应用于请求视图',native_persisted:'摘要已写入独立会话历史，无需再次投影',source_mismatch:'历史前缀已变化，本次未套用旧检查点'};
      $('#usage-context-status').textContent+=` ${checkpoint[relevant.checkpoint_state]||''}。`+(relevant.checkpoint_warning==='checkpoint_unreadable'?' 不可读检查点指针已隔离，原文文件保留；可重新压缩。':'');
    }
  }
  const meter=(name,rate)=>`<div><h3>${name} <span>${percent(rate)}</span></h3><div class="usage-meter" role="img" aria-label="${name} ${percent(rate)}">${rate==null?'<span>—</span>':`<i style="width:${Math.max(0,Math.min(100,rate*100))}%"></i>`}</div></div>`;
  $('#usage-cache').innerHTML=card('缓存与报告质量',`<div class="usage-cache-grid">${meter('有报告样本的命中率',s.cache_hit_rate)}${meter('缓存报告覆盖',s.cache_coverage)}</div><p class="usage-note">适用完成调用 ${fmt(s.cache_applicable_calls)} 次；有报告 ${fmt(s.cache_reported_calls)} 次；未知 ${fmt(s.cache_unknown_calls)} 次。命中率输入分母 ${fmt(s.cache_eligible_input)} Token。缓存读已包含于输入，不重复相加。</p><p class="usage-note">记录完整性与观测范围：${esc(d.coverage_note||'')} 至少部分用量已知 ${s.known_usage_calls}/${s.calls} 次；宿主未暴露完整调用链 ${fmt(s.partial_calls)} 次。</p><p class="usage-note">台账累计未写入或未确认 ${fmt(d.dropped_records)} 条；损坏隔离 ${fmt(d.quarantined_records)} 条。此处累计诊断不随筛选改变。</p>`);
}
function renderVisuals(){
  if(!snapshot||!$('#pane-usage').classList.contains('is-active'))return;
  disposeChart();$('#usage-visuals').querySelectorAll('select').forEach(s=>s._selApi?.dispose());
  const rows=dailySeries(snapshot.data.daily),b=bucket(),buckets=costBuckets(snapshot.data.summary);
  const bucketHint=buckets.length<=1?`费用桶 = 币种 + 金额来源；当前台账只有一个组合（${buckets.length?costLabel(buckets[0]):'无金额记录'}），更多来源出现后会自动列出。仅在指标选「费用」时启用。`:'';
  $('#usage-trend-table').innerHTML=table('趋势数据',['日期 UTC','调用','已报告输入','输入覆盖','已报告输出','输出覆盖',costLabel(b)],rows.map(r=>[r.day,r.calls,fmt(r.input_total),`${r.field_coverage?.input_total||0}/${r.calls}`,fmt(r.output),`${r.field_coverage?.output||0}/${r.calls}`,bucketAmount(r,b)??'无金额记录']));
  $('#usage-visuals').innerHTML=`<section class="card usage-trend-card"><div class="card-head"><h3>日级趋势（UTC）</h3><span class="card-head-sub">按 UTC 日期分组</span><div class="usage-trend-actions"><button type="button" class="btn btn--gray" data-action="usage-trend-open">数据表</button></div></div><div class="card-body"><div class="card-body-inner"><div class="usage-trend-layout"><div class="usage-trend-main"><div class="usage-chart-tools"><label class="usage-filter"><span>指标</span><select class="field" id="usage-trend-metric">${options({io:'已报告输入 / 输出',calls:'调用',cost:'费用'},metric)}</select></label><label class="usage-filter"><span>费用桶</span><select class="field" id="usage-cost-bucket" ${metric==='cost'&&buckets.length?'':'disabled'}>${buckets.map((x,i)=>`<option value="${i}" ${i===bucketIndex?'selected':''}>${esc(costLabel(x))}</option>`).join('')||'<option>无金额记录</option>'}</select></label></div><p class="usage-caption usage-bucket-hint" id="usage-bucket-hint"${bucketHint?'':' hidden'}>${esc(bucketHint)}</p><div class="usage-legend">${metric==='io'?'<span><i class="usage-input"></i>已报告输入</span><span><i class="usage-output"></i>已报告输出</span>':esc(metric==='calls'?'可见调用':costLabel(b))} · 空心点为未报告</div><div id="usage-trend"></div></div><div class="usage-trend-side"><div class="usage-rank-heading">主要来源</div><div class="usage-chart-tools"><label class="usage-filter"><span>分组</span><select class="field" id="usage-dimension">${options(DIMENSIONS,dimension)}</select></label><label class="usage-filter"><span>指标</span><select class="field" id="usage-rank-metric">${options({calls:'调用',input_total:'已报告输入',output:'已报告输出',cost:'费用'},rankMetric)}</select></label></div><div id="usage-ranking"></div></div></div></div></div></section>`;
  const r=ranking(snapshot.data.groups[dimension]||[],rankMetric,b),list=showAll?r.rows:[...r.top,...(r.other?[r.other]:[]),...r.unknown];
  const filterable=!!FILTERS[dimension]||dimension==='group';
  const rankAttr=x=>x.key==='unknown'||x.key==='独立会话'?'disabled':x.other?'data-usage-all="1"':filterable?`data-usage-rank="${esc(x.key)}"`:'disabled';
  $('#usage-ranking').innerHTML=list.map(x=>`<button type="button" class="usage-rank-row" ${rankAttr(x)}><span class="usage-rank-head"><span>${esc(x.label&&x.label!==x.key?x.label+' · '+x.key.slice(0,8):x.key)}</span><b>${rankMetric==='cost'?amount(x.value):fmt(x.value)}</b></span><span class="usage-rank-track"><i style="width:${x.value==null||!r.leader?0:Math.max(0,Math.min(100,Number(x.value)/r.leader*100))}%"></i></span><span class="usage-caption">${fmt(x.calls)} 次调用 · ${x.value==null?'未报告':Number(r.total)>0?`${(Number(x.value)/Number(r.total)*100).toFixed(1)}%（占当前已知量）`:'已知零'}${x.other?' · 展开查看分组':''}</span></button>`).join('')||'<p class="usage-note">当前筛选无记录</p>';
  if(r.rows.length>7)$('#usage-ranking').insertAdjacentHTML('beforeend',`<button type="button" class="btn btn--gray" data-usage-all="1">${showAll?'收起为 Top 7':'展开全部分组'}</button>`);
  disposeChart=mountTrend($('#usage-trend'),rows,metric,b);
  enhanceSelects($('#usage-visuals'));
}

function renderDetails(){
  const rows=snapshot.data.items;
  $('#usage-details').innerHTML=card('调用明细',`<p class="usage-note">共 ${total} 次匹配调用，分页只影响此表。时间为 UTC；展开查看完整元数据。</p><div class="table-wrap"><table class="data" aria-label="调用明细"><thead><tr>${['时间 UTC','用途 / 模型','输入 / 输出','缓存读','费用 / 来源','状态 / 覆盖','详情'].map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.map((r,i)=>{
    const u=r.usage||{},open=expanded.has(r.call_id),id='usage-detail-'+i;
    const fields={'入口':r.source_umo,'会话':r.scope_id,'角色':r.persona_id,'供应商或端点':r.provider_id,'模型':r.model,'输入 Token':fmt(u.input_total),'输出 Token':fmt(u.output),'缓存读':fmt(u.input_cached),'缓存读取依据':u.cache_report_source==='host_normalized'?'宿主归一化，未暴露供应商原始字段':u.cache_reported?'原始用量字段':'未报告','缓存写数值':fmt(u.cache_write),'缓存写报告证据':u.cache_write_reported===true?'已报告':'未报告','缓存写来源':u.cache_write_source,'用量来源':u.usage_source,'费用':r.cost_amount==null?'未知':`${r.currency} ${r.cost_amount}`,'计价来源':SOURCE[r.cost_source]||r.cost_source,'费用说明':r.cost_note,'换算展示':Object.entries(r.display_costs_by_source||{}).flatMap(([c,b])=>Object.entries(b).map(([source,v])=>`${c} ${v} / ${SOURCE[source]||source}`)).join('；')||'未换算','价格版本':r.price_snapshot?.version,'调用 ID':r.call_id};
    const purpose=`${r.purpose} · ${r.model||'未报告'}`;
    const cost=`${r.cost_amount==null?'未知':`${r.currency} ${r.cost_amount}`} · ${SOURCE[r.cost_source]||r.cost_source||'未报告'}`;
    const state=`${r.status} · ${r.billing_coverage}`;
    return `<tr><td>${esc(new Date(r.created_at*1000).toISOString().replace('T',' ').slice(0,19))}</td><td class="usage-model"><span class="cell-truncate" title="${esc(purpose)}">${esc(purpose)}</span></td><td>${fmt(u.input_total)} / ${fmt(u.output)}</td><td>${fmt(u.input_cached)}</td><td><span class="cell-truncate" title="${esc(cost)}">${esc(cost)}</span></td><td><span class="cell-truncate" title="${esc(state)}">${esc(state)}</span></td><td><button type="button" class="btn btn--gray" data-usage-detail="${i}" aria-expanded="${open}" aria-controls="${id}">${open?'收起':'展开'}</button></td></tr><tr id="${id}" ${open?'':'hidden'}><td colspan="7"><dl class="usage-detail">${Object.entries(fields).map(([k,v])=>`<div class="usage-detail-item"><dt>${k}</dt><dd>${esc(v==null||v===''?'未报告':String(v))}</dd></div>`).join('')}</dl></td></tr>`;
  }).join('')||'<tr><td colspan="7" class="t3" style="text-align:center">当前筛选无记录</td></tr>'}</tbody></table></div>`);
}
function render(data,f,key){

  const previousBucket=costLabel(bucket());
  snapshot={data,filters:f,key,offset,at:Date.now()};total=data.total;
  const found=costBuckets(data.summary).findIndex(b=>costLabel(b)===previousBucket);bucketIndex=found<0?0:found;
  $('#usage-results').hidden=false;
  if(bucketIndex>=costBuckets(data.summary).length)bucketIndex=0;
  const selected=$('#usage-scope').value;
  $('#usage-scope').innerHTML='<option value="">全部会话</option>'+(data.sessions||[]).filter(r=>r.scope_id).map(r=>`<option value="${esc(r.scope_id)}">${esc(r.label||r.scope_id)} · ${esc(r.scope_id.slice(0,8))} (${r.calls})</option>`).join('');
  if(selected&&!Array.from($('#usage-scope').options).some(o=>o.value===selected)){const o=document.createElement('option');o.value=o.textContent=selected;$('#usage-scope').append(o);}
  $('#usage-scope').value=selected;enhanceSelects($('#usage-filters'));
  const originSelected=$('#usage-origin').value;
  $('#usage-origin').innerHTML='<option value="">全部入口</option>'+(data.origins||[]).map(r=>`<option value="${esc(r.source_umo)}">${esc(r.label||r.source_umo)} · ${esc(r.source_umo.slice(0,8))} (${r.calls})</option>`).join('');
  $('#usage-origin').value=originSelected;enhanceSelects($('#usage-filters'));
  renderOverview();renderDetails();pager();
}
const SETTINGS_FIELDS=[['compression','enabled'],['compression','max_tokens'],['compression','provider_id'],['usage','display_currency'],['usage','usd_cny_rate']];
let settingsEpoch=0,settingsBaseline=null,settingsBusy=false;
const settingsElement=(group,key)=>document.getElementById(`u-${group}-${key.replaceAll('_','-')}`);
const settingsValue=e=>e.type==='checkbox'?e.checked:e.type==='number'?Number(e.value):e.value;
function settingsBusyState(busy){
  settingsBusy=busy;
  const disabled=busy||!settingsBaseline;
  for(const [g,k] of SETTINGS_FIELDS)settingsElement(g,k).disabled=disabled;
  $('#usage-settings-save').disabled=disabled;
  $('#usage-settings').setAttribute('aria-busy',String(busy));
  enhanceSelects($('#usage-settings'));
}
async function openUsageSettings(){
  const epoch=++settingsEpoch,d=$('#usage-settings-dialog');
  settingsBaseline=null;settingsBusyState(true);$('#usage-settings-status').textContent='正在读取最新设置…';
  if(!d.open)d.showModal();
  try{
    const [providers,cfg]=await Promise.all([api('provider/list').catch(()=>null),api('config/all')]);
    if(epoch!==settingsEpoch||!d.open)return;
    const providerSelect=settingsElement('compression','provider_id');
    providerSelect.innerHTML='<option value="">（默认主模型）</option>'+((providers?.llm)||[]).map(x=>`<option value="${esc(x.id)}">${esc(x.id)}${x.model?' — '+esc(x.model):''}</option>`).join('');
    const baseline={};
    for(const [g,k] of SETTINGS_FIELDS){
      const e=settingsElement(g,k),v=cfg[g][k];
      if(e.type==='checkbox')e.checked=v;
      else{
        e.value=v??'';
        if(e.tagName==='SELECT'&&e.value===''&&v){const o=document.createElement('option');o.value=o.textContent=v;e.append(o);e.value=v;}
      }
      baseline[g+'.'+k]=settingsValue(e);
    }
    settingsBaseline=baseline;$('#usage-settings-status').textContent='';
  }catch(error){if(epoch===settingsEpoch)$('#usage-settings-status').textContent='读取设置失败，请关闭后重试：'+error.message;}
  finally{if(epoch===settingsEpoch)settingsBusyState(false);}
}
$('#usage-settings-dialog').addEventListener('close',()=>{settingsEpoch++;settingsBaseline=null;settingsBusyState(false);});
$('#usage-settings-save').addEventListener('click',async()=>{
  if(settingsBusy||!settingsBaseline)return;
  if(SETTINGS_FIELDS.some(([g,k])=>!settingsElement(g,k).reportValidity()))return;
  const epoch=settingsEpoch;
  // R6：以本次打开时的配置为基线，只写用户实际改动；旧表单不能整组回写。
  const updates=SETTINGS_FIELDS.map(([group,key])=>({group,key,value:settingsValue(settingsElement(group,key))})).filter(x=>x.value!==settingsBaseline[x.group+'.'+x.key]);
  if(!updates.length){$('#usage-settings-dialog').close();showToast('没有需要保存的修改','success');return;}
  settingsBusyState(true);
  try{
    const latest=await api('config/all');
    if(epoch!==settingsEpoch)return;
    if(updates.some(x=>latest[x.group][x.key]!==settingsBaseline[x.group+'.'+x.key])){
      $('#usage-settings-status').textContent='这些设置已在其他页面更新。请关闭并重新打开设置，核对后再保存。';return;
    }
    await apiPost('config/save_batch',{updates});
    if(epoch===settingsEpoch){$('#usage-settings-dialog').close();showToast('设置已保存','success');}
    await loadUsage();
  }catch(error){if(epoch===settingsEpoch)$('#usage-settings-status').textContent='保存失败：'+error.message;}
  finally{if(epoch===settingsEpoch)settingsBusyState(false);}
});
export function loadUsage(reset=true){
  if(reset)offset=0;
  const f=filters(),key=new URLSearchParams(f).toString(),params=key+`&limit=${LIMIT}&offset=${offset}`;
  wanted=params;chips(f);
  if(flight && activeParams===params && !pending)return flight;
  pending={f,key,params,reset};
  $('#usage-message').textContent=snapshot?'正在查询；下方保留上次成功快照。':'正在查询…';
  $('#usage-results').setAttribute('aria-busy','true');pager(true);
  if(flight)return flight;
  flight=(async()=>{
    while(pending){
      const job=pending;pending=null;activeParams=job.params;
      try{
        const data=await api('usage/stats?'+job.params);
        if(job.params!==wanted)continue;
        if(offset&&offset>=data.total){offset=data.total?Math.floor((data.total-1)/LIMIT)*LIMIT:0;pending={...job,params:job.key+`&limit=${LIMIT}&offset=${offset}`};wanted=pending.params;continue;}
        // 同筛选刷新仍更新图形数据；分页则保留现有图形与焦点。
        const refresh=job.reset||snapshot?.key!==job.key||JSON.stringify([snapshot?.data.daily,snapshot?.data.groups])!==JSON.stringify([data.daily,data.groups]);
        render(data,job.f,job.key);if(refresh)renderVisuals();
        $('#usage-message').textContent=data.total?'':'当前筛选无记录。';
      }catch(e){if(job.params===wanted){if(snapshot)offset=snapshot.offset;$('#usage-message').textContent='查询失败，可重试。'+(snapshot?'下方为上次成功快照。':'')+' '+e.message;showToast('用量查询失败：'+e.message);}}
    }
  })().finally(()=>{flight=null;activeParams='';$('#usage-results').setAttribute('aria-busy','false');pager();});
  return flight;
}
export function disposeUsage(){disposeChart();disposeChart=()=>{};for(const id of ['usage-settings-dialog','usage-trend-dialog']){const dialog=document.getElementById(id);if(dialog?.open)dialog.close();}}
export function usagePage(direction){const n=offset+direction*LIMIT;if(n<0||n>=total)return;offset=n;return loadUsage(false);}
export function clearUsage(){sandboxConfirm('清空全部会话的用量台账？此操作不能通过剧情读档恢复。',async()=>{try{await apiPost('usage/clear',{confirm:'CLEAR_USAGE'});expanded.clear();await loadUsage();showToast('用量台账已清空','success');}catch(e){showToast(e.message);}},true,'CLEAR_USAGE');}
// 本页委托一次；局部渲染和返回页面不会累积监听器。
$('#pane-usage').addEventListener('change',e=>{
  const id=e.target.id;
  if(id==='usage-trend-metric')metric=e.target.value;
  else if(id==='usage-dimension'){dimension=e.target.value;showAll=false;}
  else if(id==='usage-rank-metric')rankMetric=e.target.value;
  else if(id==='usage-cost-bucket')bucketIndex=Number(e.target.value);
  else if(['usage-days','usage-scope','usage-origin','usage-purpose','usage-model','usage-provider','usage-persona'].includes(id)){loadUsage();return;}
  else return;
  renderVisuals();
  queueMicrotask(()=>document.getElementById(id)?._selApi?.btn.focus({preventScroll:true}));
});
$('#pane-usage').addEventListener('click',e=>{
  const button=e.target.closest('button');if(!button)return;
  const action=button.dataset.action;
  if(action==='usage-settings-open'){openUsageSettings();return;}
  if(action==='usage-settings-close'){$('#usage-settings-dialog').close();return;}
  if(action==='usage-trend-open'){$('#usage-trend-dialog').showModal();return;}
  if(action==='usage-trend-close'){$('#usage-trend-dialog').close();return;}
  if(action==='usage-clear'){$('#usage-settings-dialog').close();}
  if(button.dataset.usageRank!==undefined){const sel=dimension==='group'?FILTERS.scope_id:FILTERS[dimension];$(sel).value=button.dataset.usageRank;showAll=false;loadUsage();}
  else if(button.dataset.usageRemove){$(FILTERS[button.dataset.usageRemove]).value='';loadUsage();}
  else if(button.dataset.usageAll){showAll=!showAll;renderVisuals();}
  else if(button.dataset.usageDetail!==undefined){const r=snapshot.data.items[Number(button.dataset.usageDetail)],open=expanded.has(r.call_id);if(open)expanded.delete(r.call_id);else expanded.add(r.call_id);button.setAttribute('aria-expanded',String(!open));button.textContent=open?'展开':'收起';document.getElementById(button.getAttribute('aria-controls')).hidden=open;}
});
