"""从本机浏览器导出 cookies（Netscape 格式），喂给 yt-dlp / lux。

**为什么走浏览器登录态**：实测（2026-09-22）抖音网页版对未登录访问**直接弹登录墙**
（截图 `tools/douyin-shot.png`），页面自己的 XHR 也返回 200 空体 ⇒ 没有登录态就没有数据。
"解析站"因此都靠**账号池**。个人最干净的做法是：**在你自己电脑上登录一次，复用你自己的会话**。

Firefox / LibreWolf 的 cookies **不加密**（明文 sqlite），所以直读即可；Chrome 系要过系统钥匙串，本模块不做。
"""

from __future__ import annotations

import configparser
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CookieRecord",
    "find_firefox_profiles",
    "load_firefox_cookies",
    "to_netscape",
    "DEFAULT_COOKIE_DOMAINS",
    "normalize_expiry",
]

# 默认只导这几个平台的 cookie，不要把整站登录态一股脑倒出来
DEFAULT_COOKIE_DOMAINS = (
    "douyin.com",
    "xiaohongshu.com",
    "kuaishou.com",
    "weibo.com",
    "bilibili.com",
)

_ROOT_CANDIDATES = (
    ("librewolf", "~/.librewolf"),
    ("firefox", "~/.mozilla/firefox"),
)


@dataclass(frozen=True, slots=True)
class CookieRecord:
    host: str
    name: str
    value: str
    path: str
    expiry: int
    secure: bool

    def to_netscape(self) -> str:
        domain = self.host if self.host.startswith(".") else f".{self.host}"
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if self.secure else "FALSE"
        return "\t".join(
            [domain, flag, self.path or "/", secure, str(normalize_expiry(self.expiry)), self.name, self.value]
        )


def find_firefox_profiles() -> list[tuple[str, Path]]:
    """列出 (browser, profile_dir)；顺带认 `.librewolf/migrated` 这种非标准目录。"""
    found: list[tuple[str, Path]] = []
    for browser, raw_root in _ROOT_CANDIDATES:
        root = Path(raw_root).expanduser()
        if not root.is_dir():
            continue
        ini = root / "profiles.ini"
        if ini.is_file():
            parser = configparser.ConfigParser()
            try:
                parser.read(ini, encoding="utf-8")
            except configparser.Error:
                parser = configparser.ConfigParser()
            for section in parser.sections():
                if not section.startswith("Profile"):
                    continue
                raw_path = parser.get(section, "Path", fallback="").strip()
                if not raw_path:
                    continue
                candidate = (root / raw_path) if parser.getboolean(section, "IsRelative", fallback=True) else Path(raw_path)
                if (candidate / "cookies.sqlite").is_file():
                    found.append((browser, candidate))
        # 非标准/迁移目录兜底
        for cookies in root.glob("*/cookies.sqlite"):
            profile = cookies.parent
            if all(profile != existing for _, existing in found):
                found.append((browser, profile))
    return found


def load_firefox_cookies(
    profile_dir: Path, *, domains: tuple[str, ...] = DEFAULT_COOKIE_DOMAINS
) -> list[CookieRecord]:
    """读某个 profile 的 cookies；`domains` 为空则全导。"""
    db = Path(profile_dir) / "cookies.sqlite"
    if not db.is_file():
        raise FileNotFoundError(f"找不到 cookies.sqlite：{db}")
    where, params = "", []
    if domains:
        clauses = " OR ".join("host LIKE ?" for _ in domains)
        where = f" WHERE ({clauses})"
        params = [f"%{domain}%" for domain in domains]
    query = (
        "SELECT host, name, value, path, expiry, isSecure FROM moz_cookies"
        + where
        + " ORDER BY host, name"
    )
    # ⚠️ 浏览器在跑时新 cookie 还在 WAL 里：把 db(+wal/shm) 拷出来再读，
    # 否则「刚登录」的 cookie 会漏掉（immutable=1 会直接无视 WAL）。
    with tempfile.TemporaryDirectory(prefix="yuanliu-ck-") as tmp:
        snapshot = Path(tmp) / "cookies.sqlite"
        shutil.copy2(db, snapshot)
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(snapshot) + suffix))
        con = sqlite3.connect(snapshot)
        try:
            rows = con.execute(query, params).fetchall()
        finally:
            con.close()
    return [
        CookieRecord(host=h, name=n, value=v, path=p or "/", expiry=int(e or 0), secure=bool(s))
        for h, n, v, p, e, s in rows
    ]


def normalize_expiry(expiry: int | None) -> int:
    """Netscape 的过期时间是**秒**。

    ⚠️ 实测坑（2026-09-22）：新版 Firefox/LibreWolf 的 `moz_cookies.expiry` 存的是**毫秒**，
    直接写出去会变成 13 位数字 ⇒ Playwright `addCookies` 一律报
    "Cookie should have a valid expires" ⇒ **所有 cookie 注入失败**（表现为"明明登录了还是匿名"）。
    """
    value = int(expiry or 0)
    if value > 10**11:  # 毫秒（约 1973 年以后就不可能这么大）
        value //= 1000
    return value


def to_netscape(cookies: list[CookieRecord]) -> str:
    header = [
        "# Netscape HTTP Cookie File",
        "# 由 yuanliu 从本机浏览器导出；含登录态，权限务必 600",
        "",
    ]
    return "\n".join(header + [cookie.to_netscape() for cookie in cookies]) + "\n"
