"""Check cache lifetime and bodyless replay against real HTTP responses."""

import json
import sqlite3

from test_proxy_live import Proxy


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
