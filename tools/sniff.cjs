#!/usr/bin/env node
/**
 * 嗅探器：真浏览器打开页面 → 让**页面自己的 JS**去请求 → 我们只读响应。
 * 输出 JSON（stdout）：
 *   { ok, title, attempts, meta:{desc,author,duration}, play_urls:[...], download_urls:[...], error }
 * 不做签名、不逆向；坏处是**不稳定**（平台会变招），所以内置重试。
 * 用法: node sniff.cjs <url> [maxAttempts]
 */
const fs = require('fs');
// Playwright 位置：环境变量优先 → 常见安装位 → 让 require 自己找
const PW_CANDIDATES = [
  process.env.YUANLIU_PLAYWRIGHT,
  process.env.PLAYWRIGHT_PATH,
  '/opt/yuanliu/vendor/node_modules/playwright',
  '/home/bubu12/dev/qwen-audio-agent/node_modules/playwright',
].filter(Boolean);
let PW = 'playwright';
for (const c of PW_CANDIDATES) { try { require.resolve(c.endsWith('playwright') ? c + '/package.json' : c); PW = c; break; } catch (e) {} }
const CHROME = process.env.CHROME_PATH || '/home/bubu12/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome';
const { chromium } = require(PW);

const url = process.argv[2];
const maxAttempts = Number(process.argv[3] || 3);
const outFile = process.argv[4] || '';   // 给了就把媒体**在浏览器上下文里**直接下下来
const cookieFile = process.env.SNIFF_COOKIES || '';

const UA_POOL = [
  'Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
];

function parseCookies(file) {
  if (!file || !fs.existsSync(file)) return [];
  const out = [];
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    if (!line || line.startsWith('#')) continue;
    const f = line.split('\t');
    if (f.length < 7) continue;
    let exp = Number(f[4]); if (exp > 1e11) exp = Math.floor(exp / 1000);
    out.push({
      name: f[5], value: f[6], domain: f[0], path: f[2] || '/', httpOnly: false,
      secure: f[3] === 'TRUE', sameSite: 'Lax',
      // Playwright 只接受 -1（会话）或正整数
      ...(Number.isFinite(exp) && exp > 0 ? { expires: Math.floor(exp) } : { expires: -1 }),
    });
  }
  return out;
}

const HOOK = () => {
  window.__sniffed = [];
  const keep = (u, t) => { try { window.__sniffed.push({ url: String(u).slice(0, 200), len: t.length, text: t.slice(0, 400000) }); } catch (e) {} };
  const of = window.fetch;
  window.fetch = async function (...a) { const r = await of.apply(this, a);
    try { const c = r.clone(); c.text().then((t) => keep(r.url || a[0], t)).catch(() => {}); } catch (e) {} return r; };
  const oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.addEventListener('load', () => { try { keep(u, this.responseText || ''); } catch (e) {} });
    return oo.call(this, m, u, ...rest);
  };
};

// 小红书：视频在 note.video.media.stream.*.masterUrl；图片在 imageList[].urlDefault
function extractXhs(node, play, images) {
  let meta = null;
  const walk = (n) => {
    if (!n || typeof n !== 'object') return;
    const note = n.note || n.note_card || n.noteCard || null;
    if (note && (note.video || note.imageList)) {
      meta = meta || { desc: (note.title || note.desc || '').slice(0, 80), author: (note.user && (note.user.nickname || note.user.nick_name)) || null, type: note.type || null };
      const v = note.video || {};
      const streams = (v.media && v.media.stream) || {};
      for (const codec of Object.keys(streams)) {
        for (const item of streams[codec] || []) {
          for (const u of [item.masterUrl, ...(item.backupUrls || [])]) if (u) play.add(u);
        }
      }
      const key = v.consumer && v.consumer.originVideoKey;
      if (key) play.add(`https://sns-video-bd.xhscdn.com/${key}`);
      for (const img of note.imageList || []) {
        for (const u of [img.urlDefault, img.urlPre]) if (u) images.add(u);
      }
    }
    for (const k of Object.keys(n)) walk(n[k]);
  };
  walk(node);
  return meta;
}

