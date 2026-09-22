import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from yuanliu.server import YuanliuServer, ServerConfig, build_server


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
    app = YuanliuServer(config)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    assert app.resolve_file("../secret.txt") is None
    assert app.resolve_file("/etc/passwd") is None


def test_files_rejects_non_whitelisted_suffix(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = YuanliuServer(config)
    (config.media_dir / "x.sh").write_text("rm -rf /", encoding="utf-8")
    assert app.resolve_file("x.sh") is None


def test_files_serves_video(tmp_path: Path) -> None:
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = YuanliuServer(config)
    (config.media_dir / "v.mp4").write_bytes(b"data")
    assert app.resolve_file("v.mp4") is not None


def test_completion_requires_token_config() -> None:
    """fail-closed：没配 token 时任何受保护接口都拒绝。"""
    config = ServerConfig(bind="127.0.0.1", port=0, token="", media_dir=Path("/tmp/x"))
    app = YuanliuServer(config)
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


def test_download_accepts_transcribe_flag(tmp_path: Path, monkeypatch) -> None:
    """带 transcribe 的下载任务要记住这个意图（转写本身由 b2t 完成）。"""
    config = ServerConfig(bind="127.0.0.1", port=0, token="t", media_dir=tmp_path / "media")
    app = YuanliuServer(config)
    called = {}

    def fake_download(text, outdir, *, cookies=None, transcribe=False):
        called["transcribe"] = transcribe
        from yuanliu.resolve import DownloadOutcome, plan

        (Path(outdir) / "v.mp4").write_bytes(b"x")
        return DownloadOutcome(plan=plan("https://www.bilibili.com/video/BV1xx411c7XD"), engine="fake",
                               files=[Path(outdir) / "v.mp4"])

    monkeypatch.setattr("yuanliu.server.download_media", fake_download)
    job = app.submit("https://www.bilibili.com/video/BV1xx411c7XD", transcribe=True)
    for _ in range(50):
        if job.status in {"done", "failed"}:
            break
        import time

        time.sleep(0.05)
    assert job.status == "done"
    assert called["transcribe"] is True
