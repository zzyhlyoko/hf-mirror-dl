"""The only place this program is allowed to touch the network.

Do not replace this with urllib, requests, httpx, the Windows copy of curl,
or a curl found on PATH. Those pick up HTTP_PROXY, curlrc, or the system
proxy. hf-mirror rejects proxied downloads, and a proxy would also bill the
traffic.

Every transfer uses the curl 8.23.0 build shipped in third_party/curl.
The child process gets an allowlisted environment with no proxy variables,
and the argument list always starts with:

    curl.exe -q --noproxy * --proxy <empty> --proto ... --proto-redir ...

-q is first so curl does not read _curlrc or .curlrc.
--noproxy * turns proxy use off for every host.
--proxy "" overrides a proxy that was still configured.
The environment allowlist means the child cannot see HTTP(S)_PROXY,
ALL_PROXY, CURL_HOME, or the user profile that would hold a curlrc.
"""

from __future__ import annotations

import hashlib
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hfmd.limiter import BandwidthLimiter

PINNED_VERSION = "8.23.0"
PINNED_BUILD = "8.23.0_9"
_CURL_SHA256 = "3c871d76f592b7b2050c6070a293d296c9fd67a0b3d165b2fa4cd7d7e4a02f58"
_DLL_SHA256 = "097a9d6f0b204deacf3953c43376bde1d012762c9798c238c69f60b006e44965"
_READ_SIZE = 64 * 1024
_ENV_ALLOW = {
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "PATHEXT",
}

_CURL_DIR = Path(__file__).resolve().parent.parent / "third_party" / "curl"
_verified = False
_verify_lock = threading.Lock()
_procs: set[subprocess.Popen[bytes]] = set()
_procs_lock = threading.Lock()
_stderr_bufs: dict[int, bytearray] = {}
_stderr_threads: dict[int, threading.Thread] = {}


class CurlError(Exception):
    pass


class Cancelled(CurlError):
    pass


class RangeIgnored(CurlError):
    """The server answered a partial Range with the whole object."""


@dataclass
class TransferResult:
    status: int
    nbytes: int
    headers: dict[str, str]
    path: Path


def curl_dir() -> Path:
    return _CURL_DIR


def curl_exe() -> Path:
    return _CURL_DIR / "curl.exe"


def child_env() -> dict[str, str]:
    """Environment for a curl child. Proxy variables are not copied, even if set."""
    source = {key.upper(): value for key, value in os.environ.items()}
    env: dict[str, str] = {}
    for name in _ENV_ALLOW:
        if name in source and source[name]:
            env[name] = source[name]
    system_root = source.get("SYSTEMROOT") or source.get("WINDIR")
    if not system_root:
        raise CurlError("SYSTEMROOT is missing; refusing to launch curl")
    env["SYSTEMROOT"] = system_root
    env["WINDIR"] = source.get("WINDIR") or system_root
    # Only Windows itself. The user PATH can contain another curl or a proxy tool.
    env["PATH"] = str(Path(system_root) / "System32") + os.pathsep + system_root
    return env


def command_prefix(*, allow_loopback_http: bool = False) -> list[str]:
    # =https means "only these protocols", not "add https".
    proto = "=http,https" if allow_loopback_http else "=https"
    return [
        str(curl_exe()),
        "-q",
        "--noproxy",
        "*",
        "--proxy",
        "",
        "--proto",
        proto,
        "--proto-redir",
        proto,
    ]


def verify_bundled_curl() -> str:
    """Confirm the bytes and the reported version. Returns the version line."""
    global _verified
    with _verify_lock:
        if _verified:
            return f"curl {PINNED_VERSION}"
        exe = curl_exe()
        dll = _CURL_DIR / "libcurl-x64.dll"
        if not exe.is_file() or not dll.is_file():
            raise CurlError(
                "内置 curl 不完整。需要 third_party/curl/curl.exe 和同目录的 libcurl-x64.dll，"
                "不能改用系统里的 curl。"
            )
        _check_hash(exe, _CURL_SHA256)
        _check_hash(dll, _DLL_SHA256)
        proc = subprocess.run(
            [str(exe), "-q", "-V"],
            capture_output=True,
            env=child_env(),
            creationflags=_creation_flags(),
            check=False,
        )
        text = proc.stdout.decode("utf-8", "replace")
        first = text.splitlines()[0] if text else ""
        if proc.returncode != 0 or not first.startswith(f"curl {PINNED_VERSION} "):
            detail = proc.stderr.decode("utf-8", "replace")[:300]
            raise CurlError(f"内置 curl 版本不是 {PINNED_VERSION}：{first or detail}")
        _verified = True
        return first