function extract(sniffed) {
  const play = new Set(), download = new Set(), images = new Set();
  let meta = null;
  for (const h of sniffed) {
    if (/aweme\/v1\/web\/aweme\/(detail|post)/.test(h.url) && h.len > 500) {
      try {
        const j = JSON.parse(h.text);
        const d = j.aweme_detail || (j.aweme_list && j.aweme_list[0]);
        if (d) {
          meta = meta || { desc: (d.desc || '').slice(0, 80), author: d.author && d.author.nickname, duration: d.video && d.video.duration };
          const v = d.video || {};
          for (const k of ['play_addr', 'play_addr_h264', 'play_addr_265', 'bit_rate']) {
            const node = v[k];
            const list = Array.isArray(node) ? node.flatMap((x) => (x.play_addr && x.play_addr.url_list) || []) : (node && node.url_list) || [];
            list.forEach((u) => play.add(u));
          }
          ((v.download_addr || {}).url_list || []).forEach((u) => download.add(u));
        }
      } catch (e) {}
    }
    // 小红书：页面会调 edith.xiaohongshu.com 的 feed 接口，或 SSR 里带 noteDetailMap
    if (/noteDetailMap|edith\.xiaohongshu\.com|sns\/web\/v1\/feed/.test(h.url) || /noteDetailMap/.test(h.text.slice(0, 5000))) {
      try { meta = meta || extractXhs(JSON.parse(h.text), play, images); } catch (e) {}
      // 直接从文本里抠 xhscdn 直链（视频/图片都可能没有扩展名）
      const cdn = h.text.match(/https?:\\?\/\\?\/[a-z0-9.-]*xhscdn\.com\\?\/[^"'\\ ]+/g) || [];
      cdn.map((u) => u.replace(/\\u002F/g, '/').replace(/\\\//g, '/')).forEach((u) => (/\.(mp4|m3u8)/.test(u) ? play : images).add(u));
    }
    // 通用兜底：任何响应里出现的 mp4/m3u8 直链（小红书/微博/其它平台）
    if (/xiaohongshu|noteDetailMap|sns\/v1\/feed/.test(h.url) || /\.(mp4|m3u8)/.test(h.text.slice(0, 3000))) {
      const found = h.text.match(/https?:\\?\/\\?\/[^"'\\ ]+?(?:\.mp4|\.m3u8)[^"'\\ ]*/g) || [];
      found.map((u) => u.replace(/\\u002F/g, '/').replace(/\\\//g, '/')).forEach((u) => play.add(u));
    }
  }
  return { meta, play: [...play], download: [...download], images: [...images] };
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: CHROME,
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'] });
  const cookies = parseCookies(cookieFile);
  const result = { ok: false, url, title: null, attempts: 0, meta: null, play_urls: [], download_urls: [], images: [], error: null };
  for (let attempt = 1; attempt <= maxAttempts && !result.ok; attempt++) {
    const ctx = await browser.newContext({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
      viewport: { width: 1366, height: 900 }, userAgent: UA_POOL[(attempt - 1) % UA_POOL.length] });
    try { if (cookies.length) await ctx.addCookies(cookies); } catch (e) {}
    const page = await ctx.newPage();
    const netMedia = [];
    page.on('response', (r) => {
      const ct = (r.headers()['content-type'] || '').toLowerCase();
      const len = Number(r.headers()['content-length'] || 0);
      if (/^(video|audio)\//.test(ct) || /\.(mp4|m3u8)(\?|$)/.test(r.url())) {
        netMedia.push({ u: r.url(), len, ct });
      }
    });
    await page.addInitScript(HOOK);
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page.waitForTimeout(5000 + attempt * 2000);
      // 让视频真的开始播：blob/MSE 播放器**只有播了才会去拉媒体流**
      await page.evaluate(() => {
        const v = document.querySelector('video');
        if (!v) return;
        try { v.scrollIntoView({ block: 'center' }); } catch (e) {}
        try { v.muted = true; } catch (e) {}
        try { v.play && v.play().catch(() => {}); } catch (e) {}
      }).catch(() => {});
      const box = await page.evaluate(() => {
        const v = document.querySelector('video');
        if (!v) return null;
        const r = v.getBoundingClientRect();
        return r.width > 0 ? { x: r.x + r.width / 2, y: r.y + r.height / 2 } : null;
      });
      if (box) await page.mouse.click(box.x, box.y).catch(() => {});
      await page.mouse.wheel(0, 300).catch(() => {});
      await page.waitForTimeout(6000);
      result.title = await page.title();
      result.attempts = attempt;
      const sniffed = await page.evaluate(() => window.__sniffed || []);
      const { meta, play, download, images } = extract(sniffed);
      result.images = [...new Set(images)].slice(0, 20);
      const videoEl = await page.evaluate(() => { const v = document.querySelector('video'); return v ? (v.currentSrc || v.src || '') : ''; });
      if (videoEl && /^https?:/.test(videoEl)) play.push(videoEl);
      // 按 content-type 抓到的媒体（小红书这类 blob/MSE 播放器只有这条能中）
      netMedia.sort((a, b) => b.len - a.len).forEach((m) => { if (/^https?:/.test(m.u) && !m.u.startsWith('blob:')) play.push(m.u); });
      // 图文笔记：页面里的大图
      const imgs = await page.evaluate(() => [...document.querySelectorAll('img')]
        .filter((i) => i.naturalWidth >= 480 && /^https?:/.test(i.src)).map((i) => i.src));
      imgs.forEach((u) => result.images.push(u));
      if (!result.images) result.images = [];
      // 关键：直链有签名+短时效，**用浏览器自己的会话去下**才不会 403（换 curl 常被拒）
      if (outFile && play.length) {
        const candidates = [...new Set(play)].filter((u) => /^https?:/.test(u));
        for (const media of candidates.slice(0, 4)) {
          try {
            const resp = await ctx.request.get(media, { headers: { Referer: url, Accept: '*/*' }, timeout: 600000 });
            if (resp.ok()) {
              const body = await resp.body();
              if (body && body.length > 10000) { fs.writeFileSync(outFile, body); result.saved = outFile; result.savedBytes = body.length; break; }
              else result.error = `媒体响应过小(${body ? body.length : 0}B)`;
            } else result.error = `媒体直链 HTTP ${resp.status()}`;
          } catch (e) { result.error = String(e).slice(0, 160); }
        }
      }
      if (play.length || meta || (result.images && result.images.length)) { result.ok = true; result.meta = meta; result.play_urls = [...new Set(play)].slice(0, 8); result.download_urls = [...new Set(download)].slice(0, 4); }
      else result.error = `attempt ${attempt}: 页面没给出可用的媒体直链（hits=${sniffed.length}）`;
    } catch (err) { result.error = String(err).slice(0, 200); }
    await ctx.close();
  }
  console.log(JSON.stringify(result, null, 1));
  await browser.close();
  process.exit(result.ok ? 0 : 5);
})();
