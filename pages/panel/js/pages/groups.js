/* M7 会话组：先预览共享范围，再提交；不把已观测入口自动入组。 */
import { api, apiPost } from "../api.js";
import { $, $$, esc } from "../utils.js";
import { showToast, sandboxConfirm } from "../components/feedback.js";
let inventory={groups:[],origins:[],actors:[]}, cards=[], preview=null, initialized=false;

function invalidate() { preview=null; $("#group-confirm").disabled=true; $("#group-preview").textContent="请先预览共享范围。"; }

export async function loadGroups() {
  try {
    const [data,personas]=await Promise.all([api("groups/list"),api("persona/list")]);
    inventory=data; cards=Array.isArray(personas)?personas:(personas.items||personas.personas||[]);
    $("#groups-list").innerHTML=data.groups.filter(g=>g.active).map(group=>{
      const members=data.origins.filter(o=>o.scope_id===group.scope_id);
      const name=cards.find(c=>c.id===group.persona_id)?.name||group.persona_id||"默认人设";
      return `<div class="card"><div class="card-head"><h3>${esc(group.name)}</h3><span class="card-head-spacer"></span><span class="badge">${members.length} 个入口</span></div><div class="card-body"><div class="card-body-inner"><p class="row-desc">共享角色：${esc(name)}${group.busy?" · 正在生成，修改请等待本轮结束":""}</p><ul class="group-origins">${members.map(o=>`<li>${esc(o.umo)}</li>`).join("")}</ul><div class="group-actions"><button type="button" class="btn btn--gray" data-action="group-edit" data-id="${esc(group.scope_id)}" ${group.busy?"disabled":""}>编辑成员与角色</button><button type="button" class="btn btn--gray" data-action="group-recent" data-id="${esc(group.scope_id)}">查看近期消息</button><button type="button" class="btn btn--gray" data-action="group-archive" data-id="${esc(group.scope_id)}" ${group.busy?"disabled":""}>解散</button></div></div></div></div>`;
    }).join("")||'<p class="t2">尚无会话组。先从目标私聊或群聊发送 /quill，再选择入口建组。</p>';
    const blocked = Object.entries(data.origin_capabilities || {}).filter(([,cap])=>cap && !cap.supported);
    if (!data.host_capability?.supported || blocked.length) $("#groups-list").insertAdjacentHTML("afterbegin", `<div class="note">共享上下文能力受阻：${esc(!data.host_capability?.supported ? data.host_capability?.reason || "未核验" : blocked.map(([key,cap])=>(data.origins.find(o=>o.origin_key===key)?.umo || key)+"："+cap.reason).join("；"))}。受影响请求会停止，请检查宿主或模型配置。</div>`);
    const archived=data.groups.filter(g=>!g.active);
    $("#groups-archived").innerHTML=`<p class="row-desc">保留 ${archived.length} 个已解散会话组；原成员不会继续读到组历史。</p><div class="group-actions">`+archived.map(g=>`<button type="button" class="btn btn--gray" data-action="group-recent" data-id="${esc(g.scope_id)}">${esc(g.name)}</button>`).join("")+"</div>";
    $("#group-actors").innerHTML=data.actors.map(a=>`<label class="check group-member"><input type="checkbox" data-group-actor="${esc(a.actor_id)}"><span>${esc(a.label||a.sender_id)}<small>${esc(a.platform_id)} / ${esc(a.sender_id)}${a.canonical_id?" · 已显式关联":""}</small></span></label>`).join("")||'<p class="row-desc">尚无已观测参与者。</p>';
    if (!initialized) {
      $("#group-form").addEventListener("input",invalidate);
      $("#group-form").addEventListener("change",invalidate);
      initialized=true;
    }
    editGroup($("#group-id").value||"");
  } catch(error) { showToast(error.message,"error"); }
}

