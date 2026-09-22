"""浏览器嗅探引擎 —— 不签名、不逆向：让**页面自己的 JS**去请求，我们只读响应。

为什么需要它（实测 2026-09-22）
-----------------------------
抖音网页版对未登录访问直接弹登录墙；`yt-dlp` 报 `Fresh cookies needed`；`lux` 自签 `X-Bogus` 仍 403。
但**真浏览器打开页面后，页面自己发的 `aweme/v1/web/aweme/detail/` 是 200 且带完整 JSON**，
里面有 `play_addr.url_list` —— 那一路 CDN 流**没有水印**（而 `download_addr` 反而带 `watermark=1`）。

⇒ 这就是"解析站"的核心技术之一：**借一个真浏览器的身份 + 让它自己去请求**。
代价是**不稳定**（平台随时改），所以内置重试与多 UA。

外部依赖：`node` + Playwright（脚本 `tools/sniff.cjs`，随包走）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse as _urlparse

from yuanliu.engines import EngineFailed, EngineUnavailable

__all__ = ["BrowserEngine", "DEFAULT_SNIFF_SCRIPT", "DEFAULT_NODE", "MIN_INTERVAL_SECONDS", "RateLimiter"]

# ⚠️ 事故教训（2026-09-22）：短时间高频自动访问把小红书打到风控。
# 从此**同一个浏览器引擎两次请求之间强制最小间隔**，宁慢勿死。
MIN_INTERVAL_SECONDS = float(os.getenv("YUANLIU_MIN_INTERVAL", "20"))

DEFAULT_NODE = "/usr/bin/node"
DEFAULT_SNIFF_SCRIPT = "/opt/yuanliu/tools/sniff.cjs"

# 嗅探器要访问的平台（其余平台走 yt-dlp/lux 更划算）
BROWSER_PLATFORMS = frozenset({"douyin", "xiaohongshu", "weibo", "bilibili", "kuaishou", "unknown"})


class RateLimiter:
    """进程内最小间隔限流（同一引擎共享）。测试里可注入时钟。"""

    def __init__(self, min_interval: float = MIN_INTERVAL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval = max(0.0, min_interval)
        self._clock = clock
        self._last: float | None = None  # None = 还没调用过
        self._lock = threading.Lock()

    def wait(self) -> float:
        """返回实际等待秒数（0 = 没等）。"""
        with self._lock:
            now = self._clock()
            waited = 0.0
            if self._last is not None:
                remaining = self.min_interval - (now - self._last)
                if remaining > 0:
                    time.sleep(remaining)
                    waited = remaining
            self._last = self._clock()
            return waited


@dataclass(slots=True)
class BrowserEngine:
    name: str = "browser"
    platforms: frozenset[str] = BROWSER_PLATFORMS
    node_env: str = "YUANLIU_NODE"
    script_env: str = "YUANLIU_SNIFF"
    default_node: str = DEFAULT_NODE
    default_script: str = DEFAULT_SNIFF_SCRIPT
    runner: Callable[[Sequence[str], float, dict], subprocess.CompletedProcess[str]] | None = field(
        default=None, repr=False
    )
    limiter: RateLimiter | None = field(default=None, repr=False)

    # ---- 发现 ----
    @property
    def node(self) -> str | None:
        return self._resolve(self.node_env, self.default_node, "node")

    @property
    def script(self) -> str | None:
        explicit = os.getenv(self.script_env)
        if explicit and Path(explicit).is_file():
            return explicit
        if Path(self.default_script).is_file():
            return self.default_script
        # 开发场兜底（源码位）
        dev = Path.home() / "dev" / "yuanliu" / "tools" / "sniff.cjs"
        return str(dev) if dev.is_file() else None

    def _resolve(self, env_var: str, default: str, which: str) -> str | None:
        # 显式设了环境变量就是**硬约定**：指到不存在的路径 = 不可用，不回退
        # （否则测试/运维里"关掉某引擎"根本关不掉）
        explicit = os.getenv(env_var)
        if explicit:
            return explicit if Path(explicit).exists() else None
        if Path(default).exists():
            return default
        return shutil.which(which)

    def available(self) -> bool:
        return self.node is not None and self.script is not None

    # ---- 嗅探 ----
    def sniff(
        self,
        url: str,
        *,
        cookies: Path | None = None,
        attempts: int = 3,
        timeout: float = 300.0,
        download_to: Path | None = None,
    ) -> dict:
        node, script = self.node, self.script
        if node is None or script is None:
            raise EngineUnavailable(
                "浏览器嗅探需要 node + tools/sniff.cjs（设 YUANLIU_NODE / YUANLIU_SNIFF，"
                "或把 tools/sniff.cjs 装到 /opt/yuanliu/tools/）。"
            )
        limiter = self.limiter or _SHARED_LIMITER
        limiter.wait()  # 防"重试轰炸"：同一引擎两次请求之间强制间隔
        env = dict(os.environ)
        if cookies is not None:
            env["SNIFF_COOKIES"] = str(cookies)
        cmd = [node, script, url, str(attempts)]
        if download_to is not None:
            download_to.parent.mkdir(parents=True, exist_ok=True)
            cmd.append(str(download_to))
        run = self.runner or (
            lambda c, t, e: subprocess.run(  # noqa: S603
                list(c), capture_output=True, encoding="utf-8", errors="replace", timeout=t, env=e
            )
        )
        try:
            result = run(cmd, timeout, env)
        except subprocess.TimeoutExpired as exc:
            raise EngineFailed(f"浏览器嗅探超时（{timeout}s）：{url}") from exc
        payload = (result.stdout or "").strip()
        if not payload:
            raise EngineFailed(f"浏览器嗅探没有输出：{(result.stderr or '').strip()[:300]}")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise EngineFailed(f"浏览器嗅探输出不是 JSON：{exc}") from exc
        if not data.get("ok"):
            raise EngineFailed(f"浏览器嗅探未能拿到媒体直链：{data.get('error') or '未知原因'}")
        return data

    def probe(self, url: str, *, timeout: float = 300.0) -> dict:
        data = self.sniff(url, timeout=timeout)
        return {
            "engine": self.name,
            "title": data.get("title"),
            "meta": data.get("meta"),
            "play_urls": data.get("play_urls", []),
            "download_urls": data.get("download_urls", []),
        }

    def download(
        self, url: str, outdir: Path, *, cookies: Path | None = None, timeout: float = 900.0
    ) -> list[Path]:
        # 嗅探本质是"跟平台赛跑"，下载这条路上失败一次不奇怪 ⇒ 多给两次机会
        data = self.sniff(url, cookies=cookies, attempts=5, timeout=timeout)
        media = data.get("play_urls") or []
        if not media:
            raise EngineFailed("嗅探到了页面信息，但没有可下载的媒体直链")
        outdir.mkdir(parents=True, exist_ok=True)
        meta = data.get("meta") or {}
        stem = _safe_stem(meta.get("desc") or data.get("title") or "media")
        target = outdir / f"{stem}.mp4"
        self._fetch(media[0], target, referer=_referer(url), timeout=timeout)
        return [target] if target.exists() and target.stat().st_size > 0 else []

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def _fetch_first_ok(
        self,
        media_urls: Sequence[str],
        target: Path,
        *,
        referer: str,
        timeout: float,
        cookies: Path | None = None,
    ) -> None:
        last: Exception | None = None
        for index, media_url in enumerate(media_urls):
            attempt_target = target if index == 0 else target.with_name(f"{target.stem}-{index}{target.suffix}")
            try:
                self._fetch(media_url, attempt_target, referer=referer, timeout=timeout, cookies=cookies)
            except EngineFailed as exc:
                last = exc
                continue
            if attempt_target != target:
                attempt_target.replace(target)
            return
        raise EngineFailed(f"所有候选直链都下载失败：{last}") from last

    def _looks_like_media(self, target: Path) -> bool:
        """CDN 失败时返回的是 HTML 错误页（403/404），不能当视频收下。"""
        if not target.exists():
            return False
        size = target.stat().st_size
        if size < 10_000:
            return False
        with target.open("rb") as handle:
            head = handle.read(4)
        return not head.startswith(b"<")

    def _fetch(
        self,
        media_url: str,
        target: Path,
        *,
        referer: str,
        timeout: float,
        cookies: Path | None = None,
    ) -> None:
        """下 CDN 直链。**必须像浏览器**：UA + Referer + Origin，否则 CDN 直接 403。

        urllib 挡不住时退到 curl（服务器对两者的指纹判定并不总是相同，实测遇到过）。
        """
        headers = {
            "User-Agent": self._UA,
            "Referer": referer,
            "Origin": referer.rstrip("/"),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        request = urllib.request.Request(media_url, headers=headers)  # noqa: S310 - 直链来自平台
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as handle:  # noqa: S310
                shutil.copyfileobj(response, handle, length=1 << 20)
            if self._looks_like_media(target):
                return
        except Exception as urllib_error:  # noqa: BLE001
            if target.exists():
                target.unlink(missing_ok=True)
            self._fetch_with_curl(media_url, target, referer=referer, timeout=timeout, cookies=cookies, cause=urllib_error)
            return
        raise EngineFailed("下载媒体直链失败：响应为空")

    def _fetch_with_curl(
        self,
        media_url: str,
        target: Path,
        *,
        referer: str,
        timeout: float,
        cookies: Path | None,
        cause: Exception,
    ) -> None:
        curl = shutil.which("curl")
        if curl is None:
            raise EngineFailed(f"下载媒体直链失败（直链通常几分钟内失效）：{cause}") from cause
        cmd = [curl, "-fsSL", "--max-time", str(int(timeout)), "-A", self._UA, "-e", referer, "-o", str(target)]
        if cookies is not None:
            cmd += ["-b", str(cookies)]
        cmd.append(media_url)
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)  # noqa: S603
        if result.returncode != 0 or not self._looks_like_media(target):
            raise EngineFailed(
                f"下载媒体直链失败（直链通常几分钟内失效）：{result.stderr.strip()[:200] or cause}"
            ) from cause


_SHARED_LIMITER = RateLimiter()


def _referer(url: str) -> str:
    parsed = _urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else "https://www.douyin.com/"


def _safe_stem(value: str) -> str:
    import re

    stem = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "-", value or "").strip("-._")
    return (stem or "media")[:60]
