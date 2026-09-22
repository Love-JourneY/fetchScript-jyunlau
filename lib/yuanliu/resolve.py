"""编排层：分享文案 → 计划(plan) → 探测(probe) → 下载(download)。

失败一律**分型**（借 chubbyskills 的分类思路），不糊一个大 RuntimeError：
- 平台不支持   → :class:`UnsupportedPlatform`
- 引擎都没有   → :class:`NoEngineAvailable`
- 引擎跑挂了   → :class:`EngineFailed`（含引擎名与 stderr 片段；要 cookie 也走这里，需人工判读）
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable
from datetime import datetime
from pathlib import Path

from yuanliu.engines import (
    Engine,
    EngineFailed,
    EngineUnavailable,
    engines_for_platform,
    pick_engines,
)
from yuanliu.share_links import ShareLink, ShareLinkError, parse_input, resolve_short_url

__all__ = [
    "ResolveError",
    "UnsupportedPlatform",
    "NoEngineAvailable",
    "Plan",
    "DownloadOutcome",
    "plan",
    "probe",
    "download",
    "REJECTED_PLATFORMS",
    "COOLDOWN_FILE",
]

# 视频号没有直链引擎（只能装证书做 MITM 嗅探），明确拒绝而不是给个跑不通的方案。
REJECTED_PLATFORMS = {"wechat_channels": "微信视频号没有公开直链，只能走 MITM 嗅探（另行决定要不要做）"}

# 冷却期闸门：某个平台被我们打到风控时，**禁止再自动打它**（防止越打越死）
# 文件内容是 ISO 时间戳或 unix 秒；到点自动失效。
COOLDOWN_FILE = Path(os.getenv("YUANLIU_COOLDOWN_FILE", Path.home() / ".cache/yuanliu/cooldown.json"))


def _cooldown_error(platform: str) -> str | None:
    if not COOLDOWN_FILE.is_file():
        return None
    try:
        data = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏文件不该拦住正常使用
        return None
    entry = data.get(platform)
    if not entry:
        return None
    until = entry.get("until") if isinstance(entry, dict) else entry
    if until is None:
        return None
    if isinstance(until, str):
        try:
            deadline = datetime.fromisoformat(until).timestamp()
        except ValueError:
            return None
    else:
        deadline = float(until)
    if time.time() >= deadline:
        return None
    when = datetime.fromtimestamp(deadline).strftime("%m-%d %H:%M")
    reason = entry.get("reason", "") if isinstance(entry, dict) else ""
    return f"「{platform}」处于冷却期（到 {when} 才能再用）。原因：{reason or '此前触发风控'}。冷却期内不再自动请求。"


class ResolveError(RuntimeError):
    """本模块所有失败的基类。"""


class UnsupportedPlatform(ResolveError):
    """链接识别出来了，但没有可用路线。"""


class NoEngineAvailable(ResolveError):
    """该平台有路线，但本机没有任何可用引擎（二进制没装）。"""


@dataclass(slots=True)
class Plan:
    link: ShareLink
    platforms: str
    engines: tuple[str, ...]
    available: tuple[str, ...]
    resolved_url: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.available)


@dataclass(slots=True)
class DownloadOutcome:
    plan: Plan
    engine: str
    files: list[Path] = field(default_factory=list)
    transcripts: list[Path] = field(default_factory=list)
    transcribe_error: str = ""
    discarded: list[Path] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "platform": self.plan.link.platform,
            "resolved_url": self.plan.resolved_url or self.plan.link.url,
            "engine": self.engine,
            "files": [str(path) for path in self.files],
            "transcripts": [str(path) for path in self.transcripts],
            "transcribe_error": self.transcribe_error,
            "discarded": [str(path) for path in self.discarded],
        }


def plan(text: str, *, only_available: bool = True) -> Plan:
    """只做离线决策：判平台、给规范化链接与候选引擎，**不发网络请求**。"""
    try:
        link = parse_input(text)   # 宽松入口：整段文案 / 裸 BV 号 / 无 scheme 域名都认
    except ShareLinkError as exc:
        raise UnsupportedPlatform(str(exc)) from exc

    if link.platform in REJECTED_PLATFORMS:
        raise UnsupportedPlatform(f"「{link.platform}」：{REJECTED_PLATFORMS[link.platform]}")
    cooling = _cooldown_error(link.platform)
    if cooling:
        raise UnsupportedPlatform(cooling)

    engines = engines_for_platform(link.platform)
    usable = pick_engines(link.platform, only_available=only_available)
    return Plan(
        link=link,
        platforms=link.platform,
        engines=tuple(engine.name for engine in engines),
        available=tuple(engine.name for engine in usable),
    )


def _resolve_if_short(link: ShareLink, *, timeout: float = 10.0) -> str:
    if not link.is_short:
        return link.url
    import logging
    import time as _time

    started = _time.monotonic()
    resolved = canonical_after_redirect(link)
    logging.getLogger("yuanliu").info(
        "短链解析 %s → %s（%.1fs）", link.url, resolved, _time.monotonic() - started
    )
    return resolved


def canonical_after_redirect(link: ShareLink, *, timeout: float = 10.0) -> str:
    """短链跟跳转后再规范化一次（B 站短链才拿得到 BV 号）。"""
    final = resolve_short_url(link.url, timeout=timeout)
    if final == link.url:
        return link.url
    from yuanliu.share_links import canonicalize_url, detect_platform

    return canonicalize_url(final, detect_platform(final))


def probe(text: str, *, timeout: float = 60.0) -> dict:
    """取元信息（不下载）。依次尝试该平台可用引擎，第一个成功即返回。"""
    current = plan(text)
    resolved = _resolve_if_short(current.link)
    current.resolved_url = resolved

    usable = pick_engines(current.link.platform)
    if not usable:
        raise NoEngineAvailable(
            f"「{current.link.platform}」需要这些引擎之一：{current.engines or '(无)'}；"
            f"本机都没有。装法见模块 README（yt-dlp / lux）。"
        )

    errors: list[str] = []
    for engine in usable:
        try:
            data = engine.probe(resolved, timeout=timeout)
        except (EngineFailed, EngineUnavailable) as exc:
            errors.append(f"{engine.name}: {exc}")
            continue
        return {"platform": current.link.platform, "engine": engine.name, "resolved_url": resolved, "info": data}
    raise EngineFailed("所有引擎都失败 —— " + "；".join(errors))


def _title_hint(resolved_url: str, engine_name: str) -> str | None:
    """标题从哪来：浏览器引擎已在文件名里给了标题；yt-dlp/lux 也用标题当模板。
    这里只做兜底：拿不到就算了，交给引擎自己的名字规范化。"""
    return None


def _discard_media(files: list[Path]) -> list[Path]:
    """删中间媒体（只留文字模式）。删不掉不算失败，记下来让人知道。"""
    removed: list[Path] = []
    for path in files:
        try:
            if path.exists():
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def _transcribe_into(outcome: DownloadOutcome, outdir: Path) -> None:
    """转写失败**不算下载失败**：文稿拿不到就只记错误，视频照样交付。"""
    from yuanliu.transcribe import Transcriber

    transcriber = Transcriber()
    for media in outcome.files:
        if media.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".flac"}:
            continue
        try:
            result = transcriber.transcribe(media, copy_to=outdir)
            outcome.transcripts.append(Path(result.transcript))
        except Exception as exc:  # noqa: BLE001 - 转写是附加能力，不能拖垮下载
            outcome.transcribe_error = f"{type(exc).__name__}: {exc}"[:300]
        break


def _rename_by_title(files: list[Path], *, title: str | None) -> list[Path]:
    """把产物统一成 `safe_title(标题).ext`（同名冲突时加 -2、-3）。"""
    from yuanliu.names import normalize_existing_name, safe_title

    renamed: list[Path] = []
    for path in files:
        stem = safe_title(title) if title else normalize_existing_name(path.name).rsplit(".", 1)[0]
        suffix = path.suffix
        target = path.with_name(f"{stem}{suffix}")
        index = 2
        while target.exists() and target != path:
            target = path.with_name(f"{stem}-{index}{suffix}")
            index += 1
        if target != path:
            path.replace(target)
        renamed.append(target)
    return renamed


def summarize_probe(payload: dict) -> dict:
    """把不同引擎的探测结果**归一成人类看得懂的一行**（标题/时长/作者/媒体数）。

    实测痛点：yt-dlp 返回的是它自己的 info dict，里面没有 `play_urls`/`meta` 字段，
    直接透传给前端就变成 `play_count: 0, meta: null` —— 看着像"没做"，其实拿到了。
    """
    info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
    info = info or {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else None
    play = info.get("play_urls") or []
    images = info.get("images") or []
    title = info.get("title") or (meta or {}).get("desc") or info.get("fulltitle")
    duration = info.get("duration") or (meta or {}).get("duration")
    if isinstance(duration, (int, float)) and duration > 1000:  # 毫秒 → 秒
        duration = duration / 1000
    return {
        "engine": payload.get("engine") or info.get("extractor"),
        "title": title,
        "uploader": info.get("uploader") or info.get("channel") or (meta or {}).get("author"),
        "duration": round(duration, 1) if isinstance(duration, (int, float)) else None,
        "thumbnail": info.get("thumbnail"),
        "media_count": len(play) or (1 if info.get("url") or info.get("formats") else 0),
        "image_count": len(images),
        "webpage_url": info.get("webpage_url"),
    }


def _existing_cookies(cookies: Path | None) -> Path | None:
    """cookies 路径不存在就当没有 —— 否则会把 `--cookies /bad/path` 喂给引擎，全线失败。
    （实测事故：install.sh 以 root 跑，$HOME 展开成 /root，服务一直拿着 /root/.cache/...）"""
    if cookies is None:
        return None
    return cookies if Path(cookies).is_file() else None


def download(
    text: str,
    outdir: Path,
    *,
    engine_name: str | None = None,
    cookies: Path | None = None,
    timeout: float = 900.0,
    transcribe: bool = False,
    keep_media: bool = True,
    audio_only: bool = False,
    on_progress: Callable[[float], None] | None = None,
) -> DownloadOutcome:
    """下载到 ``outdir``，可选接着转文字。

    解耦（Nija 2026-09-22）：**「要不要视频」和「要不要文字」是两件事** ——

    - ``transcribe=True, keep_media=True``：视频 + 文稿都留（默认）
    - ``transcribe=True, keep_media=False``：**只留文字**，转写完把视频/音频删掉
      （文字存储成本低得多、检索与引用效率高得多；纯音频内容的视频尤其没必要留）
    - ``audio_only=True``：只为取文字时**干脆不下整片**（yt-dlp 取 `ba`、lux 用 `-ao`）
    """
    cookies = _existing_cookies(cookies)
    current = plan(text)
    resolved = _resolve_if_short(current.link)
    current.resolved_url = resolved

    usable = pick_engines(current.link.platform)
    if engine_name is not None:
        usable = tuple(engine for engine in usable if engine.name == engine_name)
        if not usable:
            raise NoEngineAvailable(f"引擎 {engine_name} 不适用于「{current.link.platform}」")
    if not usable:
        raise NoEngineAvailable(
            f"「{current.link.platform}」需要这些引擎之一：{current.engines or '(无)'}；本机都没有。"
        )

    # 进度语义：下载占 0~0.8（要转写时），转写 0.8~1.0；只要视频时下载直接 0~1
    download_span = 0.8 if transcribe else 1.0

    def engine_progress(value: float) -> None:
        if on_progress is not None:
            on_progress(min(download_span, max(0.0, value) * download_span))

    errors: list[str] = []
    for engine in usable:
        try:
            files = engine.download(
                resolved,
                outdir,
                cookies=cookies,
                timeout=timeout,
                audio_only=audio_only,
                on_progress=engine_progress,
            )
        except (EngineFailed, EngineUnavailable) as exc:
            errors.append(f"{engine.name}: {exc}")
            continue
        if files:
            files = _rename_by_title(files, title=_title_hint(resolved, engine.name))
            outcome = DownloadOutcome(plan=current, engine=engine.name, files=files)
            if transcribe:
                if on_progress is not None:
                    on_progress(0.85)  # 阶段 3：转文字（CPU 上最慢的一段）
                _transcribe_into(outcome, outdir)
                if on_progress is not None:
                    on_progress(1.0)
                if not keep_media and not outcome.transcribe_error:
                    # 只要文字：**转写成功才删**（失败时必须留着媒体，人还能自己再试）
                    outcome.discarded = _discard_media(outcome.files)
                    outcome.files = []
            return outcome
        errors.append(f"{engine.name}: 报告成功但没有产出文件")
    # 把**每个引擎**的原因都说出来：只报最后一个会把真正的病根藏起来（实测踩过）
    raise EngineFailed("所有引擎都失败 —— " + "；".join(errors))


def describe_routes() -> list[dict]:
    """给 CLI/验收用：平台 → 候选引擎 → 本机可用性。"""
    rows: list[dict] = []
    for platform, engine_names in sorted(_routing_items()):
        engines: tuple[Engine, ...] = engines_for_platform(platform)
        rows.append(
            {
                "platform": platform,
                "route": list(engine_names),
                "available": [engine.name for engine in engines if engine.available()],
            }
        )
    return rows


def _routing_items():
    from yuanliu.engines import PLATFORM_ROUTING

    return PLATFORM_ROUTING.items()
