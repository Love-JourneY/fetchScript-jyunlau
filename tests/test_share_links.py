"""share_links 在 fetchscript 侧的最小回归集（b2t 侧有同名完整测试）。"""

from fetchscript.share_links import (
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


def test_parse_input_accepts_bare_bv() -> None:
    """Nija 实测诉求：直接贴裸 BV 号也要认。"""
    from fetchscript.share_links import parse_input

    link = parse_input("BV198tR6EEit")
    assert link.platform == "bilibili"
    assert link.url == "https://www.bilibili.com/video/BV198tR6EEit"


def test_parse_input_accepts_scheme_less_host() -> None:
    from fetchscript.share_links import parse_input

    link = parse_input("www.bilibili.com/video/BV198tR6EEit?p=2")
    assert link.platform == "bilibili"
    assert link.url == "https://www.bilibili.com/video/BV198tR6EEit?p=2"


def test_parse_input_still_rejects_plain_text() -> None:
    import pytest

    from fetchscript.share_links import ShareLinkError, parse_input

    with pytest.raises(ShareLinkError):
        parse_input("今天天气不错")


def test_parse_input_prefers_share_text_url() -> None:
    from fetchscript.share_links import parse_input

    link = parse_input("看看这个 https://v.douyin.com/iRNBho6G/ 复制此链接")
    assert link.platform == "douyin"


def test_resolve_short_url_total_timeout_returns_original() -> None:
    """慢速响应不能把整条流水线卡死：总时限一到就放弃，交原短链给引擎。"""
    import time as _time

    def sleepy_opener(url: str, timeout: float) -> str:
        _time.sleep(5)
        return "https://www.bilibili.com/video/BV1xx411c7XD"

    started = _time.monotonic()
    result = resolve_short_url("https://b23.tv/abc", opener=sleepy_opener, total_timeout=0.3)
    assert result == "https://b23.tv/abc"
    assert _time.monotonic() - started < 2


def test_resolve_short_url_without_total_timeout_still_works() -> None:
    target = "https://www.bilibili.com/video/BV1xx411c7XD"
    assert resolve_short_url("https://b23.tv/abc", opener=lambda u, t: target, total_timeout=None) == target
