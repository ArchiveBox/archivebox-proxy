"""Read-only response lookup over ArchiveBox files and WACZ CDXJ indexes."""

import gzip
import hashlib
import json
import logging
import sqlite3
import threading
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urldefrag

from warcio.archiveiterator import ArchiveIterator
from warcio.bufferedreaders import ChunkedDataException, ChunkedDataReader
from warcio.exceptions import ArchiveLoadFailed
from warcio.recordloader import ArcWarcRecordLoader
from warcio.statusandheaders import StatusAndHeadersParser

LOG = logging.getLogger(__name__)
READ_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    EOFError,
    zipfile.BadZipFile,
    ArchiveLoadFailed,
    ChunkedDataException,
    zlib.error,
    OverflowError,
)


def original_url(entry):
    """ArchiveWeb.page appends method/body metadata to its indexed URL."""
    url = entry["url"]
    if not isinstance(url, str):
        raise TypeError("invalid CDXJ URL")
    for marker in ("?__wb_method=", "&__wb_method="):
        if marker in url:
            return url.split(marker, 1)[0]
    return urldefrag(url)[0]


def method_for(entry):
    if entry.get("method"):
        if not isinstance(entry["method"], str):
            raise TypeError("invalid CDXJ method")
        return entry["method"].upper()
    if "__wb_method=" in entry["url"]:
        return entry["url"].split("__wb_method=", 1)[1].split("&", 1)[0].upper()
    return "GET"


def timestamp(value):
    # responses/ uses YYYYMMDDTHHMMSS; CDXJ may include millisecond precision.
    return "".join(c for c in str(value) if c.isdecimal())


