"""Exercise the shipped CLI, real Proxelar transports and live public origins."""

import json
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proxy:
    def __init__(self, data, *extra):
        self.data, self.port, self.worker = data, port(), port()
        log_path = data.parent / (data.name + ".log")
        self.log = log_path.open("ab")
        log_start = self.log.tell()
        self.process = subprocess.Popen(
            [
                "uv",
                "run",
                "archivebox-proxy",
                "--data",
                str(data),
                "--port",
                str(self.port),
                "--worker-port",
                str(self.worker),
                *extra,
            ],
            cwd=ROOT,
            stdout=self.log,
            stderr=self.log,
        )
        for _ in range(200):
            if self.process.poll() is not None:
                raise AssertionError((data.parent / (data.name + ".log")).read_text())
            # Existing CA files and the first worker listener do not establish
            # visitor readiness. The CLI reports readiness after both bind.
            if b" INFO Proxy on " in log_path.read_bytes()[log_start:]:
                break
            time.sleep(0.05)
        else:
            self.close()
            raise AssertionError("proxy did not become ready")

    def fetch(self, url, *, payload=None, worker=False, headers=None):
        # curl honors an explicit proxy even when ambient NO_PROXY is set.
        command = [
            "curl",
            "--noproxy",
            "",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--max-time",
            "30",
            "--proxy",
            f"http://127.0.0.1:{self.worker if worker else self.port}",
            "--cacert",
            str(self.data / "ca/proxelar-ca.pem"),
            "-D",
            "-",
        ]
        for k, v in (headers or {}).items():
            command += ["-H", f"{k}: {v}"]
        if payload is not None:
            command += ["--data-binary", payload]
        result = subprocess.run(command + [url], capture_output=True, check=False)
        assert result.returncode == 0, result.stderr.decode() + result.stdout.decode(
            errors="replace"
        )
        raw = result.stdout
        if raw.startswith(b"HTTP/1.1 200 OK\r\n\r\n"):
            raw = raw.split(b"\r\n\r\n", 1)[1]
        fields, content = raw.split(b"\r\n\r\n", 1)
        return fields.lower(), content

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=15)
        self.log.close()


def test_https_cache_survives_restart_and_worker_does_not_enqueue(tmp_path):
    data = tmp_path / "cache"
    p = Proxy(data)
    nav = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    try:
        _, original = p.fetch("https://news.ycombinator.com/", headers=nav)
        fields, second = p.fetch(
            "https://news.ycombinator.com/", worker=True, headers=nav
        )
        assert b"x-archivebox-proxy-cache: hit" in fields
        assert second == original and b"Hacker News" in second
        assert len((data / "navigations.jsonl").read_text().splitlines()) == 1
        with sqlite3.connect(data / "cache.sqlite3") as db:
            assert (
                db.execute("SELECT min(expires-used) FROM responses").fetchone()[0]
                > 86390
            )
    finally:
        p.close()
    p = Proxy(data, "--policy", "offline")
    try:
        fields, replayed = p.fetch("https://news.ycombinator.com/", worker=True)
        assert replayed == original and b"x-archivebox-proxy-cache: hit" in fields
    finally:
        p.close()


def test_post_body_matching_and_cookie_reuse(tmp_path):
    p = Proxy(tmp_path / "cache")
    try:
        url = "https://httpbingo.org/post"
        _, first = p.fetch(url, payload="archivebox-proxy=first")
        assert json.loads(first)["form"]["archivebox-proxy"] == ["first"]
        fields, repeated = p.fetch(
            url,
            payload="archivebox-proxy=first",
            worker=True,
            headers={"Cookie": "different=1"},
        )
        assert repeated == first and b"x-archivebox-proxy-cache: hit" in fields
        _, second = p.fetch(url, payload="archivebox-proxy=second")
        assert json.loads(second)["form"]["archivebox-proxy"] == ["second"]
        assert second != first
    finally:
        p.close()


def test_replay_preserves_header_bytes_and_duplicates(tmp_path):
    p = Proxy(tmp_path / "cache")
    try:
        url = (
            "https://httpbingo.org/response-headers?X-Test=caf%C3%A9"
            "&Set-Cookie=a%3D1&Set-Cookie=b%3D2"
        )
        first, content = p.fetch(url)
        repeated, replayed = p.fetch(url, worker=True)
        for fields in (first, repeated):
            assert b"x-test: caf\xc3\xa9" in fields.split(b"\r\n")
            assert [
                line
                for line in fields.split(b"\r\n")
                if line.startswith(b"set-cookie:")
            ] == [b"set-cookie: a=1", b"set-cookie: b=2"]
        assert b"x-archivebox-proxy-cache: hit" in repeated
        assert replayed == content
    finally:
        p.close()


def test_replay_preserves_gzip_representation(tmp_path):
    import gzip

    p = Proxy(tmp_path / "cache")
    try:
        fields, original = p.fetch("https://httpbingo.org/gzip")
        repeated, replayed = p.fetch("https://httpbingo.org/gzip", worker=True)
        assert b"content-encoding: gzip" in fields
        assert b"content-encoding: gzip" in repeated
        assert b"x-archivebox-proxy-cache: hit" in repeated
        assert replayed == original
        assert json.loads(gzip.decompress(replayed))["gzipped"] is True
    finally:
        p.close()


def test_offline_cli_replays_existing_wacz_into_independent_cache(tmp_path):
    import hashlib

    archive = ROOT / "tests/fixtures"
    wacz = archive / "archivewebpage/archivewebpage.wacz"
    before = hashlib.sha256(wacz.read_bytes()).hexdigest()
    data = tmp_path / "cache"
    p = Proxy(data, "--policy", "offline", "--archive", str(archive))
    try:
        fields, content = p.fetch("https://news.ycombinator.com/y18.svg")
        assert b"x-archivebox-proxy-cache: hit" in fields
        assert (
            hashlib.sha256(content).hexdigest()
            == "e1b6622b6ea15f1f48dfda6e720265cd426981d2a7ae1f9d7ddeb8409897c6ff"
        )
        assert hashlib.sha256(wacz.read_bytes()).hexdigest() == before
        assert (data / "cache.sqlite3").is_file()
        assert not (archive / "cache.sqlite3").exists()
    finally:
        p.close()
