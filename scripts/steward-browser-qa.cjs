const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.AMS_QA_URL||'http://127.0.0.1:8765';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-proxy-server','--enable-unsafe-swiftshader']});
 const page=await browser.newPage({viewport:{width:1512,height:1100}});page.setDefaultTimeout(60000);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));const result={base,errors};fs.mkdirSync('data/qa',{recursive:true});
 await page.goto(base+'/#overview');await page.locator('.steward-project').first().waitFor();
 result.projects=await page.locator('.steward-project').count();await page.screenshot({path:'data/qa/steward-desktop.png',fullPage:false});
 await page.locator('#steward-search').fill('设计');await page.waitForTimeout(800);result.search=await page.locator('[data-knowledge]').count();
 const d=await page.evaluate(()=>fetch('/api/workbench').then(r=>r.json()));const p=d.projects.find(p=>p.turns>5&&p.counts.models)||d.projects.find(p=>p.turns);
 if(p){await page.goto(base+'/#engineering/'+p.id+'?tab=development');await page.locator('.development-turn').first().waitFor();result.development=await page.locator('.development-turn').count();await page.screenshot({path:'data/qa/steward-development.png',fullPage:false});
 const change=page.locator('.development-change').first();if(await change.count()){await change.locator('summary').click();result.diff=await change.locator('pre').textContent()!==''}
 await page.locator('[data-project-tab=files]').click();await page.locator('#project-folder table').waitFor();result.currentLabel=await page.locator('#project-run option').first().textContent();
 }
 await page.setViewportSize({width:390,height:844});await page.goto(base+'/#overview');await page.locator('.steward-project').first().waitFor();result.mobile=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));await page.screenshot({path:'data/qa/steward-mobile.png',fullPage:false});
 if(p){await page.goto(base+'/#engineering/'+p.id+'?tab=development');await page.locator('.development-turn').first().waitFor();result.mobileDevelopment=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));await page.screenshot({path:'data/qa/steward-development-mobile.png',fullPage:false})}
 fs.writeFileSync('data/qa/steward-browser.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result));await browser.close();if(errors.length||result.mobile.scroll>result.mobile.width||result.mobileDevelopment?.scroll>result.mobileDevelopment?.width)process.exitCode=1;
})().catch(e=>{console.error(e);process.exit(1)});
