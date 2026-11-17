"""The only place this program is allowed to touch the network.

Do not replace this with urllib, requests, httpx, the operating system's curl,
or a curl found on PATH. Those pick up HTTP_PROXY, curlrc, or the system
proxy. hf-mirror rejects proxied downloads, and a proxy would also bill the
traffic.

Every transfer uses the curl 8.23.0 build shipped in third_party/curl.
Windows uses curl.exe plus libcurl-x64.dll. Apple silicon uses
third_party/curl/macos-arm64/curl. The child process gets an allowlisted
environment with no proxy variables, and the argument list always starts with:

    <bundled curl> -q --noproxy * --proxy <empty> --proto ... --proto-redir ...

-q is first so curl does not read _curlrc or .curlrc.
--noproxy * turns proxy use off for every host.
--proxy "" overrides a proxy that was still configured.
The environment allowlist means the child cannot see HTTP(S)_PROXY,
ALL_PROXY, CURL_HOME, or a home directory that would hold a curlrc.
"""

from __future__ import annotations

import hashlib
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hfmd.limiter import BandwidthLimiter

PINNED_VERSION = "8.23.0"
PINNED_BUILD = "8.23.0_9"
MACOS_ARM64_BUILD = "8.23.0-macos-arm64"
# SHA256 of third_party/curl/macos-arm64/curl, including its linker signature.
_MACOS_ARM64_SHA256 = "a9a9a678ac276b6232dbbe0eac0e981d98082dd6fdddd16f77a1ed1205e898c9"
_CURL_SHA256 = "3c871d76f592b7b2050c6070a293d296c9fd67a0b3d165b2fa4cd7d7e4a02f58"
_DLL_SHA256 = "097a9d6f0b204deacf3953c43376bde1d012762c9798c238c69f60b006e44965"
_SCUTIL_ENABLE = frozenset({
    "HTTPEnable",
    "HTTPSEnable",
    "SOCKSEnable",
    "ProxyAutoConfigEnable",
})
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

    def __init__(self, message: str, index: int = 0, written: int = 0) -> None:
        super().__init__(message)
        self.index = index
        self.written = written


class PieceFailed(CurlError):
    """One piece in a multi-range curl process failed. Earlier pieces are kept."""

    def __init__(self, message: str, index: int = 0, written: int = 0) -> None:
        super().__init__(message)
        self.index = index
        self.written = written


class SlowTransfer(PieceFailed):
    """This connection is far behind the others, so its process was closed."""


@dataclass(frozen=True)
class Piece:
    url: str
    start: int
    end: int
    total_size: int
    dest: Path
    offset: int


@dataclass
class TransferResult:
    status: int
    nbytes: int
    headers: dict[str, str]
    path: Path


def curl_dir() -> Path:
    return _CURL_DIR