class Archive:
    def __init__(self, root, database):
        self.root = Path(root).resolve() if root else None
        self.lock = threading.RLock()
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sources (path TEXT PRIMARY KEY, stamp TEXT)"
        )
        # The key includes record position: POSTs to one endpoint can have
        # different bodies within the same capture. URL-only replacement loses them.
        schema = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='archive'"
        ).fetchone()
        if schema and "position" not in schema[0]:
            self.db.execute("DROP TABLE archive")
            self.db.execute("DELETE FROM sources")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS archive (url TEXT, method TEXT, source TEXT, position TEXT, metadata TEXT, timestamp TEXT, PRIMARY KEY(url,method,source,position))"
        )
        self.db.commit()

    def contained(self, path):
        path = path.resolve()
        if not self.root or not path.is_relative_to(self.root):
            raise ValueError("archive path escapes configured collection")
        return path

    def _entries(self, path):
        if path.suffix == ".jsonl":
            with path.open() as f:
                for number, line in enumerate(f):
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            continue
                        # Loose responses have decoded bytes and MIME, but no
                        # original headers, request body, or redirect destination.
                        if entry.get("method") != "GET" or entry.get("status") != 200:
                            continue
                        self.contained(path.parent / entry["path"])
                        yield (
                            entry["url"],
                            "GET",
                            str(path),
                            str(number),
                            json.dumps(entry),
                            timestamp(entry.get("ts", "")),
                        )
                    except (ValueError, KeyError, TypeError):
                        continue  # A capture may still be appending its final line.
        else:
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if not name.startswith("indexes/") or not name.endswith(
                        (".cdx", ".cdxj", ".cdx.gz", ".cdxj.gz")
                    ):
                        continue
                    with z.open(name) as f:
                        stream = gzip.GzipFile(fileobj=f) if name.endswith(".gz") else f
                        # Concatenated gzip members used by WACZ block indexes
                        # are supported by GzipFile; no whole-index buffer needed.
                        for line in stream:
                            if not line.strip() or line.startswith(b"!"):
                                continue
                            _, when, raw = line.decode().split(" ", 2)
                            entry = json.loads(raw)
                            if not isinstance(entry, dict):
                                raise TypeError("invalid CDXJ record")
                            if int(entry["offset"]) < 0 or int(entry["length"]) <= 0:
                                raise ValueError("invalid WARC record range")
                            position = f"{entry['filename']}:{entry['offset']}"
                            yield (
                                original_url(entry),
                                method_for(entry),
                                str(path),
                                position,
                                json.dumps(entry),
                                timestamp(when),
                            )

    def refresh(self):
        if not self.root:
            return
        files = list(self.root.glob("**/responses/index.jsonl")) + list(
            self.root.glob("**/archivewebpage/*.wacz")
        )
        current = set()
        with self.lock:
            for candidate in files:
                try:
                    path = self.contained(candidate)
                    current.add(str(path))
                    stat = path.stat()
                    stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
                    if self.db.execute(
                        "SELECT stamp FROM sources WHERE path=?", (str(path),)
                    ).fetchone() == (stamp,):
                        continue
                    # Parse before opening a write transaction: decompression
                    # and collection I/O must not keep SQLite's writer locked.
                    entries = list(self._entries(path))
                    # Replace one source atomically. A partial/corrupt WACZ must
                    # not erase its last usable index or abort other sources.
                    with self.db:
                        self.db.execute(
                            "DELETE FROM archive WHERE source=?", (str(path),)
                        )
                        self.db.executemany(
                            "INSERT OR REPLACE INTO archive VALUES (?,?,?,?,?,?)",
                            entries,
                        )
                        self.db.execute(
                            "INSERT OR REPLACE INTO sources VALUES (?,?)",
                            (str(path), stamp),
                        )
                except READ_ERRORS as exc:
                    LOG.warning("Cannot index archive %s: %s", candidate, exc)
            with self.db:
                for (path,) in self.db.execute("SELECT path FROM sources").fetchall():
                    if path not in current:
                        self.db.execute("DELETE FROM archive WHERE source=?", (path,))
                        self.db.execute("DELETE FROM sources WHERE path=?", (path,))

    def _candidates(self, url, method):
        # Only materialize matching records, never every response in a collection.
        return self.db.execute(
            "SELECT source,metadata FROM archive WHERE url=? AND method=? ORDER BY timestamp DESC",
            (url, method),
        ).fetchall()

    def _warc(self, path, entry, request, visited):
        from .cache import body

        identity = (str(path), entry["filename"], int(entry["offset"]))
        if identity in visited or len(visited) >= 8:
            return None
        visited = visited | {identity}
        with zipfile.ZipFile(path) as z:
            filename = entry["filename"]
            name = PurePosixPath(filename)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("invalid WARC member path")
            member = filename if filename in z.namelist() else "archive/" + filename
            info = z.getinfo(member)
            if int(entry["offset"]) + int(entry["length"]) > info.file_size:
                raise ValueError("WARC record range exceeds member")
            with z.open(member) as f:
                f.seek(int(entry["offset"]))
                records = ArchiveIterator(f, no_record_parse=True)
                record = _http_record(next(records))
                if (
                    record.rec_type not in ("response", "revisit")
                    or not record.http_headers
                ):
                    return None
                target = record.rec_headers.get_header("WARC-Target-URI")
                if target != request["uri"]:
                    return None
                if record.rec_headers.get_header("WARC-Truncated"):
                    return None
                status = int(record.http_headers.get_statuscode())
                if status < 200 or status in (206, 304):
                    return None
                fields = list(record.http_headers.headers)
                # Dechunk framing without decoding the representation: compressed
                # bytes remain usable even when optional decoders are unavailable.
                payload = _payload(record)
                record_id = record.rec_headers.get_header("WARC-Record-ID")
                if request["method"] not in ("GET", "HEAD"):
                    # CDXJ requestBody is often normalized for replay indexing.
                    # Compare the original paired request bytes instead, so JSON,
                    # multipart, and form bodies never collide accidentally.
                    paired = _http_record(next(records, None))
                    if (
                        not paired
                        or paired.rec_type != "request"
                        or not paired.http_headers
                    ):
                        return None
                    if paired.rec_headers.get_header("WARC-Concurrent-To") != record_id:
                        return None
                    if (
                        paired.rec_headers.get_header("WARC-Target-URI")
                        != request["uri"]
                    ):
                        return None
                    if paired.http_headers.protocol.upper() != request[
                        "method"
                    ] or _payload(paired) != body(request):
                        return None
                # 204 has no entity to resolve: recorders may deduplicate its
                # empty payload without retaining a separate response record.
                if record.rec_type == "revisit" and status != 204:
                    ref_url = (
                        record.rec_headers.get_header("WARC-Refers-To-Target-URI")
                        or target
                    )
                    digest = record.rec_headers.get_header("WARC-Payload-Digest")
                    for source, raw in self._candidates(ref_url, request["method"]):
                        other = json.loads(raw)
                        if digest and _digest(other.get("digest", "")) != _digest(
                            digest
                        ):
                            continue
                        resolved = self._warc(
                            self.contained(Path(source)),
                            other,
                            {**request, "uri": ref_url},
                            visited,
                        )
                        if resolved:
                            encoding = record.http_headers.get_header(
                                "Content-Encoding", ""
                            ).lower()
                            other_encoding = {
                                k.lower(): v.lower() for k, v in resolved[1]
                            }.get("content-encoding", "")
                            if encoding != other_encoding:
                                continue
                            payload = resolved[2]
                            break
                    else:
                        return None
                # Preserve encoded entity bytes and Content-Encoding. Only wire
                # transfer framing was removed; the cache will frame the new response.
                fields = [
                    (k, v)
                    for k, v in fields
                    if k.lower() not in ("transfer-encoding", "content-length")
                ]
                fields.append(("Content-Length", str(len(payload))))
                return status, fields, payload

    def lookup(self, request):
        if not self.root:
            return None
        from .cache import body, headers

        hs = {k.lower(): v for k, v in headers(request)}
        if any(
            k in hs
            for k in (
                "range",
                "if-none-match",
                "if-modified-since",
                "if-match",
                "if-unmodified-since",
            )
        ):
            return None
        request = {**request, "uri": urldefrag(request["uri"])[0]}
        method = request["method"]
        # GET-with-body cannot be reconstructed from loose captures or CDX alone.
        if method in ("GET", "HEAD") and body(request):
            return None
        with self.lock:
            for source, metadata in self._candidates(
                request["uri"], "GET" if method == "HEAD" else method
            ):
                try:
                    entry = json.loads(metadata)
                    path = self.contained(Path(source))
                    if path.suffix == ".jsonl":
                        payload = self.contained(
                            path.parent / entry["path"]
                        ).read_bytes()
                        digest = entry.get("responseSha256")
                        if digest and hashlib.sha256(payload).hexdigest() != digest:
                            raise ValueError("saved response digest mismatch")
                        result = (
                            200,
                            [
                                (
                                    "Content-Type",
                                    entry.get("mimeType") or "application/octet-stream",
                                ),
                                ("Content-Length", str(len(payload))),
                            ],
                            payload,
                        )
                    else:
                        result = self._warc(path, entry, request, set())
                    if result:
                        if method == "HEAD":
                            return result[0], result[1], b""
                        return result
                except (StopIteration, *READ_ERRORS) as exc:
                    LOG.warning("Cannot replay archive %s: %s", source, exc)
        return None


def _digest(value):
    return str(value).lower().replace("sha-256:", "sha256:").replace("sha-1:", "sha1:")


def _payload(record):
    if record.http_headers.get_header("Transfer-Encoding", "").lower() == "chunked":
        return ChunkedDataReader(record.raw_stream, raise_exceptions=True).read()
    return record.raw_stream.read()


class HTTPHeadersParser(StatusAndHeadersParser):
    # HTTP field values are bytes. warcio's default UTF-8 guess loses whether
    # identical Unicode text originally used UTF-8 or Latin1 on the wire.
    @staticmethod
    def decode_header(line):
        return line.decode("latin1") if isinstance(line, bytes) else line


def _http_record(record):
    if (
        record
        and record.rec_type in ("response", "request", "revisit")
        and record.length
    ):
        protocols = (
            ArcWarcRecordLoader.HTTP_VERBS
            if record.rec_type == "request"
            else ArcWarcRecordLoader.HTTP_TYPES
        )
        record.http_headers = HTTPHeadersParser(protocols, verify=False).parse(
            record.raw_stream
        )
    return record
