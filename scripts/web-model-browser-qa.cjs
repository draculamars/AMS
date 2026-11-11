// Test a published model_library adaptation on an archive node or SSH tunnel.
// ATLAS_WEB_CASES is JSON: {entry_path,expected_cards,models:[{title,status}]}.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.ATLAS_QA_URL||'http://127.0.0.1:8767';
const cases=JSON.parse(fs.readFileSync(process.env.ATLAS_WEB_CASES,'utf8'));
const output=process.env.ATLAS_QA_OUTPUT||'data/qa/web';fs.mkdirSync(output,{recursive:true});
const digest=b=>crypto.createHash('sha256').update(b).digest('hex');
const redact=s=>s.replace(/web-preview\/[^/]+/g,'web-preview/[redacted]');
(async()=>{
 const browser=await chromium.launch({channel:process.env.ATLAS_BROWSER_CHANNEL||undefined,headless:true,args:['--no-proxy-server','--enable-unsafe-swiftshader']});
 try {
  const page=await browser.newPage({viewport:{width:1600,height:1000}}),errors=[];page.setDefaultTimeout(60000);
  page.on('pageerror',e=>errors.push(redact(e.message)));
  page.on('console',m=>{if(m.type()==='error')errors.push(redact(m.text()))});
  page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+':'+redact(new URL(r.url()).pathname))});
  await page.goto(base+'/#web');await page.getByRole('heading',{name:'网页与应用',exact:true}).waitFor();
  const data=await(await page.request.get(base+'/api/web-assets?q='+encodeURIComponent(cases.entry_path))).json();
  const item=data.items.find(x=>x.path===cases.entry_path);if(!item?.publications.length)throw Error('Published entry not found');
  const publication=item.publications[0];
  const ticket=await(await page.request.post(base+'/api/web-publications/'+publication.id+'/preview',{headers:{'X-Asset-Request':'1'},data:{}})).json();
  await page.goto(base+ticket.url);await page.locator('.card').first().waitFor();
  const counts={cards:await page.locator('.card').count(),graphNodes:await page.locator('.version-node').count()};
  if(counts.cards!==cases.expected_cards||!counts.graphNodes)throw Error('Catalog or version graph count mismatch');
  const lineageLink=page.locator('#lineagePanel > details > p > a').first();
  const lineage=await page.request.get(new URL(await lineageLink.getAttribute('href'),page.url()).href);
  if(!lineage.ok())throw Error('Lineage document is missing');
  const models=[];
  for(const spec of cases.models){
   await page.locator('#query').fill(spec.title);await page.locator('.card button').first().click();
   await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('拖动旋转'));
   await page.waitForTimeout(500);const status=await page.locator('#status').innerText();
   if(spec.status&&!status.includes(spec.status))throw Error('Expected rendering state missing: '+spec.title);
   const canvas=page.locator('#view canvas').first(),before=await canvas.screenshot(),box=await canvas.boundingBox();
   await page.mouse.move(box.x+box.width*.45,box.y+box.height*.45);await page.mouse.down();
   await page.mouse.move(box.x+box.width*.65,box.y+box.height*.6,{steps:10});await page.mouse.up();await page.waitForTimeout(300);
   const rotationChangesCanvas=!before.equals(await canvas.screenshot());
   const item={title:spec.title,status,rotationChangesCanvas,sourceLinks:await page.locator('#provenance .version-link').count()};
   if(!rotationChangesCanvas)throw Error('Rotation did not change rendering: '+spec.title);
   const link=page.locator('#filelist a[download]').filter({hasText:'.urdf'}).first();
   if(await link.count()){
    const url=new URL(await link.getAttribute('href'),page.url()).href,response=await page.request.get(url);
    if(!response.ok())throw Error('Download failed');const bytes=await response.body();item.download={path:redact(new URL(url).pathname),bytes:bytes.length,sha256:digest(bytes)};
    if(spec.download_sha256&&item.download.sha256!==spec.download_sha256)throw Error('Download hash mismatch');
   }
   models.push(item);await page.screenshot({path:path.join(output,spec.title.replace(/[^a-zA-Z0-9_-]/g,'_')+'.png')});
   await page.locator('#close').click();
  }
  const evidence={status:errors.length?'failed':'passed',tested_url:base+'/api/web-preview/[redacted]',publication:publication.id,bundle:publication.bundle_id,counts,models,errors,
   checks:['归档节点目录与版本关系图','沿革说明文档','指定模型 GLB 加载和旋转','指定 VIEW-R2 视图','URDF 下载及指定校验值','浏览器控制台与请求'],
   limitations:['仅验收所列模型，其余版本未逐个交互测试','不执行原服务动态打包，已有交付包原样保存','查看模型不代表制造或实机验收'],at:new Date().toISOString()};
  fs.writeFileSync(path.join(output,'browser-evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence));
  if(errors.length)process.exitCode=1;
 } finally {await browser.close()}
})().catch(e=>{console.error(redact(e.stack||String(e)));process.exitCode=1});
