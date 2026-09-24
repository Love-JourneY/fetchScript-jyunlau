// 验证 cookie 能真的注入浏览器（历史坑：毫秒过期时间 ⇒ 全军覆没）
const fs=require('fs');
const PW='/home/bubu12/dev/qwen-audio-agent/node_modules/playwright'; const {chromium}=require(PW);
const file=process.argv[2]||process.env.HOME+'/.cache/fetchscript/cookies.txt';
const out=[];
for(const line of fs.readFileSync(file,'utf8').split('\n')){
  if(!line||line.startsWith('#'))continue; const f=line.split('\t'); if(f.length<7)continue;
  let exp=Number(f[4]); if(exp>1e11) exp=Math.floor(exp/1000);
  out.push({name:f[5],value:f[6],domain:f[0],path:f[2]||'/',httpOnly:false,secure:f[3]==='TRUE',sameSite:'Lax',
    ...(Number.isFinite(exp)&&exp>0?{expires:Math.floor(exp)}:{expires:-1})});
}
(async()=>{
 const b=await chromium.launch({headless:true,executablePath:'/home/bubu12/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',args:['--no-sandbox']});
 const ctx=await b.newContext(); let ok=0; const errs=[];
 for(const c of out){ try{ await ctx.addCookies([c]); ok++; }catch(e){ if(errs.length<3) errs.push(c.name+': '+e.message.slice(0,60)); } }
 const hosts={}; for(const c of await ctx.cookies()){ hosts[c.domain]=(hosts[c.domain]||0)+1; }
 console.log(JSON.stringify({parsed:out.length,added:ok,errs,byDomain:hosts},null,1));
 await b.close();
})();
