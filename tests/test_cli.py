import json

from yuanliu.cli import EXIT_NO_ENGINE, EXIT_OK, EXIT_UNSUPPORTED, main


def test_cli_engines_prints_json(capsys) -> None:
    assert main(["engines"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "engines" in payload and "routes" in payload


def test_cli_plan_bilibili(capsys) -> None:
    code = main(["plan", "看这个 https://www.bilibili.com/video/BV18puh6wEmt"])
    assert code == EXIT_OK
    assert "platform: bilibili" in capsys.readouterr().out


def test_cli_rejects_video_channels(capsys) -> None:
    assert main(["plan", "https://mp.weixin.qq.com/s/abc"]) == EXIT_UNSUPPORTED
    assert "视频号" in capsys.readouterr().err


def test_cli_plan_without_engine(monkeypatch, capsys) -> None:
    monkeypatch.setenv("YUANLIU_LUX", "/nope")
    monkeypatch.setenv("YUANLIU_YTDLP", "/nope")
    monkeypatch.setenv("YUANLIU_NODE", "/nope")
    monkeypatch.setenv("YUANLIU_SNIFF", "/nope")
    assert main(["plan", "https://v.kuaishou.com/xyz"]) == EXIT_NO_ENGINE
