"""Local controller for Proxelar's interception events and replay responses."""

import asyncio
import base64
import json
import logging
import secrets
import time
from collections import Counter

from aiohttp import web
from multidict import CIMultiDict

from .archive import Archive
from .cache import Cache, body, fingerprint, headers

LOG = logging.getLogger(__name__)


class Controller:
    def __init__(
        self,
        directory,
        token,
        *,
        archive=None,
        policy="auto",
        max_bytes=50 * 1024**3,
        body_limit=64 * 1024**2,
        hooks=(),
        hook_env=None,
        hook_cwd=None,
    ):
        self.cache = Cache(directory, max_bytes)
        self.archive = Archive(archive, self.cache.root / "archive-index.sqlite3")
        self.token, self.policy = token, policy
        # Complete events carry both request and response as base64. Keep the
        # authenticated local envelope large enough for configured capture limits.
        self.event_limit = max(512 * 1024**2, body_limit * 3 + 1024**2)
        self.hooks, self.hook_env, self.hook_cwd = hooks, hook_env, hook_cwd
        self.lock = asyncio.Lock()
        self.replays = {}
        self.stats = Counter()
        self.base = ""
        self.log = self.cache.root / "navigations.jsonl"

    async def lookup(self, req):
        envelope = await req.json()
        request = envelope["request"]
        source = "worker" if envelope.get("worker") else "visitor"
        request_id = envelope.get("id", "-")
        hs = {k.lower(): v for k, v in headers(request)}
        if (
            not envelope.get("worker")
            and hs.get("sec-fetch-mode") == "navigate"
            and hs.get("sec-fetch-dest") == "document"
        ):
            await asyncio.to_thread(self.cache.enqueue, request["uri"])
            with self.log.open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "time": time.time(),
                            "url": request["uri"],
                            "method": request["method"],
                        }
                    )
                    + "\n"
                )
        # Upgrade streams are handled by Proxelar, never replayed as a cached 101.
        if (
            hs.get("upgrade", "").lower() == "websocket"
            or request["method"] == "CONNECT"
            or request.get("body_metadata", {}).get("truncated")
        ):
            # A capped request snapshot is only a prefix. It cannot share a
            # cache key with a complete smaller POST containing those bytes.
            return web.json_response({"block": self.policy == "offline"})
        key = fingerprint(request)
        async with self.lock:
            hit = (
                await asyncio.to_thread(
                    self.cache.get,
                    key,
                    self.policy in ("replay", "offline"),
                    request["method"],
                )
                if self.policy != "record"
                else None
            )
            if hit:
                LOG.info(
                    "HIT %s %s %s %s",
                    source,
                    request_id,
                    request["method"],
                    request["uri"],
                )
                return self.replay_ticket(hit, request["method"])
        if self.policy != "record":
            # Archive lookup can seek/decompress a WACZ on slow storage. Keep
            # that work outside the cache lock so existing hits remain fast.
            saved = await asyncio.to_thread(self.archive.lookup, request)
            if saved:
                status, fields, payload = saved
                async with self.lock:
                    await asyncio.to_thread(
                        self.cache.put, key, status, fields, payload, request["method"]
                    )
                    hit = await asyncio.to_thread(
                        self.cache.get, key, method=request["method"]
                    )
                    self.stats["archive_hit"] += 1
                    if hit:
                        LOG.info(
                            "ARCHIVE %s %s %s %s",
                            source,
                            request_id,
                            request["method"],
                            request["uri"],
                        )
                        return self.replay_ticket(hit, request["method"])
        self.stats["miss"] += 1
        LOG.info(
            "%s %s %s %s %s",
            "BLOCK" if self.policy == "offline" else "MISS",
            source,
            request_id,
            request["method"],
            request["uri"],
        )
        return web.json_response({"block": self.policy == "offline"})

    def replay_ticket(self, hit, method):
        # Open under the same lock as eviction. An already-open file stays
        # readable if LRU pruning removes its directory entry before delivery.
        stream = hit["path"].open("rb")
        ticket = secrets.token_urlsafe(32)
        self.replays[ticket] = (time.monotonic() + 60, hit, method, stream)
        self.stats["hit"] += 1
        # Send opaque header values as metadata, not through the local HTTP
        # server's UTF-8 header serializer. Preserve duplicates and original bytes.
        fields = [
            (k, v) for k, v in hit["headers"] if k.lower() != "x-archivebox-proxy-cache"
        ]
        fields.append(("X-ArchiveBox-Proxy-Cache", "HIT"))
        encoded = []
        for name, value in fields:
            try:
                raw = value.encode("latin1")
            except UnicodeEncodeError:
                raw = value.encode("utf8")
            encoded.append(
                {"name": name, "value_base64": base64.b64encode(raw).decode()}
            )
        return web.json_response(
            {
                "replay": f"{self.base}/replay/{ticket}",
                "status": hit["status"],
                "headers": encoded,
            }
        )

    async def event(self, req):
        event = await req.json()
        if "RequestComplete" in event:
            flow = event["RequestComplete"]
            source = (
                "worker"
                if req.headers.get("X-ArchiveBox-Proxy-Worker") == "true"
                else "visitor"
            )
            request, response = flow["request"], flow["response"]
            if any(
                k.lower() == "x-archivebox-proxy-cache" and v == "HIT"
                for k, v in headers(response)
            ):
                # Synthetic replay also emits RequestComplete. Re-storing it
                # would extend its TTL on every hit instead of expiring it.
                return web.Response(status=204)
            if any(
                m.get("body_metadata", {}).get("truncated") for m in (request, response)
            ):
                self.stats["truncated"] += 1
                LOG.warning(
                    "Capture incomplete or exceeded body limit: %s %s %s %s",
                    source,
                    flow["id"],
                    request["method"],
                    request["uri"],
                )
            elif self.policy != "offline":
                async with self.lock:
                    stored = await asyncio.to_thread(
                        self.cache.put,
                        fingerprint(request),
                        int(response["status"]),
                        headers(response),
                        body(response),
                        request["method"],
                    )
                    self.stats["stored" if stored else "uncacheable"] += 1
                    LOG.info(
                        "%s %s %s %s %s %s",
                        "STORED" if stored else "UNCACHED",
                        source,
                        flow["id"],
                        response["status"],
                        request["method"],
                        request["uri"],
                    )
        else:
            kind = next(iter(event))
            self.stats[kind] += 1
            # Keep native protocol events without inventing WARC encodings for
            # DNS, WebSocket messages or raw UDP. Rotation bounds debug storage.
            path = self.cache.root / "events.jsonl"
            if path.exists() and path.stat().st_size > 16 * 1024**2:
                path.replace(path.with_suffix(".previous.jsonl"))
            with path.open("a") as f:
                f.write(json.dumps(event) + "\n")
        return web.Response(status=204)

    async def replay(self, req):
        item = self.replays.pop(req.match_info["ticket"], None)
        if not item:
            raise web.HTTPNotFound()
        expires, hit, method, stream = item
        with stream as f:
            if expires < time.monotonic():
                raise web.HTTPNotFound()
            fields = CIMultiDict(hit["headers"])
            fields["X-ArchiveBox-Proxy-Cache"] = "HIT"
            response = web.StreamResponse(status=hit["status"], headers=fields)
            await response.prepare(req)
            if method != "HEAD":
                while chunk := await asyncio.to_thread(f.read, 128 * 1024):
                    await response.write(chunk)
            await response.write_eof()
        return response

    async def maintenance(self):
        while True:
            async with self.lock:
                await asyncio.to_thread(self.cache.prune)
            await asyncio.to_thread(self.archive.refresh)
            for ticket, item in list(self.replays.items()):
                if item[0] <= time.monotonic():
                    self.replays.pop(ticket)[3].close()
            await asyncio.sleep(60)

    async def run_hooks(self):
        from .hooks import HookRunner

        return await HookRunner(
            self.cache, self.hooks, self.hook_env, self.hook_cwd
        ).run()

    def app(self):
        @web.middleware
        async def auth(request, handler):
            if (
                not request.path.startswith("/replay/")
                and request.headers.get("Authorization") != f"Bearer {self.token}"
            ):
                raise web.HTTPUnauthorized()
            return await handler(request)

        app = web.Application(middlewares=[auth], client_max_size=self.event_limit)
        app.router.add_post("/lookup", self.lookup)
        app.router.add_post("/event", self.event)
        app.router.add_get("/replay/{ticket}", self.replay)

        async def health(_):
            return web.json_response({"status": "ok", "counters": dict(self.stats)})

        app.router.add_get("/health", health)

        async def close_replays(_):
            for item in self.replays.values():
                item[3].close()
            self.replays.clear()

        app.on_cleanup.append(close_replays)
        return app
