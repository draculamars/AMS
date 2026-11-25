const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const size = n => `${(Number(n || 0) / 1024 ** 3).toFixed(2)} GiB`;
const states = {planned:'待确认方案',running:'进行中',paused:'已暂停',failed:'需要处理',ready_to_activate:'内容已校验 · 待接续验收',activation_prepared:'接续副本已准备',preparing_activation:'正在准备接续',activation_failed:'接续需要处理',activated_pending_acceptance:'已接入 · 待原生验收',native_verified:'原生会话验收通过',complete:'迁移验收通过',received:'此节点已接收'};

export async function mountCodexMigration(host, {api, toast}) {
  let disposed = false, selected = null, refreshing = false, busy = false, receipt = null;
  const post = (url, body = {}) => api(url, {method:'POST',body:JSON.stringify(body)});
  host.innerHTML = `<div class="page-heading"><div><p class="eyebrow">CODEX MIGRATION</p><h1>迁移你的开发工作空间</h1><p class="subtitle">工程、对话、技能和持久化记忆一起迁移。保留来源与目标原有内容，逐阶段验证。</p></div></div>
    <div class="notice">先复制，再校验，最后验证新机能够接续工作。源机文件始终保留。</div>
    <section class="panel settings-card" style="margin-top:20px"><h2>新建迁移</h2><form id="codex-migration-form">
    <div class="actions" style="flex-wrap:wrap;align-items:end"><label>目标 SSH<input name="host" placeholder="user@host 或 SSH 别名" required></label><label>端口<input name="port" type="number" min="1" max="65535" value="22" required></label><label style="flex:1;min-width:240px">目标暂存目录<input name="root" placeholder="留空则在目标用户下创建独立目录" style="width:100%"></label><button class="primary">检查资源并生成方案</button></div>
    <label style="display:block;margin-top:16px">补充来源目录（每行一个，完整保留其内容）<textarea name="extra_roots" rows="3" style="width:100%" placeholder="工程以外的模型、数据或文档目录"></textarea></label>
    <label><input name="include_atlas" type="checkbox" checked> 同时迁移本机 Atlas 常驻目录库、归档文件与备份工具</label></form><p id="codex-migration-error" class="error" role="alert"></p></section>
    <section class="panel settings-card" style="margin-top:20px"><h2>迁移任务</h2><div id="codex-migration-jobs"></div></section>
    <section id="codex-migration-detail" style="margin-top:20px"></section>
    <section class="panel settings-card" style="margin-top:20px"><h2>内存、缓存与磁盘怎么处理</h2><div id="codex-migration-policies"></div></section>`;
  const error = e => {host.querySelector('#codex-migration-error').textContent = e.message;toast(e.message,true)};
  host.querySelector('form').onsubmit = async event => {
    event.preventDefault();const button=event.submitter;button.disabled=true;
    host.querySelector('#codex-migration-error').textContent='';
    try {
      const data=new FormData(event.target);const body={host:data.get('host').trim(),port:Number(data.get('port')),include_atlas:data.has('include_atlas'),extra_roots:data.get('extra_roots').split('\n').map(s=>s.trim()).filter(Boolean)};
      if(data.get('root').trim())body.root=data.get('root').trim();
      const plan=await post('/api/codex-migrations/plan',body);selected=plan.id;await refresh();
    } catch(e) {error(e)} finally {button.disabled=false}
  };
  async function show(plan) {
    const target=host.querySelector('#codex-migration-detail');
    const progress=plan.progress||{},sources=plan.source?.databases||[],destinations=plan.target?.databases||[];
    const count = rows => rows.find(x=>x.path.endsWith('/state_5.sqlite'))?.threads ?? '—';
    target.innerHTML=`<div class="panel settings-card"><div class="actions" style="justify-content:space-between"><h2>${esc(plan.name)}</h2><span class="badge">${esc(states[plan.status]||plan.status)}</span></div>
      <p><strong>${esc(plan.host)}</strong> · ${esc(plan.root)}</p><p>${esc(plan.detail)}</p>
      <div class="actions" style="flex-wrap:wrap;margin:16px 0">${[
        ['文件与链接',plan.result?.status==='passed'],['原生会话与历史',plan.native_acceptance?.status==='passed'],
        ['工程与 Git',plan.workspace_acceptance?.status==='passed'],['目标 Atlas',plan.atlas_deployment?.status==='running']
      ].map(([label,ok])=>`<span class="badge">${ok?'✓':'○'} ${label}${ok?' 已通过':' 待验证'}</span>`).join('')}</div>
      ${plan.native_acceptance?`<p>原生读取 ${Number(plan.native_acceptance.read_threads||0)} 条会话 · 保留目标原有 ${Number(plan.native_acceptance.preserved_target_threads||0)} 条 · 历史分页验证 ${Number(plan.native_acceptance.history_samples?.length||0)} 条</p>`:''}
      ${plan.workspace_acceptance?`<p>Git 仓库 ${Number(plan.workspace_acceptance.repositories?.length||0)} 个 · 缺失工程目录 ${Number(plan.workspace_acceptance.missing_thread_workspaces?.length||0)} 个 · Git 问题 ${Number(plan.workspace_acceptance.git_errors?.length||0)} 项。项目构建与设备连接另行验收。</p>`:''}
      ${plan.atlas_deployment?`<p>目标本机入口：<code>${esc(plan.atlas_deployment.local_url_on_target)}</code> · 此地址在目标设备上访问。</p>`:''}
      <div class="actions" style="flex-wrap:wrap"><span>目标可用 ${size(plan.target?.free_bytes)}</span><span>来源 ${count(sources)} 条会话索引</span><span>目标原有 ${count(destinations)} 条</span></div>
      <p>${plan.phase==='copy'||plan.phase==='reconcile'?`目录 ${Number(progress.root||0)+1}/${progress.roots||plan.roots.length} · 本次连接已传 ${Number(progress.files_transferred_in_attempt||0).toLocaleString()} 个文件${progress.directory_entries?` · 当前目录共 ${Number(progress.directory_entries).toLocaleString()} 项`:""}`:`已哈希 ${Number(progress.entries_hashed||0).toLocaleString()} 项 · ${size(progress.bytes_hashed)}`}</p>
      ${plan.manifest?`<p>清单：${plan.manifest.entries.toLocaleString()} 项 · ${size(plan.manifest.bytes)} · ${plan.manifest.link_reviews} 项软链接需核对</p>`:''}
      <details><summary>两端资源检查</summary><div class="table-wrap"><table class="asset-table"><thead><tr><th>资源</th><th>来源</th><th>目标</th></tr></thead><tbody><tr><td>系统 / 架构</td><td>${esc(plan.source?.platform)} / ${esc(plan.source?.architecture)}</td><td>${esc(plan.target?.platform)} / ${esc(plan.target?.architecture)}</td></tr><tr><td>物理内存</td><td>${size(plan.source?.memory?.physical_bytes)}</td><td>${size(plan.target?.memory?.physical_bytes)}</td></tr><tr><td>可用磁盘</td><td>${size(plan.source?.free_bytes)}</td><td>${size(plan.target?.free_bytes)}</td></tr><tr><td>系统交换区</td><td>${esc(plan.source?.memory?.swap||'—')}</td><td>${esc(plan.target?.memory?.swap||'—')}</td></tr></tbody></table></div><p>资源值为创建方案时的预检快照。复制过程分块读取，仅暂存单个数据库；不迁移 RAM 或 swap 文件。</p></details>
      <details ${plan.status==='planned'?'open':''}><summary>逐目录迁移方案 · ${plan.roots.length} 项</summary><div class="table-wrap"><table class="asset-table"><thead><tr><th>来源</th><th>目标使用路径</th></tr></thead><tbody>${plan.roots.map(r=>`<tr><td style="overflow-wrap:anywhere">${esc(r.source)}</td><td style="overflow-wrap:anywhere">${esc(r.target)}</td></tr>`).join('')}</tbody></table></div></details>
      <details><summary>数据库、排除项与来源缺口</summary><p>${plan.databases.length} 个数据库单独生成一致快照。</p>${plan.missing_source_roots.map(s=>`<p class="error">来源已不存在：${esc(s)}</p>`).join('')}${plan.excluded.map(r=>`<p><code>${esc(r.path)}</code><br><small>${esc(r.reason)}</small></p>`).join('')}</details>
      <div class="actions" style="margin-top:20px">${['planned','failed','paused'].includes(plan.status)?'<button class="primary" data-start>已核对方案，开始／续传</button>':''}${plan.status==='running'?'<button class="secondary" data-pause>暂停传输</button>':''}${plan.status==='ready_to_activate'?'<button class="primary" data-prepare>准备远端接续环境</button>':''}${plan.status==='activation_prepared'?'<button class="primary" data-activate>保留原有资料，重启目标 Codex 并接入</button>':''}${['activated_pending_acceptance','native_verified'].includes(plan.status)?'<button class="primary" data-native>验证原生 Codex 会话与历史</button>':''}${['activated_pending_acceptance','native_verified'].includes(plan.status)&&!plan.atlas_deployment?'<button class="secondary" data-deploy>在目标设备安装独立 Atlas 服务</button>':''}${['native_verified','activated_pending_acceptance'].includes(plan.status)?'<button class="secondary" data-workspaces>校验工程目录与 Git 历史</button>':''}${plan.status==='native_verified'&&plan.workspace_acceptance?.status==='passed'&&(!plan.atlas||plan.atlas_deployment)?'<button class="primary" data-complete>完成迁移验收</button>':''}<button class="secondary" data-receipt>查看校验与接收报告</button></div><pre data-receipt-output style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></div>`;
    if(receipt?.id===plan.id)target.querySelector('[data-receipt-output]').textContent=JSON.stringify(receipt.data,null,2);
    function action(selector, path, body, message) {
      target.querySelector(selector)?.addEventListener('click',async()=>{
        if(busy)return;busy=true;target.querySelectorAll('button').forEach(b=>b.disabled=true);
        try {const result=await post(`/api/codex-migrations/${plan.id}/${path}`,body||{});
          receipt={id:plan.id,data:result};toast(result.status==='failed'||result.status==='needs_attention'?'验收发现问题，请查看报告':message,result.status==='failed'||result.status==='needs_attention');
        } catch(e) {error(e)} finally {busy=false;await refresh()}
      });
    }
    action('[data-deploy]','deploy-atlas',{},'目标 Atlas 服务已启动');
    action('[data-native]','verify-native',{},'原生会话验收完成');
    action('[data-workspaces]','verify-workspaces',{},'工程与 Git 校验完成');
    action('[data-complete]','complete',{},'迁移验收通过，来源和原始快照保留');
    action('[data-prepare]','prepare-profile',{},'开始准备远端接续环境');
    action('[data-activate]','activate',{reviewed_plan_id:plan.id,allow_app_restart:true},'正在重启目标应用并接入');
    action('[data-start]','start',{reviewed_plan_id:plan.id},'迁移已启动，关闭网页后任务仍会继续');
    action('[data-pause]','pause',{},'已请求暂停，正在保存可续传状态');
    target.querySelector('[data-receipt]')?.addEventListener('click',async()=>{
      if(busy)return;busy=true;
      try{receipt={id:plan.id,data:await api(`/api/codex-migrations/${plan.id}/verification`)}}
      catch(e){error(e)}finally{busy=false;await refresh()}
    });
  }
  async function refresh() {
    if(disposed||refreshing||busy)return;refreshing=true;
    try {
      const data=await api('/api/codex-migrations');if(disposed)return;
      host.querySelector('#codex-migration-jobs').innerHTML=data.items.length?data.items.map(p=>`<p><button class="secondary" data-plan="${esc(p.id)}">${esc(p.name)} · ${esc(p.host)}</button> <span class="badge">${esc(states[p.status]||p.status)}</span><br><small>${esc(p.detail)}</small></p>`).join(''):'<p>尚无迁移任务。填写目标 SSH 后，Atlas 将检查目标已有内容和可用资源。</p>';
      host.querySelectorAll('[data-plan]').forEach(b=>b.onclick=async()=>{selected=b.dataset.plan;await refresh()});
      host.querySelector('#codex-migration-policies').innerHTML=data.policies.map(p=>`<p><strong>${esc(p.kind)}</strong><br>${esc(p.reason)}</p>`).join('');
      if(!selected&&data.items.length)selected=data.items[0].id;
      if(selected){const p=await api('/api/codex-migrations/'+selected);if(!disposed)await show(p)}
    } catch(e) {if(!disposed)error(e)} finally {refreshing=false}
  }
  await refresh();const timer=setInterval(refresh,4000);
  return {dispose(){disposed=true;clearInterval(timer)}};
}
