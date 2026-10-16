import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {STLLoader} from 'three/addons/loaders/STLLoader.js';
import {OBJLoader} from 'three/addons/loaders/OBJLoader.js';
import {MTLLoader} from 'three/addons/loaders/MTLLoader.js';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {ThreeMFLoader} from 'three/addons/loaders/3MFLoader.js';

const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const palette=[0x7599c1,0xd5a568,0x78b5a4,0xa49bc8,0xc6cdd6,0xd28c8d,0x86acc0];
const vec=(str,fallback=[0,0,0])=>str?str.trim().split(/\s+/).map(Number):fallback;
const textDecoder=new TextDecoder();

export function mountModel(container,asset,options={}){
 const abort=new AbortController();let disposed=false,renderer,controls,scene,camera,root,resize,observer,worker,frame,selected;const warnings=[];
 container.innerHTML=`<div class="model-viewer"><div class="model-toolbar"><div><button data-v="fit" title="适合窗口">⌖ 全部</button><button data-v="front">正视</button><button data-v="top">俯视</button><button data-v="iso">等轴</button><button data-v="wire">线框</button><button data-v="shot">↓ 图片</button></div><span class="model-format">${esc(asset.ext.slice(1).toUpperCase())}</span></div><div class="model-stage"><div class="model-canvas"></div><div class="model-loading">正在加载工程几何…</div><aside class="model-structure"><h4>装配结构</h4><div class="model-tree"></div></aside></div><div class="model-controls"><label>分解查看 <input data-v="explode" aria-label="分解查看" type="range" min="0" max="100" value="0"></label><label><input type="checkbox" data-v="section"> 剖切 <input data-v="cut" aria-label="剖切位置" type="range" min="0" max="100" value="50"></label><span class="model-metrics"></span></div><div class="model-joints"></div><p class="model-notes">拖拽旋转 · 右键平移 · 滚轮 / 双指缩放。分解与剖切仅影响预览。</p></div>`;
 const shell=container.firstElementChild,canvasHost=shell.querySelector('.model-canvas'),loading=shell.querySelector('.model-loading'),tree=shell.querySelector('.model-tree'),metrics=shell.querySelector('.model-metrics'),notes=shell.querySelector('.model-notes');
 const live=()=>{if(disposed||abort.signal.aborted)throw new DOMException('预览已关闭','AbortError')};
 const request=async(url,init={})=>{live();const r=await fetch(url,{...init,signal:abort.signal,headers:{'X-Asset-Request':'1',...(init.body?{'Content-Type':'application/json'}:{}),...init.headers}});if(!r.ok){let d;try{d=await r.json()}catch{}throw Error(d?.detail||'读取模型失败：'+r.status)}return r};
 const fileUrl=(a)=>a.file_url||`/api/assets/${a.id}/file${a.version_id?'?version='+a.version_id:a.id===asset.id&&options.version?'?version='+options.version:''}`;
 const buffer=async a=>{if(a.size>120*1024*1024)throw Error('模型超过 120 MiB 浏览器预览上限，请选择工程内较小的导出模型');const response=await request(fileUrl(a));if(Number(response.headers.get('content-length'))>120*1024*1024)throw Error('模型当前版本超过 120 MiB 浏览器预览上限');return response.arrayBuffer()};
 const dispose=()=>{if(disposed)return;disposed=true;abort.abort();worker?.terminate();cancelAnimationFrame(frame);resize?.disconnect();observer?.disconnect();controls?.dispose();if(scene){const geometries=new Set(),materials=new Set(),textures=new Set();scene.traverse(o=>{if(o.geometry)geometries.add(o.geometry);for(const m of Array.isArray(o.material)?o.material:o.material?[o.material]:[]){materials.add(m);Object.values(m).filter(x=>x?.isTexture).forEach(t=>textures.add(t))}});geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());textures.forEach(t=>t.dispose())}renderer?.dispose();renderer?.forceContextLoss()};
 const ready=(async()=>{
 const context=await (await request(options.contextUrl||`/api/assets/${asset.id}/model-context${options.version?'?version='+options.version:''}`)).json();live();
 const byid=new Map(context.assets.map(a=>[a.id,a])),byPath=new Map(context.assets.map(a=>[a.relpath,a]));
 if(context.historical_dependencies)warnings.push(context.historical_dependencies);
 for(const link of context.links)if(!link.target)warnings.push('未加载引用：'+link.reference+'（'+link.status+'）');
 warnings.push(...context.errors);
 const manager=new THREE.LoadingManager();manager.setURLModifier(url=>{
  if(url.startsWith('data:')||url.startsWith('blob:'))return url;
  const parsed=new URL(url,'https://atlas.invalid/');
  if(parsed.hostname!=='atlas.invalid')throw Error('外部模型资源未获授权');
  const a=byPath.get(decodeURIComponent(parsed.pathname.slice(1)));
  if(!a)throw Error('工程资源未找到：'+decodeURIComponent(parsed.pathname));return fileUrl(a);
 });
 manager.onError=url=>warnings.push('材质或资源无法加载：'+url);
 const defaultMaterial=(index=0)=>new THREE.MeshStandardMaterial({color:palette[index%palette.length],roughness:.48,metalness:.12,side:THREE.DoubleSide});
 const transformed=(group,origin)=>{const xyz=vec(origin?.getAttribute('xyz')),rpy=vec(origin?.getAttribute('rpy'));group.position.set(...xyz);group.rotation.set(rpy[0],rpy[1],rpy[2],'ZYX')};
 const direct=(el,name)=>[...(el?.children||[])].find(e=>e.tagName===name);
 const joints=[];
 async function loadAsset(a){
  live();const ext=a.ext;loading.textContent='正在读取 '+a.name+'…';
  if(ext==='.blend'){
   let status=await (await request(`/api/assets/${a.id}/model-preview${options.version?'?version='+options.version:''}`)).json();
   if(status.status!=='ready'){
    const started=await (await request(`/api/assets/${a.id}/model-preview`,{method:'POST',body:JSON.stringify({version:options.version||null})})).json();
    if(started.job_id){let done=false;for(let i=0;i<180;i++){live();await new Promise(resolve=>setTimeout(resolve,1500));const jobs=await (await request('/api/jobs')).json(),job=jobs.find(x=>x.id===started.job_id);loading.textContent=job?.detail||'正在生成原生工程预览…';if(job?.status==='failed'||job?.status==='interrupted')throw Error(job.detail);if(job?.status==='complete'){done=true;break}}if(!done)throw Error('预览任务仍在运行，请稍后重新打开')}
    status=await (await request(`/api/assets/${a.id}/model-preview${options.version?'?version='+options.version:''}`)).json();
   }
   if(status.status!=='ready')throw Error(status.message||'原生工程预览尚未就绪');
   warnings.push(...(status.metadata.missing_dependencies||[]).map(x=>'原生工程缺失引用：'+x.reference));
   const data=await (await request(status.url)).arrayBuffer(),gltf=await new GLTFLoader(manager).parseAsync(data,'https://atlas.invalid/');gltf.scene.rotation.x=Math.PI/2;
   gltf.scene.name=a.name+' · 原生场景';gltf.scene.userData.source=asset.id;return gltf.scene;
  }
  const data=await buffer(a);live();
  if(ext==='.stl'){const mesh=new THREE.Mesh(new STLLoader().parse(data),defaultMaterial());mesh.name=a.name;return mesh}
  if(['.step','.stp','.iges','.igs','.brep'].includes(ext)){
   loading.textContent='正在解析 CAD 装配与曲面…';
   const result=await new Promise((resolve,reject)=>{worker=new Worker('/static/step-worker.js');const timeout=setTimeout(()=>{worker.terminate();reject(Error('CAD 解析超时，请选择零件或更小的工程'))},120000);worker.onmessage=({data})=>{clearTimeout(timeout);worker.terminate();data.error?reject(Error(data.error)):resolve(data.result)};worker.onerror=e=>{clearTimeout(timeout);reject(Error(e.message||'CAD 解析失败'))};abort.signal.addEventListener('abort',()=>{clearTimeout(timeout);worker?.terminate();reject(new DOMException('预览已关闭','AbortError'))},{once:true});worker.postMessage({buffer:data,ext},[data])});
   const meshes=result.meshes.map((m,i)=>{const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(m.attributes.position.array,3));if(m.attributes.normal)geometry.setAttribute('normal',new THREE.Float32BufferAttribute(m.attributes.normal.array,3));geometry.setIndex(m.index.array);if(!m.attributes.normal)geometry.computeVertexNormals();const material=defaultMaterial(i);if(m.color)material.color.setRGB(...m.color);const mesh=new THREE.Mesh(geometry,material);mesh.name=m.name||'零件 '+(i+1);return mesh});
   const build=n=>{const g=new THREE.Group();g.name=n.name||'装配';for(const i of n.meshes||[])if(meshes[i])g.add(meshes[i].clone());for(const child of n.children||[])g.add(build(child));return g};return build(result.root);
  }
  if(ext==='.obj'){
   const loader=new OBJLoader(manager),materials=context.links.filter(x=>x.source===a.id&&x.relation==='material'&&x.target);
   if(materials.length){const m=byid.get(materials[0].target);try{const creator=new MTLLoader(manager).parse(textDecoder.decode(await buffer(m)),'https://atlas.invalid/'+m.relpath.slice(0,m.relpath.lastIndexOf('/')+1));creator.preload();loader.setMaterials(creator)}catch(e){warnings.push('材质未加载：'+e.message)}}
   const object=loader.parse(textDecoder.decode(data));object.name=a.name;return object;
  }
  if(ext==='.glb'||ext==='.gltf'){const base='https://atlas.invalid/'+a.relpath.slice(0,a.relpath.lastIndexOf('/')+1);const gltf=await new GLTFLoader(manager).parseAsync(ext==='.gltf'?textDecoder.decode(data):data,base);gltf.scene.rotation.x=Math.PI/2;gltf.scene.name=a.name;return gltf.scene}
  if(ext==='.3mf'){const object=new ThreeMFLoader(manager).parse(data);object.name=a.name;return object}
  if(ext==='.urdf'){
   const xml=new DOMParser().parseFromString(textDecoder.decode(data),'application/xml');if(xml.querySelector('parsererror'))throw Error('URDF XML 无效');
   const robot=xml.documentElement,links=new Map(),robotGroup=new THREE.Group();robotGroup.name=robot.getAttribute('name')||a.name;
   const globalMaterials=new Map([...robot.children].filter(x=>x.tagName==='material').map(x=>[x.getAttribute('name'),x]));
   const linkElements=[...robot.children].filter(x=>x.tagName==='link');for(const el of linkElements){const g=new THREE.Group();g.name=el.getAttribute('name');links.set(g.name,g)}
   for(const el of linkElements){const g=links.get(el.getAttribute('name'));const visuals=[...el.children].filter(e=>e.tagName==='visual');for(const visual of visuals){const placement=new THREE.Group();placement.name=visual.getAttribute('name')||g.name+' · 外观';transformed(placement,direct(visual,'origin'));const geo=direct(visual,'geometry');let object;const mesh=direct(geo,'mesh');if(mesh){const reference=mesh.getAttribute('filename'),link=context.links.find(x=>x.source===a.id&&x.reference===reference&&x.target);if(link){try{object=await loadAsset(byid.get(link.target));object.scale.multiply(new THREE.Vector3(...vec(mesh.getAttribute('scale'),[1,1,1])))}catch(e){warnings.push(g.name+'：'+e.message)}}else warnings.push(g.name+' 缺失网格：'+reference)}else if(direct(geo,'box'))object=new THREE.Mesh(new THREE.BoxGeometry(...vec(direct(geo,'box').getAttribute('size'),[1,1,1])),defaultMaterial(links.size));else if(direct(geo,'sphere'))object=new THREE.Mesh(new THREE.SphereGeometry(Number(direct(geo,'sphere').getAttribute('radius')),32,16),defaultMaterial());else if(direct(geo,'cylinder')){const c=direct(geo,'cylinder'),geometry=new THREE.CylinderGeometry(Number(c.getAttribute('radius')),Number(c.getAttribute('radius')),Number(c.getAttribute('length')),32);geometry.rotateX(Math.PI/2);object=new THREE.Mesh(geometry,defaultMaterial())}
    if(object){const m=direct(visual,'material'),rgba=vec(direct(m,'color')?.getAttribute('rgba')||direct(globalMaterials.get(m?.getAttribute('name')),'color')?.getAttribute('rgba'),[]);if(rgba.length>=3)object.traverse(x=>{if(x.isMesh){x.material=defaultMaterial();x.material.color.setRGB(...rgba.slice(0,3));x.material.opacity=rgba[3]??1;x.material.transparent=x.material.opacity<1}});placement.add(object);g.add(placement)}}}
   const children=new Set();for(const el of [...robot.children].filter(x=>x.tagName==='joint')){const parent=links.get(direct(el,'parent')?.getAttribute('link')),child=links.get(direct(el,'child')?.getAttribute('link'));if(!parent||!child){warnings.push('关节链接缺失：'+el.getAttribute('name'));continue}let ancestor=parent,cycle=false;while(ancestor){if(ancestor===child){cycle=true;break}ancestor=ancestor.parent}if(cycle||children.has(child)){warnings.push('装配层级包含环或重复父节点');continue}const frame=new THREE.Group();frame.name=el.getAttribute('name');transformed(frame,direct(el,'origin'));parent.add(frame);frame.add(child);children.add(child);const type=el.getAttribute('type');if(['revolute','continuous','prismatic'].includes(type)){const limit=direct(el,'limit');const mimic=direct(el,'mimic');joints.push({name:frame.name,frame,child,type,mimic:mimic?{joint:mimic.getAttribute('joint'),multiplier:Number(mimic.getAttribute('multiplier')??1),offset:Number(mimic.getAttribute('offset')??0)}:null,value:0,axis:new THREE.Vector3(...vec(direct(el,'axis')?.getAttribute('xyz'),[1,0,0])).normalize(),min:type==='continuous'?-Math.PI:Number(limit?.getAttribute('lower')||0),max:type==='continuous'?Math.PI:Number(limit?.getAttribute('upper')||0)})}}
   for(const g of links.values())if(!children.has(g))robotGroup.add(g);if(!robotGroup.children.length)throw Error('URDF 装配关系没有根节点');return robotGroup;
  }
  throw Error('此原生格式尚无渲染器，请在工程内选择对应 STEP、GLB 或网格预览；原工程仍完整保留');
 }
 root=await loadAsset(context.asset);live();
 scene=new THREE.Scene();scene.background=new THREE.Color(0xeef2f7);scene.add(root);root.updateMatrixWorld(true);
 let hiddenEnvironment=0;root.traverse(o=>{if(/(^|[_ -])(studio|ground|floor|backdrop|background)([_ -]|$)/i.test(o.name)){o.visible=false;hiddenEnvironment++}});if(hiddenEnvironment)warnings.push('拍摄背景默认隐藏，可在装配结构中重新显示');const box=new THREE.Box3();root.traverseVisible(o=>{if(o.isMesh)box.union(new THREE.Box3().setFromObject(o))});if(box.isEmpty())throw Error('文件中未找到可渲染的几何，请选择工程中的装配或零件模型');const center=box.getCenter(new THREE.Vector3()),extent=box.getSize(new THREE.Vector3()),radius=Math.max(extent.x,extent.y,extent.z)||1;
 camera=new THREE.PerspectiveCamera(38,1,radius/10000,radius*1000);camera.up.set(0,0,1);
 renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setClearColor(0xeef2f7);renderer.localClippingEnabled=true;renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.15;canvasHost.append(renderer.domElement);renderer.domElement.setAttribute('aria-label','模型三维渲染，支持旋转、缩放与部件显隐');
 controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.target.copy(center);
 const fit=(direction=[1,-1,.8])=>{const halfFov=Math.min(THREE.MathUtils.degToRad(camera.fov/2),Math.atan(Math.tan(THREE.MathUtils.degToRad(camera.fov/2))*camera.aspect));const distance=box.getBoundingSphere(new THREE.Sphere()).radius/Math.sin(halfFov)*1.08;camera.position.copy(center).add(new THREE.Vector3(...direction).normalize().multiplyScalar(distance));camera.near=radius/10000;camera.far=radius*1000;camera.updateProjectionMatrix();controls.target.copy(center);controls.update()};fit();
 scene.add(new THREE.HemisphereLight(0xffffff,0x6a7890,2.8));for(const [position,intensity] of [[[1,-1,2],3.1],[[-1,1,1],1.8]]){const l=new THREE.DirectionalLight(0xffffff,intensity);l.position.copy(center).add(new THREE.Vector3(...position).multiplyScalar(radius));scene.add(l)}
 const grid=new THREE.GridHelper(radius*2.5,20,0xb6c3d4,0xd7dfe9);grid.rotation.x=Math.PI/2;grid.position.set(center.x,center.y,box.min.z-radius*.015);scene.add(grid);
 const meshes=[];let triangles=0;root.traverse(o=>{if(o.isMesh){meshes.push(o);triangles+=(o.geometry.index?.count||o.geometry.attributes.position?.count||0)/3;o.userData.initialPosition=o.position.clone();o.userData.explodeDirection=new THREE.Box3().setFromObject(o).getCenter(new THREE.Vector3()).sub(center).normalize();if(o.parent){const inverse=new THREE.Matrix4().copy(o.parent.matrixWorld).invert();o.userData.explodeDirection.transformDirection(inverse)}for(const m of Array.isArray(o.material)?o.material:[o.material])if(m){m.side=THREE.DoubleSide;m.needsUpdate=true}}});
 if(!meshes.length)throw Error('未找到可渲染网格');metrics.textContent=`${meshes.length} 个部件 · ${Math.round(triangles).toLocaleString()} 三角面`;shell.dataset.meshes=meshes.length;shell.dataset.triangles=Math.round(triangles);
 const all=()=>root.traverse(o=>o.visible=true);
 const nodeHTML=(o,depth=0)=>{if(depth>20)return '';const name=o.name||(o.isMesh?'几何部件':'装配组');return `<details class="model-node" ${depth<2?'open':''}><summary><input type="checkbox" data-node="${o.uuid}" aria-label="显示 ${esc(name)}" ${o.visible?'checked':''}><button data-select-node="${o.uuid}" title="选择 ${esc(name)}">${esc(name)}</button><button data-isolate="${o.uuid}" title="单独查看 ${esc(name)}">◎</button></summary>${o.children.map(c=>nodeHTML(c,depth+1)).join('')}</details>`};
 tree.innerHTML=`<button class="model-show-all">显示全部部件</button>${nodeHTML(root)}`;tree.querySelector('.model-show-all').onclick=()=>{all();tree.querySelectorAll('input').forEach(x=>x.checked=true)};
 const select=o=>{if(selected){selected.traverse(x=>{if(x.isMesh)for(const m of Array.isArray(x.material)?x.material:[x.material])if(m?.emissive)m.emissive.setHex(0)})}selected=o;selected.traverse(x=>{if(x.isMesh)for(const m of Array.isArray(x.material)?x.material:[x.material])if(m?.emissive)m.emissive.setHex(0x183e66)});tree.querySelectorAll('[data-select-node]').forEach(b=>b.classList.toggle('selected',b.dataset.selectNode===o.uuid))};
 tree.querySelectorAll('[data-node]').forEach(input=>{input.onclick=e=>e.stopPropagation();input.onchange=()=>{root.getObjectByProperty('uuid',input.dataset.node).visible=input.checked}});
 tree.querySelectorAll('[data-select-node]').forEach(b=>b.onclick=e=>{e.preventDefault();select(root.getObjectByProperty('uuid',b.dataset.selectNode))});
 tree.querySelectorAll('[data-isolate]').forEach(b=>b.onclick=e=>{e.preventDefault();all();const node=root.getObjectByProperty('uuid',b.dataset.isolate),keep=new Set();node.traverse(o=>keep.add(o));meshes.forEach(m=>m.visible=keep.has(m));select(node)});
 const raycaster=new THREE.Raycaster();let down;renderer.domElement.addEventListener('pointerdown',e=>down=[e.clientX,e.clientY]);renderer.domElement.addEventListener('pointerup',e=>{if(!down||Math.hypot(e.clientX-down[0],e.clientY-down[1])>4)return;const r=renderer.domElement.getBoundingClientRect();raycaster.setFromCamera(new THREE.Vector2((e.clientX-r.left)/r.width*2-1,-(e.clientY-r.top)/r.height*2+1),camera);const hit=raycaster.intersectObjects(meshes).find(x=>x.object.visible);if(hit)select(hit.object)});
 const plane=new THREE.Plane(new THREE.Vector3(0,0,-1),center.z);let wire=false;
 shell.querySelectorAll('button[data-v]').forEach(b=>b.onclick=()=>{const v=b.dataset.v;if(v==='fit'||v==='iso')fit();if(v==='front')fit([0,-1,0]);if(v==='top')fit([0,-.001,1]);if(v==='wire'){wire=!wire;meshes.forEach(o=>{for(const m of Array.isArray(o.material)?o.material:[o.material])m.wireframe=wire});b.classList.toggle('active',wire)}if(v==='shot'){renderer.render(scene,camera);const a=document.createElement('a');a.download=asset.name+'-预览.png';a.href=renderer.domElement.toDataURL('image/png');a.click()}});
 shell.querySelector('[data-v=explode]').oninput=e=>meshes.forEach(o=>o.position.copy(o.userData.initialPosition).addScaledVector(o.userData.explodeDirection,radius*Number(e.target.value)/120));
 shell.querySelector('[data-v=section]').onchange=e=>{meshes.forEach(o=>{for(const m of Array.isArray(o.material)?o.material:[o.material]){m.clippingPlanes=e.target.checked?[plane]:[];m.needsUpdate=true}})};shell.querySelector('[data-v=cut]').oninput=e=>plane.constant=box.min.z+extent.z*Number(e.target.value)/100;
 if(joints.length){
  const section=shell.querySelector('.model-joints'),free=joints.filter(j=>!j.mimic),byName=new Map(joints.map(j=>[j.name,j]));
  const applyJoints=()=>{const active=new Set(),done=new Set();function apply(j){if(done.has(j))return;if(active.has(j)){warnings.push('URDF mimic 关节包含循环引用');return}active.add(j);if(j.mimic){const driver=byName.get(j.mimic.joint);if(driver){apply(driver);j.value=driver.value*j.mimic.multiplier+j.mimic.offset}else warnings.push('mimic 驱动关节缺失：'+j.mimic.joint)}if(j.type==='prismatic')j.child.position.copy(j.axis).multiplyScalar(j.value);else j.child.quaternion.setFromAxisAngle(j.axis,j.value);active.delete(j);done.add(j)}joints.forEach(apply)};
  section.innerHTML=`<details><summary>关节姿态 · ${free.length} 个独立关节${free.length<joints.length?' · '+(joints.length-free.length)+' 个联动关节':''}</summary><div>${free.map((j,i)=>`<label>${esc(j.name)}<input data-joint="${i}" type="range" min="${j.min}" max="${j.max}" step="${(j.max-j.min)/200||.01}" value="${Math.max(j.min,Math.min(j.max,0))}"><output>0</output></label>`).join('')}</div></details>`;
  section.querySelectorAll('[data-joint]').forEach(input=>input.oninput=()=>{const j=free[+input.dataset.joint],v=Number(input.value);j.value=v;applyJoints();input.nextElementSibling.textContent=j.type==='prismatic'?v.toFixed(3)+' m':THREE.MathUtils.radToDeg(v).toFixed(1)+'°'});applyJoints();
 }

 const resized=()=>{const width=Math.max(100,canvasHost.clientWidth),height=Math.max(100,canvasHost.clientHeight);renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix()};resize=new ResizeObserver(resized);resize.observe(canvasHost);resized();fit();
 function draw(){if(disposed)return;controls.update();renderer.render(scene,camera);frame=requestAnimationFrame(draw)}draw();loading.hidden=true;renderer.domElement.dataset.modelReady='true';
 if(warnings.length){notes.classList.add('model-warning');notes.textContent=[...new Set(warnings)].join('；')}else notes.textContent+=' '+(asset.ext==='.blend'?'当前渲染由原生 Blender 场景生成，源工程不变。':asset.ext==='.urdf'?'装配采用源关节、坐标与网格比例。':'');
 observer=new MutationObserver(()=>{if(!container.isConnected)dispose()});observer.observe(document.body,{childList:true,subtree:true});return {meshes:meshes.length,triangles,warnings};
 })().catch(error=>{if(error.name!=='AbortError'&&!disposed){loading.textContent=error.message;loading.classList.add('model-error');notes.textContent='工程文件和目录结构仍完整保留，可从工程包下载。'}throw error});
 return {ready,dispose};
}
