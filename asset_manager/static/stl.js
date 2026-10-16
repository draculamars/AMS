// Offline STL preview: bounded triangle sampling, drag rotation, wheel zoom.
export async function renderSTL(container,url){
 const response=await fetch(url);if(!response.ok)throw Error('读取模型失败');const buffer=await response.arrayBuffer();const view=new DataView(buffer);let faces=[];
 const count=buffer.byteLength>=84?view.getUint32(80,true):0;
 if(count>0&&84+count*50===buffer.byteLength){const step=Math.max(1,Math.ceil(count/18000));for(let i=0;i<count;i+=step){const base=84+i*50+12;faces.push([0,1,2].map(v=>[0,1,2].map(c=>view.getFloat32(base+v*12+c*4,true))))}}
 else{const text=new TextDecoder().decode(buffer),numbers=[...text.matchAll(/vertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)/g)].map(m=>m.slice(1).map(Number));for(let i=0;i+2<numbers.length;i+=3)faces.push(numbers.slice(i,i+3));if(faces.length>18000)faces=faces.filter((_,i)=>i%Math.ceil(faces.length/18000)===0)}
 if(!faces.length||faces.flat(2).some(x=>!Number.isFinite(x)))throw Error('无有效 STL 三角面');
 const bounds=[[Infinity,-Infinity],[Infinity,-Infinity],[Infinity,-Infinity]];for(const f of faces)for(const v of f)for(let i=0;i<3;i++){bounds[i][0]=Math.min(bounds[i][0],v[i]);bounds[i][1]=Math.max(bounds[i][1],v[i])}
 const center=bounds.map(b=>(b[0]+b[1])/2),extent=Math.max(...bounds.map(b=>b[1]-b[0]))||1;faces=faces.map(f=>f.map(v=>v.map((x,i)=>(x-center[i])/extent)));
 container.replaceChildren();const canvas=document.createElement('canvas');canvas.width=1200;canvas.height=840;canvas.setAttribute('aria-label','STL 三维预览，拖拽旋转，滚轮缩放');container.append(canvas);const ctx=canvas.getContext('2d');let ax=-.55,ay=.6,zoom=600,drag=null;
 function draw(){ctx.fillStyle='#f4f6fa';ctx.fillRect(0,0,1200,840);const project=v=>{const [x,y,z]=v;const x1=x*Math.cos(ay)+z*Math.sin(ay),z1=-x*Math.sin(ay)+z*Math.cos(ay);return[x1,y*Math.cos(ax)-z1*Math.sin(ax),y*Math.sin(ax)+z1*Math.cos(ax)]};
 const ff=faces.map(f=>f.map(project)).sort((a,b)=>a.reduce((s,v)=>s+v[2],0)-b.reduce((s,v)=>s+v[2],0));for(const f of ff){const u=f[1].map((x,i)=>x-f[0][i]),v=f[2].map((x,i)=>x-f[0][i]);const n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]],l=Math.hypot(...n)||1,light=.45+.5*Math.abs((n[0]*.3+n[1]*.7+n[2]*.64)/l);ctx.fillStyle=`rgb(${Math.round(75+90*light)},${Math.round(105+90*light)},${Math.round(155+85*light)})`;ctx.beginPath();f.forEach((v,i)=>ctx[i?'lineTo':'moveTo'](600+v[0]*zoom,420-v[1]*zoom));ctx.closePath();ctx.fill()}
 ctx.fillStyle='#8391a6';ctx.font='20px sans-serif';ctx.fillText('拖拽旋转 · 滚轮缩放 · STL 预览',28,800)}
 canvas.onpointerdown=e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId)};canvas.onpointerup=()=>drag=null;canvas.onpointermove=e=>{if(!drag)return;ay+=(e.clientX-drag[0])*.01;ax+=(e.clientY-drag[1])*.01;drag=[e.clientX,e.clientY];draw()};canvas.onwheel=e=>{e.preventDefault();zoom=Math.max(120,Math.min(2200,zoom*Math.exp(-e.deltaY*.001)));draw()};draw();
}