export function editGroup(id="") {
  const group=inventory.groups.find(g=>g.scope_id===id&&g.active);
  $("#group-id").value=group?.scope_id||"";
  $("#group-name").value=group?.name||"";
  $("#group-persona").innerHTML='<option value="">默认人设</option>'+cards.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  $("#group-persona").value=group?.persona_id||"";
  $("#group-seed").innerHTML='<option value="">从空状态开始</option>'+inventory.origins.filter(o=>!o.scope_id).map(o=>`<option value="${esc(o.origin_key)}">复制状态：${esc(o.umo)}</option>`).join("");
  $("#group-seed").disabled=!!group;
  $("#group-members").innerHTML=inventory.origins.map(o=>{
    const foreign=o.scope_id&&o.scope_id!==group?.scope_id;
    return `<div class="group-member"><label class="check"><input type="checkbox" data-group-member="${esc(o.origin_key)}" ${o.scope_id===group?.scope_id&&group?"checked":""} ${foreign?"disabled":""}><span>${esc(o.umo)} <small>${esc(o.platform_id)} · ${esc(o.message_type)}${foreign?" · 已在其他组":""}</small></span></label>${!group&&!foreign?`<label class="check group-history"><input type="checkbox" data-group-history="${esc(o.origin_key)}">纳入该入口同角色的旧记忆与日志</label>`:""}</div>`;
  }).join("")||'<p class="t2">尚未观测到入口，请先从该入口发送 /quill。</p>';
  $("#group-form-title").textContent=group?"编辑会话组":"新建会话组";
  invalidate();
}

function readForm() {
  const members=$$("[data-group-member]:checked").map(el=>el.dataset.groupMember);
  const persona=$("#group-persona").value;
  const history=$$("[data-group-history]:checked").map(el=>el.dataset.groupHistory).filter(key=>members.includes(key)).map(key=>{
    const origin=inventory.origins.find(o=>o.origin_key===key);
    return persona?origin.umo+"::"+persona:origin.umo;
  });
  return {scope_id:$("#group-id").value,name:$("#group-name").value,persona_id:persona,members,history_sources:history,seed_origin:$("#group-seed").disabled?"":$("#group-seed").value};
}

export async function previewGroup() {
  try {
    const data=readForm(); const result=await apiPost("groups/preview",data);
    preview={data,result};
    $("#group-preview").textContent=`共享角色：${cards.find(c=>c.id===data.persona_id)?.name||"默认人设"}\n共享入口：\n${result.affected.join("\n")}\n退出入口：\n${result.removed.join("\n")||"无"}\n复制状态：${Object.entries(result.state_vars).map(([key,value])=>key+"："+String(value)).join("；")||"无"}\n纳入的旧历史：\n${result.history.map(h=>h.session_id+"："+h.memories+" 条记忆，"+h.logs+" 条日志\n"+h.samples.map(s=>s.role+": "+s.text).join("\n")).join("\n")||"无，从此刻开始共享"}\n\n同组入口可以看到共享剧情与角色状态；同组不代表同一个人。由管理员在此面板移除入口后恢复该入口原有独立数据，已分享的信息无法收回。`;
    $("#group-confirm").disabled=false;
  } catch(error) { invalidate(); showToast(error.message,"error"); }
}

export async function saveGroup() {
  if (!preview) return;
  try {
    const result=await apiPost("groups/save",{...preview.data,preview_token:preview.result.preview_token});
    $("#group-id").value=result.scope_id;
    showToast("会话组已保存，共享范围已生效","success");
    await loadGroups();
  } catch(error) { invalidate(); showToast(error.message,"error"); }
}

export function archiveGroup(id) {
  const group=inventory.groups.find(g=>g.scope_id===id);
  const members=inventory.origins.filter(o=>o.scope_id===id);
  sandboxConfirm(`解散「${group.name}」？以下入口恢复独立数据：\n${members.map(o=>o.umo).join("\n")}\n组历史保留为管理员档案。`,async()=>{
    try { await apiPost("groups/archive",{scope_id:id,revision:group.revision,confirm:"ARCHIVE"}); $("#group-id").value=""; await loadGroups(); }
    catch(error) { showToast(error.message,"error"); }
  },true,"ARCHIVE");
}

export async function showGroupRecent(id) {
  try {
    const group=inventory.groups.find(g=>g.scope_id===id);
    const result=await api("groups/recent?"+new URLSearchParams({scope_id:id,persona_id:group.persona_id}));
    $("#group-recent").textContent=result.items.map(row=>`${row.created_at} · ${row.role} · ${row.actor_id||row.source_ref||"角色"}\n${row.content}`).join("\n\n")||"暂无已完成消息。失败或中断的输入不会冒充已完成剧情。";
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
