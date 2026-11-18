import json
import shutil
import socket
import threading
import time
import unittest
from pathlib import Path

from hfmd.cli import main
from hfmd.download import DownloadError, RemoteFile, _Task, _plan, _split_task, destination_for, download_model, parse_model

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
