const state = { dashboard: null, groups: [], tasks: [], view: 'today', month: new Date(), filter: 'open', lastNotification: Number(localStorage.getItem('lastSchoolNotification') || 0) };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function escapeHtml(value = '') { return String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function toast(title, detail = '', error = false) {
  const node = document.createElement('div'); node.className = `toast${error ? ' error' : ''}`;
  node.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span>`;
  $('#toastStack').append(node); setTimeout(() => node.remove(), 4800);
}
function dateText(value) {
  if (!value) return '待确认时间'; const d = new Date(value.length === 10 ? `${value}T18:00:00+08:00` : value);
  const now = new Date(); const same = d.toDateString() === now.toDateString();
  return same ? `今天 ${d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}` : d.toLocaleString('zh-CN',{month:'short',day:'numeric',hour:value.length===10?undefined:'2-digit',minute:value.length===10?undefined:'2-digit'});
}
function priorityText(p) { return ({urgent:'紧急',high:'重要',normal:'普通',low:'稍后'})[p] || '普通'; }

function renderStatus(service = {}) {
  const ready = service.wechat_ready; const dot = $('#statusDot'); dot.className = `status-dot ${ready ? 'ready' : service.last_error ? 'error' : ''}`;
  $('#statusText').textContent = ready ? '实时监测中' : service.last_error ? '等待恢复' : '正在连接';
  $('#statusDetail').textContent = service.last_error || (service.last_sync_at ? `最近同步 ${new Date(service.last_sync_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}` : '正在初始化本地数据库');
}

function renderMetrics(counts) { ['open','overdue','today','missing'].forEach(k => $(`#${k}Count`).textContent = counts[k] || 0); }
function taskHtml(task) {
  const attachmentIssue = ['missing','unreadable'].includes(task.attachment_state);
  const attachmentText = task.attachment_state === 'unreadable' ? '附件需手动查看' : '附件未下载';
  return `<article class="task-item ${task.status === 'done' ? 'done' : ''}" data-task="${task.id}">
    <input class="task-check" type="checkbox" ${task.status === 'done' ? 'checked' : ''} aria-label="完成事项">
    <div class="task-main"><div class="task-title">${escapeHtml(task.title)}</div><div class="task-meta">
      <span class="pill ${task.priority}">${priorityText(task.priority)}</span>
      ${task.source_group_name ? `<span class="pill">${escapeHtml(task.source_group_name)}</span>` : '<span class="pill">手动添加</span>'}
      ${attachmentIssue ? `<span class="pill missing">${attachmentText}</span>` : ''}
    </div></div><div class="task-due"><strong>${dateText(task.due_at)}</strong><span>${task.evidence_count || 0} 条依据</span></div></article>`;
}
function renderTasks(tasks) {
  state.tasks = tasks; const list = $('#taskList');
  list.innerHTML = tasks.length ? tasks.map(taskHtml).join('') : '<div class="empty-state">现在没有待办。轻松一下，或去选择要关注的群聊。</div>';
  list.querySelectorAll('.task-item').forEach(node => {
    node.querySelector('.task-main').onclick = () => openTask(Number(node.dataset.task));
    node.querySelector('.task-check').onchange = async e => { e.stopPropagation(); await api(`/api/tasks/${node.dataset.task}`, {method:'PATCH', body:JSON.stringify({status:e.target.checked?'done':'open'})}); await refresh(); };
  });
  const missing = tasks.filter(t => ['missing','unreadable'].includes(t.attachment_state));
  $('#missingList').innerHTML = missing.length ? missing.slice(0,6).map(t => `<div class="missing-item" data-task="${t.id}"><strong>${escapeHtml(t.title)}</strong><span>${escapeHtml(t.source_group_name || '')} · ${t.attachment_state === 'unreadable' ? '已下载，需手动查看' : '需要手动下载'}</span></div>`).join('') : '<p class="muted">目前没有需要人工处理的附件</p>';
  $$('.missing-item').forEach(n => n.onclick = () => openTask(Number(n.dataset.task)));
  renderMiniCalendar(); renderMonth();
}

function renderMiniCalendar() {
  const now = new Date(), year=now.getFullYear(), month=now.getMonth(); $('#miniMonthTitle').textContent = `${year} 年 ${month+1} 月`;
  const first = new Date(year,month,1), start=(first.getDay()+6)%7, days=new Date(year,month+1,0).getDate();
  const dueDays = new Set(state.tasks.filter(t=>t.due_at).map(t=>String(t.due_at).slice(0,10)));
  let html=['一','二','三','四','五','六','日'].map(x=>`<span class="mini-week">${x}</span>`).join('');
  const prevDays=new Date(year,month,0).getDate(); for(let i=0;i<start;i++) html+=`<span class="mini-day muted">${prevDays-start+i+1}</span>`;
  for(let d=1;d<=days;d++){const date=`${year}-${String(month+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`; const today=d===now.getDate(); html+=`<span class="mini-day ${today?'today ':''}${dueDays.has(date)?'has-task':''}">${d}</span>`;}
  $('#miniCalendar').innerHTML=html;
}

function renderMonth() {
  const root=$('#monthGrid'); if(!root) return; const view=new Date(state.month.getFullYear(),state.month.getMonth(),1); const y=view.getFullYear(),m=view.getMonth();
  $('#calendarTitle').textContent=`${y} 年 ${m+1} 月`; const start=(view.getDay()+6)%7, total=new Date(y,m+1,0).getDate(), prev=new Date(y,m,0).getDate(); const today=new Date();
  const grouped={}; state.tasks.forEach(t=>{if(t.due_at)(grouped[String(t.due_at).slice(0,10)] ||= []).push(t)}); let html='';
  for(let i=0;i<42;i++){let d,other=false,dy=y,dm=m;if(i<start){d=prev-start+i+1;other=true;dm=m-1;if(dm<0){dm=11;dy--}}else if(i>=start+total){d=i-start-total+1;other=true;dm=m+1;if(dm>11){dm=0;dy++}}else d=i-start+1; const key=`${dy}-${String(dm+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`; const isToday=key===`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`; const items=(grouped[key]||[]).slice(0,4).map(t=>`<button class="day-task ${t.priority}" data-task="${t.id}">${escapeHtml(t.title)}</button>`).join(''); html+=`<div class="calendar-day ${other?'other ':''}${isToday?'today':''}"><span class="day-number">${d}</span><div class="day-tasks">${items}</div></div>`;}
  root.innerHTML=html; root.querySelectorAll('[data-task]').forEach(n=>n.onclick=()=>openTask(Number(n.dataset.task)));
}

function renderGroups() {
  const q=$('#groupSearch').value.trim().toLowerCase(); const rows=state.groups.filter(g=>!q||g.name.toLowerCase().includes(q));
  $('#groupList').innerHTML=rows.length?rows.map(g=>`<div class="group-row"><div class="group-avatar">${escapeHtml(g.name.slice(0,1))}</div><div class="group-copy"><strong title="${escapeHtml(g.name)}">${escapeHtml(g.name)}</strong><span>${g.message_count||0} 条已收录 · ${g.open_tasks||0} 个待办</span></div><label class="switch"><input type="checkbox" data-group="${encodeURIComponent(g.id)}" ${g.selected?'checked':''}><span class="switch-track"></span></label></div>`).join(''):'<div class="empty-state">没有匹配的群聊</div>';
  $('#groupList').querySelectorAll('[data-group]').forEach(input=>input.onchange=async()=>{input.disabled=true;try{await api(`/api/groups/${input.dataset.group}/select`,{method:'POST',body:JSON.stringify({selected:input.checked,lookback_days:7})});toast(input.checked?'已开始关注':'已取消关注',input.checked?'正在回看最近 7 天消息':'不会再采集这个群的新消息');await refresh();}catch(e){input.checked=!input.checked;toast('设置失败',e.message,true)}finally{input.disabled=false}});
}

async function openTask(id) {
  const data=await api(`/api/tasks/${id}`), t=data.task; $('#taskId').value=t.id; $('#dialogTitle').textContent='事项详情'; $('#taskTitle').value=t.title; $('#taskDescription').value=t.description||t.action_text||''; $('#taskPriority').value=t.priority; $('#taskDue').value=t.due_at ? String(t.due_at).slice(0,16) : '';
  const evidence=$('#taskEvidence'); if(t.messages?.length){evidence.classList.remove('hidden');evidence.innerHTML='<strong>原始依据</strong>'+t.messages.map(m=>`<p>${new Date(m.create_time*1000).toLocaleString('zh-CN')} · ${escapeHtml(m.sender_name||'')}<br>${escapeHtml((m.content||m.file_name||'').slice(0,500))}</p>`).join('')}else evidence.classList.add('hidden'); $('#taskDialog').showModal();
}
function openNewTask(){ $('#taskForm').reset();$('#taskId').value='';$('#dialogTitle').textContent='新建事项';$('#taskEvidence').classList.add('hidden');$('#taskPriority').value='normal';$('#taskReminder').value='1440';$('#taskDialog').showModal(); }
async function saveTask(event){event.preventDefault();const id=$('#taskId').value;const due=$('#taskDue').value;const payload={title:$('#taskTitle').value,description:$('#taskDescription').value,priority:$('#taskPriority').value,due_at:due||null,reminder_lead_minutes:Number($('#taskReminder').value)}; if(id) await api(`/api/tasks/${id}`,{method:'PATCH',body:JSON.stringify(payload)});else await api('/api/tasks',{method:'POST',body:JSON.stringify(payload)});$('#taskDialog').close();toast('已保存','事项和提醒已更新');await refresh();}

function switchView(view){state.view=view;$$('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.view===view));$$('.view').forEach(n=>n.classList.remove('active'));$(`#${view}View`).classList.add('active');$('#pageTitle').textContent=({today:'今日总览',calendar:'日程月历',groups:'关注群聊'})[view];if(view==='calendar')renderMonth();if(view==='groups')renderGroups();}
async function checkNotifications(){try{const data=await api(`/api/notifications?after=${state.lastNotification}`);for(const n of data.notifications){state.lastNotification=Math.max(state.lastNotification,n.id);toast(n.kind.includes('attachment')?'附件需要人工处理':'新的事项提醒',n.text);if(Notification.permission==='granted')new Notification('知序 · 学校事务',{body:n.text});}localStorage.setItem('lastSchoolNotification',state.lastNotification);}catch(_) {}}
async function refresh(){try{const data=await api('/api/dashboard');state.dashboard=data;state.groups=data.groups;renderStatus(data.service);renderMetrics(data.counts);renderTasks(data.tasks);renderGroups();await checkNotifications();}catch(e){renderStatus({last_error:e.message});}}

document.addEventListener('DOMContentLoaded',()=>{
  $('#dateLine').textContent=new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
  $$('.nav-item').forEach(n=>n.onclick=()=>switchView(n.dataset.view));$$('.segmented button').forEach(n=>n.onclick=async()=>{$$('.segmented button').forEach(x=>x.classList.remove('active'));n.classList.add('active');state.filter=n.dataset.filter;const d=await api(`/api/tasks?status=${state.filter}`);renderTasks(d.tasks)});
  $('#syncButton').onclick=async()=>{await api('/api/sync',{method:'POST',body:'{}'});toast('已请求同步','后台会读取最新本地消息');}; $('#addTaskButton').onclick=openNewTask;$('#openCalendarButton').onclick=()=>switchView('calendar');$('#taskForm').addEventListener('submit',saveTask);$('#groupSearch').oninput=renderGroups;
  $('#prevMonth').onclick=()=>{state.month=new Date(state.month.getFullYear(),state.month.getMonth()-1,1);renderMonth()};$('#nextMonth').onclick=()=>{state.month=new Date(state.month.getFullYear(),state.month.getMonth()+1,1);renderMonth()};$('#todayMonth').onclick=()=>{state.month=new Date();renderMonth()};
  $('#notifyButton').onclick=async()=>{if(!('Notification'in window)){toast('浏览器不支持系统通知','页面内提醒仍然可用',true);return}const p=await Notification.requestPermission();toast(p==='granted'?'通知已开启':'未开启通知',p==='granted'?'到期事项会弹出浏览器通知':'可稍后在浏览器设置中允许')};
  $$('[data-close-dialog]').forEach(button=>button.onclick=()=>$('#taskDialog').close());
  refresh();setInterval(refresh,5000);
});
