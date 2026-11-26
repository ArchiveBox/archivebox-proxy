"""Check cache lifetime and bodyless replay against real HTTP responses."""

import json
import sqlite3
import subprocess
import time

import pytest
from test_proxy_live import Proxy


@pytest.mark.parametrize(
    "url",
    [
        "https://httpbingo.org/drip?duration=3&numbytes=300&delay=0",
        "https://httpbingo.org/sse?count=10&duration=3&delay=0",
    ],
)
def test_cancelled_response_is_not_cached(tmp_path, url):
    proxy = Proxy(tmp_path / "cancel")
    try:
        # Both origins really stream for three seconds. SSE has no declared
        # length, so only native EOF tracking can distinguish it from a prefix.
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--noproxy",
                "",
                "--max-time",
                "1",
                "--proxy",
                f"http://127.0.0.1:{proxy.port}",
                "--cacert",
                str(proxy.data / "ca/proxelar-ca.pem"),
                url,
            ],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 28, result.stderr.decode()
        assert result.stdout
        log = tmp_path / "cancel.log"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(
                value in log.read_text()
                for value in ("STORED ", "UNCACHED ", "Capture incomplete")
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(log.read_text())
        with sqlite3.connect(proxy.data / "cache.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM responses").fetchone()[0] == 0
        fields, complete = proxy.fetch(url)
        assert b"x-archivebox-proxy-cache:" not in fields
        assert len(complete) > len(result.stdout)
        fields, replayed = proxy.fetch(url, worker=True)
        assert b"x-archivebox-proxy-cache: hit" in fields
        assert replayed == complete
    finally:
        proxy.close()


def test_duplicate_cache_control_retains_longer_origin_lifetime(tmp_path):
    proxy = Proxy(tmp_path / "cache")
    try:
        # Real HN assets send max-age and public as separate fields. This live
        # response has the same shape without Expires masking a dropped max-age.
        url = (
            "https://httpbingo.org/response-headers?"
            "Cache-Control=max-age%3D172800&Cache-Control=public"
        )
        fields, _ = proxy.fetch(url)
        assert b"cache-control: max-age=172800" in fields.split(b"\r\n")
        assert b"cache-control: public" in fields.split(b"\r\n")
        repeated, _ = proxy.fetch(url, worker=True)
        assert b"x-archivebox-proxy-cache: hit" in repeated
        with sqlite3.connect(proxy.data / "cache.sqlite3") as db:
            lifetime = db.execute("SELECT expires-used FROM responses").fetchone()[0]
        assert lifetime > 172790
    finally:
        proxy.close()


def test_cached_204_has_no_body_or_content_length(tmp_path):
    proxy = Proxy(tmp_path / "cache")
    try:
        _, original = proxy.fetch("https://httpbingo.org/status/204")
        fields, replayed = proxy.fetch("https://httpbingo.org/status/204", worker=True)
        assert original == replayed == b""
        assert fields.split(b"\r\n", 1)[0].split()[1] == b"204"
        assert b"x-archivebox-proxy-cache: hit" in fields
        assert b"content-length:" not in fields
        assert b"transfer-encoding:" not in fields
        with sqlite3.connect(proxy.data / "cache.sqlite3") as db:
            stored = json.loads(
                db.execute("SELECT headers FROM responses").fetchone()[0]
            )
        assert all(name.lower() != "content-length" for name, _ in stored)
    finally:
        proxy.close()
