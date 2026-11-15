"""List a model on hf-mirror and download it with direct, bandwidth-capped curl transfers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit

from hfmd.curlsafe import (
    Cancelled,
    CurlError,
    RangeIgnored,
    check_initial_url,
    download_to_file,
    get_bytes,
    kill_all,
    system_proxy_enabled,
    verify_bundled_curl,
)
from hfmd.limiter import BandwidthLimiter

ENDPOINT = "https://hf-mirror.com"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,96}/[A-Za-z0-9][A-Za-z0-9._-]{0,96}$")
_REVISION_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MIN_CHUNK = 4 * 1024 * 1024
_MAX_CHUNK = 32 * 1024 * 1024


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class _Task:
    file: RemoteFile
    start: int
    end: int


@dataclass
class Progress:
    phase: str
    files_done: int
    files_total: int
    bytes_done: int
    bytes_total: int
    speed_mbps: float
    message: str
    dest: str


ProgressCallback = Callable[[Progress], None]


def parse_model(model: str) -> tuple[str, str]:
    text = model.strip()
    if not _MODEL_RE.fullmatch(text):
        raise DownloadError("模型名必须是 provider/model_name，例如 Qwen/Qwen2.5-7B-Instruct")
    provider, name = text.split("/", 1)
    return provider, name


def parse_revision(revision: str) -> str:
    text = revision.strip() or "main"
    if not _REVISION_RE.fullmatch(text):
        raise DownloadError("revision 只允许字母、数字、点、下划线和连字符")
    return text


def destination_for(download_home: Path, model: str) -> Path:
    provider, name = parse_model(model)
    home = Path(download_home).expanduser().resolve()
    dest = (home / provider / name).resolve()
    if dest != home and home not in dest.parents:
        raise DownloadError("保存路径越出了 download_home")
    return dest


def download_model(
    model: str,
    concurrency: int,
    bandwidth_mbps: float,
    download_home: Path,
    *,
    revision: str = "main",
    token: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    endpoint: str = ENDPOINT,
    allow_loopback_http: bool = False,
) -> Path:
    provider, name = parse_model(model)
    revision = parse_revision(revision)
    if concurrency < 1 or concurrency > 64:
        raise DownloadError("并发数必须是 1 到 64 的整数")
    if bandwidth_mbps <= 0 or bandwidth_mbps > 100_000:
        raise DownloadError("总带宽必须大于 0，单位是 Mbps（兆比特/秒）")
    if token is not None:
        token = token.strip() or None
    endpoint = _check_endpoint(endpoint, allow_loopback_http)
    dest = destination_for(download_home, f"{provider}/{name}")
    dest.mkdir(parents=True, exist_ok=True)
    meta_dir = dest / ".hfmd"
    meta_dir.mkdir(exist_ok=True)
    verify_bundled_curl()

    def emit(phase: str, message: str, files_done: int = 0, files_total: int = 0,
             bytes_done: int = 0, bytes_total: int = 0, speed: float = 0.0) -> None:
        if progress is None:
            return
        progress(Progress(phase, files_done, files_total, bytes_done, bytes_total, speed, message, str(dest)))

    note = "Windows 系统代理开着，但下载不会使用它。" if system_proxy_enabled() else "未使用系统代理。"
    emit("start", f"直连 {endpoint}。内置 curl，代理已屏蔽。{note}")
    if cancel is not None and cancel.is_set():
        raise Cancelled("已取消")
    emit("listing", "正在获取文件清单")
    files = list_files(
        f"{provider}/{name}",
        revision,
        token=token,
        endpoint=endpoint,
        allow_loopback_http=allow_loopback_http,
        cancel=cancel,
    )
    if not files:
        raise DownloadError("仓库里没有可下载的文件")
    total_bytes = sum(item.size for item in files)
    emit("listing", f"共 {len(files)} 个文件，{total_bytes} 字节", 0, len(files), 0, total_bytes)

    state_path = meta_dir / "state.json"
    state = _load_state(state_path, f"{provider}/{name}", revision)
    _reconcile(dest, files, state)
    _save_state(state_path, state)
    baseline = _completed_bytes(files, state)
    meter = _Meter()
    limiter = BandwidthLimiter(bandwidth_mbps * 1_000_000)
    per_rate = max(1, math.ceil((bandwidth_mbps * 1_000_000 / 8) / concurrency))
    tasks = [task for task in _plan(files, concurrency) if not _task_done(state, task)]
    file_locks = {item.path: threading.Lock() for item in files}
    ignored: set[str] = set()
    errors: list[BaseException] = []
    lock = threading.Lock()
    cancel = cancel or threading.Event()
    work: queue.Queue[_Task | None] = queue.Queue()
    for task in tasks:
        work.put(task)

    last_emit = 0.0

    def emit_progress(message: str) -> None:
        nonlocal last_emit
        now = time.monotonic()
        if now - last_emit < 0.3 and message == "":
            return
        last_emit = now
        done_files = sum(1 for item in files if state["files"].get(item.path, {}).get("complete"))
        emit(
            "downloading",
            message,
            done_files,
            len(files),
            min(total_bytes, baseline + meter.session),
            total_bytes,
            meter.speed_mbps(),
        )

    def worker() -> None:
        while True:
            task = work.get()
            try:
                if task is None:
                    return
                if cancel.is_set() or errors:
                    continue
                if task.file.path in ignored and not _whole(task):
                    continue
                if _task_done(state, task):
                    continue
                _run_task(
                    task,
                    dest=dest,
                    endpoint=endpoint,
                    revision=revision,
                    token=token,
                    allow_loopback_http=allow_loopback_http,
                    limiter=limiter,
                    limit_rate=per_rate,
                    cancel=cancel,
                    state=state,
                    state_path=state_path,
                    state_lock=lock,
                    file_lock=file_locks[task.file.path],
                    meter=meter,
                    on_progress=lambda message: emit_progress(message),
                )
            except RangeIgnored:
                with lock:
                    if task.file.path not in ignored:
                        ignored.add(task.file.path)
                        _reset_file(dest, task.file, state)
                        _save_state(state_path, state)
                        work.put(_Task(task.file, 0, task.file.size))
            except Cancelled:
                cancel.set()
            except Exception as exc:  # noqa: BLE001 - keep the first worker error
                errors.append(exc)
                cancel.set()
                kill_all()
            finally:
                work.task_done()

    if tasks:
        threads = [
            threading.Thread(target=worker, name=f"hfmd-{index}", daemon=True)
            for index in range(max(1, min(concurrency, len(tasks))))
        ]
        for thread in threads:
            thread.start()
        work.join()
        for _ in threads:
            work.put(None)
        for thread in threads:
            thread.join(timeout=5)
    if cancel.is_set() and not errors:
        raise Cancelled("已取消")
    if errors:
        raise DownloadError(str(errors[0])) from errors[0]

    for item in files:
        _verify_file(dest, item, state, state_path)
    emit(
        "done",
        "下载完成",
        len(files),
        len(files),
        total_bytes,
        total_bytes,
        meter.speed_mbps(),
    )
    return dest


def list_files(
    model: str,
    revision: str,
    *,
    token: str | None,
    endpoint: str,
    allow_loopback_http: bool,
    cancel: threading.Event | None = None,
) -> list[RemoteFile]:
    repo = _repo_path(model)
    revision = parse_revision(revision)
    meta_url = f"{endpoint}/api/models/{repo}/revision/{quote(revision, safe='')}?blobs=true"
    body, _headers = get_bytes(
        meta_url,
        token=token,
        allow_loopback_http=allow_loopback_http,
        cancel=cancel,
    )
    try:
        meta = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DownloadError("模型信息不是合法的 JSON") from exc
    _reject_if_gated(meta, token)
    siblings = _files_from_siblings(meta.get("siblings") or [])
    treesize = _try_treesize(
        endpoint, repo, revision, token, allow_loopback_http, cancel
    )
    if treesize is not None and siblings and sum(item.size for item in siblings) == treesize:
        return siblings
    try:
        walked = _walk_tree(endpoint, repo, revision, token, allow_loopback_http, cancel)
    except (CurlError, DownloadError):
        walked = []
    if walked:
        return walked
    if siblings:
        return siblings
    raise DownloadError("没有获取到文件清单")


def _check_endpoint(endpoint: str, allow_loopback_http: bool) -> str:
    endpoint = endpoint.rstrip("/")
    parts = urlsplit(endpoint)
    if allow_loopback_http:
        if parts.scheme == "http" and (parts.hostname or "") in {"127.0.0.1", "localhost"}:
            return endpoint
        raise DownloadError("测试模式只允许 http://127.0.0.1")
    if parts.scheme == "https" and parts.hostname == "hf-mirror.com":
        return endpoint
    raise DownloadError("只允许直连 https://hf-mirror.com")


def _repo_path(model: str) -> str:
    provider, name = parse_model(model)
    return f"{quote(provider, safe='')}/{quote(name, safe='')}"


def _reject_if_gated(meta: dict, token: str | None) -> None:
    gated = meta.get("gated", False)
    if gated in (False, None, "false", "False"):
        return
    if token:
        return
    raise DownloadError(
        "这个模型需要访问许可。请先在 Hugging Face 官网申请，再用 --token 传入 access token。"
        "hf-mirror 不提供网页登录。"
    )


def _try_treesize(endpoint, repo, revision, token, allow_loopback_http, cancel) -> int | None:
    url = f"{endpoint}/api/models/{repo}/treesize/{quote(revision, safe='')}"
    try:
        body, _headers = get_bytes(
            url, token=token, allow_loopback_http=allow_loopback_http, cancel=cancel
        )
    except CurlError:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None
    size = payload.get("size") if isinstance(payload, dict) else None
    if isinstance(size, int) and size >= 0:
        return size
    return None


def _walk_tree(endpoint, repo, revision, token, allow_loopback_http, cancel) -> list[RemoteFile]:
    url = f"{endpoint}/api/models/{repo}/tree/{quote(revision, safe='')}?recursive=true&expand=false"
    found: list[RemoteFile] = []
    seen: set[str] = set()
    for _page in range(10000):
        if cancel is not None and cancel.is_set():
            raise Cancelled("已取消")
        body, headers = get_bytes(
            url, token=token, allow_loopback_http=allow_loopback_http, cancel=cancel
        )
        try:
            page = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DownloadError("文件树不是合法的 JSON") from exc
        if not isinstance(page, list):
            raise DownloadError("文件树格式无法识别")
        for item in page:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            remote = _file_from_mapping(str(item.get("path") or ""), item)
            if remote.path not in seen:
                seen.add(remote.path)
                found.append(remote)
        nxt = _next_link(headers, url)
        if not nxt:
            return found
        check_initial_url(nxt, allow_loopback_http=allow_loopback_http)
        url = nxt
    raise DownloadError("文件树分页过多，已中止")


def _next_link(header_text: str, current_url: str) -> str | None:
    links: list[str] = []
    for line in header_text.splitlines():
        if line.lower().startswith("link:"):
            links.append(line.split(":", 1)[1])
    for part in ",".join(links).split(","):
        folded = part.replace(" ", "").lower()
        if 'rel="next"' not in folded and "rel=next" not in folded:
            continue
        match = re.search(r"<([^>]+)>", part)
        if match:
            return urljoin(current_url, match.group(1))
    return None


def _files_from_siblings(siblings: list) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    seen: set[str] = set()
    for item in siblings:
        if not isinstance(item, dict):
            continue
        remote = _file_from_mapping(str(item.get("rfilename") or ""), item)
        if remote.path in seen:
            continue
        seen.add(remote.path)
        files.append(remote)
    return files


def _file_from_mapping(path: str, item: dict) -> RemoteFile:
    safe = _safe_rel(path)
    lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
    size = lfs.get("size")
    if not isinstance(size, int):
        size = item.get("size")
    if not isinstance(size, int) or size < 0:
        raise DownloadError(f"文件大小未知：{path}")
    sha = lfs.get("sha256") if isinstance(lfs.get("sha256"), str) else ""
    oid = lfs.get("oid") if isinstance(lfs.get("oid"), str) else ""
    if not sha and oid.startswith("sha256:"):
        sha = oid.split(":", 1)[1]
    if sha and not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        sha = ""
    return RemoteFile(safe, size, sha.lower() if sha else None)


def _safe_rel(path: str) -> str:
    if not path or "\\" in path or ":" in path or path.startswith("/") or "\x00" in path:
        raise DownloadError(f"拒绝不安全的文件路径：{path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DownloadError(f"拒绝不安全的文件路径：{path!r}")
    return "/".join(parts)


def _plan(files: list[RemoteFile], concurrency: int) -> list[_Task]:
    tasks: list[_Task] = []
    for item in files:
        if item.size <= 0:
            tasks.append(_Task(item, 0, 0))
            continue
        piece = item.size
        if item.size > _MIN_CHUNK and concurrency > 1:
            fair = math.ceil(item.size / concurrency)
            piece = max(_MIN_CHUNK, min(_MAX_CHUNK, fair))
        start = 0
        while start < item.size:
            end = min(item.size, start + piece)
            tasks.append(_Task(item, start, end))
            start = end
    return tasks


def _whole(task: _Task) -> bool:
    return task.start == 0 and task.end == task.file.size


def _load_state(path: Path, model: str, revision: str) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if data.get("model") == model and data.get("revision") == revision and isinstance(data.get("files"), dict):
            return data
    return {"model": model, "revision": revision, "files": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _record(state: dict, item: RemoteFile) -> dict:
    files = state["files"]
    current = files.get(item.path)
    if not isinstance(current, dict) or current.get("size") != item.size or current.get("sha256") != item.sha256:
        current = {"size": item.size, "sha256": item.sha256, "ranges": [], "complete": False}
        files[item.path] = current
    current.setdefault("ranges", [])
    current.setdefault("complete", False)
    return current


def _reconcile(dest: Path, files: list[RemoteFile], state: dict) -> None:
    for item in files:
        record = _record(state, item)
        path = _local_path(dest, item.path)
        if record.get("complete") and path.is_file() and path.stat().st_size == item.size:
            continue
        record["complete"] = False
        if path.is_file() and path.stat().st_size != item.size:
            path.unlink()
            record["ranges"] = []
        if not path.exists():
            record["ranges"] = []


def _completed_bytes(files: list[RemoteFile], state: dict) -> int:
    total = 0
    for item in files:
        record = state["files"].get(item.path) or {}
        if record.get("complete"):
            total += item.size
            continue
        for start, end in record.get("ranges") or []:
            total += max(0, int(end) - int(start))
    return total


def _task_done(state: dict, task: _Task) -> bool:
    record = state["files"].get(task.file.path) or {}
    if record.get("complete"):
        return True
    if task.start == task.end:
        return False
    for start, end in record.get("ranges") or []:
        if int(start) <= task.start and int(end) >= task.end:
            return True
    return False


def _reset_file(dest: Path, item: RemoteFile, state: dict) -> None:
    record = _record(state, item)
    record["ranges"] = []
    record["complete"] = False
    path = _local_path(dest, item.path)
    if path.exists():
        path.unlink()


def _local_path(dest: Path, rel: str) -> Path:
    path = dest.joinpath(*rel.split("/")).resolve()
    if dest not in path.parents and path != dest:
        raise DownloadError(f"拒绝写出目录：{rel}")
    return path


def _run_task(
    task: _Task,
    *,
    dest: Path,
    endpoint: str,
    revision: str,
    token: str | None,
    allow_loopback_http: bool,
    limiter: BandwidthLimiter,
    limit_rate: int,
    cancel: threading.Event,
    state: dict,
    state_path: Path,
    state_lock: threading.Lock,
    file_lock: threading.Lock,
    meter: _Meter,
    on_progress: Callable[[str], None],
) -> None:
    target = _local_path(dest, task.file.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if task.end == task.start:
        target.write_bytes(b"")
        with state_lock:
            record = _record(state, task.file)
            record["complete"] = True
            _save_state(state_path, state)
        on_progress(task.file.path)
        return
    with file_lock:
        if not target.exists() or target.stat().st_size > task.file.size:
            with target.open("wb") as handle:
                handle.truncate(task.file.size)
        elif target.stat().st_size < task.file.size:
            with target.open("r+b") as handle:
                handle.truncate(task.file.size)
    url = f"{endpoint}/{_repo_path(state['model'])}/resolve/{quote(revision, safe='')}/{quote(task.file.path, safe='/')}"
    part_dir = dest / ".hfmd" / "parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    part = part_dir / f"{_part_name(task)}.part"
    delay = 1.0
    for attempt in range(1, 5):
        if cancel.is_set():
            raise Cancelled("已取消")
        try:
            download_to_file(
                url,
                part,
                start=task.start,
                end=task.end,
                total_size=task.file.size,
                token=token,
                allow_loopback_http=allow_loopback_http,
                limiter=limiter,
                cancel=cancel,
                limit_rate=limit_rate,
                on_chunk=meter.add,
            )
            break
        except RangeIgnored:
            part.unlink(missing_ok=True)
            raise
        except Cancelled:
            part.unlink(missing_ok=True)
            raise
        except CurlError:
            part.unlink(missing_ok=True)
            if attempt == 4:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 8)
    with file_lock:
        with target.open("r+b") as out, part.open("rb") as src:
            out.seek(task.start)
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    part.unlink(missing_ok=True)
    with state_lock:
        record = _record(state, task.file)
        ranges = [tuple(pair) for pair in record.get("ranges") or []]
        ranges.append((task.start, task.end))
        record["ranges"] = _merge_ranges(ranges)
        if _covers(record["ranges"], task.file.size):
            record["complete"] = _hash_matches(target, task.file)
            if not record["complete"] and task.file.sha256:
                target.unlink(missing_ok=True)
                record["ranges"] = []
                _save_state(state_path, state)
                raise DownloadError(f"校验失败：{task.file.path}")
        _save_state(state_path, state)
    on_progress(task.file.path)


def _verify_file(dest: Path, item: RemoteFile, state: dict, state_path: Path) -> None:
    record = _record(state, item)
    path = _local_path(dest, item.path)
    if not path.is_file() or path.stat().st_size != item.size:
        raise DownloadError(f"文件不完整：{item.path}")
    if not _hash_matches(path, item):
        path.unlink(missing_ok=True)
        record["complete"] = False
        record["ranges"] = []
        _save_state(state_path, state)
        raise DownloadError(f"校验失败：{item.path}。请再运行一次以下载该文件。")
    record["complete"] = True
    _save_state(state_path, state)


def _hash_matches(path: Path, item: RemoteFile) -> bool:
    if not item.sha256:
        return path.is_file() and path.stat().st_size == item.size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == item.sha256


def _covers(ranges: list, size: int) -> bool:
    cursor = 0
    for start, end in sorted((int(a), int(b)) for a, b in ranges):
        if start > cursor:
            return False
        cursor = max(cursor, end)
    return cursor >= size


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def _part_name(task: _Task) -> str:
    flat = task.file.path.replace("/", "__")
    return f"{flat}-{task.start}-{task.end}"


class _Meter:
    def __init__(self) -> None:
        self.session = 0
        self._samples: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def add(self, nbytes: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.session += nbytes
            self._samples.append((now, self.session))
            while self._samples and now - self._samples[0][0] > 2:
                self._samples.popleft()

    def speed_mbps(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            start_t, start_b = self._samples[0]
            end_t, end_b = self._samples[-1]
            elapsed = end_t - start_t
            if elapsed < 0.2:
                return 0.0
            return (end_b - start_b) * 8 / elapsed / 1_000_000
