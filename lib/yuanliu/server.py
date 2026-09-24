"""yuanliu 常驻服务：给平板/手机用的"贴链接就下载"网页 + JSON API。

零第三方依赖（标准库 http.server）—— 离线自洽、随包可迁移，不需要 pip 装任何东西。
浏览器嗅探那部分仍然调 `tools/sniff.cjs`（node + Playwright），那是可选能力。

安全模型（LAN 场景，默认 fail-closed）：
- 必须带 token：`?k=<token>` 或 `X-Token` 头；token 放 `/etc/yuanliu/env`（600 可见范围由 systemd 控制）。
- `/files/<name>` 只服务**白名单扩展名**且**必须在状态位目录内**（防穿越）。
- 不做"匿名开放"——本机 `/etc/yuanliu/env` 里有 token，服务可触发下载，不能无条件裸奔。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from yuanliu import __version__
from yuanliu.engines import EngineFailed, EngineUnavailable
from yuanliu.resolve import (
    NoEngineAvailable,
    ResolveError,
    download as download_media,
    plan,
    probe,
    summarize_probe,
)

__all__ = ["ServerConfig", "YuanliuServer", "main", "MAX_CONCURRENT_JOBS"]

logger = logging.getLogger("yuanliu")

MAX_CONCURRENT_JOBS = 2
FILE_ALLOWED_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".jpg", ".jpeg", ".png", ".webp", ".txt"}


@dataclass(slots=True)
class ServerConfig:
    bind: str = "0.0.0.0"
    port: int = 8901
    token: str = ""
    media_dir: Path = Path("/var/lib/yuanliu/media")
    cookies: Path | None = None

    @classmethod
    def from_env(cls) -> "ServerConfig":
        state = Path(os.getenv("YUANLIU_STATE_DIR", "/var/lib/yuanliu")).expanduser()
        cookies = os.getenv("YUANLIU_COOKIES", "").strip()
        return cls(
            bind=os.getenv("YUANLIU_BIND", "0.0.0.0"),
            port=int(os.getenv("YUANLIU_PORT", "8901")),
            token=os.getenv("YUANLIU_TOKEN", "").strip(),
            media_dir=Path(os.getenv("YUANLIU_MEDIA_DIR", state / "media")).expanduser(),
            cookies=Path(cookies).expanduser() if cookies else None,
        )


# 三种模式（Nija 2026-09-22：下载与转文字不要绑死）
MODE_MEDIA = "media"   # 只要视频
MODE_BOTH = "both"     # 视频 + 文字
MODE_TEXT = "text"     # 只要文字（转写完删视频，且只下音轨）


@dataclass
class Job:
    id: str
    text: str
    mode: str = MODE_MEDIA
    status: str = "queued"  # queued|running|done|failed
    stage: str = ""
    progress: float = 0.0
    engine: str = ""
    files: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text[:120],
            "mode": self.mode,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "engine": self.engine,
            "files": [Path(f).name for f in self.files] + [Path(f).name for f in self.transcripts],
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class LoginThrottle:
    """密码试错限速：连续失败就越来越慢（LAN 场景下的最低限度防护）。"""

    def __init__(self, max_attempts: int = 5, base_delay: float = 1.0) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()

    def delay_for(self, key: str) -> float:
        with self._lock:
            failures = self._failures.get(key, 0)
        if failures < self.max_attempts:
            return 0.0
        return min(30.0, self.base_delay * (2 ** (failures - self.max_attempts)))

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._failures[key] = self._failures.get(key, 0) + 1

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class YuanliuServer:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.jobs: dict[str, Job] = {}
        self.throttle = LoginThrottle()
        self._lock = threading.Lock()
        self._running = 0
        config.media_dir.mkdir(parents=True, exist_ok=True)

    # ---- 任务 ----
    def submit(self, text: str, *, mode: str = MODE_MEDIA) -> Job:
        if mode not in {MODE_MEDIA, MODE_BOTH, MODE_TEXT}:
            mode = MODE_MEDIA
        job = Job(id=uuid.uuid4().hex[:12], text=text, mode=mode)
        with self._lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job

    def _run_job(self, job: Job) -> None:
        while True:
            with self._lock:
                if self._running < MAX_CONCURRENT_JOBS:
                    self._running += 1
                    break
            time.sleep(0.5)
        job.status = "running"
        job.stage = "downloading"
        job.progress = 0.0

        started = time.time()
        logger.info("job %s 开始 mode=%s text=%s", job.id, job.mode, job.text[:80])
        _last_logged = {"pct": -1}

        def report(value: float) -> None:
            job.progress = min(1.0, max(0.0, float(value)))
            if 0.8 <= job.progress < 1.0:
                job.stage = "transcribing"
            pct = int(job.progress * 10) * 10
            if pct != _last_logged["pct"]:
                _last_logged["pct"] = pct
                logger.info("job %s 进度 %d%% stage=%s", job.id, pct, job.stage)

        try:
            outcome = download_media(
                job.text,
                self.config.media_dir,
                cookies=self.config.cookies,
                transcribe=job.mode in {MODE_BOTH, MODE_TEXT},
                keep_media=job.mode != MODE_TEXT,
                audio_only=job.mode == MODE_TEXT,
                on_progress=report,
            )
            job.engine = outcome.engine
            job.stage = "done"
            job.progress = 1.0
            job.files = [str(path) for path in outcome.files]
            job.transcripts = [str(path) for path in outcome.transcripts]
            if job.mode in {MODE_BOTH, MODE_TEXT} and outcome.transcribe_error:
                job.error = f"转文字失败（视频已下好）：{outcome.transcribe_error}"
            job.status = "done"
            logger.info(
                "job %s 完成 engine=%s 用时=%.1fs 文件=%s",
                job.id, job.engine, time.time() - started, [Path(f).name for f in job.files] or "-",
            )
        except (ResolveError, EngineFailed, EngineUnavailable) as exc:
            job.status = "failed"
            job.stage = "failed"
            job.error = str(exc)[:500]
            logger.warning("job %s 失败：%s", job.id, job.error)
        except Exception as exc:  # noqa: BLE001 - 服务端不能让单任务炸掉线程
            job.status = "failed"
            job.stage = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            job.finished_at = time.time()
            with self._lock:
                self._running -= 1

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [job.as_dict() for job in jobs[:50]]

    # ---- 业务 ----
    def resolve(self, text: str) -> dict[str, Any]:
        current = plan(text)
        payload: dict[str, Any] = {
            "platform": current.platforms,
            "url": current.link.url,
            "is_short": current.link.is_short,
            "route": list(current.engines),
            "available": list(current.available),
        }
        try:
            info = probe(text, timeout=240)
            payload["info"] = summarize_probe(info)
        except Exception as exc:  # noqa: BLE001 - 探测失败也要给出路线信息
            payload["probe_error"] = str(exc)[:300]
        return payload

    def list_files(self) -> list[dict[str, Any]]:
        """媒体目录清单（网页文件管理用）。"""
        entries: list[dict[str, Any]] = []
        directory = self.config.media_dir
        if not directory.is_dir():
            return entries
        for path in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_file() or path.suffix.lower() not in FILE_ALLOWED_SUFFIXES:
                continue
            stat = path.stat()
            entries.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime})
        return entries

    def _safe_media_path(self, name: str) -> Path | None:
        """只做**校验**（白名单 + 防穿越），不要求文件存在。

        带路径分隔符 / `..` 的一律**直接拒绝**（而不是悄悄取 basename）——
        语义清楚、拒绝得明白，避免"看着删了别的目录里的东西"的错觉。
        """
        raw = (name or "").strip()
        if any(sep in raw for sep in ("/", "\\")) or ".." in raw:
            return None
        safe = Path(raw).name
        if not safe or Path(safe).suffix.lower() not in FILE_ALLOWED_SUFFIXES:
            return None
        candidate = (self.config.media_dir / safe).resolve()
        if not str(candidate).startswith(str(self.config.media_dir.resolve())):
            return None
        return candidate

    def resolve_file(self, name: str) -> Path | None:
        candidate = self._safe_media_path(name)
        return candidate if candidate is not None and candidate.is_file() else None

    def delete_file(self, name: str) -> str:
        """删除媒体目录里的一个文件。返回 'ok' / 'not_found' / 'forbidden'。"""
        candidate = self._safe_media_path(name)
        if candidate is None:
            return "forbidden"
        try:
            candidate.unlink()
        except FileNotFoundError:
            return "not_found"
        except OSError as exc:
            logger.warning("删除失败 %s：%s", candidate, exc)
            return "forbidden"
        logger.info("已删除文件 %s", candidate.name)
        return "ok"


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yuanliu</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;padding:16px;font:16px/1.5 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;background:#111;color:#eee}
 h1{font-size:20px;margin:0 0 12px}
 textarea{width:100%;min-height:96px;padding:12px;border-radius:10px;border:1px solid #444;background:#1b1b1b;color:#eee;font-size:16px}
 button{margin:8px 8px 8px 0;padding:12px 18px;font-size:16px;border-radius:10px;border:0;background:#3b82f6;color:#fff}
 button.sec{background:#374151}
 .row{display:flex;gap:22px;flex-wrap:wrap;margin:12px 0;font-size:14px;color:#9ca3af}
 .card{border:1px solid #333;border-radius:10px;padding:12px;margin:10px 0;background:#181818}
 .ok{color:#34d399}.bad{color:#f87171}.run{color:#fbbf24}
 a{color:#60a5fa;word-break:break-all}
 pre{white-space:pre-wrap;font-size:13px;color:#cbd5e1}
 .bar{height:8px;border-radius:99px;background:#333;overflow:hidden;margin:6px 0}
 .bar > i{display:block;height:100%;background:#3b82f6;width:0;transition:width .4s}
 .bar.indet > i{width:35%;animation:slide 1.2s infinite}
 @keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
 .stage{font-size:13px;color:#9ca3af}
</style></head><body>
<h1>yuanliu <span style="font-size:13px;color:#6b7280">v__VERSION__</span></h1>
<div id="banner">__BANNER__</div>
<div id="gate" class="card" style="display:none">
  <div>请输入密码（一次即可，会记在这台设备上）</div>
  <div style="margin-top:8px"><input id="pw" type="password" placeholder="密码" autocomplete="current-password"
     style="padding:12px;font-size:16px;border-radius:10px;border:1px solid #444;background:#1b1b1b;color:#eee"></div>
  <button onclick="login()">进入</button>
  <span id="gateMsg" class="bad"></span>
</div>
<div id="app" style="display:none">
<textarea id="t" placeholder="把 App 里复制的分享文案整段贴进来（含短链也行）"></textarea>
<div>
 <button onclick="go('resolve')">解析（不下载）</button>
 <button onclick="go('download')">下载</button>
 <button class="sec" onclick="load()">刷新列表</button>
 <button class="sec" onclick="logout()">退出</button>
</div>
<div class="row">
 <label><input type="radio" name="mode" value="media" checked> 只要视频</label>
 <label><input type="radio" name="mode" value="both"> 视频 + 文字</label>
 <label><input type="radio" name="mode" value="text"> 只要文字（转完删视频，只下音轨）</label>
</div>
<div class="row"><span>平台/路线会显示在下面</span><span id="k"></span></div>
<div id="out"></div>
<div id="jobs"></div>
<div id="files"></div>
</div>
<script>
// 密码模式：优先用 URL 里的 k（老书签），否则用本机记住的密码
let K = new URLSearchParams(location.search).get('k') || localStorage.getItem('yuanliu_pw') || '';
document.getElementById('k').textContent = K ? '已登录' : '';
async function login(){
  const pw = document.getElementById('pw').value;
  if(!pw) return;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
  if(r.ok){ localStorage.setItem('yuanliu_pw', pw); K = pw; showApp(); }
  else { document.getElementById('gateMsg').textContent = '密码不对'; }
}
function showApp(){
  document.getElementById('gate').style.display = 'none';
  document.getElementById('app').style.display = 'block';
  document.getElementById('k').textContent = '已登录';
  load();
}
function logout(){
  localStorage.removeItem('yuanliu_pw'); K='';
  document.getElementById('app').style.display='none';
  document.getElementById('gate').style.display='block';
}
async function api(path, body){
  const r = await fetch(path + (K?('?k='+encodeURIComponent(K)):''), {method:'POST',
    headers:{'Content-Type':'application/json','X-Token':K}, body: JSON.stringify(body||{})});
  const j = await r.json().catch(()=>({error:'非 JSON 响应', status:r.status}));
  if (r.status === 401) {
    document.getElementById('banner').innerHTML =
      '<div class="card"><b class="bad">token 不对或已过期</b>：你打开的链接里的 k 不是这台机器当前的 token。'
      + '请在电脑上执行 <code>cat /etc/yuanliu/token</code>，用里面那个值重新拼 URL。</div>';
  }
  return {status:r.status, j};
}
async function go(kind){
  const text = document.getElementById('t').value.trim();
  if(!text) return;
  document.getElementById('out').innerHTML = '<div class="card">处理中…</div>';
  const body = {text};
  const picked = document.querySelector('input[name=mode]:checked');
  if (kind === 'download' && picked) body.mode = picked.value;
  const {status,j} = await api('/api/'+kind, body);
  document.getElementById('out').innerHTML = '<div class="card"><pre>'+JSON.stringify(j,null,1).replace(/[<>]/g,'')+'</pre></div>';
  if(kind==='download') setTimeout(load, 1500);
}
async function loadFiles(){
  const r = await fetch('/api/files'+(K?('?k='+encodeURIComponent(K)):''), {headers:{'X-Token':K}});
  if(!r.ok) return;
  const j = await r.json();
  const el = document.getElementById('files');
  el.innerHTML = '<h1 style="font-size:16px;margin-top:20px">文件（服务机上 /var/lib/yuanliu/media）</h1>';
  const fmt = n => n>1048576 ? (n/1048576).toFixed(1)+'MB' : (n/1024).toFixed(0)+'KB';
  if(!(j.files||[]).length){ el.innerHTML += '<div class="stage">（空）</div>'; return; }
  (j.files||[]).forEach(f=>{
    const url = '/files/'+encodeURIComponent(f.name)+(K?('?k='+encodeURIComponent(K)):'');
    const when = new Date(f.mtime*1000).toLocaleString();
    el.innerHTML += '<div class="card"><div><a href="'+url+'">'+f.name+'</a></div>'
      + '<div class="stage">'+fmt(f.size)+' · '+when+'</div>'
      + '<button class="sec" onclick="delFile(\''+f.name.replace(/'/g,"\\'")+'\')">删除</button></div>';
  });
}
async function delFile(name){
  if(!confirm('确定删除「'+name+'」？删了就没了。')) return;
  const r = await fetch('/api/files/delete'+(K?('?k='+encodeURIComponent(K)):''), {method:'POST',
    headers:{'Content-Type':'application/json','X-Token':K}, body: JSON.stringify({name})});
  if(!r.ok){ alert('删除失败：'+(await r.text()).slice(0,120)); }
  loadFiles();
}
async function load(){
  const r = await fetch('/api/jobs'+(K?('?k='+encodeURIComponent(K)):''), {headers:{'X-Token':K}});
  const j = await r.json();
  const el = document.getElementById('jobs'); el.innerHTML = '<h1 style="font-size:16px;margin-top:20px">下载记录</h1>';
  (j.jobs||[]).forEach(x=>{
    const cls = x.status==='done'?'ok':(x.status==='failed'?'bad':'run');
    const modeLabel = {media:'只要视频', both:'视频+文字', text:'只要文字'}[x.mode] || x.mode;
    const stageLabel = {queued:'排队中', downloading:'下载中', transcribing:'转文字中', done:'完成', failed:'失败'}[x.stage] || x.stage || x.status;
    let html = '<div class="card"><div>['+x.status+'] <span class="'+cls+'">'+x.engine+'</span> <b>'+modeLabel+'</b> '+x.text.slice(0,50)+'</div>';
    if (x.status === 'running' || x.status === 'queued') {
      const pct = Math.round((x.progress||0)*100);
      html += '<div class="bar'+(pct>0?'':' indet')+'"><i style="width:'+(pct>0?pct:35)+'%"></i></div>'
            + '<div class="stage">'+stageLabel+(pct>0?(' · '+pct+'%'):'')+'</div>';
    }
    if(x.error) html += '<pre>'+x.error.replace(/[<>]/g,'')+'</pre>';
    (x.files||[]).forEach(f=>{ html += '<div><a href="/files/'+encodeURIComponent(f)+(K?('?k='+encodeURIComponent(K)):'')+'">'+f+'</a></div>'; });
    el.innerHTML += html+'</div>';
  });
}
// 启动：有密码就先验一下，验不过就回退到密码门
(async () => {
  if (K) {
    const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: K})}).catch(()=>null);
    if (r && r.ok) { showApp(); return; }
    localStorage.removeItem('yuanliu_pw'); K='';
  }
  document.getElementById('gate').style.display = 'block';
})();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = f"yuanliu/{__version__}"
    app: YuanliuServer

    def log_message(self, fmt: str, *args: Any) -> None:  # 保持 journal 干净
        if os.getenv("YUANLIU_VERBOSE"):
            super().log_message(fmt, *args)

    # ---- helpers ----
    def _token_ok(self, query: dict[str, list[str]]) -> bool:
        expected = self.app.config.token
        if not expected:
            return False  # fail-closed：没配 token 就不开
        supplied = ""
        if query.get("k"):
            supplied = query["k"][0]
        elif self.headers.get("X-Token"):
            supplied = self.headers["X-Token"]
        return supplied == expected

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---- routes ----
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            if self._token_ok(query):
                banner = '<div class="card ok">token 有效，可以直接用。</div>'
            elif query.get("k"):
                banner = ('<div class="card"><b class="bad">token 无效或已过期</b>'
                          '（多半用了旧链接）。请在电脑上跑 <code>cat /etc/yuanliu/token</code>，'
                          '用里面的值重新拼 <code>?k=…</code>。</div>')
            else:
                banner = ('<div class="card"><b class="run">链接里没带 token</b>：请在 URL 后面加 '
                          '<code>?k=&lt;token&gt;</code>，否则解析/下载都会 401。</div>')
            body = (
                PAGE.replace("__VERSION__", __version__)
                .replace("__BANNER__", banner)
                .encode()
            )
            self._send(200, body, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._json(
                200,
                {
                    "ok": True,
                    "version": __version__,
                    "module": __file__,          # 服务到底跑的哪份代码（排查"改了没生效"）
                    "python": sys.version.split()[0],
                    "jobs": len(self.app.jobs),
                },
            )
            return
        if not self._token_ok(query):
            self._json(401, {"error": "需要 token（?k=… 或 X-Token）"})
            return
        if parsed.path == "/api/jobs":
            self._json(200, {"jobs": self.app.snapshot()})
            return
        if parsed.path == "/api/files":
            self._json(200, {"files": self.app.list_files()})
            return
        if parsed.path.startswith("/files/"):
            name = urllib.parse.unquote(parsed.path[len("/files/") :])
            path = self.app.resolve_file(name)
            if path is None:
                self._json(404, {"error": "文件不存在或类型不允许"})
                return
            self._send(200, path.read_bytes(), "application/octet-stream")
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        # 登录**必须在 token 检查之前**（否则永远 401，密码门形同虚设）
        if parsed.path == "/api/login":
            import time as _time

            payload = self._body()
            key = self.client_address[0] if self.client_address else "?"
            delay = self.app.throttle.delay_for(key)
            if delay:
                _time.sleep(min(delay, 5.0))
            password = str(payload.get("password") or "")
            if password and password == self.app.config.token:
                self.app.throttle.reset(key)
                self._json(200, {"ok": True})
            else:
                self.app.throttle.record_failure(key)
                self._json(401, {"error": "密码不对"})
            return
        if not self._token_ok(query):
            self._json(401, {"error": "需要密码（页面里输入，或 ?k=… / X-Token）"})
            return
        payload = self._body()
        text = str(payload.get("text") or "").strip()
        if not text:
            self._json(400, {"error": "text 不能为空"})
            return
        if parsed.path == "/api/resolve":
            try:
                self._json(200, self.app.resolve(text))
            except ResolveError as exc:
                self._json(400, {"error": str(exc), "kind": type(exc).__name__})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        if parsed.path == "/api/files/delete":
            result = self.app.delete_file(str(payload.get("name") or ""))
            self._json(200 if result == "ok" else (404 if result == "not_found" else 403), {"result": result})
            return
        if parsed.path == "/api/download":
            mode = str(payload.get("mode") or "").strip()
            if not mode:  # 向后兼容老的 transcribe 布尔字段
                mode = MODE_BOTH if payload.get("transcribe") else MODE_MEDIA
            job = self.app.submit(text, mode=mode)
            self._json(202, {"job": job.as_dict()})
            return
        self._json(404, {"error": "not found"})


def build_server(config: ServerConfig) -> ThreadingHTTPServer:
    app = YuanliuServer(config)
    handler = type("BoundHandler", (_Handler,), {"app": app})
    httpd = ThreadingHTTPServer((config.bind, config.port), handler)
    httpd.daemon_threads = True
    return httpd


def _lan_addresses() -> list[str]:
    import socket

    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:  # 只靠 hostname 可能拿不到，补一条"连出去看源地址"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.168.31.1", 9))
            found.add(sock.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith("127."))


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )

    parser = argparse.ArgumentParser(prog="yuanliu serve", description="常驻服务（网页 + JSON API）")
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default=None)
    args = parser.parse_args(argv or [])

    config = ServerConfig.from_env()
    if args.bind:
        config.bind = args.bind
    if args.port:
        config.port = args.port
    if args.token is not None:
        config.token = args.token

    if not config.token:
        print("拒绝启动：没有 token（设 YUANLIU_TOKEN，或 install.sh 生成 /etc/yuanliu/env）")
        return 2

    httpd = build_server(config)
    print(f"yuanliu 服务已起：http://{config.bind}:{config.port}/?k={config.token}")
    for ip in _lan_addresses():
        print(f"  平板可用：http://{ip}:{config.port}/?k={config.token}")
    print(f"  下载目录：{config.media_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
