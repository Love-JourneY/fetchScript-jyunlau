from pathlib import Path

from yuanliu.transcribe import Transcriber, wait_for_new_file


def _fake_converter(source: Path, target: Path) -> None:
    """测试里不真跑 ffmpeg：造一个 wav 占位。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"RIFF")


def _make_b2t(tmp_path: Path, transcripts: Path, body: str = "转写结果") -> Path:
    binary = tmp_path / "bili2text"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'mkdir -p "{transcripts}"\n'
        f'printf "%s\\n" "{body}" > "{transcripts}/demo-20260922-120000.txt"\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def test_transcriber_unavailable_when_binary_missing(tmp_path: Path) -> None:
    transcriber = Transcriber(binary=tmp_path / "nope", transcripts_dir=tmp_path / "t")
    assert transcriber.available() is False


def test_transcribe_finds_new_transcript_and_copies(tmp_path: Path) -> None:
    transcripts = tmp_path / "transcripts"
    binary = _make_b2t(tmp_path, transcripts)
    media = tmp_path / "我的视频标题.mp4"
    media.write_bytes(b"v")
    outdir = tmp_path / "out"

    result = Transcriber(
        binary=binary, transcripts_dir=transcripts, converter=_fake_converter
    ).transcribe(media, copy_to=outdir)
    assert result.transcript.parent == outdir
    # 文稿跟视频同名（去掉扩展名 + .txt）
    assert result.transcript.name == "我的视频标题.txt"
    assert result.transcript.read_text(encoding="utf-8").strip() == "转写结果"
    assert result.engine == "b2t/qwen3-asr"


def test_transcribe_error_includes_b2t_output(tmp_path: Path) -> None:
    """b2t 出错也会退出 0（实测"出错了: unable to open database file"）⇒ 报错必须带它的输出。"""
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    binary = tmp_path / "bili2text"
    binary.write_text(
        "#!/usr/bin/env bash\necho '出错了: unable to open database file' >&2\nexit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    media = tmp_path / "v.mp4"
    media.write_bytes(b"v")
    import pytest

    with pytest.raises(RuntimeError) as excinfo:
        Transcriber(binary=binary, transcripts_dir=transcripts, converter=_fake_converter).transcribe(media)
    assert "unable to open database" in str(excinfo.value)


def test_transcribe_raises_when_no_new_file(tmp_path: Path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    binary = tmp_path / "bili2text"
    binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    media = tmp_path / "video.mp4"
    media.write_bytes(b"v")
    import pytest

    with pytest.raises(RuntimeError):
        Transcriber(binary=binary, transcripts_dir=transcripts, converter=_fake_converter).transcribe(media)


def test_wait_for_new_file(tmp_path: Path) -> None:
    directory = tmp_path / "t"
    directory.mkdir()
    before = set(directory.rglob("*.txt"))
    (directory / "new.txt").write_text("x", encoding="utf-8")
    assert wait_for_new_file(directory, before) is not None


def test_m4a_is_converted_to_wav_before_b2t(tmp_path: Path) -> None:
    """b2t 的 sherpa 只认 WAV：.m4a 必须先转 16k 单声道 wav（实测报 Expected chunk_id RIFF）。"""
    transcripts = tmp_path / "transcripts"
    binary = _make_b2t(tmp_path, transcripts)
    media = tmp_path / "song.m4a"
    media.write_bytes(b"m4a")
    converted: dict = {}

    def fake_converter(source: Path, target: Path) -> None:
        converted["source"] = source
        converted["target"] = target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF")

    transcriber = Transcriber(binary=binary, transcripts_dir=transcripts, converter=fake_converter)
    result = transcriber.transcribe(media, copy_to=tmp_path / "out")
    assert converted["source"] == media
    assert converted["target"].suffix == ".wav"
    assert result.transcript.name == "song.txt"
    # 临时 wav 用完即删
    assert not converted["target"].exists()


def test_wav_passes_through_without_conversion(tmp_path: Path) -> None:
    transcripts = tmp_path / "transcripts"
    binary = _make_b2t(tmp_path, transcripts)
    media = tmp_path / "a.wav"
    media.write_bytes(b"RIFF")

    def exploding_converter(source, target):  # pragma: no cover
        raise AssertionError("wav 不该再转")

    transcriber = Transcriber(binary=binary, transcripts_dir=transcripts, converter=exploding_converter)
    assert transcriber.transcribe(media).transcript.exists()
