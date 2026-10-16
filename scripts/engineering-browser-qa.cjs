const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.ATLAS_QA_URL||'http://127.0.0.1:8765';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-proxy-server','--enable-unsafe-swiftshader']});
 const page=await browser.newPage({viewport:{width:1500,height:1000}});page.setDefaultTimeout(60000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/?v=0.3.0#engineering',{waitUntil:'networkidle'});await page.locator('.project-cards .project-card').first().waitFor();
 const results={host:base,cards:await page.locator('.project-card').count(),errors};
 fs.mkdirSync('data/qa',{recursive:true});await page.screenshot({path:'data/qa/engineering-overview.png',fullPage:true});
 const projects=await page.evaluate(()=>fetch('/api/engineering').then(r=>r.json()).then(d=>d.items.map(p=>({id:p.project.id,name:p.project.name,docs:p.documents.length,repos:p.repositories.filter(r=>r.metadata.commits).length}))));
 const p=projects.find(p=>p.id===process.env.ATLAS_QA_PROJECT)||projects.find(p=>p.docs&&p.repos)||projects.find(p=>p.docs)||projects[0];
 await page.goto(base+'/#engineering/'+p.id,{waitUntil:'networkidle'});await page.locator('#project-folder table').waitFor();results.project=p;
 for(const tab of ['documents','models','relations','git','timeline','coverage']){
  await page.locator('[data-project-tab='+tab+']').click();await page.waitForTimeout(1500);results[tab]=await page.locator('#project-panel').innerText();results[tab]=results[tab].slice(0,160);
  if(tab==='documents'&&await page.locator('[data-doc-item]').count()){await page.locator('[data-doc-item]').first().click();await page.locator('#project-file-content').waitFor();await page.waitForFunction(()=>{const e=document.querySelector('#project-file-content');return e&&!e.textContent.includes('正在读取文件内容')});await page.locator('#project-inspector').screenshot({path:'data/qa/engineering-document.png'});}
  if(tab==='git'&&await page.locator('[data-commit]').count()){await page.locator('[data-commit]').first().click();await page.locator('.project-git-tree').waitFor();await page.screenshot({path:'data/qa/engineering-git.png',fullPage:true});}
  if(tab==='relations')await page.screenshot({path:'data/qa/engineering-relations.png',fullPage:true});
 }
 await page.setViewportSize({width:390,height:844});await page.goto(base+'/#engineering',{waitUntil:'networkidle'});await page.locator('.project-card').first().waitFor();results.mobile=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));await page.screenshot({path:'data/qa/engineering-mobile.png',fullPage:true});
 fs.writeFileSync('data/qa/engineering-browser-results.json',JSON.stringify(results,null,2));console.log(JSON.stringify(results,null,2));await browser.close();if(errors.length||results.mobile.scroll>results.mobile.width)process.exitCode=1;
})().catch(e=>{console.error(e);process.exit(1)});
