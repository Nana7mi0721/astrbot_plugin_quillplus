/* M7 会话组：列表为主，新建/编辑使用统一模态框；先预览共享范围，再提交。 */
import { api, apiPost } from "../api.js";
import { $, $$, esc } from "../utils.js";
import { showToast, sandboxConfirm } from "../components/feedback.js";
import { closeModal, openModal } from "../components/modal.js";
import { enhanceSelects } from "../components/select.js";
import { S } from "../state.js";

let inventory={groups:[],origins:[],actors:[]}, cards=[], preview=null, initialized=false, dirty=false;

function invalidate() {
  preview=null;
  const save=$("#modalSaveBtn");
  if (save) save.disabled=true;
  const box=$("#group-preview");
  if (box) box.textContent="先点「预览共享范围」核对成员；确认无误后再点「保存会话组」。";
}

function personaName(id) {
  return cards.find(c=>c.id===id)?.name || id || "默认人设";
}

function groupFormHtml(group) {
  const personaOptions='<option value="">默认人设</option>'+cards.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  const seedOptions='<option value="">从空状态开始</option>'+inventory.origins.filter(o=>!o.scope_id).map(o=>`<option value="${esc(o.origin_key)}">复制状态：${esc(o.umo)}</option>`).join("");
  return `<div class="group-modal-form"><input type="hidden" id="group-id" value="${esc(group?.scope_id||"")}">
    <div class="form-row">
      <div class="form-group"><label for="group-name">名称</label><input class="field" id="group-name" maxlength="80" placeholder="为这段共同剧情命名"></div>
      <div class="form-group"><label for="group-persona">共享角色</label><select class="field" id="group-persona">${personaOptions}</select></div>
    </div>
    <div class="form-group"><label for="group-seed">初始状态</label><select class="field" id="group-seed" ${group?"disabled":""}>${seedOptions}</select><p class="row-desc">默认从此刻开始共享；也可以复制一个入口的角色变量。原数据仍保留。</p></div>
    <fieldset class="group-choices"><legend class="group-title">共享入口与旧历史</legend><div id="group-members" class="group-member-list"></div></fieldset>
    <div class="group-modal-preview"><button type="button" class="btn btn--gray" data-action="group-preview">预览共享范围</button><span class="row-desc">先预览并核对成员，再保存。</span></div>
    <pre id="group-preview" class="group-preview" tabindex="0" aria-label="共享范围预览" aria-live="polite">先点「预览共享范围」核对成员；确认无误后再点「保存会话组」。</pre>
  </div>`;
}

