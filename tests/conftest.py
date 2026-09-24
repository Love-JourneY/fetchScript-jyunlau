import pytest

from fetchscript import browser as browser_mod


@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    """测试里不要真的 sleep：把共享限流器换成零间隔。"""
    monkeypatch.setattr(browser_mod, "_SHARED_LIMITER", browser_mod.RateLimiter(min_interval=0))
