/**
 * 决定性实验：把**你自己浏览器导出的登录态**注入无头浏览器，
 * 再打开抖音视频页 —— 看是不是"有登录就不一样"。
 * 用法: node tools/probe-with-login.cjs <cookies.txt> <url>
 */
const fs = require('fs');
const PW = '/home/bubu12/dev/qwen-audio-agent/node_modules/playwright';
const { chromium } = require(PW);

function parseNetscape(file) {
  const out = [];
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    if (!line || line.startsWith('#')) continue;
    const f = line.split('\t');
    if (f.length < 7) continue;
    out.push({
      domain: f[0].startsWith('.') ? f[0] : '.' + f[0],
      path: f[2] || '/',
      secure: f[3] === 'TRUE',
      expires: (() => { const n = Number(f[4]); return Number.isFinite(n) && n > 0 ? n : -1; })(),
      name: f[5],
      value: f[6],
    });
  }
  return out;
}

(async () => {
  const cookies = parseNetscape(process.argv[2]);
  const url = process.argv[3];
  const browser = await chromium.launch({ headless: true,
    executablePath: '/home/bubu12/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'] });
  const ctx = await browser.newContext({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    viewport: { width: 1366, height: 900 },
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0' });
  // 逐条加，坏 cookie 只警告不炸整场实验
  let added = 0; const skipped = [];
  for (const c of cookies) {
    try { await ctx.addCookies([c]); added++; }
    catch (e) { skipped.push(c.name + ':' + String(e.message).slice(0, 60)); }
  }
  if (skipped.length) console.error('skipped', skipped.slice(0, 5));
  console.error('cookies added:', added);
  const page = await ctx.newPage();
  await page.addInitScript(() => {
    window.__sniffed = [];
    const keep = (u, t) => { try { window.__sniffed.push({ url: String(u).slice(0,180), len: t.length, text: t.slice(0, 400000) }); } catch (e) {} };
    const of = window.fetch;
    window.fetch = async function (...a) { const r = await of.apply(this, a);
      try { const c = r.clone(); c.text().then((t) => keep(r.url || a[0], t)).catch(() => {}); } catch (e) {} return r; };
    const oo = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u, ...rest) {
      this.addEventListener('load', () => { try { keep(u, this.responseText || ''); } catch (e) {} });
      return oo.call(this, m, u, ...rest);
    };
  });
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(12000);
  const info = await page.evaluate(() => {
    const el = document.getElementById('RENDER_DATA');
    const raw = el ? decodeURIComponent(el.textContent || '') : '';
    const hits = (window.__sniffed || []).filter((x) => /aweme\/v1\/web\/aweme\/(detail|post)/.test(x.url));
    let parsed = null;
    const big = hits.find((h) => h.len > 200);
    if (big) {
      try {
        const j = JSON.parse(big.text);
        const d = j.aweme_detail || (j.aweme_list && j.aweme_list[0]);
        if (d) parsed = { desc: (d.desc || '').slice(0, 40), author: d.author && d.author.nickname,
          duration: d.video && d.video.duration,
          play: ((d.video && d.video.play_addr) || {}).url_list && d.video.play_addr.url_list.slice(0, 1),
          download: ((d.video && d.video.download_addr) || {}).url_list && d.video.download_addr.url_list.slice(0, 1) };
      } catch (e) { parsed = { parseError: String(e).slice(0, 80) }; }
    }
    return {
      title: document.title,
      loginWall: /登录后免费|扫码登录/.test(document.body.innerText || ''),
      videoSrcCount: document.querySelectorAll('video').length,
      videoSrc: document.querySelector('video') && (document.querySelector('video').src || '').slice(0, 120),
      renderHasPlayAddr: /play_addr/.test(raw),
      awemeHits: hits.map((h) => ({ url: h.url.slice(0, 90), len: h.len })),
      parsed,
      mp4InRender: [...new Set((raw.match(/https?:\\?\/\\?\/[^"'\\ ]+?\.mp4[^"'\\ ]*/g) || []))].slice(0, 3),
    };
  });
  await page.screenshot({ path: '/home/bubu12/dev/fetchscript/tools/douyin-with-login.png' }).catch(() => {});
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})();
