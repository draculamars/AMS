const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export async function mountPortableRecovery(el,{api,toast}){
 let closed=false;
 async function refresh(){
  const items=await api('/api/portable/backups');if(closed)return;
  el.innerHTML=`<div class="steward-intro"><div><p class="eyebrow">ATLAS / PORTABLE RECOVERY</p><h1>SSD 备份与恢复</h1><p>明文保存 · 无需恢复密钥 · 保留每次目录与版本状态</p></div><button id="portable-create-backup" class="primary">创建明文恢复点</button></div><div class="panel vault-intro"><strong>盘内恢复点</strong><p>保存一致的目录库、网页清单和 Git 历史，共享盘内不可变资产文件。用于恢复误修改；整盘独立备份请复制完整 AssetVault 目录。</p><p>恢复前请安全停止 Atlas，再运行磁盘上的 Restore Atlas 启动器并输入恢复点编号。系统先完整校验，再创建新数据集，原数据集保留。</p></div><div class="vault-snapshots">${items.map(x=>`<article class="panel vault-profile"><div class="vault-card-top"><span class="badge green">明文 · 已保存</span><time>${esc(new Date(x.created_at).toLocaleString())}</time></div><h2>${Number(x.objects).toLocaleString()} 个内容对象</h2><p>${Number(x.metadata_files).toLocaleString()} 个目录与历史文件</p><p>完整校验：${x.verification?.status==='passed'?'已通过':x.verification?.status==='failed'?'失败，请保留磁盘并检查内容':'待执行'}</p><code>${esc(x.id)}</code><div class="vault-actions"><button data-check="${esc(x.id)}">校验全部内容</button><button data-copy="${esc(x.id)}">复制恢复点编号</button></div></article>`).join('')||'<div class="panel vault-intro">尚未建立明文恢复点。资产本身已保存在盘内，可以随时创建当前目录状态的恢复点。</div>'}</div>`;
  const job=async url=>{await api(url,{method:'POST',body:'{}'});toast('任务已开始，进度见任务记录。')};
  el.querySelector('#portable-create-backup').onclick=()=>job('/api/portable/backup').catch(e=>toast(e.message,true));
  el.querySelectorAll('[data-check]').forEach(b=>b.onclick=()=>job('/api/portable/backups/'+b.dataset.check+'/verify').catch(e=>toast(e.message,true)));
  el.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>navigator.clipboard.writeText(b.dataset.copy).then(()=>toast('恢复点编号已复制')).catch(e=>toast(e.message,true)));
 }
 await refresh();const timer=setInterval(()=>refresh().catch(()=>{}),6000);
 return {dispose(){closed=true;clearInterval(timer)}};
}
