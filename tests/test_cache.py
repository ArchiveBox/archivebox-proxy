"""Cache persistence and eviction over real capture files and SQLite databases."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from archivebox_proxy.cache import Cache

FIXTURES = Path(__file__).parent / "fixtures"
PAYLOAD = next((FIXTURES / "responses/all").iterdir()).read_bytes()
URL = "https://news.ycombinator.com/y18.svg"


def test_legacy_normalized_prefix_is_invalidated_without_losing_queue(tmp_path):
    # This is the actual pre-migration SQLite layout. Its shortened normalized
    # Content-Length cannot distinguish an interrupted 200 from a complete one.
    prefix = PAYLOAD[:100]
    name = hashlib.sha256(prefix).hexdigest()
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / name).write_bytes(prefix)
    with sqlite3.connect(tmp_path / "cache.sqlite3") as db:
        db.execute(
            "CREATE TABLE responses (key TEXT PRIMARY KEY, filename TEXT, status INTEGER, headers TEXT, size INTEGER, expires REAL, used REAL)"
        )
        db.execute(
            "CREATE TABLE queue (url TEXT PRIMARY KEY, due REAL, submitted REAL, error TEXT)"
        )
        db.execute(
            "INSERT INTO responses VALUES (?,?,?,?,?,?,?)",
            (
                "old",
                name,
                200,
                json.dumps([["Content-Length", "100"]]),
                100,
                time.time() + 86400,
                time.time(),
            ),
        )
        db.execute("INSERT INTO queue VALUES (?,123,NULL,NULL)", (URL,))
    cache = Cache(tmp_path)
    assert cache.get("old", historical=True) is None
    assert cache.db.execute("SELECT * FROM queue").fetchall() == [
        (URL, 123, None, None)
    ]
    assert cache.put("complete", 200, [("Content-Length", str(len(PAYLOAD)))], PAYLOAD)
    cache.close()
    reopened = Cache(tmp_path)
    hit = reopened.get("complete", open_body=True)
    with hit["stream"] as stream:
        assert stream.read() == PAYLOAD
    reopened.prune()
    assert not (bodies / name).exists()
    reopened.close()


def test_open_replay_survives_eviction(tmp_path):
    cache = Cache(tmp_path, max_bytes=1)
    assert cache.put("svg", 200, [], PAYLOAD)
    hit = cache.get("svg", open_body=True)
    cache.prune()
    assert cache.get("svg") is None
    assert not hit["path"].exists()
    with hit["stream"] as stream:
        assert stream.read() == PAYLOAD
    cache.close()


def test_requeue_clears_failure_and_preserves_inflight_work(tmp_path):
    cache = Cache(tmp_path)
    cache.enqueue(URL)
    cache.db.execute(
        "UPDATE queue SET submitted=?,error='hook exited 1'", (time.time(),)
    )
    cache.db.commit()
    cache.enqueue(URL, delay=20)
    queued = cache.db.execute("SELECT due,submitted,error FROM queue").fetchone()
    assert queued[1:] == (None, None)
    cache.enqueue(URL, delay=500)
    assert (
        cache.db.execute("SELECT due,submitted,error FROM queue").fetchone() == queued
    )
    for state in ("claimed by 123", "started; inspect hooks.jsonl before resubmitting"):
        cache.db.execute("UPDATE queue SET submitted=1,error=?", (state,))
        cache.db.commit()
        cache.enqueue(URL)
        assert cache.db.execute("SELECT submitted,error FROM queue").fetchone() == (
            1,
            state,
        )
    cache.close()


def test_parallel_publication_and_prune_keep_complete_blobs(tmp_path):
    payloads = [PAYLOAD, (FIXTURES / "archivewebpage/archivewebpage.wacz").read_bytes()]
    cache = Cache(tmp_path, max_bytes=len(PAYLOAD))

    def store(number):
        payload = payloads[number % 2]
        assert cache.put(str(number), 200, [], payload)
        hit = cache.get(str(number), open_body=True)
        if hit:
            with hit["stream"] as stream:
                assert stream.read() == payload

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(store, n) for n in range(40)]
        futures += [pool.submit(cache.prune) for _ in range(20)]
        for future in futures:
            future.result()
    cache.prune()
    files = list(cache.blobs.iterdir())
    assert sum(path.stat().st_size for path in files) <= cache.max_bytes
    for filename, size in cache.db.execute(
        "SELECT filename,size FROM responses"
    ).fetchall():
        payload = (cache.blobs / filename).read_bytes()
        assert len(payload) == size
        assert hashlib.sha256(payload).hexdigest() == filename
    assert not cache.db.in_transaction
    # Concurrent pruning may legitimately evict either writer before its get.
    # Once pruning stops, require both real payloads to replay in their entirety.
    for number, payload in enumerate(payloads):
        assert cache.put(f"final-{number}", 200, [], payload)
        hit = cache.get(f"final-{number}", open_body=True)
        assert hit is not None
        with hit["stream"] as stream:
            assert stream.read() == payload
    cache.close()


def test_existing_body_is_reused_without_replacing_file(tmp_path):
    cache = Cache(tmp_path)
    assert cache.put("first", 200, [], PAYLOAD)
    path = cache.get("first")["path"]
    before = path.stat()
    assert cache.put("second", 200, [("Content-Type", "image/svg+xml")], PAYLOAD)
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert cache.get("second")["headers"] == [
        ["Content-Type", "image/svg+xml"],
        ["Content-Length", str(len(PAYLOAD))],
    ]
    cache.close()


def test_prune_cleans_killed_writer_staging_but_preserves_live_writer(tmp_path):
    cache = Cache(tmp_path)
    # Use a real process-held lock: the kernel releases it after SIGKILL,
    # including when a restarted container would reuse the same numeric PID.
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,sys,tempfile; from pathlib import Path; "
                "f=tempfile.NamedTemporaryFile(dir=sys.argv[1], "
                "prefix='.tmp-', delete=False); "
                "fcntl.flock(f.fileno(),fcntl.LOCK_EX); "
                "f.write(Path(sys.argv[2]).read_bytes()); f.flush(); "
                "print(f.name,flush=True); sys.stdin.read()"
            ),
            str(tmp_path),
            str(next((FIXTURES / "responses/all").iterdir())),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        path = Path(writer.stdout.readline().strip())
        assert path.read_bytes() == PAYLOAD
        cache.prune()
        assert path.read_bytes() == PAYLOAD
        writer.kill()
        assert writer.wait(timeout=10) != 0
        cache.prune()
        assert not path.exists()
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.wait(timeout=10)
        writer.stdin.close()
        writer.stdout.close()
        cache.close()
