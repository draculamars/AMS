let installed=false;
export async function portableBanner(api,toast){
 if(installed)return;
 const state=await api('/api/portable');installed=true;
 if(!state.enabled)return;
 document.title='Atlas 随行资产库';
 const panel=document.createElement('div');panel.className='notice';panel.id='portable-banner';
 const text=document.createElement('span');
 const date=state.captured_at?new Date(state.captured_at).toLocaleString('zh-CN'):'';
 text.textContent=`随行资产库 · ${date} 导出。${state.notice}`;
 const stop=document.createElement('button');stop.textContent='安全停止';
 stop.onclick=async()=>{
  stop.disabled=true;
  try{const result=await api('/api/portable/stop',{method:'POST',body:'{}'});text.textContent=result.message;stop.textContent='正在保存…'}
  catch(error){toast(error.message,true);stop.disabled=false}
 };
 panel.append(text,stop);document.querySelector('.page').prepend(panel);
 const refresh=document.querySelector('#rescan');if(refresh)refresh.hidden=true;
 for(const view of ['governance','migration']){const button=document.querySelector(`[data-view="${view}"]`);if(button)button.hidden=true;}
 const label=document.querySelector('.status-pill');if(label)label.textContent='磁盘内独立副本';
}
