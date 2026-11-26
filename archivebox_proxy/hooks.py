"""Durable asynchronous navigation hooks, executed as literal argument vectors."""

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

LOG = logging.getLogger(__name__)


async def stop_process(process, grace=10):
    """Stop a hook and its descendants so capture workers cannot outlive the proxy."""
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), grace)
    except TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


class HookRunner:
    def __init__(self, cache, commands, env=None, cwd=None):
        self.cache = cache
        self.commands = tuple(tuple(command) for command in commands)
        self.env = env
        self.cwd = Path(cwd) if cwd else None
        self.events = cache.root / "hooks.jsonl"

    def report(self, **event):
        with self.events.open("a") as output:
            output.write(json.dumps({"time": time.time(), **event}) + "\n")
            output.flush()
            os.fsync(output.fileno())

    def claim(self):
        with self.cache.lock:
            # Claim in one statement so overlapping process restarts cannot
            # both launch the same hook. A side effect may commit before a
            # crash, so ambiguous starts are not automatically replayed.
            row = self.cache.db.execute(
                "UPDATE queue SET submitted=?,error=NULL WHERE url=("
                "SELECT url FROM queue WHERE submitted IS NULL AND due<=? ORDER BY due LIMIT 1"
                ") RETURNING url",
                (time.time(), time.time()),
            ).fetchone()
            self.cache.db.commit()
            return row[0] if row else None

    async def execute(self, url):
        error = None
        try:
            for command in self.commands:
                argv = [argument.replace("{url}", url) for argument in command]
                self.report(url=url, command=argv, event="start")
                # Shell syntax in a URL is data. Keeping argv intact also lets
                # callers use uv/docker prefixes without another wrapper script.
                with (self.cache.root / "hooks.log").open("ab") as output:
                    process = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=self.cwd,
                        env=self.env,
                        stdout=output,
                        stderr=output,
                        start_new_session=os.name == "posix",
                    )
                    try:
                        code = await process.wait()
                    except asyncio.CancelledError:
                        await stop_process(process)
                        self.report(
                            url=url,
                            command=argv,
                            event="interrupted",
                            returncode=process.returncode,
                        )
                        raise
                self.report(url=url, command=argv, event="finish", returncode=code)
                if code:
                    raise RuntimeError(f"hook exited {code}: {argv[0]}")
        except asyncio.CancelledError:
            error = "interrupted; inspect hooks.jsonl before resubmitting"
            raise
        except (OSError, RuntimeError) as exc:
            error = str(exc)
            self.report(url=url, event="error", error=error)
            LOG.error("%s", error)
        finally:
            with self.cache.lock:
                self.cache.db.execute(
                    "UPDATE queue SET error=? WHERE url=?", (error, url)
                )
                self.cache.db.commit()

    async def run(self):
        while True:
            url = await asyncio.to_thread(self.claim)
            if url is None:
                await asyncio.sleep(0.25)
            else:
                await self.execute(url)
