const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function markdown(text,links=[],onOpen){
 const linkMap=new Map(links.map(x=>[x.reference,x]));
 const tokens=[];
 function inline(value){
  let safe=esc(value);
  safe=safe.replace(/(!?)\[([^\]]*)\]\(([^\s)]+)(?:\s+&quot;[^)]*)?\)/g,(_,image,label,encoded)=>{
   const ref=encoded.replace(/&amp;/g,'&'),link=linkMap.get(ref),i=tokens.length;tokens.push({ref,link});
   if(image&&link?.target_asset_id&&/\.(png|jpe?g|webp|gif)$/i.test(link.target_path))return `<img class="project-doc-image" src="/api/assets/${link.target_asset_id}/file?inline=1" alt="${label}">`;
   if(link?.target_path)return `<button class="document-link" data-doc-link="${i}">${image?'▧ ':''}${label||encoded} ↗</button>`;
   if(/^https?:\/\//.test(ref))return `<a href="${esc(ref)}" target="_blank" rel="noopener noreferrer">${label||encoded} ↗</a>`;
   return `<span class="document-unresolved" title="${esc(ref)}">${label||encoded}</span>`;
  });
  return safe.replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
 }
 const lines=text.split('\n');let html='',code=null,list=false;
 for(const line of lines){
  if(line.startsWith('```')){if(code!==null){html+='<pre><code>'+esc(code.join('\n'))+'</code></pre>';code=null}else{if(list){html+='</ul>';list=false}code=[]}continue}
  if(code!==null){code.push(line);continue}
  const heading=line.match(/^(#{1,6})\s+(.+)/),item=line.match(/^\s*[-*+]\s+(.+)/);
  if(list&&!item){html+='</ul>';list=false}
  if(heading){const n=Math.min(heading[1].length+1,6);html+=`<h${n}>${inline(heading[2])}</h${n}>`}
  else if(item){if(!list){html+='<ul>';list=true}html+='<li>'+inline(item[1])+'</li>'}
  else if(line.startsWith('> '))html+='<blockquote>'+inline(line.slice(2))+'</blockquote>';
  else if(/^\s*([-*_])\1\1+\s*$/.test(line))html+='<hr>';
  else if(line.trim())html+='<p>'+inline(line)+'</p>';
 }
 if(list)html+='</ul>';if(code!==null)html+='<pre><code>'+esc(code.join('\n'))+'</code></pre>';
 const element=document.createElement('article');element.className='project-document';element.innerHTML=html;
 element.querySelectorAll('[data-doc-link]').forEach(b=>b.onclick=()=>onOpen?.(tokens[+b.dataset.docLink]));return element;
}

export async function renderPreview(host,asset,{api,links=[],onOpen,version=null,git=null}={}){
 let viewer,dead=false;const dispose=()=>{dead=true;viewer?.dispose()};
 const ext=asset.ext||'',name=asset.name||asset.path?.split('/').at(-1)||'';
 const query=git?new URLSearchParams({ref:git.ref,path:git.path}):new URLSearchParams(version?{version}:{});
 const base=git?`/api/repositories/${git.rid}`:`/api/assets/${asset.id}`;
 const url=base+'/file?'+query;
 host.innerHTML='<div class="empty">正在读取文件内容…</div>';
 const ready=(async()=>{
  if(['.stl','.obj','.step','.stp','.iges','.igs','.brep','.glb','.gltf','.3mf','.urdf','.blend'].includes(ext)){
   if(git&&ext==='.blend'){host.innerHTML='<div class="empty">此 Git 历史版本的 Blender 原件可以下载。当前原生场景预览在模型工程页提供。</div>';return}
   const {mountModel}=await import('/static/model-viewer.js');if(dead)return;
   viewer=mountModel(host,{...asset,name},{version,contextUrl:git?base+'/model-context?'+query:null});await viewer.ready;return;
  }
  if(dead)return;
  if(['.png','.jpg','.jpeg','.webp','.gif','.bmp'].includes(ext)){host.innerHTML=`<div class="project-image"><img src="${url}&inline=true" alt="${esc(name)}"></div>`;return}
  if(ext==='.pdf'){host.innerHTML=`<iframe class="project-pdf" title="${esc(name)}" src="${url}&inline=true"></iframe>`;return}
  if(['.mp4','.webm','.mov','.mp3','.wav','.ogg','.flac'].includes(ext)){const tag=['.mp3','.wav','.ogg','.flac'].includes(ext)?'audio':'video';host.innerHTML=`<${tag} controls src="${url}&inline=true" class="project-media"></${tag}>`;return}
  const data=await api(base+'/preview?'+query);if(dead)return;
  if(data.type==='text'){
   if(['.md','.markdown'].includes(ext)){host.innerHTML='';host.append(markdown(data.text,links,onOpen))}
   else host.innerHTML='<pre class="project-code">'+esc(data.text)+'</pre>';
   if(data.truncated)host.insertAdjacentHTML('beforeend','<p class="project-footnote">内容较长，预览已截断；下载可获得完整文件。</p>');
  }else if(data.type==='table')host.innerHTML='<div class="table-wrap"><table class="asset-table">'+data.rows.map((r,i)=>'<tr>'+r.map(c=>`<${i?'td':'th'}>${esc(c)}</${i?'td':'th'}>`).join('')+'</tr>').join('')+'</table></div>';
  else host.innerHTML='<div class="empty">'+esc(data.message||'请下载后使用对应专业软件查看')+'</div>';
 })().catch(e=>{if(!dead&&e.name!=='AbortError')host.innerHTML='<div class="empty">'+esc(e.message)+'</div>'});
 return {ready,dispose};
}
