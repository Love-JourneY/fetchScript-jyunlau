from pathlib import Path

import pytest

from yuanliu.engines import (
    EngineFailed,
    EngineUnavailable,
    LuxEngine,
    YtDlpEngine,
    engines_for_platform,
    pick_engines,
    resolve_binary,
)


def _fake_binary(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolve_binary_prefers_env(monkeypatch, tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, "yt-dlp", "exit 0\n")
    monkeypatch.setenv("YUANLIU_YTDLP", str(fake))
    assert resolve_binary("yt-dlp", "YUANLIU_YTDLP", ()) == str(fake)


def test_resolve_binary_env_missing_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("YUANLIU_YTDLP", "/nope/not-here")
    assert resolve_binary("yt-dlp", "YUANLIU_YTDLP", ()) is None


def test_ytdlp_probe_parses_json(monkeypatch, tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, "yt-dlp", 'echo \'{"id":"BV1","title":"t"}\'\n')
    monkeypatch.setenv("YUANLIU_YTDLP", str(fake))
    engine = YtDlpEngine()
    assert engine.probe("https://example.com/x")["id"] == "BV1"


def test_ytdlp_probe_raises_engine_failed(monkeypatch, tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, "yt-dlp", "echo boom >&2; exit 1\n")
    monkeypatch.setenv("YUANLIU_YTDLP", str(fake))
    with pytest.raises(EngineFailed):
        YtDlpEngine().probe("https://example.com/x")


def test_ytdlp_unavailable_raises(monkeypatch) -> None:
    monkeypatch.setenv("YUANLIU_YTDLP", "/nope")
    with pytest.raises(EngineUnavailable):
        YtDlpEngine().probe("https://example.com/x")


def test_lux_probe_wraps_stdout(monkeypatch, tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, "lux", "echo 'Title: demo'\n")
    monkeypatch.setenv("YUANLIU_LUX", str(fake))
    data = LuxEngine().probe("https://example.com/x")
    assert data["engine"] == "lux"
    assert "demo" in data["raw"]


def test_ytdlp_download_returns_new_files(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "out"
    fake = _fake_binary(tmp_path, "yt-dlp", f'mkdir -p "{out}" && touch "{out}/v.mp4"\n')
    monkeypatch.setenv("YUANLIU_YTDLP", str(fake))
    files = YtDlpEngine().download("https://example.com/x", out)
    assert [p.name for p in files] == ["v.mp4"]


def test_engines_for_platform_routing() -> None:
    # 快手/抖音/小红书：实测 yt-dlp/lux 都过不了风控 ⇒ 浏览器嗅探排第一
    assert [e.name for e in engines_for_platform("kuaishou")] == ["browser", "lux"]
    assert [e.name for e in engines_for_platform("douyin")] == ["browser", "lux", "yt-dlp"]
    assert [e.name for e in engines_for_platform("youtube")] == ["yt-dlp", "lux"]


def test_pick_engines_filters_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("YUANLIU_LUX", "/nope")
    monkeypatch.setenv("YUANLIU_YTDLP", "/nope")
    monkeypatch.setenv("YUANLIU_NODE", "/nope")
    monkeypatch.setenv("YUANLIU_SNIFF", "/nope")
    assert pick_engines("bilibili") == ()