export async function loadGroups() {
  try {
    const [data,personas]=await Promise.all([api("groups/list"),api("persona/list")]);
    inventory=data; cards=Array.isArray(personas)?personas:(personas.items||personas.personas||[]);
    $("#groups-list").innerHTML=data.groups.filter(g=>g.active).map(group=>{
      const members=data.origins.filter(o=>o.scope_id===group.scope_id);
      return `<article class="card group-card">
        <div class="card-head"><h3><button type="button" class="group-card-title" data-action="group-edit" data-id="${esc(group.scope_id)}" ${group.busy?"disabled":""}>${esc(group.name)}</button></h3><span class="card-head-spacer"></span><span class="badge">${members.length} 个入口</span></div>
        <div class="card-body"><div class="card-body-inner">
          <p class="row-desc">共享角色：${esc(personaName(group.persona_id))}${group.busy?" · 正在生成，修改请等待本轮结束":""}</p>
          <ul class="group-origins">${members.map(o=>`<li title="${esc(o.umo)}">${esc(o.umo)}</li>`).join("")}</ul>
          <div class="group-actions"><button type="button" class="btn btn--gray" data-action="group-edit" data-id="${esc(group.scope_id)}" ${group.busy?"disabled":""}>编辑成员与角色</button><button type="button" class="btn btn--gray" data-action="group-recent" data-id="${esc(group.scope_id)}">查看近期消息</button><button type="button" class="btn btn--destructive-tinted" data-action="group-archive" data-id="${esc(group.scope_id)}" ${group.busy?"disabled":""}>解散</button></div>
        </div></div>
      </article>`;
    }).join("")||`<div class="empty-state"><svg class="icon icon--xl" aria-hidden="true"><use href="#i-layers"/></svg><div class="empty-title">还没有会话组</div><div class="empty-desc">先从目标私聊或群聊发送 /quill，再点「刷新入口」，然后新建会话组。</div><div class="empty-extra"><button type="button" class="btn btn--filled" data-action="group-new"><svg class="icon icon--sm" aria-hidden="true"><use href="#i-plus"/></svg>新建会话组</button></div></div>`;
    const blocked = Object.entries(data.origin_capabilities || {}).filter(([,cap])=>cap && !cap.supported);
    $("#groups-capability").innerHTML = (!data.host_capability?.supported || blocked.length) ? `<div class="note">共享上下文能力受阻：${esc(!data.host_capability?.supported ? data.host_capability?.reason || "未核验" : blocked.map(([key,cap])=>(data.origins.find(o=>o.origin_key===key)?.umo || key)+"："+cap.reason).join("；"))}。受影响请求会停止，请检查宿主或模型配置。</div>` : "";
    const archived=data.groups.filter(g=>!g.active);
    const archivedCard=$("#groups-archived-card");
    archivedCard.hidden=archived.length===0;
    $("#groups-archived").innerHTML=`<p class="row-desc">保留 ${archived.length} 个已解散会话组；原成员不会继续读到组历史。</p><div class="group-actions">`+archived.map(g=>`<button type="button" class="btn btn--gray" data-action="group-recent" data-id="${esc(g.scope_id)}">${esc(g.name)}</button>`).join("")+"</div>";
    $("#group-actors").innerHTML=data.actors.map(a=>`<label class="check group-member"><input type="checkbox" data-group-actor="${esc(a.actor_id)}"><span>${esc(a.label||a.sender_id)}<small>${esc(a.platform_id)} / ${esc(a.sender_id)}${a.canonical_id?" · 已显式关联":""}</small></span></label>`).join("")||'<p class="row-desc">尚无已观测参与者。</p>';
    if (!initialized) {
      $("#modalBody").addEventListener("input",event=>{ if(event.target.closest(".group-modal-form")){ dirty=true; invalidate(); } });
      $("#modalBody").addEventListener("change",event=>{ if(event.target.closest(".group-modal-form")){ dirty=true; invalidate(); } });
      // 从聊天窗口发送 /quill 后切回面板，自动重新读取新入口；正在编辑时不要覆盖未保存输入。
      document.addEventListener("visibilitychange",()=>{
        if (!document.hidden && !dirty && !$("#scrim").classList.contains("is-open") && $("#pane-groups").classList.contains("is-active")) loadGroups();
      });
      $("#group-recent-dialog").addEventListener("click",event=>{ if(event.target===event.currentTarget) event.currentTarget.close(); });
      initialized=true;
    }
    if (!dirty && !$("#scrim").classList.contains("is-open")) dirty=false;
  } catch(error) { showToast(error.message,"error"); }
}

export function editGroup(id="") {
  const group=inventory.groups.find(g=>g.scope_id===id&&g.active);
  S._modalState={type:"group"};
  S._modalMode=group?"edit":"create";
  $("#modalTitle").textContent=group?"编辑会话组":"新建会话组";
  $("#modalBody").innerHTML=groupFormHtml(group);
  $("#modalSaveBtn").textContent="保存会话组";
  $("#modalSaveBtn").disabled=true;
  openModal();
  if(group) {
    $("#group-name").value=group.name||"";
    $("#group-persona").value=group.persona_id||"";
  }
  $("#group-members").innerHTML=inventory.origins.map(o=>{
    const foreign=o.scope_id&&o.scope_id!==group?.scope_id;
    return `<div class="group-member"><label class="check"><input type="checkbox" data-group-member="${esc(o.origin_key)}" ${o.scope_id===group?.scope_id&&group?"checked":""} ${foreign?"disabled":""}><span>${esc(o.umo)} <small>${esc(o.platform_id)} · ${esc(o.message_type)}${foreign?" · 已在其他组":""}</small></span></label>${!group&&!foreign?`<label class="check group-history"><input type="checkbox" data-group-history="${esc(o.origin_key)}">纳入该入口同角色的旧记忆与日志</label>`:""}</div>`;
  }).join("")||'<p class="t2">尚未观测到入口，请先从该入口发送 /quill。</p>';
  enhanceSelects($("#modalBody"));
  preview=null; dirty=false;
}

