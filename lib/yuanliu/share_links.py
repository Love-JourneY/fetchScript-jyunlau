"""分享文案 → 规范化链接 + 平台识别。

为什么需要它：用户在 App 里点"分享/复制链接"，拿到的是**一整段文案**
（口令、表情、广告词都在里面），而不是干净的 URL。yt-dlp / lux 这类下载器
只认"你给我一个 URL"，所以"抠 URL → 跟短链 → 去追踪参数 → 判平台"必须由我们自己完成。

设计约束：
- **本模块不做网络请求**（`resolve_short_url` 除外，且必须显式调用，opener 可注入 ⇒ 可离线单测）。
- 识别不出平台不算错（返回 ``platform="unknown"``）；由调用方决定是否拒绝。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

__all__ = [
    "ShareLink",
    "ShareLinkError",
    "UnsupportedPlatformError",
    "extract_urls",
    "detect_platform",
    "canonicalize_url",
    "parse_share_text",
    "is_short_url",
    "resolve_short_url",
    "extract_bv",
    "SUPPORTED_PLATFORMS",
]


class ShareLinkError(ValueError):
    """分享文案里没有可用链接。"""


class UnsupportedPlatformError(ShareLinkError):
    """平台识别出来了，但本版本还没有该平台的下载后端。"""


@dataclass(frozen=True, slots=True)
class ShareLink:
    raw: str
    url: str
    platform: str
    is_short: bool = False


# B 站是当前唯一打通下载/转写的平台。
SUPPORTED_PLATFORMS = frozenset({"bilibili"})

# URL 里不该跟着走的各种尾巴：中文标点、括号、引号、空白
_URL_STOP = r"[^\s，。、；：！？…“”‘’（）()<>《》【】\[\]{}'\"|]+"
_URL_RE = re.compile(r"https?://" + _URL_STOP, re.IGNORECASE)

# host 后缀 → 平台
_HOST_PLATFORM: tuple[tuple[tuple[str, ...], str], ...] = (
    (("bilibili.com", "bilibili.tv", "b23.tv"), "bilibili"),
    (("douyin.com", "iesdouyin.com"), "douyin"),
    (("xiaohongshu.com", "xhslink.com", "xhslink.cn"), "xiaohongshu"),
    (("kuaishou.com", "chenzhongtech.com"), "kuaishou"),
    (("weibo.com", "weibo.cn", "t.cn"), "weibo"),
    (("youtube.com", "youtu.be"), "youtube"),
    (("tiktok.com",), "tiktok"),
    (("weixin.qq.com",), "wechat_channels"),
)

# 短链域名：需要走一次网络跳转才知道最终地址（B 站短链还直接拿不到 BV 号）
_SHORT_HOSTS = frozenset(
    {"b23.tv", "v.douyin.com", "xhslink.com", "xhslink.cn", "v.kuaishou.com", "t.cn", "youtu.be"}
)

# 追踪参数：一律删；注意 xsec_token(小红书) 和 p(B 站分 P) 必须保留
_TRACKING_EXACT = frozenset(
    {
        "spm_id_from",
        "vd_source",
        "from_source",
        "from_spmid",
        "share_source",
        "share_medium",
        "share_plat",
        "share_session_id",
        "share_tag",
        "share_token",
        "unique_k",
        "timestamp",
        "msource",
        "wxfid",
        "tt_from",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
_TRACKING_PREFIXES = ("utm_",)

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_DOUYIN_ID_RE = re.compile(r"/(?:video|note)/(\d+)")
_XHS_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)")
_WEIBO_ID_RE = re.compile(r"/(?:tv/show|status)/([0-9a-zA-Z]+)")


def extract_urls(text: str) -> list[str]:
    """从整段分享文案里抠出所有 http(s) 链接（按出现顺序、去重）。"""
    seen: dict[str, None] = {}
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?)]}>'\"")
        seen.setdefault(url, None)
    return list(seen)


def _host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host


def detect_platform(url: str) -> str:
    host = _host_of(url)
    if not host:
        return "unknown"
    for suffixes, platform in _HOST_PLATFORM:
        for suffix in suffixes:
            if host == suffix or host.endswith("." + suffix):
                return platform
    return "unknown"


def is_short_url(url: str) -> bool:
    return _host_of(url) in _SHORT_HOSTS


def _strip_tracking(query: str) -> str:
    if not query:
        return ""
    kept: list[tuple[str, str]] = []
    for key, values in parse_qs(query, keep_blank_values=True).items():
        if key in _TRACKING_EXACT or any(key.startswith(p) for p in _TRACKING_PREFIXES):
            continue
        for value in values:
            kept.append((key, value))
    return urlencode(kept)


def extract_bv(value: str) -> str | None:
    match = _BV_RE.search(value or "")
    return match.group(1) if match else None


def canonicalize_url(url: str, platform: str | None = None) -> str:
    """把链接收敛成该平台的"稳定形态"，并去掉追踪参数。

    - B 站：``https://www.bilibili.com/video/<BV>[?p=N]``（短链原样返回，需 resolve）
    - 抖音：``https://www.douyin.com/{video,note}/<id>``
    - 小红书：``https://www.xiaohongshu.com/explore/<id>``（保留 ``xsec_token``）
    - 其余平台：只删追踪参数
    """
    platform = platform or detect_platform(url)
    parsed = urlparse(url)

    if platform == "bilibili":
        bv = extract_bv(url)
        page = parse_qs(parsed.query).get("p", [None])[0]
        if bv:
            query = f"?p={page}" if page and page.isdigit() and int(page) >= 1 else ""
            return f"https://www.bilibili.com/video/{bv}{query}"
        return url

    if platform == "douyin":
        match = _DOUYIN_ID_RE.search(parsed.path)
        if match:
            kind = "note" if "/note/" in parsed.path else "video"
            return f"https://www.douyin.com/{kind}/{match.group(1)}"
        return url

    if platform == "xiaohongshu":
        match = _XHS_ID_RE.search(parsed.path)
        query = _strip_tracking(parsed.query)  # xsec_token 不在黑名单 ⇒ 保留
        if match:
            base = f"https://www.xiaohongshu.com/explore/{match.group(1)}"
            return f"{base}?{query}" if query else base
        return urlunparse(parsed._replace(query=query))

    if platform == "weibo":
        match = _WEIBO_ID_RE.search(parsed.path)
        query = _strip_tracking(parsed.query)
        if match:
            base = f"https://weibo.com/{match.group(1)}"
            return f"{base}?{query}" if query else base
        return urlunparse(parsed._replace(query=query))

    return urlunparse(parsed._replace(query=_strip_tracking(parsed.query)))


def parse_share_text(text: str) -> ShareLink:
    """解析一段分享文案；找不到链接就抛 :class:`ShareLinkError`。

    平台识别不出来**不抛错**（``platform="unknown"``），是否拒绝由调用方决定。
    """
    urls = extract_urls(text)
    if not urls:
        raise ShareLinkError("分享文案里没有找到 http(s) 链接")
    url = urls[0]
    platform = detect_platform(url)
    return ShareLink(
        raw=text,
        url=canonicalize_url(url, platform),
        platform=platform,
        is_short=is_short_url(url),
    )


# 手机端 UA：App 分享出来的短链（尤其小红书）在桌面 UA 下会被导向登录页
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


def default_opener(url: str, timeout: float) -> str:
    import urllib.request

    host = _host_of(url)
    ua = _MOBILE_UA if host in _SHORT_HOSTS or "xiaohongshu" in host else _DESKTOP_UA
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": ua})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.geturl()


def resolve_short_url(
    url: str,
    *,
    opener: Callable[[str, float], str] = default_opener,
    timeout: float = 10.0,
    max_hops: int = 5,
) -> str:
    """跟随短链跳转，返回最终 URL。**唯一会发网络请求的函数**（opener 可注入 ⇒ 可单测）。"""
    current = url
    for _ in range(max_hops):
        if not is_short_url(current):
            return current
        try:
            nxt = opener(current, timeout)
        except Exception:  # 网络失败不应把整条链路炸掉
            return current
        if not nxt or nxt == current:
            return current
        # 落到登录页 = 这条短链在当前身份下拿不到内容；**别把登录页当成解析结果**
        # （否则下游引擎会拿一个 /login?redirectPath=… 去下载，报 Unsupported URL）
        if _is_login_wall(nxt):
            return current
        current = nxt
    return current


def _is_login_wall(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return "/login" in path or path.endswith("/login")


def iter_detected_platforms(texts: Iterable[str]) -> list[str]:
    """给批量输入用：返回每段文本识别出的平台（识别不出记 ``unknown``）。"""
    result: list[str] = []
    for text in texts:
        try:
            result.append(parse_share_text(text).platform)
        except ShareLinkError:
            result.append("unknown")
    return result
