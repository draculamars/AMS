// Run against an isolated fixture instance; never confirms a real project's Git plan.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs');
const base=process.env.QA_BASE_URL||'http://127.0.0.1:8766';
const output=process.env.QA_OUTPUT||'data/qa';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-proxy-server']});const page=await browser.newPage({viewport:{width:1512,height:1050}});const errors=[];page.on('pageerror',e=>errors.push(e.message));fs.mkdirSync(output,{recursive:true});
 await page.goto(base,{waitUntil:'networkidle'});await page.getByRole('heading',{name:'你的成果，井然有序。'}).waitFor();
 await page.locator('[data-kind=document]').click();await page.locator('#asset-search').waitFor();await page.locator('#asset-search').fill('验收说明');await page.waitForTimeout(700);await page.locator('[data-asset]').first().click();await page.locator('#asset-preview pre').waitFor();assert.match(await page.locator('#asset-preview').textContent(),/验收说明/);
 await page.locator('#asset-tags').fill('浏览器验收');await page.locator('#save-metadata').click();await page.locator('#close-drawer').click();
 await page.locator('[data-action=upload]').click();await page.locator('#upload-form select').selectOption('qa');await page.locator('#upload-form input[type=file]').setInputFiles({name:'网页上传.csv',mimeType:'text/csv',buffer:Buffer.from('名称,数值\n电压,3.3\n')});await page.getByRole('button',{name:'开始上传',exact:false}).click();await page.locator('#asset-preview table').waitFor();assert.match(await page.locator('#asset-preview').textContent(),/电压/);
 await page.locator('#asset-update').click();await page.locator('#upload-form input[type=file]').setInputFiles({name:'网页上传.csv',mimeType:'text/csv',buffer:Buffer.from('名称,数值\n电压,5.0\n')});await page.locator('#upload-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#asset-preview')?.textContent.includes('5.0'));
 const download=page.waitForEvent('download');await page.locator('.drawer a.primary').click();const dl=await download;assert.equal(dl.suggestedFilename(),'网页上传.csv');await dl.saveAs(output+'/uploaded.csv');assert.match(fs.readFileSync(output+'/uploaded.csv','utf8'),/5.0/);await page.locator('#close-drawer').click();
 await page.locator('nav [data-view=governance]').click();await page.locator('[data-plan=qa]').click();await page.locator('#plan-form button').click();await page.locator('#execute-plan').waitFor();assert(await page.locator('#execute-plan').isDisabled());await page.locator('#modal-close').click();
 await page.locator('nav [data-view=migration]').click();await page.locator('[data-action=migration-plan]').click();await page.locator('.migration-item').waitFor();
 await page.locator('nav [data-view=assets]').click();await page.locator('#kind-filter').selectOption('model');await page.locator('[data-asset]').first().click();await page.locator('#asset-preview canvas').waitFor();await page.screenshot({path:output+'/stl-preview.png'});await page.locator('#close-drawer').click();
 await page.locator('nav [data-view=overview]').click();await page.getByRole('heading',{name:'你的成果，井然有序。'}).waitFor();await page.screenshot({path:output+'/fixture-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:output+'/fixture-mobile.png',fullPage:true});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));await page.locator('#mobile-menu').click();await page.locator('nav [data-view=projects]').click();await page.locator('.project-card').waitFor();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
 assert.equal(errors.length,0,errors.join('\n'));console.log(JSON.stringify({status:'passed',checks:['search','text-preview','metadata','upload','version-update','download','git-approval-gate','migration-plan','stl-preview','mobile-layout'],errors}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