function readForm() {
  const members=$$("[data-group-member]:checked").map(el=>el.dataset.groupMember);
  const persona=$("#group-persona").value;
  const history=$$("[data-group-history]:checked").map(el=>el.dataset.groupHistory).filter(key=>members.includes(key)).map(key=>{
    const origin=inventory.origins.find(o=>o.origin_key===key);
    return persona?origin.umo+"::"+persona:origin.umo;
  });
  return {scope_id:$("#group-id")?.value||"",name:$("#group-name").value,persona_id:persona,members,history_sources:history,seed_origin:$("#group-seed").disabled?"":$("#group-seed").value};
}

export async function previewGroup() {
  try {
    const data=readForm(); const result=await apiPost("groups/preview",data);
    preview={data,result};
    const NL=String.fromCharCode(10);
    $("#group-preview").textContent=`共享角色：${personaName(data.persona_id)}${NL}共享入口：${NL}${result.affected.join(NL)}${NL}退出入口：${NL}${result.removed.join(NL)||"无"}${NL}复制状态：${Object.entries(result.state_vars).map(([key,value])=>key+"："+String(value)).join("；")||"无"}${NL}纳入的旧历史：${NL}${result.history.map(h=>h.session_id+"："+h.memories+" 条记忆，"+h.logs+" 条日志"+NL+h.samples.map(s=>s.role+": "+s.text).join(NL)).join(NL)||"无，从此刻开始共享"}${NL}${NL}同组入口可以看到共享剧情与角色状态；同组不代表同一个人。由管理员在此面板移除入口后恢复该入口原有独立数据，已分享的信息无法收回。`;
    $("#modalSaveBtn").disabled=false;
  } catch(error) { invalidate(); showToast(error.message,"error"); }
}

export async function saveGroup() {
  if (!preview) return;
  try {
    const result=await apiPost("groups/save",{...preview.data,preview_token:preview.result.preview_token});
    const savedName=preview.data.name;
    preview=null; dirty=false;
    closeModal();
    showToast(`会话组「${savedName}」已保存`,"success");
    await loadGroups();
  } catch(error) { showToast(error.message,"error"); }
}

export function archiveGroup(id) {
  const group=inventory.groups.find(g=>g.scope_id===id);
  const members=inventory.origins.filter(o=>o.scope_id===id);
  sandboxConfirm(`解散「${group.name}」？以下入口恢复独立数据：
${members.map(o=>o.umo).join(String.fromCharCode(10))}
组历史保留为管理员档案。`,async()=>{
    try { await apiPost("groups/archive",{scope_id:id,revision:group.revision,confirm:"ARCHIVE"}); await loadGroups(); }
    catch(error) { showToast(error.message,"error"); }
  },true,"ARCHIVE");
}

export async function showGroupRecent(id) {
  try {
    const group=inventory.groups.find(g=>g.scope_id===id);
    const result=await api("groups/recent?"+new URLSearchParams({scope_id:id,persona_id:group.persona_id}));
    $("#group-recent").textContent=result.items.map(row=>`${row.created_at} · ${row.role} · ${row.actor_id||row.source_ref||"角色"}
${row.content}`).join(String.fromCharCode(10)+String.fromCharCode(10))||"暂无已完成消息。失败或中断的输入不会冒充已完成剧情。";
    $("#group-recent-dialog").showModal();
  } catch(error) { showToast(error.message,"error"); }
}

export function linkGroupActors(unlink=false) {
  const actors=$$("[data-group-actor]:checked").map(el=>el.dataset.groupActor);
  if (!actors.length) { showToast("请先勾选参与者","warning"); return; }
  sandboxConfirm(unlink?"解除所选参与者的显式关联？历史轮次保留当时身份。":"确认这些账号属于同一个人？此关联只影响后续身份标识，不授予管理员权限，也不修改历史归属。",async()=>{
    try { await apiPost("groups/actors",{actors,unlink}); await loadGroups(); }
    catch(error) { showToast(error.message,"error"); }
  });
}
