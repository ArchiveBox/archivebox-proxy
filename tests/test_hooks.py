"""Run real navigation hooks through the shipped worker proxy and persistent queue."""

import json
import sqlite3
import time

from test_proxy_live import Proxy


def test_navigation_hook_survives_restart_and_reuses_visitor_response(tmp_path):
    data = tmp_path / "cache"
    output, headers = tmp_path / "page.html", tmp_path / "headers.txt"
    navigation = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    # curl is a real configurable fetch hook. It must inherit the worker proxy
    # and CA rather than supplying proxy arguments that hide environment bugs.
    command = [
        "curl",
        "--noproxy",
        "",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--max-time",
        "30",
        "--dump-header",
        str(headers),
        "--output",
        str(output),
        "-H",
        "Sec-Fetch-Mode: navigate",
        "-H",
        "Sec-Fetch-Dest: document",
        "{url}",
    ]
    url = "https://news.ycombinator.com/newest"
    args = ("--hook", json.dumps(command))
    proxy = Proxy(data, *args)
    try:
        _, original = proxy.fetch(url, headers=navigation)
        assert b"Hacker News" in original
        proxy.wait_stored(url)
    finally:
        proxy.close()

    with sqlite3.connect(data / "cache.sqlite3") as db:
        assert db.execute(
            "SELECT submitted,error FROM queue WHERE url=?", (url,)
        ).fetchone() == (None, None)

    # Turning recording off also pauses previously queued work. Leaving the
    # hook configuration empty must not consume that queue either.
    for disabled in ((*args, "--no-record"), ()):
        proxy = Proxy(data, *disabled)
        try:
            with sqlite3.connect(data / "cache.sqlite3") as db:
                due = db.execute(
                    "SELECT due FROM queue WHERE url=?", (url,)
                ).fetchone()[0]
            time.sleep(max(0, due - time.time()) + 0.5)
            with sqlite3.connect(data / "cache.sqlite3") as db:
                assert db.execute(
                    "SELECT submitted,error FROM queue WHERE url=?", (url,)
                ).fetchone() == (None, None)
            assert not output.exists()
        finally:
            proxy.close()

    # Re-enable hooks against the same durable queue and previously cached body.
    proxy = Proxy(data, *args)
    try:
        events = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (data / "hooks.jsonl").exists():
                text = (data / "hooks.jsonl").read_text()
                events = [
                    json.loads(line)
                    for line in text[: text.rfind("\n") + 1].splitlines()
                ]
                if any(event["event"] in ("finish", "error") for event in events):
                    break
            time.sleep(0.1)
        assert [event["event"] for event in events] == ["start", "finish"], (
            events,
            (data / "hooks.log").read_text() if (data / "hooks.log").exists() else "",
        )
        assert events[-1]["returncode"] == 0
        assert output.read_bytes() == original
        assert "x-archivebox-proxy-cache: hit" in headers.read_text().lower()
        assert len((data / "navigations.jsonl").read_text().splitlines()) == 1
        with sqlite3.connect(data / "cache.sqlite3") as db:
            submitted, error = db.execute(
                "SELECT submitted,error FROM queue WHERE url=?", (url,)
            ).fetchone()
            assert submitted is not None and error is None
        proxy.fetch(url, headers=navigation)
        with sqlite3.connect(data / "cache.sqlite3") as db:
            assert db.execute(
                "SELECT submitted,error FROM queue WHERE url=?", (url,)
            ).fetchone() == (submitted, None)
    finally:
        proxy.close()
