from pathlib import Path

import pytest

from fetchscript.engines import (
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
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    assert resolve_binary("yt-dlp", "FETCHSCRIPT_YTDLP", ()) == str(fake)


def test_resolve_binary_env_missing_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", "/nope/not-here")
    assert resolve_binary("yt-dlp", "FETCHSCRIPT_YTDLP", ()) is None


def test_ytdlp_probe_parses_json(monkeypatch, tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, "yt-dlp", 'echo \'{"id":"BV1","title":"t"}\'\n')
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    engine = YtDlpEngine()
    assert engine.probe("https://example.com/x")["id"] == "BV1"


def test_ytdlp_probe_raises_engine_failed(monkeypatch, tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, "yt-dlp", "echo boom >&2; exit 1\n")
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    with pytest.raises(EngineFailed):
        YtDlpEngine().probe("https://example.com/x")


def test_ytdlp_unavailable_raises(monkeypatch) -> None:
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", "/nope")
    with pytest.raises(EngineUnavailable):
        YtDlpEngine().probe("https://example.com/x")


def test_lux_probe_wraps_stdout(monkeypatch, tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, "lux", "echo 'Title: demo'\n")
    monkeypatch.setenv("FETCHSCRIPT_LUX", str(fake))
    data = LuxEngine().probe("https://example.com/x")
    assert data["engine"] == "lux"
    assert "demo" in data["raw"]


def test_ytdlp_download_returns_new_files(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "out"
    fake = _fake_binary(tmp_path, "yt-dlp", f'mkdir -p "{out}" && touch "{out}/v.mp4"\n')
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    files = YtDlpEngine().download("https://example.com/x", out)
    assert [p.name for p in files] == ["v.mp4"]


def test_engines_for_platform_routing() -> None:
    # 快手/抖音/小红书：实测 yt-dlp/lux 都过不了风控 ⇒ 浏览器嗅探排第一
    assert [e.name for e in engines_for_platform("kuaishou")] == ["browser", "lux"]
    assert [e.name for e in engines_for_platform("douyin")] == ["browser", "lux", "yt-dlp"]
    assert [e.name for e in engines_for_platform("youtube")] == ["yt-dlp", "lux"]


def test_pick_engines_filters_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("FETCHSCRIPT_LUX", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_NODE", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_SNIFF", "/nope")
    assert pick_engines("bilibili") == ()


def test_stream_kills_stalled_child(monkeypatch) -> None:
    """子进程没有任何输出时必须被看门狗杀掉（实测：lux 多 P 交互提问会永久卡住）。"""
    import time as _time

    from fetchscript import engines as engines_mod
    from fetchscript.engines import EngineFailed

    monkeypatch.setenv("FETCHSCRIPT_STALL_LIMIT", "1")
    with pytest.raises(EngineFailed) as excinfo:
        engines_mod._stream(["/bin/sh", "-c", "sleep 30"], timeout=30)
    assert "卡住" in str(excinfo.value)


def test_stream_passes_stdin_devnull(monkeypatch, tmp_path) -> None:
    """stdin 必须是 /dev/null —— 否则交互式提问会把下载挂死。"""
    from fetchscript import engines as engines_mod

    result = engines_mod._stream(["/bin/sh", "-c", "read x || echo EOF"], timeout=10)
    assert "EOF" in result.stdout


def test_reported_filepaths_extracts_absolute_paths() -> None:
    from fetchscript.engines import _reported_filepaths

    out = " 42.0%\n/var/lib/fetchscript/media/我的视频.mp4\n100.0%\n"
    paths = _reported_filepaths(out)
    assert [p.name for p in paths] == ["我的视频.mp4"]


def test_ytdlp_returns_existing_file_when_skipped(monkeypatch, tmp_path) -> None:
    """重复下载同一视频时 yt-dlp 会跳过 ⇒ 必须认领已存在的文件，而不是当失败。"""
    out = tmp_path / "out"
    out.mkdir()
    existing = out / "老视频.mp4"
    existing.write_bytes(b"x")
    fake = tmp_path / "yt-dlp"
    fake.write_text(f'#!/usr/bin/env bash\necho "{existing}"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    files = YtDlpEngine().download("https://example.com/x", out)
    assert files == [existing]
