"""命令行入口：``yuanliu <子命令>``。

    yuanliu engines                      # 看本机装了哪些引擎、每个平台走哪条路
    yuanliu plan "<分享文案>"             # 只判平台与路线（不联网）
    yuanliu probe "<分享文案>"            # 取元信息（联网，不下载）
    yuanliu download "<分享文案>" -o DIR  # 下载（联网）

退出码：0 成功｜2 平台不可用｜3 引擎缺失｜4 引擎失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from yuanliu import __version__
from yuanliu.cookies import (
    DEFAULT_COOKIE_DOMAINS,
    find_firefox_profiles,
    load_firefox_cookies,
    to_netscape,
)
from yuanliu.engines import EngineFailed, EngineUnavailable, available_engines
from yuanliu.resolve import NoEngineAvailable, ResolveError, describe_routes, download, plan, probe

EXIT_OK = 0
EXIT_UNSUPPORTED = 2
EXIT_NO_ENGINE = 3
EXIT_ENGINE_FAILED = 4


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yuanliu", description="分享链接 → 本地媒体（多引擎路由）")
    parser.add_argument("--version", action="version", version=f"yuanliu {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("engines", help="列出引擎可用性与平台路由")

    for name, help_text in (
        ("plan", "只判平台与路线（不联网）"),
        ("probe", "取元信息（联网，不下载）"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("text", help="分享文案或链接")

    tx = sub.add_parser("transcribe", help="只转文字：给一个本地音视频文件，出同名 .txt（调本机 b2t）")
    tx.add_argument("path", help="本地音/视频文件")
    tx.add_argument("--keep-original", action="store_true", help="转完不删原文件（默认保留）")

    sv = sub.add_parser("serve", help="起常驻服务（网页 + JSON API，给平板接入）")
    sv.add_argument("--bind", default=None, help="监听地址（默认 0.0.0.0，供局域网）")
    sv.add_argument("--port", type=int, default=None, help="端口（默认 8901）")
    sv.add_argument("--token", default=None, help="访问 token（默认读 YUANLIU_TOKEN）")

    ck = sub.add_parser("cookies", help="从本机浏览器导出 cookies.txt（Firefox/LibreWolf 明文库）")
    ck.add_argument("--list", action="store_true", help="只列出可用 profile 与命中数量")
    ck.add_argument("--profile", default=None, help="指定 profile 目录")
    ck.add_argument("--domain", action="append", default=None, help="只导这些域名（可多次）；默认常用国内平台")
    ck.add_argument("-o", "--out", default=None, help="输出 cookies.txt（默认只统计）")

    dl = sub.add_parser("download", help="下载到目录")
    dl.add_argument("text", help="分享文案或链接")
    dl.add_argument("-o", "--out", required=True, help="输出目录")
    dl.add_argument("--engine", default=None, help="强制指定引擎（yt-dlp / lux）")
    dl.add_argument("--cookies", default=None, help="cookies.txt（Netscape 格式），给需登录的平台")
    dl.add_argument("--transcribe", action="store_true", help="下载后顺带转文字（调本机 b2t/Qwen3-ASR）")
    dl.add_argument("--text-only", action="store_true", help="只要文字：转写完把视频删掉（自动开启 --transcribe）")
    dl.add_argument("--audio-only", action="store_true", help="只下音轨（配合 --transcribe，省带宽省盘）")
    dl.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key, value in payload.items():
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "(空)"
        print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        if args.command == "engines":
            payload = {"engines": available_engines(), "routes": describe_routes()}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return EXIT_OK

        if args.command == "plan":
            current = plan(args.text)
            _emit(
                {
                    "platform": current.platforms,
                    "url": current.link.url,
                    "is_short": current.link.is_short,
                    "route": list(current.engines),
                    "available": list(current.available),
                    "usable": current.usable,
                },
                as_json=False,
            )
            return EXIT_OK if current.usable else EXIT_NO_ENGINE

        if args.command == "probe":
            _emit(probe(args.text), as_json=True)
            return EXIT_OK

        if args.command == "transcribe":
            from pathlib import Path as _Path

            from yuanliu.transcribe import Transcriber

            media = _Path(args.path).expanduser()
            if not media.is_file():
                print(f"文件不存在：{media}", file=sys.stderr)
                return EXIT_UNSUPPORTED
            result = Transcriber().transcribe(media, copy_to=media.parent)
            print(f"文稿：{result.transcript}")
            return EXIT_OK

        if args.command == "serve":
            from yuanliu.server import main as serve_main

            argv = []
            if args.bind:
                argv += ["--bind", args.bind]
            if args.port:
                argv += ["--port", str(args.port)]
            if args.token is not None:
                argv += ["--token", args.token]
            return serve_main(argv)

        if args.command == "cookies":
            domains = tuple(args.domain) if args.domain else DEFAULT_COOKIE_DOMAINS
            profiles = find_firefox_profiles()
            if not profiles:
                print("没找到 Firefox/LibreWolf profile（cookies.sqlite）", file=sys.stderr)
                return EXIT_NO_ENGINE
            if args.profile:
                profiles = [p for p in profiles if str(p[1]) == str(Path(args.profile).expanduser())] or [
                    ("custom", Path(args.profile).expanduser())
                ]
            if args.list or not args.out:
                for browser, profile in profiles:
                    try:
                        cookies = load_firefox_cookies(profile, domains=domains)
                    except FileNotFoundError as exc:
                        print(f"{browser}\t{profile}\t{exc}")
                        continue
                    hosts: dict[str, int] = {}
                    for cookie in cookies:
                        hosts[cookie.host] = hosts.get(cookie.host, 0) + 1
                    print(f"{browser}\t{profile}\t命中 {len(cookies)} 条\t{hosts}")
                if not args.out:
                    return EXIT_OK
            cookies = []
            for _, profile in profiles:
                try:
                    cookies.extend(load_firefox_cookies(profile, domains=domains))
                except FileNotFoundError:
                    continue
            out = Path(args.out).expanduser()
            out.write_text(to_netscape(cookies), encoding="utf-8")
            out.chmod(0o600)
            print(f"已写 {out}（{len(cookies)} 条，权限 600）")
            return EXIT_OK if cookies else EXIT_UNSUPPORTED

        if args.command == "download":
            outcome = download(
                args.text,
                Path(args.out).expanduser(),
                engine_name=args.engine,
                cookies=Path(args.cookies).expanduser() if args.cookies else None,
                transcribe=args.transcribe or args.text_only,
                keep_media=not args.text_only,
                audio_only=args.audio_only or args.text_only,
            )
            _emit(outcome.as_dict(), as_json=args.json)
            return EXIT_OK

    except ResolveError as exc:
        code = EXIT_NO_ENGINE if isinstance(exc, NoEngineAvailable) else EXIT_UNSUPPORTED
        print(f"错误：{exc}", file=sys.stderr)
        return code
    except EngineUnavailable as exc:
        print(f"引擎缺失：{exc}", file=sys.stderr)
        return EXIT_NO_ENGINE
    except EngineFailed as exc:
        print(f"引擎失败：{exc}", file=sys.stderr)
        return EXIT_ENGINE_FAILED

    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
