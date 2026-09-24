"""文件名规范：**下载下来的文件必须叫视频标题**（Nija 2026-09-22 要求）。

规则（确定性、可测）：
- 以解析出的标题为主（没有标题才退到 id）；
- 去掉 Windows/Unix 都不允许的字符，压缩连续空白，去掉首尾空格与点；
- 保留中文与全角标点（可读性优先），但把 `/` 换成 `／` 以免当路径分隔符；
- 长度截断到 80 字符（含扩展名余量）。
"""

from __future__ import annotations

import re

__all__ = ["safe_title", "normalize_existing_name"]

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_MAX_STEM = 80


def safe_title(value: str, *, fallback: str = "media") -> str:
    text = (value or "").strip()
    text = _ILLEGAL.sub("／", text)
    text = _WHITESPACE.sub(" ", text)
    text = text.strip(" .。·-—_\t")
    if not text:
        text = fallback
    return text[:_MAX_STEM].strip() or fallback


def normalize_existing_name(name: str, *, fallback: str = "media") -> str:
    """把引擎自己起的名字也过一遍同样的规则（保留扩展名）。"""
    from pathlib import Path

    path = Path(name)
    stem = safe_title(path.stem, fallback=fallback)
    return f"{stem}{path.suffix}" if path.suffix else stem
