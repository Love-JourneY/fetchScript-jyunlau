from pathlib import Path

import pytest

from fetchscript.resolve import (
    NoEngineAvailable,
    UnsupportedPlatform,
    describe_routes,
    download,
    plan,
    probe,
)

DOUYIN_SHARE = "7.94 复制打开抖音，看看【某某】的作品 https://v.douyin.com/iRNBho6G/ 复制此链接"


def test_plan_detects_platform_and_route() -> None:
    current = plan("看这个 https://www.bilibili.com/video/BV18puh6wEmt")
    assert current.platforms == "bilibili"
    assert current.engines == ("yt-dlp", "lux", "browser")
    assert current.link.url == "https://www.bilibili.com/video/BV18puh6wEmt"


def test_plan_rejects_wechat_channels() -> None:
    with pytest.raises(UnsupportedPlatform):
        plan("https://mp.weixin.qq.com/s/abc")


def test_plan_raises_without_url() -> None:
    with pytest.raises(UnsupportedPlatform):
        plan("没有链接的一段话")


def _no_engines(monkeypatch) -> None:
    monkeypatch.setenv("FETCHSCRIPT_LUX", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_NODE", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_SNIFF", "/nope")


def test_plan_marks_unusable_when_no_engine(monkeypatch) -> None:
    _no_engines(monkeypatch)
    current = plan("https://v.kuaishou.com/xyz")
    assert current.platforms == "kuaishou"
    assert current.available == ()
    assert current.usable is False


def test_probe_raises_no_engine(monkeypatch) -> None:
    _no_engines(monkeypatch)
    with pytest.raises(NoEngineAvailable):
        probe("https://v.kuaishou.com/xyz")


def test_download_uses_first_working_engine(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "out"
    fake = tmp_path / "lux"
    fake.write_text(f'#!/usr/bin/env bash\nmkdir -p "{out}" && touch "{out}/a.mp4"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FETCHSCRIPT_LUX", str(fake))
    monkeypatch.setenv("FETCHSCRIPT_NODE", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_SNIFF", "/nope")
    # 用长链（非短链）避免测试里发真实网络请求
    outcome = download("https://www.kuaishou.com/short-video/3xxv9th5tdgd88u", out)
    assert outcome.engine == "lux"
    assert [p.name for p in outcome.files] == ["a.mp4"]
    assert outcome.as_dict()["platform"] == "kuaishou"


def test_download_respects_engine_filter(monkeypatch, tmp_path: Path) -> None:
    fake = tmp_path / "lux"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FETCHSCRIPT_LUX", str(fake))
    with pytest.raises(NoEngineAvailable):
        download("https://v.kuaishou.com/xyz", tmp_path / "out", engine_name="yt-dlp")


def test_describe_routes_lists_all_platforms() -> None:
    platforms = {row["platform"] for row in describe_routes()}
    assert {"bilibili", "douyin", "kuaishou", "xiaohongshu", "weibo"} <= platforms


def test_cooldown_gate_blocks_platform(monkeypatch, tmp_path) -> None:
    """冷却期内的平台必须直接拒绝，防止越打越死。"""
    import json as _json

    from fetchscript import resolve as resolve_mod

    cooldown = tmp_path / "cooldown.json"
    cooldown.write_text(_json.dumps({"xiaohongshu": {"until": "2999-01-01T00:00:00", "reason": "测试"}}), encoding="utf-8")
    monkeypatch.setattr(resolve_mod, "COOLDOWN_FILE", cooldown)
    with pytest.raises(UnsupportedPlatform) as excinfo:
        plan("https://www.xiaohongshu.com/explore/abc123")
    assert "冷却期" in str(excinfo.value)


def test_cooldown_expired_allows(monkeypatch, tmp_path) -> None:
    import json as _json

    from fetchscript import resolve as resolve_mod

    cooldown = tmp_path / "cooldown.json"
    cooldown.write_text(_json.dumps({"xiaohongshu": {"until": "2000-01-01T00:00:00"}}), encoding="utf-8")
    monkeypatch.setattr(resolve_mod, "COOLDOWN_FILE", cooldown)
    assert plan("https://www.xiaohongshu.com/explore/abc123").platforms == "xiaohongshu"


def test_missing_cookies_path_is_ignored(monkeypatch, tmp_path) -> None:
    """坏 cookies 路径不能把引擎全搞挂（实测事故：/root/.cache/... 不存在）。"""
    from fetchscript.resolve import _existing_cookies

    assert _existing_cookies(None) is None
    assert _existing_cookies(tmp_path / "nope.txt") is None
    real = tmp_path / "cookies.txt"
    real.write_text("# Netscape\n", encoding="utf-8")
    assert _existing_cookies(real) == real


def test_text_only_deletes_media_after_success(monkeypatch, tmp_path) -> None:
    """只要文字：转写成功后视频必须被删掉（文字才是要留下的东西）。"""
    out = tmp_path / "out"
    fake = tmp_path / "yt-dlp"
    fake.write_text(f'#!/usr/bin/env bash\nmkdir -p "{out}" && touch "{out}/v.mp4"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    monkeypatch.setenv("FETCHSCRIPT_LUX", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_NODE", "/nope")

    def fake_transcribe(self, media, *, timeout=3600.0, copy_to=None):
        from fetchscript.transcribe import TranscriptResult

        copy_to.mkdir(parents=True, exist_ok=True)
        target = copy_to / f"{media.stem}.txt"
        target.write_text("文稿", encoding="utf-8")
        return TranscriptResult(transcript=target)

    monkeypatch.setattr("fetchscript.transcribe.Transcriber.transcribe", fake_transcribe)
    outcome = download(
        "https://www.bilibili.com/video/BV1xx411c7XD", out, transcribe=True, keep_media=False
    )
    assert outcome.files == []
    assert len(outcome.transcripts) == 1
    assert len(outcome.discarded) == 1
    assert not (out / "v.mp4").exists()


def test_text_only_keeps_media_when_transcribe_fails(monkeypatch, tmp_path) -> None:
    """转写失败时必须留着媒体，否则人就白下了。"""
    out = tmp_path / "out"
    fake = tmp_path / "yt-dlp"
    fake.write_text(f'#!/usr/bin/env bash\nmkdir -p "{out}" && touch "{out}/v.mp4"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    monkeypatch.setenv("FETCHSCRIPT_LUX", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_NODE", "/nope")

    def boom(self, media, *, timeout=3600.0, copy_to=None):
        raise RuntimeError("no model")

    monkeypatch.setattr("fetchscript.transcribe.Transcriber.transcribe", boom)
    outcome = download(
        "https://www.bilibili.com/video/BV1xx411c7XD", out, transcribe=True, keep_media=False
    )
    assert outcome.transcribe_error
    assert (out / "v.mp4").exists()
    assert outcome.discarded == []


def test_audio_only_passes_flag_to_engine(monkeypatch, tmp_path) -> None:
    """只要文字时不该下整片视频：audio_only 要传到引擎。"""
    out = tmp_path / "out"
    seen = {}
    fake = tmp_path / "yt-dlp"
    fake.write_text(f'#!/usr/bin/env bash\necho "$@" > "{tmp_path}/args.txt"\nmkdir -p "{out}" && touch "{out}/a.m4a"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FETCHSCRIPT_YTDLP", str(fake))
    monkeypatch.setenv("FETCHSCRIPT_LUX", "/nope")
    monkeypatch.setenv("FETCHSCRIPT_NODE", "/nope")
    download("https://www.bilibili.com/video/BV1xx411c7XD", out, audio_only=True)
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert "ba/b" in args