def bundled_curl_label() -> str:
    """Build id shown to people. Windows keeps the official 8.23.0_9 id."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return MACOS_ARM64_BUILD
    return PINNED_BUILD


def curl_exe() -> Path:
    if _platform_kind() == "win32":
        return _CURL_DIR / "curl.exe"
    return _CURL_DIR / "macos-arm64" / "curl"


def child_env() -> dict[str, str]:
    """Environment for a curl child. Proxy variables are not copied, even if set."""
    if _platform_kind() == "win32":
        source = {key.upper(): value for key, value in os.environ.items()}
        return _windows_child_env(source)
    return _macos_child_env()


def _windows_child_env(source: dict[str, str]) -> dict[str, str]:
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


def _macos_child_env() -> dict[str, str]:
    # Fixed values. The parent TMPDIR often sits in a per-user directory.
    return {"PATH": "/usr/bin:/bin", "TMPDIR": "/tmp"}


def _platform_kind() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "macos-arm64"
    raise CurlError("当前只提供 Windows x64 和 Apple 芯片（arm64）的内置 curl。")


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
        kind = _platform_kind()
        exe = curl_exe()
        if kind == "win32":
            dll = _CURL_DIR / "libcurl-x64.dll"
            if not exe.is_file() or not dll.is_file():
                raise CurlError(
                    "内置 curl 不完整。需要 third_party/curl/curl.exe 和同目录的 libcurl-x64.dll，"
                    "不能改用系统里的 curl。"
                )
            _check_hash(exe, _CURL_SHA256, PINNED_BUILD)
            _check_hash(dll, _DLL_SHA256, PINNED_BUILD)
        else:
            if not _MACOS_ARM64_SHA256:
                raise CurlError(
                    "Apple 芯片的内置 curl 还没有登记 SHA256。"
                    "需要 third_party/curl/macos-arm64/curl，不能改用系统里的 curl。"
                )
            if not exe.is_file():
                raise CurlError(
                    "内置 curl 不完整。需要 third_party/curl/macos-arm64/curl，"
                    "不能改用系统里的 curl。"
                )
            _check_hash(exe, _MACOS_ARM64_SHA256, MACOS_ARM64_BUILD)
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
    stall_seconds: float = 60,
    on_chunk: callable | None = None,
) -> TransferResult:
    """Download bytes [start, end) into a new file, throttled by the shared limiter."""
    piece = Piece(
        url=url,
        start=start,
        end=end,
        total_size=total_size,
        dest=dest,
        offset=0,
    )
    status, headers, nbytes = download_pieces(
        [piece],
        token=token,
        allow_loopback_http=allow_loopback_http,
        limiter=limiter,
        cancel=cancel,
        stall_seconds=stall_seconds,
        on_chunk=on_chunk,
        discard_on_failure=True,
    )
    return TransferResult(status=status, nbytes=nbytes, headers=headers, path=dest)


def piece_command(
    pieces: list[Piece],
    *,
    allow_loopback_http: bool,
    header_paths: list[Path],
    token: str | None,
) -> list[str]:
    """One curl process, several ranges, so the TCP connection can be reused.

    --next puts curl back to defaults, which would drop the proxy lock.
    Every range repeats --noproxy, --proxy "", and the protocol lock.
    There is no --limit-rate: a slow connection must not freeze the budget
    of a faster one. The shared token bucket is the only cap.
    """
    if token and ("\r" in token or "\n" in token):
        raise CurlError("token 含有换行，已拒绝")
    proto = "=http,https" if allow_loopback_http else "=https"
    cmd = command_prefix(allow_loopback_http=allow_loopback_http)
    for index, piece in enumerate(pieces):
        if index:
            cmd.append("--next")
        partial = not (piece.start == 0 and piece.end == piece.total_size)
        cmd += [
            "--location",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "20",
            "--noproxy",
            "*",
            "--proxy",
            "",
            "--proto",
            proto,
            "--proto-redir",
            proto,
            "-A",
            "hfmd/1.0",
            "-D",
            str(header_paths[index]),
            "--output",
            "-",
        ]
        if partial:
            cmd += ["--range", f"{piece.start}-{piece.end - 1}"]
        if token:
            cmd += ["-H", f"Authorization: Bearer {token}"]
        cmd.append(piece.url)
    return cmd


def download_pieces(
    pieces: list[Piece],
    *,
    token: str | None,
    allow_loopback_http: bool,
    limiter: BandwidthLimiter,
    cancel: threading.Event | None,
    stall_seconds: float = 60,
    on_chunk: callable | None = None,
    on_piece_done: callable | None = None,
    give_up: callable | None = None,
    discard_on_failure: bool = False,
) -> tuple[int, dict[str, str], int]:
    """Download pieces in order on one curl process.

    Returns the status, headers, and size of the last piece.
    Earlier pieces are flushed and reported through on_piece_done before the
    next range is read, so a later failure does not drop them.
    """
    if not pieces:
        raise CurlError("没有要下载的分片")
    for piece in pieces:
        if piece.end < piece.start:
            raise CurlError("invalid range")
        check_initial_url(piece.url, allow_loopback_http=allow_loopback_http)
        piece.dest.parent.mkdir(parents=True, exist_ok=True)
    if len(pieces) == 1 and pieces[0].end == pieces[0].start:
        pieces[0].dest.write_bytes(b"")
        if on_piece_done is not None:
            on_piece_done(0)
        return 200, {}, 0

    header_paths: list[Path] = []
    for _piece in pieces:
        header_path, _unused = _temp_pair("hfmd-hdr")
        _unused.unlink(missing_ok=True)
        header_paths.append(header_path)
    cmd = piece_command(
        pieces,
        allow_loopback_http=allow_loopback_http,
        header_paths=header_paths,
        token=token,
    )
    verify_bundled_curl()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(),
        creationflags=_creation_flags(),
    )
    with _procs_lock:
        _procs.add(proc)
    _attach_stderr(proc)
    assert proc.stdout is not None
    incoming: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=1)
    reader = threading.Thread(
        target=_read_stdout,
        args=(proc.stdout, incoming),
        name="hfmd-curl-read",
        daemon=True,
    )
    reader.start()
    last_status = 0
    last_headers: dict[str, str] = {}
    last_nbytes = 0
    leftover = bytearray()
    try:
        for index, piece in enumerate(pieces):
            last_status, last_headers, last_nbytes = _read_piece(
                proc,
                incoming,
                piece,
                index=index,
                header_path=header_paths[index],
                limiter=limiter,
                cancel=cancel,
                stall_seconds=stall_seconds,
                on_chunk=on_chunk,
                give_up=give_up,
                discard_on_failure=discard_on_failure and len(pieces) == 1,
                leftover=leftover,
            )
            if on_piece_done is not None:
                on_piece_done(index)
        return last_status, last_headers, last_nbytes
    finally:
        if proc.poll() is None:
            proc.kill()
        deadline = time.monotonic() + 5
        while reader.is_alive() and time.monotonic() < deadline:
            try:
                incoming.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
        reader.join(timeout=1)
        _finish(proc)
        for header_path in header_paths:
            header_path.unlink(missing_ok=True)


def check_initial_url(url: str, *, allow_loopback_http: bool) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if allow_loopback_http and parts.scheme == "http" and host in {"127.0.0.1", "localhost"}:
        return
    if parts.scheme == "https" and (host == "hf-mirror.com" or host.endswith(".hf-mirror.com")):
        return
    raise CurlError("只允许直连 https://hf-mirror.com ，拒绝其它地址")


def system_proxy_enabled() -> bool:
    """True when a system proxy switch is on. The address itself is not read."""
    if sys.platform == "win32":
        return _windows_proxy_enabled()
    if sys.platform == "darwin":
        return _macos_proxy_enabled()
    return False


def proxy_enabled_from_scutil_text(text: str) -> bool:
    """Read only Enable flags from `scutil --proxy`. Never return a proxy address."""
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip() in _SCUTIL_ENABLE and value.strip() == "1":
            return True
    return False


def _windows_proxy_enabled() -> bool:
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


def _macos_proxy_enabled() -> bool:
    try:
        proc = subprocess.run(
            ["/usr/sbin/scutil", "--proxy"],
            capture_output=True,
            env=child_env(),
            creationflags=_creation_flags(),
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return proxy_enabled_from_scutil_text(proc.stdout.decode("utf-8", "replace"))


def _check_hash(path: Path, expected: str, build: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise CurlError(f"{path.name} 的 SHA256 与内置的 {build} 不一致，拒绝运行")


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


def _read_piece(
    proc: subprocess.Popen[bytes],
    incoming: queue.Queue[bytes | BaseException | None],
    piece: Piece,
    *,
    index: int,
    header_path: Path,
    limiter: BandwidthLimiter,
    cancel: threading.Event | None,
    stall_seconds: float,
    on_chunk,
    give_up,
    discard_on_failure: bool,
    leftover: bytearray,
) -> tuple[int, dict[str, str], int]:
    expected = piece.end - piece.start
    partial = not (piece.start == 0 and piece.end == piece.total_size)
    buf = bytearray()
    written = 0
    started = time.monotonic()
    checked = False
    status: int | None = None
    headers: dict[str, str] = {}
    mode = "wb" if discard_on_failure else "r+b"
    handle = piece.dest.open(mode)
    try:
        if mode == "r+b":
            handle.seek(piece.offset)
        while written < expected:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled("已取消")
            status, headers, _seen = _status_after(header_path, 0)
            if partial and status == 200:
                proc.kill()
                raise RangeIgnored("服务器忽略了 Range", index, written)
            if status is not None and status >= 400:
                proc.kill()
                raise PieceFailed(f"HTTP {status}", index, written)
            if status == 206 and not checked:
                try:
                    _check_content_range(headers, piece.start, piece.end, piece.total_size)
                except CurlError as exc:
                    proc.kill()
                    raise PieceFailed(str(exc), index, written) from exc
                checked = True
            if status is not None and status not in {200, 206}:
                proc.kill()
                raise PieceFailed(f"HTTP {status}", index, written)
            if give_up is not None and give_up(written + len(buf), time.monotonic() - started):
                proc.kill()
                raise SlowTransfer("这条连接明显慢于其它连接，已换一条", index, written)
            if status in {200, 206} and (buf or leftover):
                pending = bytes(leftover) + bytes(buf)
                leftover.clear()
                buf.clear()
                _accept_body(handle, pending, expected, written, leftover, limiter, on_chunk)
                written += min(len(pending), expected - written)
                if written >= expected:
                    break
                continue
            if written >= expected:
                break
            if leftover:
                item = bytes(leftover)
                leftover.clear()
            else:
                try:
                    item = incoming.get(timeout=stall_seconds)
                except queue.Empty:
                    proc.kill()
                    raise PieceFailed(f"{int(stall_seconds)} 秒没有收到数据，已断开这次传输", index, written)
                if isinstance(item, BaseException):
                    raise PieceFailed(str(item), index, written) from item
                if item is None:
                    break
            if status in {200, 206}:
                before = written
                _accept_body(handle, item, expected, written, leftover, limiter, on_chunk)
                written += min(len(item), expected - before)
            else:
                buf.extend(item)
                if len(buf) > 1024 * 1024:
                    proc.kill()
                    raise PieceFailed("没有收到 HTTP 状态", index, written)
        if status is None:
            status, headers, _seen = _status_after(header_path, 0)
        if partial and status == 200:
            proc.kill()
            raise RangeIgnored("服务器忽略了 Range", index, written)
        if status == 206 and not checked:
            try:
                _check_content_range(headers, piece.start, piece.end, piece.total_size)
            except CurlError as exc:
                proc.kill()
                raise PieceFailed(str(exc), index, written) from exc
            checked = True
        if status in {200, 206} and buf:
            pending = bytes(buf)
            buf.clear()
            before = written
            _accept_body(handle, pending, expected, written, leftover, limiter, on_chunk)
            written += min(len(pending), expected - before)
        if written != expected:
            proc.kill()
            detail = _stderr_text(proc).splitlines()
            tail = detail[-1] if detail else "连接在分片完成前关闭"
            if "http://" in tail or "https://" in tail:
                tail = "连接在分片完成前关闭"
            raise PieceFailed(tail, index, written)
        if status is None:
            status, headers, _seen = _status_after(header_path, 0)
        if status is None:
            proc.kill()
            raise PieceFailed("curl 没有返回 HTTP 状态", index, written)
        if partial and status != 206:
            proc.kill()
            raise RangeIgnored(f"分段请求得到 HTTP {status}，服务器没有按 Range 返回", index, written)
        handle.flush()
        os.fsync(handle.fileno())
        return status, headers, written
    except Exception:
        if discard_on_failure:
            handle.close()
            piece.dest.unlink(missing_ok=True)
        raise
    finally:
        if not handle.closed:
            handle.close()


def _accept_body(handle, payload: bytes, expected: int, written: int, leftover: bytearray, limiter: BandwidthLimiter, on_chunk) -> None:
    need = expected - written
    if need < 0:
        need = 0
    chunk = payload[:need]
    rest = payload[need:]
    if chunk:
        _write_limited(handle, chunk, limiter, on_chunk)
    if rest:
        leftover.extend(rest)


def _write_limited(handle, payload: bytes, limiter: BandwidthLimiter, on_chunk) -> None:
    view = memoryview(payload)
    while view:
        step = view[:_READ_SIZE]
        limiter.acquire(len(step))
        handle.write(step)
        if on_chunk is not None and step:
            on_chunk(len(step))
        view = view[len(step) :]


def _status_after(header_path: Path, skip: int) -> tuple[int | None, dict[str, str], int]:
    if not header_path.exists():
        return None, {}, 0
    try:
        text = header_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, {}, 0
    blocks = _header_blocks(text)
    if len(blocks) < skip:
        skip = 0
    status: int | None = None
    headers: dict[str, str] = {}
    consumed = skip
    for offset, (code, found) in enumerate(blocks[skip:]):
        if code in {301, 302, 303, 307, 308}:
            continue
        status, headers = code, found
        consumed = skip + offset + 1
        break
    return status, headers, consumed


def _header_blocks(text: str) -> list[tuple[int, dict[str, str]]]:
    raw: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("HTTP/") and current:
            raw.append(current)
            current = [line]
        elif line.startswith("HTTP/") or current:
            current.append(line)
    if current:
        raw.append(current)
    parsed: list[tuple[int, dict[str, str]]] = []
    for block in raw:
        parts = block[0].split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            continue
        try:
            code = int(parts[1])
        except ValueError:
            continue
        headers: dict[str, str] = {}
        for line in block[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        parsed.append((code, headers))
    return parsed


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
