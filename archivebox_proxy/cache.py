"""Content-addressed bodies and rebuildable metadata, independent of ArchiveBox."""

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urldefrag

DAY = 86400
HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "trailer",
}


def headers(message):
    return [
        (
            h["name"],
            # Proxelar serializes valid UTF-8 directly and other bytes as
            # base64. Normalize both into a reversible byte representation.
            h["value"].encode("utf8").decode("latin1")
            if "value" in h
            else base64.b64decode(h["value_base64"]).decode("latin1"),
        )
        for h in message.get("headers", [])
    ]


def body(message):
    value = message.get("body", [])
    return base64.b64decode(value) if isinstance(value, str) else bytes(value)


def fingerprint(request):
    # Preserve exact URL/query order and POST bytes. Cookie changes in this
    # single-user cache intentionally do not invalidate otherwise equal requests.
    hs = {k.lower(): v for k, v in headers(request)}
    parts = [
        request["method"],
        urldefrag(request["uri"])[0],
        hashlib.sha256(body(request)).hexdigest(),
    ]
    parts += [hs.get(k, "") for k in ("range", "accept-encoding", "content-type")]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


def response_headers(fields, length, method="GET", status=200):
    # A capture contains dechunked body bytes. Transfer framing must be generated
    # anew, while Content-Encoding remains valid for the stored encoded payload.
    connection = {
        v.strip().lower()
        for k, value in fields
        if k.lower() == "connection"
        for v in value.split(",")
    }
    result = [
        (k, v)
        for k, v in fields
        if k.lower() not in HOP | connection | {"content-length"}
        and not k.startswith(":")
    ]
    if status == 204:
        # 204 has no representation or framing fields, including a zero length.
        # HTTP/1 serializers reject it even if HTTP/2 tolerated the same cache row.
        return result
    if method != "HEAD":
        result.append(("Content-Length", str(length)))
    else:
        result += [(k, v) for k, v in fields if k.lower() == "content-length"]
    return result


def expiry(fields, now):
    hs = {k.lower(): v for k, v in fields}
    # Origins commonly split max-age and public into separate header fields.
    # Treat them as one field list; a dict alone would silently lose max-age.
    cache_control = ",".join(v for k, v in fields if k.lower() == "cache-control")
    ages = [
        int(n)
        for n in re.findall(
            r'(?:^|[,\s])(?:s-maxage|max-age)\s*=\s*"?(\d+)',
            cache_control,
            re.I,
        )
    ]
    age = hs.get("age", "0")
    lifetime = max(ages, default=0) - (int(age) if age.isdecimal() else 0)
    try:
        lifetime = max(lifetime, parsedate_to_datetime(hs["expires"]).timestamp() - now)
    except (KeyError, ValueError, TypeError, OverflowError):
        pass
    return now + max(DAY, lifetime)


class Cache:
    def __init__(self, directory, max_bytes=50 * 1024**3):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / "bodies"
        self.blobs.mkdir(exist_ok=True)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "cache.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, filename TEXT, status INTEGER, headers TEXT, size INTEGER, expires REAL, used REAL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS queue (url TEXT PRIMARY KEY, due REAL, submitted REAL, error TEXT)"
        )
        self.db.commit()

    def get(self, key, historical=False):
        with self.lock:
            row = self.db.execute(
                "SELECT filename,status,headers,size,expires FROM responses WHERE key=?",
                (key,),
            ).fetchone()
            if not row or (not historical and row[4] <= time.time()):
                return None
            path = self.blobs / row[0]
            if not path.is_file():
                return None
            self.db.execute(
                "UPDATE responses SET used=? WHERE key=?", (time.time(), key)
            )
            self.db.commit()
            return {
                "path": path,
                "status": row[1],
                "headers": json.loads(row[2]),
                "size": row[3],
            }

    def put(self, key, status, fields, payload, method="GET"):
        if status < 200 or status == 304:
            return False
        name = hashlib.sha256(payload).hexdigest()
        path = self.blobs / name
        # Publish complete bytes before committing a row. Interrupted writes can
        # leave an orphan, but can never make a partial response replayable.
        if not path.exists():
            import tempfile

            with tempfile.NamedTemporaryFile(dir=self.blobs, delete=False) as f:
                temp = Path(f.name)
                try:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
        now = time.time()
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,?,?)",
                (
                    key,
                    name,
                    status,
                    json.dumps(response_headers(fields, len(payload), method, status)),
                    len(payload),
                    expiry(fields, now),
                    now,
                ),
            )
            self.db.commit()
        return True

    def prune(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT key,filename,size FROM responses ORDER BY used DESC"
            ).fetchall()
            kept, used, remove = set(), 0, []
            for key, name, size in rows:
                extra = 0 if name in kept else size
                if used + extra > self.max_bytes:
                    remove.append((key,))
                else:
                    kept.add(name)
                    used += extra
            self.db.executemany("DELETE FROM responses WHERE key=?", remove)
            self.db.commit()
            # GC runs between writer operations under the same external store
            # lock; the service serializes put/prune to protect unpublished blobs.
            for path in self.blobs.iterdir():
                if path.name not in kept:
                    path.unlink(missing_ok=True)

    def enqueue(self, url, delay=10):
        with self.lock:
            self.db.execute(
                "INSERT INTO queue VALUES (?,?,NULL,NULL) ON CONFLICT(url) DO UPDATE SET due=excluded.due, submitted=NULL WHERE queue.submitted < ? OR queue.error IS NOT NULL",
                (url, time.time() + delay, time.time() - DAY),
            )
            self.db.commit()

    def close(self):
        self.db.close()
