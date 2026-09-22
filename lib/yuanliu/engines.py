"""外部下载引擎（yt-dlp / lux）。

为什么是"外部二进制"而不是 Python 依赖：
- 引擎的更新频率远高于本模块（yt-dlp 一周几版），松耦合才不会互相拖死；
- 模块保持零第三方依赖 ⇒ 离线可迁移、可被别的东西复用；
- 引擎缺了要**明确报错**（`EngineUnavailable`），而不是静默降级。

发现顺序：`$YUANLIU_<NAME>` → 已知安装位 → `PATH`。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

__all__ = [
    "Engine",
    "EngineFailed",
    "EngineUnavailable",
    "YtDlpEngine",
    "LuxEngine",
    "ENABLED_ENGINES",
    "PLATFORM_ROUTING",
    "available_engines",
    "enabled_engines",
    "engines_for_platform",
    "pick_engines",
    "resolve_binary",
]

DEFAULT_YTDLP_CANDIDATES = (
    "/opt/bili2text/app/.venv/bin/yt-dlp",
    "/opt/yuanliu/vendor/yt-dlp",
)
DEFAULT_LUX_CANDIDATES = (
    "/opt/yuanliu/vendor/lux",
    "/usr/local/bin/lux",
)


class EngineUnavailable(RuntimeError):
    """引擎二进制找不到。"""


class EngineFailed(RuntimeError):
    """引擎跑了但失败（非零退出 / 输出无法解析）。"""


class Engine(Protocol):
    name: str
    platforms: frozenset[str]

    @property
    def binary(self) -> str | None: ...

    def available(self) -> bool: ...

    def probe(self, url: str, *, timeout: float = 60.0) -> dict: ...

    def download(
        self,
        url: str,
        outdir: Path,
        *,
        cookies: Path | None = None,
        timeout: float = 900.0,
        audio_only: bool = False,
        on_progress: Callable[[float], None] | None = None,
    ) -> list[Path]: ...


def resolve_binary(name: str, env_var: str, candidates: Sequence[str]) -> str | None:
    explicit = os.getenv(env_var)
    if explicit:
        path = Path(explicit).expanduser()
        return str(path) if path.exists() else None
    for candidate in candidates:
        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which(name)


_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _stream(
    cmd: Sequence[str],
    *,
    timeout: float,
    on_progress: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """跑子进程并**边跑边报进度**（Nija 要进度条）。

    yt-dlp 用 ``--progress-template``/``--newline``，lux 自带百分比进度条 —— 两者都能从
    输出里抠出 ``NN%``。抠不到就不报（前端显示不确定态），绝不假装有进度。
    """
    import logging

    log = logging.getLogger("yuanliu")
    log.info("run: %s", " ".join(str(part) for part in cmd)[:300])
    process = subprocess.Popen(  # noqa: S603
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,  # ⚠️ lux 遇到多 P 会**交互式提问**，不给 stdin 就永远卡住（实测）
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    state = {"last": time.monotonic(), "stalled": False}
    announced = 0

    def watchdog() -> None:
        """卡住检测：**读循环本身也在等输出**，所以必须另起线程看门狗。"""
        while process.poll() is None:
            time.sleep(2)
            if time.monotonic() - state["last"] > stall_limit:
                state["stalled"] = True
                log.warning(
                    "引擎 %.0fs 无任何输出，判定卡住并杀掉：%s", stall_limit, " ".join(str(p) for p in cmd)[:160]
                )
                process.kill()
                return

    stall_limit = float(os.getenv("YUANLIU_STALL_LIMIT", "90"))
    threading.Thread(target=watchdog, daemon=True).start()
    assert process.stdout is not None
    try:
        for line in process.stdout:
            lines.append(line)
            state["last"] = time.monotonic()
            if announced < 3:
                announced += 1
                log.info("out: %s", line.strip()[:200])
            if on_progress is not None:
                match = _PERCENT_RE.search(line)
                if match:
                    try:
                        on_progress(min(1.0, max(0.0, float(match.group(1)) / 100.0)))
                    except ValueError:
                        pass
            if time.monotonic() > deadline:
                process.kill()
                raise subprocess.TimeoutExpired(list(cmd), timeout)
        returncode = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
    if state["stalled"]:
        raise EngineFailed(f"引擎 {stall_limit:.0f}s 没有任何输出（判定卡住，已终止）")
    return subprocess.CompletedProcess(list(cmd), returncode, "".join(lines), "")


def _reported_filepaths(output: str) -> list[Path]:
    """从引擎输出里认领"它说已经下好的文件"（绝对路径行）。"""
    found: list[Path] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith("/") or len(line) > 1024:
            continue
        candidate = Path(line)
        if candidate.suffix and candidate not in found:
            found.append(candidate)
    return found


def _run(cmd: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 参数列表，无 shell
        list(cmd),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


@dataclass(slots=True)
class YtDlpEngine:
    """yt-dlp：1752 个提取器，海外最强；国内平台的部分站点需 cookie。"""

    name: str = "yt-dlp"
    platforms: frozenset[str] = frozenset(
        {"bilibili", "douyin", "xiaohongshu", "weibo", "tiktok", "youtube", "unknown"}
    )
    env_var: str = "YUANLIU_YTDLP"
    candidates: Sequence[str] = DEFAULT_YTDLP_CANDIDATES

    @property
    def binary(self) -> str | None:
        return resolve_binary("yt-dlp", self.env_var, self.candidates)

    def available(self) -> bool:
        return self.binary is not None

    def _require(self) -> str:
        binary = self.binary
        if binary is None:
            raise EngineUnavailable(
                "找不到 yt-dlp：设置 YUANLIU_YTDLP 指向可执行文件，或装到 PATH。"
            )
        return binary

    def probe(self, url: str, *, timeout: float = 60.0) -> dict:
        binary = self._require()
        result = _run(
            [binary, "--skip-download", "--no-warnings", "--dump-json", "--no-playlist", url],
            timeout=timeout,
        )
        if result.returncode != 0:
            raise EngineFailed(f"yt-dlp 探测失败：{(result.stderr or '').strip()[:500]}")
        first = next((line for line in result.stdout.splitlines() if line.strip()), "")
        try:
            return json.loads(first)
        except json.JSONDecodeError as exc:
            raise EngineFailed(f"yt-dlp 输出无法解析为 JSON：{exc}") from exc

    def download(
        self,
        url: str,
        outdir: Path,
        *,
        cookies: Path | None = None,
        timeout: float = 900.0,
        audio_only: bool = False,
        on_progress: Callable[[float], None] | None = None,
    ) -> list[Path]:
        binary = self._require()
        outdir.mkdir(parents=True, exist_ok=True)
        before = set(outdir.iterdir())
        progress_args = [
            "--newline",
            "--progress-template",
            "download:%(progress._percent_str)s",
            # ⚠️ 必须让 yt-dlp 报出**最终文件路径**：否则"文件已存在⇒跳过下载⇒没有新文件"
            # 会被我们的"新增文件"启发式误判成失败（实测：重复下载同一视频时发生）
            "--print",
            "after_move:filepath",
        ]
        if audio_only:
            # 只要文字时**别下整片视频**：只取最佳音轨（省带宽、省盘、可直喂 ASR）
            cmd = [
                binary, "--no-warnings", "--no-playlist", *progress_args, "-f", "ba/b",
                "-o", str(outdir / "%(title)s.%(ext)s"), url,
            ]
        else:
            cmd = [
                binary,
                "--no-warnings",
                "--no-playlist",
                *progress_args,
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                str(outdir / "%(title)s.%(ext)s"),  # 文件名=视频标题（Nija 2026-09-22 要求）
                url,
            ]
        if cookies is not None:
            cmd += ["--cookies", str(cookies)]
        result = _stream(cmd, timeout=timeout, on_progress=on_progress)
        if result.returncode != 0:
            tail = (result.stdout or "").strip()[-500:]
            import logging as _logging

            _logging.getLogger("yuanliu").warning("yt-dlp 退出码 %s，输出尾部：%s", result.returncode, tail)
            raise EngineFailed(f"yt-dlp 下载失败：{tail}")

        created = sorted(p for p in outdir.iterdir() if p not in before and p.is_file())
        if created:
            return created
        # 没有新文件 ⇒ 多半是"已存在被跳过"：从 --print after_move:filepath 的输出里认领
        reported = _reported_filepaths(result.stdout or "")
        return [path for path in reported if path.exists()]


@dataclass(slots=True)
class LuxEngine:
    """lux：Go 单二进制，覆盖 B站/抖音/快手/小红书/微博 —— 补 yt-dlp 的快手缺口。"""

    name: str = "lux"
    platforms: frozenset[str] = frozenset(
        {"bilibili", "douyin", "kuaishou", "xiaohongshu", "weibo", "unknown"}
    )
    env_var: str = "YUANLIU_LUX"
    candidates: Sequence[str] = DEFAULT_LUX_CANDIDATES

    @property
    def binary(self) -> str | None:
        return resolve_binary("lux", self.env_var, self.candidates)

    def available(self) -> bool:
        return self.binary is not None

    def _require(self) -> str:
        binary = self.binary
        if binary is None:
            raise EngineUnavailable(
                "找不到 lux：设置 YUANLIU_LUX 指向可执行文件，或跑 tools/get-lux.sh 下载。"
            )
        return binary

    def probe(self, url: str, *, timeout: float = 60.0) -> dict:
        binary = self._require()
        result = _run([binary, "-i", url], timeout=timeout)
        if result.returncode != 0:
            raise EngineFailed(f"lux 探测失败：{(result.stderr or '').strip()[:500]}")
        return {"engine": self.name, "raw": result.stdout.strip()}

    def download(
        self,
        url: str,
        outdir: Path,
        *,
        cookies: Path | None = None,
        timeout: float = 900.0,
        audio_only: bool = False,
        on_progress: Callable[[float], None] | None = None,
    ) -> list[Path]:
        binary = self._require()
        outdir.mkdir(parents=True, exist_ok=True)
        before = set(outdir.iterdir())
        cmd = [binary, "-o", str(outdir)]
        if audio_only:
            cmd.append("-ao")  # lux 原生支持 --audio-only
        if cookies is not None:
            cmd += ["-c", str(cookies)]
        cmd.append(url)
        result = _stream(cmd, timeout=timeout, on_progress=on_progress)
        if result.returncode != 0:
            tail = (result.stdout or "").strip()[-500:]
            import logging as _logging

            _logging.getLogger("yuanliu").warning("lux 退出码 %s，输出尾部：%s", result.returncode, tail)
            raise EngineFailed(f"lux 下载失败：{tail}")
        return sorted(p for p in outdir.iterdir() if p not in before and p.is_file())


def enabled_engines() -> tuple[Engine, ...]:
    """全部引擎（含浏览器嗅探）。**延迟导入** browser 以免循环依赖。"""
    from yuanliu.browser import BrowserEngine

    return (BrowserEngine(), YtDlpEngine(), LuxEngine())


# 只含"外部二进制"两个；要拿全量（含 browser）请用 enabled_engines()
ENABLED_ENGINES: tuple[Engine, ...] = (YtDlpEngine(), LuxEngine())

# 按平台排序的引擎偏好：左边先试，失败再退到下一个。
PLATFORM_ROUTING: dict[str, tuple[str, ...]] = {
    # 抖音/小红书：实测 yt-dlp=403、lux=403 ⇒ 只有"真浏览器嗅探"这条路能拿到无水印直链
    "douyin": ("browser", "lux", "yt-dlp"),
    "xiaohongshu": ("browser", "lux", "yt-dlp"),
    "weibo": ("browser", "lux", "yt-dlp"),
    "kuaishou": ("browser", "lux"),
    "bilibili": ("yt-dlp", "lux", "browser"),
    "tiktok": ("yt-dlp", "lux"),
    "youtube": ("yt-dlp", "lux"),
    "wechat_channels": (),  # 只能 MITM/嗅探，没有直链引擎
    "unknown": ("yt-dlp", "lux"),
}


def available_engines() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for engine in enabled_engines():
        binary = getattr(engine, "binary", None)
        out[engine.name] = binary if binary else ("node" if engine.name == "browser" and engine.available() else None)
    return out


def engines_for_platform(platform: str) -> tuple[Engine, ...]:
    order = PLATFORM_ROUTING.get(platform, ())
    by_name = {engine.name: engine for engine in enabled_engines()}
    return tuple(by_name[name] for name in order if name in by_name)


def pick_engines(platform: str, *, only_available: bool = True) -> tuple[Engine, ...]:
    engines = engines_for_platform(platform)
    if only_available:
        return tuple(engine for engine in engines if engine.available())
    return engines