def kill_all() -> None:
    with _procs_lock:
        procs = list(_procs)
    for proc in procs:
        if proc.poll() is None:
            proc.kill()


def get_bytes(
    url: str,
    *,
    token: str | None = None,
    allow_loopback_http: bool = False,
    max_bytes: int = 32 * 1024 * 1024,
    cancel: threading.Event | None = None,
) -> tuple[bytes, str]:
    """GET a small body (API JSON). Returns (body, raw header dump)."""
    check_initial_url(url, allow_loopback_http=allow_loopback_http)
    header_path, body_path = _temp_pair("hfmd-api")
    try:
        proc = _popen(
            url,
            token=token,
            allow_loopback_http=allow_loopback_http,
            header_path=header_path,
            stdout_path=body_path,
            range_header=None,
            limit_rate=None,
        )
        try:
            _wait_proc(proc, cancel, timeout=120)
        finally:
            _finish(proc)
        _raise_if_failed(proc, header_path)
        data = body_path.read_bytes()
        if len(data) > max_bytes:
            raise CurlError(f"响应超过 {max_bytes} 字节，已中止")
        headers = header_path.read_text(encoding="utf-8", errors="replace")
        return data, headers
    finally:
        header_path.unlink(missing_ok=True)
        body_path.unlink(missing_ok=True)


def download_to_file(
    url: str,
    dest: Path,
    *,
    start: int,
    end: int,
    total_size: int,
    token: str | None,
    allow_loopback_http: bool,
    limiter: BandwidthLimiter,
    cancel: threading.Event | None,
    limit_rate: int,
    stall_seconds: float = 60,
    on_chunk: callable | None = None,
) -> TransferResult:
    """Download bytes [start, end) into dest, throttled by the shared limiter.

    Partial requests that come back as HTTP 200 are rejected with RangeIgnored
    so the caller can fall back to a single full-file transfer.
    """
    if end < start:
        raise CurlError("invalid range")
    expected = end - start
    if expected == 0:
        dest.write_bytes(b"")
        return TransferResult(status=200, nbytes=0, headers={}, path=dest)
    check_initial_url(url, allow_loopback_http=allow_loopback_http)
    header_path, _unused = _temp_pair("hfmd-hdr")
    _unused.unlink(missing_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = not (start == 0 and end == total_size)
    range_header = f"{start}-{end - 1}" if partial else None
    proc = _popen(
        url,
        token=token,
        allow_loopback_http=allow_loopback_http,
        header_path=header_path,
        stdout_path=None,
        range_header=range_header,
        limit_rate=limit_rate,
    )
    assert proc.stdout is not None
    incoming: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=1)
    reader = threading.Thread(
        target=_read_stdout,
        args=(proc.stdout, incoming),
        name="hfmd-curl-read",
        daemon=True,
    )
    reader.start()
    failed = False
    try:
        got = _consume(
            proc,
            incoming,
            dest,
            expected=expected,
            partial=partial,
            header_path=header_path,
            limiter=limiter,
            cancel=cancel,
            stall_seconds=stall_seconds,
            on_chunk=on_chunk,
        )
    except Exception:
        failed = True
        if proc.poll() is None:
            proc.kill()
        dest.unlink(missing_ok=True)
        raise
    finally:
        if failed and proc.poll() is None:
            proc.kill()
        deadline = time.monotonic() + 5
        while reader.is_alive() and time.monotonic() < deadline:
            try:
                incoming.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
        reader.join(timeout=1)
        _finish(proc)
    status, headers = _final_headers(header_path)
    header_path.unlink(missing_ok=True)
    if status is None:
        raise CurlError("curl 没有返回 HTTP 状态")
    if status >= 400:
        raise CurlError(f"HTTP {status}")
    if partial and status != 206:
        dest.unlink(missing_ok=True)
        raise RangeIgnored(f"分段请求得到 HTTP {status}，服务器没有按 Range 返回")
    if got != expected:
        dest.unlink(missing_ok=True)
        raise CurlError(f"下载长度不符：期望 {expected} 字节，得到 {got} 字节")
    if status == 206:
        _check_content_range(headers, start, end, total_size)
    return TransferResult(status=status, nbytes=got, headers=headers, path=dest)


