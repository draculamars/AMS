const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.ATLAS_QA_URL||'http://127.0.0.1:8765';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-proxy-server','--enable-unsafe-swiftshader']});
 const cases=JSON.parse(fs.readFileSync(process.env.ATLAS_MODEL_CASES||'data/model-cases.json','utf8'));const results=[];
 for(const asset of cases){
  const page=await browser.newPage({viewport:{width:1300,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(60000);
  await page.goto(base,{waitUntil:'networkidle'});
  const result=await page.evaluate(async asset=>{
   const host=document.createElement('section');host.style.cssText='position:fixed;inset:15px;z-index:100;background:white;padding:15px;overflow:auto';document.body.append(host);
   const {mountModel}=await import('/static/model-viewer.js');window.qaModel=mountModel(host,asset);try{return {ok:true,...await window.qaModel.ready}}catch(e){return {ok:false,error:e.message}}
  },asset);
  if(result.ok){await page.locator('canvas[data-model-ready=true]').waitFor();await page.screenshot({path:`data/qa/model-format-${asset.ext.slice(1)}.png`});if(asset.ext==='.stl'){await page.locator('[data-v=wire]').click();await page.locator('[data-v=explode]').fill('30');await page.locator('[data-v=section]').check();await page.locator('[data-v=section]').uncheck();}}
  results.push({format:asset.ext,asset_id:asset.id,path:asset.relpath,...result,errors});console.log(JSON.stringify(results.at(-1)));
  await page.close();
 }
 fs.writeFileSync('data/qa/model-render-results.json',JSON.stringify(results,null,2));await browser.close();if(results.some(r=>!r.ok||r.errors.length))process.exitCode=1;
})().catch(e=>{console.error(e);process.exit(1)});
