"""Command line entry. Agents should call `download`; people can call `gui`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hfmd import __version__
from hfmd.curlsafe import bundled_curl_label, Cancelled
from hfmd.download import DownloadError, Progress, download_model


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 2
    if args.cmd == "gui":
        from hfmd.gui import run

        return run()
    if args.cmd == "download":
        return _download(args)
    parser.print_help()
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hfmd",
        description="直连 hf-mirror 下载模型。内置固定版本的 curl，不走系统代理和环境变量代理。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hfmd {__version__} (curl {bundled_curl_label()})",
    )
    sub = parser.add_subparsers(dest="cmd")

    gui = sub.add_parser("gui", help="打开图形界面")
    gui.set_defaults(cmd="gui")

    download = sub.add_parser("download", help="命令行下载，给其它 agent 调用")
    download.add_argument("model", help="模型名，格式 provider/model_name")
    download.add_argument(
        "-c",
        "--concurrency",
        type=int,
        required=True,
        help="并发上限，1 到 128。先开少量连接，总速度没到带宽上限再增加",
    )
    download.add_argument(
        "-b",
        "--bandwidth-mbps",
        type=float,
        default=300,
        help="全局总带宽上限，单位 Mbps（兆比特/秒）。默认 300",
    )
    download.add_argument(
        "-d",
        "--download-home",
        type=Path,
        required=True,
        help="保存根目录。文件会写到 download_home/provider/model_name",
    )
    download.add_argument("--revision", default="main", help="分支或标签，默认 main")
    download.add_argument("--token", default=None, help="可选的 Hugging Face access token，不会写入日志")
    download.add_argument("--json", action="store_true", help="进度改成每行一个 JSON，方便程序读取")
    download.set_defaults(cmd="download")
    return parser


def _download(args: argparse.Namespace) -> int:
    def on_progress(progress: Progress) -> None:
        if args.json:
            print(json.dumps(_progress_payload(progress), ensure_ascii=False), flush=True)
            return
        if progress.phase == "downloading":
            percent = 0.0 if progress.bytes_total <= 0 else progress.bytes_done * 100 / progress.bytes_total
            line = (
                f"\r[{percent:5.1f}%] files {progress.files_done}/{progress.files_total}"
                f" | {_bytes(progress.bytes_done)}/{_bytes(progress.bytes_total)}"
                f" | {progress.speed_mbps:6.1f} Mbps | {progress.message}"
            )
            print(line[:180], end="", flush=True)
            return
        if progress.phase != "start":
            print(flush=True)
        print(progress.message, flush=True)

    try:
        dest = download_model(
            args.model,
            args.concurrency,
            args.bandwidth_mbps,
            args.download_home,
            revision=args.revision,
            token=args.token,
            progress=on_progress,
        )
    except Cancelled as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 130
    except DownloadError as exc:
        if args.json:
            print(json.dumps({"event": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        else:
            print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"event": "finished", "path": str(dest)}, ensure_ascii=False), flush=True)
    else:
        print(f"\ndone path={dest}")
    return 0


def _progress_payload(progress: Progress) -> dict:
    return {
        "event": progress.phase,
        "files_done": progress.files_done,
        "files_total": progress.files_total,
        "bytes_done": progress.bytes_done,
        "bytes_total": progress.bytes_total,
        "speed_mbps": round(progress.speed_mbps, 3),
        "message": progress.message,
        "path": progress.dest,
    }


def _bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1000
    return f"{value}B"
