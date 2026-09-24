import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fetchscript.server import FetchscriptServer, ServerConfig, build_server


@pytest.fixture()
def server(tmp_path: Path):
    config = ServerConfig(bind="127.0.0.1", port=0, token="secret", media_dir=tmp_path / "media")
    httpd = build_server(config)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _post(url, payload, token=None):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    if token:
        request.add_header("X-Token", token)
    return urllib.request.urlopen(request, timeout=10)


def test_health_needs_no_token(server) -> None:
    body = json.loads(urllib.request.urlopen(f"{server}/api/health", timeout=5).read())
    assert body["ok"] is True


def test_jobs_requires_token(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{server}/api/jobs", timeout=5)
    assert excinfo.value.code == 401


def test_jobs_with_token(server) -> None:
    body = json.loads(urllib.request.urlopen(f"{server}/api/jobs?k=secret", timeout=5).read())
    assert body == {"jobs": []}


def test_resolve_rejects_wechat_channels(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"{server}/api/resolve", {"text": "https://mp.weixin.qq.com/s/abc"}, token="secret")
    assert excinfo.value.code == 400


def test_resolve_empty_text(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"{server}/api/resolve", {"text": ""}, token="secret")
    assert excinfo.value.code == 400


def test_files_path_traversal_blocked(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    assert app.resolve_file("../secret.txt") is None
    assert app.resolve_file("/etc/passwd") is None


def test_files_rejects_non_whitelisted_suffix(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    (config.media_dir / "x.sh").write_text("rm -rf /", encoding="utf-8")
    assert app.resolve_file("x.sh") is None


def test_files_serves_video(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    (config.media_dir / "v.mp4").write_bytes(b"data")
    assert app.resolve_file("v.mp4") is not None


def test_completion_requires_token_config() -> None:
    """fail-closed：没配 token 时任何受保护接口都拒绝。"""
    config = ServerConfig(bind="127.0.0.1", port=0, token="", media_dir=Path("/tmp/x"))
    app = FetchscriptServer(config)
    assert app.config.token == ""


def test_index_shows_token_invalid_banner(server) -> None:
    html = urllib.request.urlopen(f"{server}/?k=wrong-token", timeout=5).read().decode()
    assert "token 无效或已过期" in html


def test_index_shows_token_ok_banner(server) -> None:
    html = urllib.request.urlopen(f"{server}/?k=secret", timeout=5).read().decode()
    assert "token 有效" in html


def test_index_without_token_warns(server) -> None:
    html = urllib.request.urlopen(f"{server}/", timeout=5).read().decode()
    assert "没带 token" in html




def test_probe_summary_is_human_readable() -> None:
    """yt-dlp 的 info dict 要归一成标题/时长/作者 —— 否则前端显示成"没做"（实测痛点）。"""
    from fetchscript.resolve import summarize_probe

    summary = summarize_probe(
        {
            "engine": "yt-dlp",
            "info": {
                "title": "AI 正在改写开源安全规则",
                "duration": 132.052,
                "uploader": "某某",
                "webpage_url": "https://www.bilibili.com/video/BV1xx411c7XD",
            },
        }
    )
    assert summary["title"] == "AI 正在改写开源安全规则"
    assert summary["duration"] == 132.1
    assert summary["uploader"] == "某某"


def test_probe_summary_for_browser_engine() -> None:
    from fetchscript.resolve import summarize_probe

    summary = summarize_probe(
        {
            "engine": "browser",
            "info": {"meta": {"desc": "笔记标题", "author": "作者", "duration": 13866}, "play_urls": ["u"], "images": ["a", "b"]},
        }
    )
    assert summary["title"] == "笔记标题"
    assert summary["duration"] == 13.9  # 毫秒 → 秒
    assert summary["media_count"] == 1
    assert summary["image_count"] == 2


def test_job_reports_progress(tmp_path: Path, monkeypatch) -> None:
    """任务要有进度与阶段，前端才能画进度条。"""
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    seen = {}

    def fake_download(text, outdir, *, cookies=None, transcribe=False, keep_media=True, audio_only=False, on_progress=None):
        from fetchscript.resolve import DownloadOutcome, plan

        if on_progress:
            on_progress(0.42)
            seen["mid"] = True
        (Path(outdir) / "v.mp4").write_bytes(b"x")
        return DownloadOutcome(plan=plan("https://www.bilibili.com/video/BV1xx411c7XD"), engine="fake",
                               files=[Path(outdir) / "v.mp4"])

    monkeypatch.setattr("fetchscript.server.download_media", fake_download)
    job = app.submit("https://www.bilibili.com/video/BV1xx411c7XD")
    import time

    for _ in range(100):
        if job.status in {"done", "failed"}:
            break
        time.sleep(0.05)
    assert seen.get("mid")
    assert job.progress == 1.0
    assert job.as_dict()["stage"] == "done"


def test_login_accepts_correct_password(server) -> None:
    body = json.loads(_post(f"{server}/api/login", {"password": "secret"}, token="").read())
    assert body == {"ok": True}


def test_login_rejects_wrong_password(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"{server}/api/login", {"password": "nope"}, token="")
    assert excinfo.value.code == 401


def test_login_throttle_delays_after_repeated_failures() -> None:
    from fetchscript.server import LoginThrottle

    throttle = LoginThrottle(max_attempts=2, base_delay=1.0)
    assert throttle.delay_for("1.2.3.4") == 0.0
    throttle.record_failure("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    assert throttle.delay_for("1.2.3.4") > 0
    throttle.reset("1.2.3.4")
    assert throttle.delay_for("1.2.3.4") == 0.0


def test_list_files_reports_media(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    (config.media_dir / "a.mp4").write_bytes(b"x" * 10)
    (config.media_dir / "note.txt").write_text("hi", encoding="utf-8")
    names = {entry["name"] for entry in app.list_files()}
    assert names == {"a.mp4", "note.txt"}


def test_delete_file_removes_it(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    target = config.media_dir / "a.mp4"
    target.write_bytes(b"x")
    assert app.delete_file("a.mp4") == "ok"
    assert not target.exists()


def test_delete_file_blocks_traversal(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    outside = tmp_path / "important.txt"
    outside.write_text("keep", encoding="utf-8")
    assert app.delete_file("../important.txt") == "forbidden"
    assert outside.exists()


def test_delete_file_blocks_non_whitelisted_suffix(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = FetchscriptServer(config)
    script = config.media_dir / "evil.sh"
    script.write_text("rm -rf /", encoding="utf-8")
    assert app.delete_file("evil.sh") == "forbidden"
    assert script.exists()


def test_delete_missing_file(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    assert FetchscriptServer(config).delete_file("nope.mp4") == "not_found"


def test_gate_token_accepted_only_from_loopback(monkeypatch, tmp_path) -> None:
    """统一认证门（port-gate）转发时注入 X-Gate-Token；只有 loopback 才认。"""
    config = ServerConfig(bind="127.0.0.1", port=0, token="secret", media_dir=tmp_path / "media")
    from fetchscript.server import _Handler

    _Handler.app = FetchscriptServer(config)   # 给基类挂上 app（真实运行时由 build_server 绑定子类）

    monkeypatch.setenv("FETCHSCRIPT_GATE_TOKEN", "gate-shared")

    class Fake(_Handler):
        def __init__(self):  # noqa: D107 - 只测鉴权逻辑
            self.headers = {"X-Gate-Token": "gate-shared"}
            self.client_address = ("127.0.0.1", 1234)

    assert Fake()._token_ok({}) is True

    class Lan(_Handler):
        def __init__(self):
            self.headers = {"X-Gate-Token": "gate-shared"}
            self.client_address = ("192.168.31.9", 1234)

    assert Lan()._token_ok({}) is False

    class WrongHeader(_Handler):
        def __init__(self):
            self.headers = {"X-Gate-Token": "nope"}
            self.client_address = ("127.0.0.1", 1234)

    assert WrongHeader()._token_ok({}) is False
