"""fetchscript —— 分享链接 → 本地媒体（多引擎路由）。

设计边界（本屋模块纪律）：
- **不内嵌平台解析逻辑**：抖音/快手/小红书/视频号这些平台的风控签名是活的，
  自己养不划算 ⇒ 交给外部引擎（yt-dlp / lux），本模块只做"归一化 + 路由 + 失败分型"。
- **不做网络请求之外的副作用**：产物落 `--out` 指定目录，由调用方（b2t / 人）决定去哪。
"""

from fetchscript.browser import BrowserEngine
from fetchscript.engines import (
    ENABLED_ENGINES,
    Engine,
    EngineFailed,
    EngineUnavailable,
    LuxEngine,
    YtDlpEngine,
    available_engines,
    pick_engines,
)
from fetchscript.resolve import (
    DownloadOutcome,
    NoEngineAvailable,
    Plan,
    ResolveError,
    UnsupportedPlatform,
    download,
    plan,
    probe,
)
from fetchscript.share_links import (
    ShareLink,
    ShareLinkError,
    canonicalize_url,
    detect_platform,
    extract_urls,
    parse_share_text,
    resolve_short_url,
)

__version__ = "0.1.0"

__all__ = [
    "DownloadOutcome",
    "ENABLED_ENGINES",
    "BrowserEngine",
    "Engine",
    "EngineFailed",
    "EngineUnavailable",
    "LuxEngine",
    "NoEngineAvailable",
    "Plan",
    "ResolveError",
    "ShareLink",
    "ShareLinkError",
    "UnsupportedPlatform",
    "YtDlpEngine",
    "available_engines",
    "canonicalize_url",
    "detect_platform",
    "download",
    "extract_urls",
    "parse_share_text",
    "pick_engines",
    "plan",
    "probe",
    "resolve_short_url",
    "__version__",
]
