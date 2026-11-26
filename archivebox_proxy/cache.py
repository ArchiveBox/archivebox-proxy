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


def complete_response(status, fields, size, method="GET"):
    # Validate the encoded bytes before replacing transfer framing. Otherwise a
    # cancelled video download gets a new, shorter Content-Length and looks valid.
    if method == "HEAD" or status in (204, 205, 304):
        return size == 0
    for name, value in fields:
        if name.lower() == "content-length":
            for length in value.split(","):
                if not length.strip().isdecimal() or int(length) != size:
                    return False
        elif name.lower() == "content-range" and status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", value, re.IGNORECASE)
            if not match:
                return False
            start, end = int(match[1]), int(match[2])
            if end < start or end - start + 1 != size:
                return False
            if match[3] != "*" and end >= int(match[3]):
                return False
    return True


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
            re.IGNORECASE,
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
        self.files_lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "cache.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, filename TEXT, status INTEGER, headers TEXT, size INTEGER, expires REAL, used REAL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS queue (url TEXT PRIMARY KEY, due REAL, submitted REAL, error TEXT)"
        )
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Old writers replaced the original Content-Length with the captured
            # prefix length. Those rows cannot prove completion, even for 200s.
            # Only disposable response metadata is invalidated; queued work and
            # the independent ArchiveBox collection remain untouched.
            self.db.execute("DELETE FROM responses")
            self.db.execute("PRAGMA user_version=1")
        self.db.commit()

    def get(self, key, historical=False, method="GET", *, open_body=False):
        # Publication, opening, and eviction are serialized separately from the
        # database. Large writes/fsync never take either lock. An opened replay
        # stays readable if later eviction unlinks its body.
        with self.files_lock:
            with self.lock:
                row = self.db.execute(
                    "SELECT filename,status,headers,size,expires FROM responses WHERE key=?",
                    (key,),
                ).fetchone()
            if not row or (not historical and row[4] <= time.time()):
                return None
            path = self.blobs / row[0]
            fields = json.loads(row[2])
            stream = None
            try:
                stream = path.open("rb")
                valid = os.fstat(stream.fileno()).st_size == row[
                    3
                ] and complete_response(row[1], fields, row[3], method)
            except OSError:
                valid = False
            if not valid:
                if stream is not None:
                    stream.close()
                with self.lock:
                    self.db.execute("DELETE FROM responses WHERE key=?", (key,))
                    self.db.commit()
                return None
            with self.lock:
                self.db.execute(
                    "UPDATE responses SET used=? WHERE key=?", (time.time(), key)
                )
                self.db.commit()
            result = {
                "path": path,
                "status": row[1],
                "headers": fields,
                "size": row[3],
            }
            if open_body:
                result["stream"] = stream
            else:
                stream.close()
            return result

    def put(self, key, status, fields, payload, method="GET"):
        if (
            status < 200
            or status == 304
            or not complete_response(status, fields, len(payload), method)
        ):
            return False
        name = hashlib.sha256(payload).hexdigest()
        path = self.blobs / name
        import tempfile

        # Staging files live outside bodies/ so pruning cannot remove an active
        # writer. Publish complete bytes and metadata together under files_lock;
        # neither disk writes nor fsync hold SQLite's lock or a transaction.
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as f:
            temp = Path(f.name)
            try:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
                now = time.time()
                metadata = (
                    key,
                    name,
                    status,
                    json.dumps(response_headers(fields, len(payload), method, status)),
                    len(payload),
                    expiry(fields, now),
                    now,
                )
                with self.files_lock:
                    os.replace(temp, path)
                    with self.lock:
                        self.db.execute(
                            "INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,?,?)",
                            metadata,
                        )
                        self.db.commit()
            finally:
                temp.unlink(missing_ok=True)
        return True

    def prune(self):
        with self.files_lock:
            with self.lock:
                rows = self.db.execute(
                    "SELECT key,filename,size,status,headers FROM responses ORDER BY used DESC"
                ).fetchall()
            kept, used, remove = set(), 0, []
            for key, name, size, status, fields in rows:
                extra = 0 if name in kept else size
                path = self.blobs / name
                # The legacy table has no request method. Empty HEAD bodies may
                # legitimately retain a representation's nonzero declared size;
                # get() checks them against the actual request method on replay.
                valid = complete_response(
                    status, json.loads(fields), size, "HEAD" if size == 0 else "GET"
                )
                if (
                    not valid
                    or not path.is_file()
                    or path.stat().st_size != size
                    or used + extra > self.max_bytes
                ):
                    remove.append((key,))
                else:
                    kept.add(name)
                    used += extra
            with self.lock:
                self.db.executemany("DELETE FROM responses WHERE key=?", remove)
                self.db.commit()
            # Publication cannot race this unlink phase; bulk writes are still
            # free to continue into their separate staging files.
            for path in self.blobs.iterdir():
                if path.name not in kept:
                    path.unlink(missing_ok=True)

    def enqueue(self, url, delay=10):
        with self.lock:
            self.db.execute(
                "INSERT INTO queue (url,due,submitted,error) VALUES (?,?,NULL,NULL) "
                "ON CONFLICT(url) DO UPDATE SET due=excluded.due,submitted=NULL,error=NULL "
                "WHERE (queue.submitted < ? OR queue.error IS NOT NULL) AND "
                "(queue.error IS NULL OR (queue.error NOT LIKE 'claimed by %' AND "
                "queue.error != 'started; inspect hooks.jsonl before resubmitting'))",
                (url, time.time() + delay, time.time() - DAY),
            )
            self.db.commit()

    def close(self):
        self.db.close()
