// 用无头浏览器验"页面真的能用"：应用区显示、列表有内容、无 JS 报错
const PW='/home/bubu12/dev/qwen-audio-agent/node_modules/playwright'; const {chromium}=require(PW);
const URL=process.argv[2]||'https://127.0.0.1:9445/';
const PASS=process.argv[3]||'Bubu12!!';
(async()=>{
 const b=await chromium.launch({headless:true,executablePath:'/home/bubu12/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',args:['--no-sandbox','--ignore-certificate-errors']});
 const ctx=await b.newContext({ignoreHTTPSErrors:true,viewport:{width:1200,height:900}});
 const page=await ctx.newPage(); const errs=[];
 page.on('pageerror',e=>errs.push('PAGEERROR '+String(e).slice(0,120)));
 page.on('console',m=>{if(m.type()==='error')errs.push('CONSOLE '+m.text().slice(0,120))});
 const bad=[]; page.on('response',r=>{if(r.status()>=400)bad.push(r.status()+' '+r.url().slice(0,110))});
 await page.goto(URL,{waitUntil:'domcontentloaded',timeout:30000});
 if(await page.$('input[type=password], input[name=pass]')){
   await page.fill('input[type=password], input[name=pass]', PASS).catch(()=>{});
   await page.click('button[type=submit], button, input[type=submit]').catch(()=>page.keyboard.press('Enter'));
   await page.waitForTimeout(2000);
 }
 await page.goto(URL,{waitUntil:'domcontentloaded',timeout:30000});
 await page.waitForTimeout(3000);
 const st=await page.evaluate(()=>({
   banner:(document.getElementById('banner')||{}).innerText||'',
   app:getComputedStyle(document.getElementById('app')||document.body).display,
   gate:getComputedStyle(document.getElementById('gate')||document.body).display,
   jobsLen:((document.getElementById('jobs')||{}).innerHTML||'').length,
   filesLen:((document.getElementById('files')||{}).innerHTML||'').length,
   fileRows:document.querySelectorAll('#files .card').length,
 }));
 console.log('STATE:',JSON.stringify(st));
 const probe = await page.evaluate(async () => { try { const r = await fetch('/api/files'); const t = await r.text(); return {status:r.status, len:t.length, head:t.slice(0,90)}; } catch(e) { return {error:String(e).slice(0,120)} } });
 console.log('IN-PAGE fetch /api/files:', JSON.stringify(probe));
 console.log('ERRORS:',errs.length?JSON.stringify(errs.slice(0,3)):'（无）');
 console.log('BAD_RESPONSES:',bad.length?JSON.stringify(bad.slice(0,5)):'（无）');
 await b.close();
})();