def check_initial_url(url: str, *, allow_loopback_http: bool) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if allow_loopback_http and parts.scheme == "http" and host in {"127.0.0.1", "localhost"}:
        return
    if parts.scheme == "https" and (host == "hf-mirror.com" or host.endswith(".hf-mirror.com")):
        return
    raise CurlError("只允许直连 https://hf-mirror.com ，拒绝其它地址")


def system_proxy_enabled() -> bool:
    """True when Windows has a proxy turned on. The value itself is not read."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "ProxyEnable")
    except OSError:
        return False
    return int(value) == 1


def _check_hash(path: Path, expected: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise CurlError(f"{path.name} 的 SHA256 与内置的 {PINNED_BUILD} 不一致，拒绝运行")


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _temp_pair(prefix: str) -> tuple[Path, Path]:
    import tempfile

    handle, name = tempfile.mkstemp(prefix=prefix + "-", suffix=".hdr")
    os.close(handle)
    body = Path(name).with_suffix(".body")
    return Path(name), body


def _popen(
    url: str,
    *,
    token: str | None,
    allow_loopback_http: bool,
    header_path: Path,
    stdout_path: Path | None,
    range_header: str | None,
    limit_rate: int | None,
) -> subprocess.Popen[bytes]:
    verify_bundled_curl()
    cmd = command_prefix(allow_loopback_http=allow_loopback_http)
    cmd += [
        "--location",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "20",
        "--retry",
        "2",
        "--retry-delay",
        "1",
        "--retry-all-errors",
        "-A",
        "hfmd/1.0",
        "-D",
        str(header_path),
    ]
    if limit_rate is not None and limit_rate > 0:
        cmd += ["--limit-rate", str(int(limit_rate))]
    if range_header is not None:
        cmd += ["--range", range_header]
    if token:
        if "\r" in token or "\n" in token:
            raise CurlError("token 含有换行，已拒绝")
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if stdout_path is None:
        cmd += ["--output", "-"]
    else:
        cmd += ["--output", str(stdout_path)]
    cmd.append(url)
    stdout: int | None = subprocess.PIPE if stdout_path is None else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=subprocess.PIPE,
        env=child_env(),
        creationflags=_creation_flags(),
    )
    with _procs_lock:
        _procs.add(proc)
    _attach_stderr(proc)
    return proc


def _attach_stderr(proc: subprocess.Popen[bytes]) -> None:
    buf = bytearray()

    def run() -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = proc.stderr.read(8192)
            if not chunk:
                break
            buf.extend(chunk)

    thread = threading.Thread(target=run, name="hfmd-curl-err", daemon=True)
    thread.start()
    _stderr_bufs[id(proc)] = buf
    _stderr_threads[id(proc)] = thread


def _finish(proc: subprocess.Popen[bytes]) -> None:
    _wait_quietly(proc)
    thread = _stderr_threads.pop(id(proc), None)
    if thread is not None:
        thread.join(timeout=5)
    if proc.stdout is not None:
        proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()
    with _procs_lock:
        _procs.discard(proc)


def _stderr_text(proc: subprocess.Popen[bytes]) -> str:
    thread = _stderr_threads.get(id(proc))
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)
    return bytes(_stderr_bufs.get(id(proc), b"")).decode("utf-8", "replace").strip()


def _read_stdout(stdout, incoming: queue.Queue[bytes | BaseException | None]) -> None:
    try:
        while True:
            chunk = stdout.read(_READ_SIZE)
            if not chunk:
                incoming.put(None)
                return
            incoming.put(chunk)
    except BaseException as exc:  # noqa: BLE001 - forward to the consumer
        incoming.put(exc)


def _consume(
    proc: subprocess.Popen[bytes],
    incoming: queue.Queue[bytes | BaseException | None],
    dest: Path,
    *,
    expected: int,
    partial: bool,
    header_path: Path,
    limiter: BandwidthLimiter,
    cancel: threading.Event | None,
    stall_seconds: float,
    on_chunk,
) -> int:
    got = 0
    with dest.open("wb") as handle:
        while got < expected:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled("已取消")
            status, _headers = _peek_status(header_path)
            if partial and status == 200:
                proc.kill()
                raise RangeIgnored("服务器忽略了 Range")
            try:
                item = incoming.get(timeout=stall_seconds)
            except queue.Empty:
                proc.kill()
                raise CurlError(f"{int(stall_seconds)} 秒没有收到数据，已断开这次传输")
            if isinstance(item, BaseException):
                raise CurlError(str(item)) from item
            if item is None:
                break
            piece = item
            if got + len(piece) > expected:
                piece = piece[: expected - got]
            limiter.acquire(len(piece))
            handle.write(piece)
            got += len(piece)
            if on_chunk is not None and piece:
                on_chunk(len(piece))
    return got


def _peek_status(header_path: Path) -> tuple[int | None, dict[str, str]]:
    if not header_path.exists():
        return None, {}
    try:
        text = header_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, {}
    status, headers = _final_headers_from_text(text)
    if status in {301, 302, 303, 307, 308}:
        return None, headers
    return status, headers


def _final_headers(header_path: Path) -> tuple[int | None, dict[str, str]]:
    if not header_path.exists():
        return None, {}
    text = header_path.read_text(encoding="utf-8", errors="replace")
    return _final_headers_from_text(text)


def _final_headers_from_text(text: str) -> tuple[int | None, dict[str, str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("HTTP/") and current:
            blocks.append(current)
            current = [line]
        else:
            if line.startswith("HTTP/") or current:
                current.append(line)
    if current:
        blocks.append(current)
    if not blocks:
        return None, {}
    last = blocks[-1]
    parts = last[0].split()
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        return None, {}
    try:
        status = int(parts[1])
    except ValueError:
        return None, {}
    headers: dict[str, str] = {}
    for line in last[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return status, headers


def _check_content_range(headers: dict[str, str], start: int, end: int, total_size: int) -> None:
    value = headers.get("content-range", "")
    prefix = "bytes "
    if not value.startswith(prefix) or "/" not in value:
        raise CurlError(f"Content-Range 无法识别：{value}")
    span, total_text = value[len(prefix) :].split("/", 1)
    got_start_text, got_end_text = span.split("-", 1)
    got_start = int(got_start_text)
    got_end = int(got_end_text)
    if got_start != start or got_end != end - 1:
        raise CurlError(f"Content-Range {value} 与请求的 {start}-{end - 1} 不一致")
    if total_text != "*" and int(total_text) != total_size:
        raise CurlError(f"Content-Range 总长度 {total_text} 与清单中的 {total_size} 不一致")


def _wait_proc(proc: subprocess.Popen[bytes], cancel: threading.Event | None, timeout: float) -> None:
    deadline = timeout
    step = 0.2
    waited = 0.0
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            raise Cancelled("已取消")
        try:
            proc.wait(timeout=step)
        except subprocess.TimeoutExpired:
            waited += step
            if waited >= deadline:
                proc.kill()
                raise CurlError("请求超时")


def _wait_quietly(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _raise_if_failed(proc: subprocess.Popen[bytes], header_path: Path) -> None:
    if proc.returncode == 0:
        return
    status, _headers = _final_headers(header_path)
    message = _stderr_text(proc).splitlines()
    tail = message[-1] if message else ""
    if status is not None:
        raise CurlError(f"HTTP {status} {tail}".strip())
    raise CurlError(tail or f"curl 退出码 {proc.returncode}")
