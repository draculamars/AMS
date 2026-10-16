const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.AMS_QA_URL||'http://127.0.0.1:8765';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-proxy-server','--enable-unsafe-swiftshader']});
 const page=await browser.newPage({viewport:{width:1512,height:1100}});page.setDefaultTimeout(60000);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));const result={base,errors};fs.mkdirSync('data/qa',{recursive:true});
 for(const mode of ['vault','knowledge']){
  await page.goto(base+'/#'+mode);await page.locator(mode==='vault'?'#vault-sync':'#knowledge-scan').waitFor();
  await page.screenshot({path:`data/qa/${mode}-desktop.png`,fullPage:false});
  result[mode]=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));
  if(mode==='vault'){
   result.profiles=await page.locator('.vault-profile').count();result.snapshots=await page.locator('.vault-snapshot').count();
   const button=page.locator('[data-snapshot]').first();if(await button.count()){await button.click();await page.locator('.vault-dialog').waitFor();await page.screenshot({path:'data/qa/vault-detail.png',fullPage:false});await page.locator('[data-close]').click()}
  }else{
   result.items=await page.locator('.knowledge-card').count();
   if(result.items){await page.locator('.knowledge-card').first().click();await page.locator('#knowledge-detail pre').first().waitFor();result.detail=await page.locator('#knowledge-detail h2').textContent();await page.screenshot({path:'data/qa/knowledge-detail.png',fullPage:false})}
   await page.locator('#knowledge-filter').fill('skill');await page.waitForTimeout(900);result.searchFocus=await page.locator('#knowledge-filter').evaluate(el=>document.activeElement===el);
  }
  await page.setViewportSize({width:390,height:844});await page.goto(base+'/#'+mode);await page.locator(mode==='vault'?'#vault-sync':'#knowledge-scan').waitFor();
  result[mode+'Mobile']=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));await page.screenshot({path:`data/qa/${mode}-mobile.png`,fullPage:false});
  await page.setViewportSize({width:1512,height:1100});
 }
 fs.writeFileSync('data/qa/vault-browser.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result));await browser.close();
 if(errors.length||Object.values(result).some(x=>x&&x.scroll>x.width)||result.searchFocus===false)process.exitCode=1;
})().catch(e=>{console.error(e);process.exit(1)});
