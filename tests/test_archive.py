"""Fallback checks against real ArchiveBox captures of public Hacker News assets.

The WACZ was captured 2026-11-14 by ArchiveWeb.page in the existing
folder-export-menu collection. The responses fixture is its independent
responses-plugin capture of y18.svg. Neither fixture contains login headers.
"""

import gzip
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from archivebox_proxy.archive import Archive

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://news.ycombinator.com/y18.svg"
DIGEST = "e1b6622b6ea15f1f48dfda6e720265cd426981d2a7ae1f9d7ddeb8409897c6ff"


def request(url=URL, method="GET", **kwargs):
    return {"uri": url, "method": method, "headers": [], "body": [], **kwargs}


def make_archive(tmp_path, name):
    root = tmp_path / "collection"
    shutil.copytree(FIXTURES / name, root / name)
    archive = Archive(root, tmp_path / "fallback.sqlite3")
    archive.refresh()
    return archive, root


def test_wacz_original_response_is_replayed_without_modifying_archive(tmp_path):
    archive, root = make_archive(tmp_path, "archivewebpage")
    path = root / "archivewebpage/archivewebpage.wacz"
    before = path.read_bytes()
    result = archive.lookup(request())
    assert result is not None
    status, headers, payload = result
    assert status == 200
    assert hashlib.sha256(payload).hexdigest() == DIGEST
    assert {k.lower(): v for k, v in headers}["content-type"].startswith(
        "image/svg+xml"
    )
    assert b"<svg" in payload
    assert path.read_bytes() == before
    count = archive.db.execute("SELECT count(*) FROM archive").fetchone()[0]
    assert count == 6
    archive.refresh()
    assert archive.db.execute("SELECT count(*) FROM archive").fetchone()[0] == count


def test_loose_response_digest_and_head(tmp_path):
    archive, root = make_archive(tmp_path, "responses")
    status, fields, payload = archive.lookup(request())
    assert status == 200
    assert hashlib.sha256(payload).hexdigest() == DIGEST
    head = archive.lookup(request(method="HEAD"))
    assert head == (status, fields, b"")
    index = next(
        json.loads(line)
        for line in (root / "responses/index.jsonl").read_text().splitlines()
    )
    (root / "responses" / index["path"]).write_bytes(b"corrupt")
    assert archive.lookup(request()) is None


def test_missing_or_corrupt_archive_is_a_miss(tmp_path):
    archive, root = make_archive(tmp_path, "archivewebpage")
    path = root / "archivewebpage/archivewebpage.wacz"
    path.write_bytes(path.read_bytes()[:100])
    archive.refresh()
    assert archive.lookup(request()) is None
    path.unlink()
    archive.refresh()
    assert archive.db.execute("SELECT count(*) FROM archive").fetchone()[0] == 0


def test_wrong_url_method_or_body_cannot_reuse_a_get(tmp_path):
    archive, _ = make_archive(tmp_path, "archivewebpage")
    assert archive.lookup(request(url=URL + "?different=1")) is None
    assert archive.lookup(request(method="POST", body=list(b"changed"))) is None
    assert archive.lookup(request(body=list(b"changed"))) is None
    assert (
        archive.lookup(request(headers=[{"name": "Range", "value": "bytes=0-10"}]))
        is None
    )


def test_capture_symlink_cannot_escape_collection(tmp_path):
    archive, root = make_archive(tmp_path, "responses")
    index = next(
        json.loads(line)
        for line in (root / "responses/index.jsonl").read_text().splitlines()
    )
    payload = root / "responses" / index["path"]
    outside = tmp_path / "outside.svg"
    shutil.move(payload, outside)
    payload.symlink_to(outside)
    assert archive.lookup(request()) is None


def test_disabled_fallback_never_opens_capture(tmp_path):
    archive = Archive(None, tmp_path / "fallback.sqlite3")
    archive.refresh()
    assert archive.lookup(request()) is None


def test_malformed_loose_url_does_not_abort_other_records(tmp_path):
    archive, root = make_archive(tmp_path, "responses")
    path = root / "responses/index.jsonl"
    entry = next(json.loads(line) for line in path.read_text().splitlines())
    with path.open("a") as output:
        output.write(json.dumps({**entry, "url": {"invalid": URL}}) + "\n")
        output.write(json.dumps({**entry, "url": [URL]}) + "\n")
        output.write(json.dumps(entry) + "\n")
    archive.refresh()
    assert hashlib.sha256(archive.lookup(request())[2]).hexdigest() == DIGEST


@pytest.mark.parametrize("capture", ["wget", "archivewebpage-post"])
@pytest.mark.parametrize("root_members", [False, True])
def test_real_post_pair_with_both_record_orders(tmp_path, capture, root_members):
    # Real httpbingo POST captures from 2026-11-26: Wget records the request
    # before the response; ArchiveWeb.page records it afterward. Each puts
    # Concurrent-To on the later record, so both association directions matter.
    archive, root = make_archive(tmp_path, capture)
    if root_members:
        path = next(root.glob("**/*.wacz"))
        # Relocate the genuine recorded WARC bytes without rewriting records or
        # indexes. These CDX filename fields already refer to member basenames.
        with zipfile.ZipFile(path) as source:
            members = [(name, source.read(name)) for name in source.namelist()]
        with zipfile.ZipFile(path, "w") as target:
            for name, data in members:
                target.writestr(name.removeprefix("archive/"), data)
        archive.refresh()
    req = request(
        url="https://httpbingo.org/status/200",
        method="POST",
        body=list(b"archivebox-proxy=recorded-public-post"),
    )
    hit = archive.lookup(req)
    assert hit is not None and hit[0] == 200 and hit[2] == b""
    assert archive.lookup({**req, "body": req["body"] + [32]}) is None


def test_archive_compression_must_be_accepted_by_request(tmp_path):
    # The same real Wget capture requested gzip from Hacker News; its WARC
    # retains the original encoded representation rather than decoded HTML.
    archive, _ = make_archive(tmp_path, "wget")
    req = request(
        url="https://news.ycombinator.com/",
        headers=[{"name": "Accept-Encoding", "value": "gzip"}],
    )
    hit = archive.lookup(req)
    assert hit is not None and hit[0] == 200
    assert b"Hacker News" in gzip.decompress(hit[2])
    for value in ("identity", "gzip;q=0", "br", ""):
        assert (
            archive.lookup(
                {**req, "headers": [{"name": "Accept-Encoding", "value": value}]}
            )
            is None
        )
