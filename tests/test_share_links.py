"""share_links 在 yuanliu 侧的最小回归集（b2t 侧有同名完整测试）。"""

from yuanliu.share_links import (
    detect_platform,
    is_short_url,
    parse_share_text,
    resolve_short_url,
)

LOGIN_WALL = "https://www.xiaohongshu.com/login?redirectPath=http%3A%2F%2Fwww.xiaohongshu.com%2Fnote"
NOTE_URL = "https://www.xiaohongshu.com/discovery/item/6aa29e9d000000001001c312?xsec_token=X"


def test_resolve_short_url_ignores_login_wall_redirect() -> None:
    """短链落到登录页时，必须保留原链，而不是把 /login?redirectPath=… 当解析结果。"""
    assert resolve_short_url("https://xhslink.cn/o/abc", opener=lambda u, t: LOGIN_WALL) == (
        "https://xhslink.cn/o/abc"
    )


def test_resolve_short_url_follows_normal_redirect() -> None:
    assert resolve_short_url("https://xhslink.cn/o/abc", opener=lambda u, t: NOTE_URL) == NOTE_URL


def test_xhslink_cn_is_xiaohongshu_short_link() -> None:
    assert detect_platform("https://xhslink.cn/o/abc") == "xiaohongshu"
    assert is_short_url("https://xhslink.cn/o/abc") is True


def test_share_text_with_traditional_chinese() -> None:
    link = parse_share_text("原來一切早已雙向奔赴 https://xhslink.cn/o/2rSCCq0xDCR 複製後開啟小紅書查看筆記")
    assert link.platform == "xiaohongshu"
    assert link.is_short is True
