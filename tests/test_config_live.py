"""Independent recording, native cache and archive replay via the real CLI."""

import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from test_proxy_live import ROOT, Proxy

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://news.ycombinator.com/y18.svg"
NAVIGATION = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


@pytest.mark.parametrize(
    "record,caching,replay", list(itertools.product((False, True), repeat=3))
)
def test_independent_environment_controls(tmp_path, record, caching, replay):
    collection = tmp_path / "collection"
    shutil.copytree(FIXTURES / "responses", collection / "responses")
    output = tmp_path / "captured.svg"
    hook = [
        "curl",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--noproxy",
        "",
        "--output",
        str(output),
        "{url}",
    ]
    env = {
        **os.environ,
        "PROXY_RECORD": str(record).lower(),
        "PROXY_CACHE": str(caching).lower(),
        "PROXY_REPLAY": str(replay).lower(),
        "PROXY_ARCHIVE": str(collection),
        "PROXY_HOOK": json.dumps(hook),
    }
    proxy = Proxy(tmp_path / "cache", env=env)
    try:
        fields, original = proxy.fetch(URL, headers=NAVIGATION)
        assert b"<svg" in original
        assert (b"x-archivebox-proxy-cache: HIT" in fields) == replay
        fields, repeated = proxy.fetch(URL, worker=True)
        assert repeated == original
        assert (b"x-archivebox-proxy-cache: HIT" in fields) == (caching or replay)
        with sqlite3.connect(proxy.data / "cache.sqlite3") as db:
            assert (
                bool(db.execute("SELECT count(*) FROM responses").fetchone()[0])
                == caching
            )
            assert db.execute("SELECT count(*) FROM queue").fetchone()[0] == int(record)
        if record:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                with sqlite3.connect(proxy.data / "cache.sqlite3") as db:
                    row = db.execute("SELECT submitted,error FROM queue").fetchone()
                if row[0] is not None and row[1] is None:
                    break
                time.sleep(0.05)
            else:
                log = proxy.data / "hooks.log"
                raise AssertionError(
                    (
                        row,
                        log.read_text()
                        if log.exists()
                        else "No hook output was created",
                    )
                )
            assert output.read_bytes() == original
        else:
            assert not output.exists()
            assert not (proxy.data / "hooks.jsonl").exists()
            assert not (proxy.data / "navigations.jsonl").exists()
        if not caching:
            assert not list((proxy.data / "bodies").iterdir())
    finally:
        proxy.close()


def test_flags_override_environment_and_offline_never_forwards(tmp_path):
    env = {
        **os.environ,
        "PROXY_CACHE": "false",
        "PROXY_RECORD": "invalid",
        "PROXY_REPLAY": "true",
        "PROXY_HOOK": "invalid",
    }
    command = ["curl", "--head", "{url}"]
    proxy = Proxy(
        tmp_path / "cache",
        "--cache",
        "--no-record",
        "--no-replay",
        "--hook",
        json.dumps(command),
        env=env,
    )
    try:
        proxy.fetch(URL)
        fields, _ = proxy.fetch(URL, worker=True)
        assert b"x-archivebox-proxy-cache: HIT" in fields
        assert not (proxy.data / "hooks.jsonl").exists()
    finally:
        proxy.close()
    proxy = Proxy(
        tmp_path / "cache",
        "--no-cache",
        "--no-record",
        "--no-replay",
        "--policy",
        "offline",
    )
    try:
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--noproxy",
                "",
                "--proxy",
                f"http://127.0.0.1:{proxy.worker}",
                "--cacert",
                str(proxy.data / "ca/proxelar-ca.pem"),
                "--write-out",
                "%{http_code}",
                URL,
            ],
            capture_output=True,
            check=True,
        )
        assert result.stdout == b"Archive replay miss504"
        log = (tmp_path / "cache.log").read_text()
        assert "BLOCK worker" in log
    finally:
        proxy.close()


@pytest.mark.parametrize(
    "name,value",
    [
        ("PROXY_CACHE", "maybe"),
        ("PROXY_CACHE", ""),
        ("PROXY_POLICY", "invalid"),
        ("PROXY_MODE", "invalid"),
    ],
)
def test_invalid_environment_value_fails_clearly(name, value):
    result = subprocess.run(
        ["uv", "run", "archivebox-proxy"],
        cwd=ROOT,
        env={**os.environ, name: value},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert name.encode() + b" must be " in result.stderr
