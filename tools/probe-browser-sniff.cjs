#!/usr/bin/env node
/**
 * 实验：真浏览器（Playwright Chromium）+ **网络响应拦截**
 * 验证"解析站为什么能下"的核心技术：
 *   我们**不签任何名** —— 让页面自己的 JS 去请求，我们只**读它拿回来的响应**。
 * 抖音详情接口返回的 JSON 里就有带/不带水印的两路 play_addr。
 *
 * 用法：node probe-browser-sniff.mjs "<页面URL>" [关键字正则]
 */
const PW = process.env.PLAYWRIGHT_PATH || '/home/bubu12/dev/qwen-audio-agent/node_modules/playwright';
const { chromium } = require(PW);

const url = process.argv[2] || 'https://www.douyin.com/video/6961737553342991651';
const pattern = new RegExp(
  process.argv[3] || 'aweme/v1/web/aweme/detail|aweme/v1/web/aweme/post|noteDetailMap|sns/v1/feed',
);

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME_PATH || '/home/bubu12/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });
  const ctx = await browser.newContext({
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    viewport: { width: 1366, height: 900 },
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  });
  const page = await ctx.newPage();

  // 关键：在页面加载**之前**挂钩 fetch/XHR，把响应原文存进 window.__sniffed
  await page.addInitScript(() => {
    window.__sniffed = [];
    const keep = (url, text) => {
      try {
        window.__sniffed.push({ url: String(url).slice(0, 200), len: text.length, text: text.slice(0, 300000) });
      } catch (e) {}
    };
    const origFetch = window.fetch;
    window.fetch = async function (...args) {
      const res = await origFetch.apply(this, args);
      try { const c = res.clone(); c.text().then((t) => keep(res.url || args[0], t)).catch(() => {}); } catch (e) {}
      return res;
    };
    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.addEventListener('load', () => { try { keep(url, this.responseText || ''); } catch (e) {} });
      return origOpen.call(this, method, url, ...rest);
    };
  });

  const hits = [];
  page.on('response', async (res) => {
    const u = res.url();
    if (!pattern.test(u)) return;
    let body = '';
    try {
      body = (await res.text()).slice(0, 200000);
    } catch {
      /* 有些响应读不到 body */
    }
    hits.push({ url: u.slice(0, 160), status: res.status(), bytes: body.length, body });
  });

  const out = { url, title: null, cookies: 0, ssrKeys: [], hits: 0, findings: [], error: null };
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(9000);
    out.title = await page.title();
    const cookies = await ctx.cookies();
    out.cookies = cookies.length;
    out.cookieNames = cookies.map((c) => c.name).slice(0, 20);
    out.ssrKeys = await page.evaluate(() => {
      const keys = [];
      for (const k of ['_ROUTER_DATA', '__INITIAL_STATE__', '__NUXT__', 'RENDER_DATA']) {
        if (window[k]) keys.push(k);
      }
      return keys;
    });
    out.hits = hits.length;
    const sniffed = await page.evaluate(() => (window.__sniffed || []).map((x) => ({ url: x.url, len: x.len })));
    out.sniffed = sniffed.slice(0, 15);
    const aweme = await page.evaluate(() => {
      const hit = (window.__sniffed || []).find((x) => /aweme\/v1\/web\/aweme\/detail/.test(x.url) && x.len > 100);
      if (!hit) return null;
      try {
        const j = JSON.parse(hit.text);
        const d = j.aweme_detail || (j.aweme_list && j.aweme_list[0]) || null;
        if (!d) return { keys: Object.keys(j).slice(0, 10) };
        const play = (d.video && d.video.play_addr) || {};
        const wm = (d.video && d.video.play_addr_265) || {};
        return {
          desc: (d.desc || '').slice(0, 60),
          author: d.author && d.author.nickname,
          duration: d.video && d.video.duration,
          uri: play.uri,
          playUrls: (play.url_list || []).slice(0, 2),
          downloadAddr: ((d.video && d.video.download_addr) || {}).url_list?.[0] || null,
          hasWatermarkField: JSON.stringify(d.video || {}).includes('watermark'),
          watermarkFlagHit: (JSON.stringify(d.video || {}).match(/watermark[^,]{0,80}/) || [null])[0],
        };
      } catch (e) { return { parseError: String(e).slice(0, 120) }; }
    });
    out.aweme = aweme;
    for (const h of hits.slice(0, 4)) {
      const urls = [...h.body.matchAll(/https?:\\?\/\\?\/[^"'\\ ]+?(?:mp4|m3u8)[^"'\\ ]*/g)].map((m) =>
        m[0].replace(/\\u002F/g, '/').replace(/\\\//g, '/'),
      );
      out.findings.push({
        api: h.url,
        status: h.status,
        bytes: h.bytes,
        mediaUrls: [...new Set(urls)].slice(0, 6),
        watermarkFlag: /watermark/i.test(h.body) ? h.body.match(/watermark[^,]{0,60}/i)?.[0] : null,
      });
    }
  } catch (err) {
    out.error = String(err).slice(0, 300);
  }
  console.log(JSON.stringify(out, null, 1));
  await browser.close();
})();
