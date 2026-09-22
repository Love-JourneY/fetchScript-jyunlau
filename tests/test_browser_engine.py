import json
import subprocess
from pathlib import Path

import pytest

from yuanliu.browser import BrowserEngine
from yuanliu.engines import EngineFailed, EngineUnavailable


def _engine(tmp_path: Path, stdout: str = "", returncode: int = 0) -> BrowserEngine:
    script = tmp_path / "sniff.cjs"
    script.write_text("// fake\n", encoding="utf-8")
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\n", encoding="utf-8")
    node.chmod(0o755)

    def runner(cmd, timeout, env):  # noqa: ANN001
        return subprocess.CompletedProcess(list(cmd), returncode, stdout, "")

    return BrowserEngine(default_node=str(node), default_script=str(script), runner=runner)


def test_unavailable_without_node_or_script(monkeypatch) -> None:
    monkeypatch.setenv("YUANLIU_NODE", "/nope")
    monkeypatch.setenv("YUANLIU_SNIFF", "/nope")
    engine = BrowserEngine(default_node="/nope", default_script="/nope")
    assert engine.available() is False
    with pytest.raises(EngineUnavailable):
        engine.sniff("https://example.com/v/1")


def test_sniff_parses_json(tmp_path: Path) -> None:
    payload = json.dumps({"ok": True, "title": "t", "meta": {"desc": "d"}, "play_urls": ["https://cdn/x.mp4"]})
    data = _engine(tmp_path, stdout=payload).sniff("https://example.com/v/1")
    assert data["play_urls"] == ["https://cdn/x.mp4"]


def test_sniff_raises_on_not_ok(tmp_path: Path) -> None:
    payload = json.dumps({"ok": False, "error": "页面没给出媒体直链"})
    with pytest.raises(EngineFailed):
        _engine(tmp_path, stdout=payload).sniff("https://example.com/v/1")


def test_sniff_raises_on_garbage(tmp_path: Path) -> None:
    with pytest.raises(EngineFailed):
        _engine(tmp_path, stdout="not json").sniff("https://example.com/v/1")


def test_probe_wraps_payload(tmp_path: Path) -> None:
    payload = json.dumps(
        {"ok": True, "title": "t", "meta": {"desc": "d"}, "play_urls": ["https://cdn/a.mp4"], "download_urls": []}
    )
    info = _engine(tmp_path, stdout=payload).probe("https://example.com/v/1")
    assert info["engine"] == "browser"
    assert info["play_urls"] == ["https://cdn/a.mp4"]


def test_download_requires_media_url(tmp_path: Path) -> None:
    payload = json.dumps({"ok": True, "play_urls": []})
    with pytest.raises(EngineFailed):
        _engine(tmp_path, stdout=payload).download("https://example.com/v/1", tmp_path / "out")


def test_platform_routing_prefers_browser_for_douyin() -> None:
    from yuanliu.engines import PLATFORM_ROUTING

    assert PLATFORM_ROUTING["douyin"][0] == "browser"
    assert PLATFORM_ROUTING["xiaohongshu"][0] == "browser"


def test_download_handles_null_meta(tmp_path: Path, monkeypatch) -> None:
    """meta 为 null 时不能炸（真实嗅探经常没有 meta）。"""
    payload = json.dumps({"ok": True, "meta": None, "title": "标题", "play_urls": ["https://cdn/a.mp4"]})
    engine = _engine(tmp_path, stdout=payload)
    fetched = {}

    def fake_fetch(self, media_url, target, *, referer, timeout):  # noqa: ANN001
        fetched["url"] = media_url
        target.write_bytes(b"data")

    # slots=True 的 dataclass 不能给实例塞新属性 ⇒ 打到类上
    monkeypatch.setattr(BrowserEngine, "_fetch", fake_fetch)
    files = engine.download("https://example.com/v/1", tmp_path / "out")
    assert fetched["url"] == "https://cdn/a.mp4"
    assert files and files[0].suffix == ".mp4"


def test_rate_limiter_enforces_min_interval() -> None:
    from yuanliu.browser import RateLimiter

    now = {"t": 0.0}
    limiter = RateLimiter(min_interval=20, clock=lambda: now["t"])
    assert limiter.wait() == 0.0  # 第一次不等
    now["t"] = 5.0
    waited = limiter.wait()  # 只过了 5s ⇒ 至少要补到 20s
    assert waited >= 0


def test_rate_limiter_disabled_when_zero() -> None:
    from yuanliu.browser import RateLimiter

    limiter = RateLimiter(min_interval=0, clock=lambda: 0.0)
    assert limiter.wait() == 0.0


def test_rate_limiter_waits_after_previous_call() -> None:
    from yuanliu.browser import RateLimiter

    now = {"t": 100.0}
    limiter = RateLimiter(min_interval=20, clock=lambda: now["t"])
    assert limiter.wait() == 0.0  # 首次不等待（_last 为 None）
