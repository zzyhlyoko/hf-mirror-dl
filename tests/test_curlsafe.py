import ast
import os
import shutil
import socket
import threading
import time
import unittest
from pathlib import Path

from hfmd.curlsafe import (
    PINNED_VERSION,
    child_env,
    command_prefix,
    curl_exe,
    verify_bundled_curl,
)
from hfmd.limiter import BandwidthLimiter

ROOT = Path(__file__).resolve().parents[1]


class BundledCurlTests(unittest.TestCase):
    def test_command_disables_proxy_before_anything_else(self) -> None:
        prefix = command_prefix()
        self.assertEqual(Path(prefix[0]).resolve(), curl_exe().resolve())
        self.assertEqual(prefix[1:6], ["-q", "--noproxy", "*", "--proxy", ""])
        self.assertIn("--proto", prefix)
        self.assertIn("=https", prefix)
        self.assertNotIn("=http,https", prefix)

    def test_child_env_drops_proxy_and_curlrc_locations(self) -> None:
        previous = dict(os.environ)
        try:
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
            os.environ["https_proxy"] = "http://127.0.0.1:9"
            os.environ["ALL_PROXY"] = "socks5h://127.0.0.1:9"
            os.environ["CURL_HOME"] = r"C:\proxy-config"
            os.environ["USERPROFILE"] = r"C:\not-a-profile"
            env = child_env()
        finally:
            os.environ.clear()
            os.environ.update(previous)
        lowered = {key.lower() for key in env}
        self.assertFalse(any("proxy" in key for key in lowered))
        self.assertNotIn("curl_home", lowered)
        self.assertNotIn("userprofile", lowered)
        self.assertNotIn("appdata", lowered)
        self.assertIn("SYSTEMROOT", {key.upper() for key in env})

    def test_bundled_binary_reports_pinned_version(self) -> None:
        line = verify_bundled_curl()
        self.assertTrue(line.startswith(f"curl {PINNED_VERSION} "))

    def test_sources_do_not_open_their_own_network_stack(self) -> None:
        banned = (
            "urllib.request",
            "http.client",
            "import requests",
            "import httpx",
            "import aiohttp",
            "urlopen",
            "System32\\curl",
            "System32/curl",
            "shutil.which",
        )
        for path in (ROOT / "hfmd").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            if path.name != "curlsafe.py":
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                        self.assertNotIn("subprocess", names, path.name)
                    if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                        self.fail(f"{path.name} imports subprocess")
            for word in banned:
                self.assertNotIn(word, text, f"{path.name} contains {word}")


class LimiterTests(unittest.TestCase):
    def test_average_stays_near_the_cap(self) -> None:
        cap_mbps = 80
        limiter = BandwidthLimiter(cap_mbps * 1_000_000)
        nbytes = 20 * 1024 * 1024
        started = time.monotonic()
        left = nbytes
        while left:
            step = min(64 * 1024, left)
            limiter.acquire(step)
            left -= step
        elapsed = time.monotonic() - started
        measured = nbytes * 8 / elapsed / 1_000_000
        self.assertLessEqual(measured, cap_mbps * 1.25, f"measured {measured:.1f} Mbps")
        self.assertGreaterEqual(measured, cap_mbps * 0.70, f"measured {measured:.1f} Mbps")


class DirectTransferTests(unittest.TestCase):
    def test_poisoned_proxy_is_not_contacted(self) -> None:
        from hfmd.curlsafe import download_to_file

        payload = b"direct-ok" * 1000
        hits = {"proxy": 0}
        proxy = _Proxy(hits)
        proxy.start()
        server = _FileServer({"/file.bin": payload})
        server.start()
        self.addCleanup(proxy.stop)
        self.addCleanup(server.stop)
        self.addCleanup(lambda: shutil.rmtree(curlrc_home, ignore_errors=True))
        previous = dict(os.environ)
        curlrc_home = ROOT / ".hfmd-test-curlrc"
        curlrc_home.mkdir(exist_ok=True)
        (curlrc_home / ".curlrc").write_text(
            f'proxy = "http://127.0.0.1:{proxy.port}"\n',
            encoding="utf-8",
        )
        os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{proxy.port}"
        os.environ["http_proxy"] = f"http://127.0.0.1:{proxy.port}"
        os.environ["https_proxy"] = f"http://127.0.0.1:{proxy.port}"
        os.environ["all_proxy"] = f"socks5h://127.0.0.1:{proxy.port}"
        os.environ["CURL_HOME"] = str(curlrc_home)
        dest = ROOT / ".hfmd-test" / "direct.bin"
        dest.parent.mkdir(exist_ok=True)
        try:
            limiter = BandwidthLimiter(1_000 * 1_000_000)
            download_to_file(
                f"http://127.0.0.1:{server.port}/file.bin",
                dest,
                start=0,
                end=len(payload),
                total_size=len(payload),
                token=None,
                allow_loopback_http=True,
                limiter=limiter,
                cancel=None,
                limit_rate=10_000_000,
            )
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(hits["proxy"], 0)
        finally:
            os.environ.clear()
            os.environ.update(previous)
            dest.unlink(missing_ok=True)


class _FileServer(threading.Thread):
    def __init__(self, routes: dict[str, bytes]) -> None:
        super().__init__(daemon=True)
        self.routes = routes
        self.hits: list[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen()
        self._stop = False

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
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n", 1)[0].decode("ascii", "replace")
            path = head.split(" ")[1].split("?", 1)[0]
            self.hits.append(path)
            body = self.routes.get(path, b"")
            header = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            conn.sendall(header + body)
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop = True
        self._sock.close()


class _Proxy(threading.Thread):
    def __init__(self, hits: dict[str, int]) -> None:
        super().__init__(daemon=True)
        self.hits = hits
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen()
        self._stop = False

    def run(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self.hits["proxy"] += 1
            conn.close()

    def stop(self) -> None:
        self._stop = True
        self._sock.close()


if __name__ == "__main__":
    unittest.main()
