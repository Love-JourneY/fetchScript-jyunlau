"""视频 → 文字：把「解析下载」和 b2t（bili2text，Qwen3-ASR）接在一起。

边界设计（本屋模块纪律）：
- **不复制 b2t 的代码**，而是**调用它**（`bili2text tx <文件>`）—— 接口是命令，两边各自可插拔。
- b2t 的 `tx --output` 是**死参数**（上游 bug，写了也不生效）⇒ 我们**用"跑前跑后目录对比"找新增文稿**
  （和 b2t 模块自己的 `b2t-archive` 同一手法），不依赖它打印什么。
- 找到的文稿**复制进本模块的媒体目录**，这样网页 `/files/` 能直接给人下载。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Transcriber", "TranscriptResult", "DEFAULT_BILI2TEXT", "DEFAULT_TRANSCRIPTS_DIR"]

DEFAULT_BILI2TEXT = "/usr/local/bin/bili2text"
DEFAULT_TRANSCRIPTS_DIR = Path("/var/lib/bili2text/transcripts/original")


@dataclass(slots=True)
class TranscriptResult:
    transcript: Path
    engine: str = "b2t/qwen3-asr"


class Transcriber:
    """调用本机 b2t 做转写；b2t 不在就明确报"不可用"，不静默降级。"""

    def __init__(
        self,
        *,
        binary: Path | str | None = None,
        transcripts_dir: Path | None = None,
        runner: object | None = None,
        converter: object | None = None,
    ) -> None:
        self._binary = Path(binary).expanduser() if binary else None
        self._transcripts_dir = Path(transcripts_dir).expanduser() if transcripts_dir else None
        self._runner = runner
        self._converter = converter

    @property
    def binary(self) -> Path | None:
        explicit = self._binary or (Path(os.environ["FETCHSCRIPT_BILI2TEXT"]).expanduser() if os.getenv("FETCHSCRIPT_BILI2TEXT") else None)
        if explicit is not None:
            return explicit if explicit.exists() else None
        default = Path(DEFAULT_BILI2TEXT)
        if default.exists():
            return default
        found = shutil.which("bili2text")
        return Path(found) if found else None

    @property
    def transcripts_dir(self) -> Path:
        explicit = self._transcripts_dir or (
            Path(os.environ["FETCHSCRIPT_TRANSCRIPTS_DIR"]).expanduser()
            if os.getenv("FETCHSCRIPT_TRANSCRIPTS_DIR")
            else None
        )
        return explicit or DEFAULT_TRANSCRIPTS_DIR

    def available(self) -> bool:
        return self.binary is not None

    def transcribe(self, media: Path, *, timeout: float = 3600.0, copy_to: Path | None = None) -> TranscriptResult:
        binary = self.binary
        if binary is None:
            raise RuntimeError(
                "本机没有 bili2text（视频转文字）—— 装法见 ~/Documents/repo/bili2text-deploy/README.md，"
                "或设 FETCHSCRIPT_BILI2TEXT 指向它的启动器。"
            )
        media = Path(media).expanduser()
        if not media.is_file():
            raise RuntimeError(f"要转写的文件不存在：{media}")

        # ⚠️ 实测：b2t 的 Qwen3-ASR 走 sherpa-onnx，**只认 16kHz 单声道 WAV**
        # （喂 .m4a 会报 `Expected chunk_id RIFF` 然后什么都没产出）⇒ 先转 WAV 再喂。
        work_media, temp_wav = self._ensure_wav(media)
        try:
            before = self._snapshot()
            run = self._runner or self._default_runner
            result = run([str(binary), "tx", str(work_media)], timeout)  # type: ignore[operator]
            after = self._snapshot()
        finally:
            if temp_wav is not None:
                temp_wav.unlink(missing_ok=True)

        created = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
        if not created:
            # b2t 出错时**也有可能退出码 0**（实测它会打印"出错了: …"再退出 0）⇒ 必须把 stderr 带出来
            stderr = (getattr(result, "stderr", "") or "").strip()
            stdout = (getattr(result, "stdout", "") or "").strip()
            detail = (stderr or stdout).replace("\n", " ")[-300:]
            raise RuntimeError(
                "bili2text 跑完了但没有发现新增文稿"
                f"（工作区 {self.transcripts_dir}）"
                + (f"；b2t 输出：{detail}" if detail else "")
            )

        transcript = created[0]
        if copy_to is not None:
            copy_to.mkdir(parents=True, exist_ok=True)
            # 文稿跟着视频同名（Nija 要求"名字要和标题一样"）⇒ 用视频的 stem，而不是 b2t 自己的命名
            target = copy_to / f"{media.stem}.txt"
            shutil.copy2(transcript, target)
            transcript = target
        return TranscriptResult(transcript=transcript)

    def _ensure_wav(self, media: Path) -> tuple[Path, Path | None]:
        """返回 (喂给 b2t 的文件, 需要事后删除的临时 wav)。WAV 原样返回。"""
        if media.suffix.lower() == ".wav":
            return media, None
        converter = self._converter or self._convert_with_ffmpeg
        temp_path = Path(tempfile.mkdtemp(prefix="fetchscript-asr-")) / f"{media.stem}.wav"
        converter(media, temp_path)
        return temp_path, temp_path

    def _convert_with_ffmpeg(self, source: Path, target: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("需要 ffmpeg 把音频转成 16kHz 单声道 WAV，但 PATH 里没有")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(  # noqa: S603
            [ffmpeg, "-y", "-i", str(source), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(target)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=1800,
        )
        if result.returncode != 0 or not target.exists():
            raise RuntimeError(f"ffmpeg 转 WAV 失败：{(result.stderr or '').strip()[-200:]}")

    def _snapshot(self) -> set[Path]:
        directory = self.transcripts_dir
        if not directory.is_dir():
            return set()
        return {path for path in directory.rglob("*.txt") if path.is_file()}

    def _default_runner(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
            cwd=str(Path.home()),
        )


def wait_for_new_file(directory: Path, before: set[Path], *, timeout: float = 5.0) -> Path | None:
    """给需要"稍等一下文件系统"的场景用（测试/慢盘）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        created = sorted(
            {p for p in directory.rglob("*.txt") if p.is_file()} - before,
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if created:
            return created[0]
        time.sleep(0.2)
    return None
