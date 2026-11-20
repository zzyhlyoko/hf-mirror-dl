import json
import shutil
import socket
import threading
import time
import unittest
from collections import deque
from pathlib import Path

from hfmd.cli import main
from hfmd.download import (
    DownloadError,
    RemoteFile,
    _Task,
    _plan,
    _raise_watermark,
    _remember_recent,
    _splice_prefix,
    _split_task,
    _tail_cut_allowed,
    destination_for,
    download_model,
    parse_model,
)

ROOT = Path(__file__).resolve().parents[1]


class ModelPathTests(unittest.TestCase):
    def test_model_name_and_destination(self) -> None:
        self.assertEqual(parse_model("Qwen/Qwen2.5-7B"), ("Qwen", "Qwen2.5-7B"))
        with self.assertRaises(DownloadError):
            parse_model("gpt2")
        with self.assertRaises(DownloadError):
            parse_model("org/../secret")
        dest = destination_for(ROOT / ".hfmd-test-home", "acme/tiny")
        self.assertEqual(dest, (ROOT / ".hfmd-test-home" / "acme" / "tiny").resolve())

    def test_plan_does_not_redownload_finished_bytes(self) -> None:
        size = 10 * 1024 * 1024
        item = RemoteFile("weights.bin", size, None)
        state = {"files": {"weights.bin": {"complete": False, "size": size, "sha256": None, "ranges": [[0, 4 * 1024 * 1024]]}}}
        tasks = _plan([item], 4, state)
        self.assertTrue(tasks)
        self.assertTrue(all(task.start >= 4 * 1024 * 1024 for task in tasks))
        self.assertEqual(tasks[0].start, 4 * 1024 * 1024)
        self.assertEqual(tasks[-1].end, size)

    def test_split_task_covers_the_range_without_tiny_pieces(self) -> None:
        item = RemoteFile("weights.bin", 32 * 1024 * 1024, None)
        task = _Task(item, 0, 32 * 1024 * 1024, 1, "https://hf-mirror.com/acme/tiny/resolve/main/weights.bin", 2)
        pieces = _split_task(task, 8, task.tries)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(pieces[0].start, 0)
        self.assertEqual(pieces[-1].end, task.end)
        self.assertEqual([piece.start for piece in pieces[1:]], [piece.end for piece in pieces[:-1]])
        self.assertTrue(all(piece.end - piece.start >= 4 * 1024 * 1024 for piece in pieces))
        self.assertTrue(all(piece.url == task.url and piece.hops == 2 and piece.tries == 1 for piece in pieces))
        tiny = _Task(item, 0, 4 * 1024 * 1024)
        self.assertEqual(len(_split_task(tiny, 8, 0)), 1)

    def test_recent_speed_ignores_an_old_fast_start(self) -> None:
        samples: deque[tuple[float, int]] = deque()
        self.assertIsNone(_remember_recent(samples, 0.0, 0))
        self.assertIsNone(_remember_recent(samples, 4.0, 8_000_000))
        stalled = _remember_recent(samples, 16.0, 8_100_000)
        self.assertIsNotNone(stalled)
        assert stalled is not None
        self.assertLess(stalled, 200_000)

    def test_tail_cut_stops_a_slow_link_when_the_pack_is_slow(self) -> None:
        common = dict(
            aggregate_mbps=17.0,
            sustained_mbps=250.0,
            waiting_fraction=0.0,
            fair_bps=2_000_000.0,
            remaining=20 * 1024 * 1024,
            larger_remainders=0,
            cuts_in_window=0,
        )
        self.assertTrue(_tail_cut_allowed(recent_bps=100_000.0, **common))
        self.assertFalse(_tail_cut_allowed(recent_bps=1_800_000.0, **common))
        held = dict(common)
        held["waiting_fraction"] = 0.3
        self.assertFalse(_tail_cut_allowed(recent_bps=100_000.0, **held))
        ceiling = dict(common)
        ceiling["aggregate_mbps"] = 340.0
        ceiling["sustained_mbps"] = 360.0
        self.assertFalse(_tail_cut_allowed(recent_bps=100_000.0, **ceiling))
        short = dict(common)
        short["remaining"] = 1024 * 1024
        self.assertFalse(_tail_cut_allowed(recent_bps=100_000.0, **short))
        capped = dict(common)
        capped["cuts_in_window"] = 4
        self.assertFalse(_tail_cut_allowed(recent_bps=100_000.0, **capped))
        crowded = dict(common)
        crowded["larger_remainders"] = 4
        self.assertFalse(_tail_cut_allowed(recent_bps=100_000.0, **crowded))

    def test_watermark_remembers_a_held_rate_not_a_spike(self) -> None:
        samples: deque[tuple[float, float]] = deque()
        mark = 0.0
        for second in range(12):
            mark = _raise_watermark(samples, float(second), 40.0 if second == 6 else 320.0, mark)
        self.assertGreater(mark, 250.0)
        self.assertLess(mark, 340.0)
        for second in range(12, 40):
            mark = _raise_watermark(samples, float(second), 20.0, mark)
        self.assertGreater(mark, 250.0)

    def test_splice_prefix_keeps_only_the_downloaded_bytes(self) -> None:
        home = ROOT / ".hfmd-test-home"
        home.mkdir(parents=True, exist_ok=True)
        temp = home / "piece"
        target = home / "target"
        temp.write_bytes(b"abcdefghij")
        target.write_bytes(b"\0" * 8)
        moved = _splice_prefix(temp, target, 2, 4)
        self.assertEqual(moved, 4)
        self.assertEqual(target.read_bytes(), b"\0\0abcd\0\0")
        temp.unlink()
        target.unlink()

    def test_cli_requires_concurrency_and_home(self) -> None:
        self.assertEqual(main(["download", "acme/tiny"]), 2)


class LocalMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = ROOT / ".hfmd-test-home"
        shutil.rmtree(self.home, ignore_errors=True)
        self.weights = b"W" * (8 * 1024 * 1024)
        self.note = b"hello"
        self.server = _Mirror(self.weights, self.note)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.addCleanup(lambda: shutil.rmtree(self.home, ignore_errors=True))

    def test_layout_resume_and_total_bandwidth(self) -> None:
        endpoint = f"http://127.0.0.1:{self.server.port}"
        started = time.monotonic()
        speeds: list[float] = []

        def on_progress(progress) -> None:
            if progress.speed_mbps:
                speeds.append(progress.speed_mbps)

        dest = download_model(
            "acme/tiny",
            4,
            80,
            self.home,
            endpoint=endpoint,
            allow_loopback_http=True,
            progress=on_progress,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(dest, (self.home / "acme" / "tiny").resolve())
        self.assertEqual((dest / "weights.bin").read_bytes(), self.weights)
        self.assertEqual((dest / "dir" / "note.txt").read_bytes(), self.note)
        measured = (len(self.weights) + len(self.note)) * 8 / elapsed / 1_000_000
        self.assertLessEqual(measured, 80 * 1.40, f"average {measured:.1f} Mbps, speeds {speeds[-5:]}")
        self.assertGreater(elapsed, 0.5)
        file_hits = self.server.file_hits
        self.server.file_hits = 0
        download_model(
            "acme/tiny",
            4,
            300,
            self.home,
            endpoint=endpoint,
            allow_loopback_http=True,
        )
        self.assertEqual(self.server.file_hits, 0, "completed files were downloaded again")
        self.assertGreater(file_hits, 0)


class _Mirror(threading.Thread):
    def __init__(self, weights: bytes, note: bytes) -> None:
        super().__init__(daemon=True)
        self.weights = weights
        self.note = note
        self.file_hits = 0
        self._lock = threading.Lock()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen()
        self._stop = False
        total = len(weights) + len(note)
        self.meta = json.dumps(
            {
                "gated": False,
                "siblings": [
                    {"rfilename": "weights.bin", "size": len(weights)},
                    {"rfilename": "dir/note.txt", "size": len(note)},
                ],
            }
        ).encode("utf-8")
        self.treesize = json.dumps({"size": total}).encode("utf-8")

    def run(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            request = data.decode("latin1", "replace")
            line = request.split("\r\n", 1)[0]
            target = line.split(" ")[1]
            path = target.split("?", 1)[0]
            headers = {}
            for header in request.split("\r\n")[1:]:
                if ":" in header:
                    name, value = header.split(":", 1)
                    headers[name.lower()] = value.strip()
            if path.endswith("/revision/main"):
                body = self.meta
                status = "200 OK"
                extra = ""
            elif path.endswith("/treesize/main"):
                body = self.treesize
                status = "200 OK"
                extra = ""
            elif path.endswith("/weights.bin"):
                with self._lock:
                    self.file_hits += 1
                body, status, extra = self._ranged(self.weights, headers.get("range"))
            elif path.endswith("/dir/note.txt"):
                with self._lock:
                    self.file_hits += 1
                body, status, extra = self.note, "200 OK", ""
            else:
                body, status, extra = b"missing", "404 Not Found", ""
            head = (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                f"{extra}"
                "\r\n"
            ).encode("ascii")
            conn.sendall(head)
            view = memoryview(body)
            while view:
                sent = conn.send(view[:65536])
                view = view[sent:]
        finally:
            conn.close()

    def _ranged(self, payload: bytes, range_header: str | None) -> tuple[bytes, str, str]:
        if not range_header or not range_header.startswith("bytes="):
            return payload, "200 OK", "Accept-Ranges: bytes\r\n"
        start_text, end_text = range_header.split("=", 1)[1].split("-", 1)
        start = int(start_text)
        end = int(end_text)
        chunk = payload[start : end + 1]
        extra = f"Content-Range: bytes {start}-{end}/{len(payload)}\r\nAccept-Ranges: bytes\r\n"
        return chunk, "206 Partial Content", extra

    def stop(self) -> None:
        self._stop = True
        self._sock.close()


if __name__ == "__main__":
    unittest.main()
