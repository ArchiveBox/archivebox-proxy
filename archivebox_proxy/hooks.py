"""Durable asynchronous navigation hooks, executed as literal argument vectors."""

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

LOG = logging.getLogger(__name__)


def append_log(path, data, *, sync=False):
    """Keep the current and previous output files bounded while hooks stream."""
    if path.exists() and path.stat().st_size + len(data) > 16 * 1024**2:
        path.replace(path.with_name(path.stem + ".previous" + path.suffix))
    with path.open("ab") as output:
        output.write(data)
        if sync:
            output.flush()
            os.fsync(output.fileno())


async def wait_exit(process):
    # asyncio Process.wait() can wait for pipe EOF after the leader has exited.
    # An explicitly detached descendant may retain stdout, so watch the leader's
    # actual exit independently. This puts no deadline on a running hook.
    while process.returncode is None:
        await asyncio.sleep(0.05)
    return process.returncode


async def stop_process(process, grace=10):
    """Stop a hook's group even when its leader exits before its descendants."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(wait_exit(process), grace)
    except TimeoutError:
        pass
    finally:
        # A successful wait only proves the leader exited. Background children
        # can still hold sockets and output pipes open after their parent exits.
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait_for(wait_exit(process), grace)


class HookRunner:
    def __init__(self, cache, commands, env=None, cwd=None):
        self.cache = cache
        self.commands = tuple(tuple(command) for command in commands)
        self.env = env
        self.cwd = Path(cwd) if cwd else None
        self.events = cache.root / "hooks.jsonl"

    def report(self, **event):
        append_log(
            self.events,
            (json.dumps({"time": time.time(), **event}) + "\n").encode(),
            sync=True,
        )

    def recover(self):
        # Only claims whose owner died before starting a subprocess are safe to
        # repeat. Once launch is imminent, a crash is ambiguous and needs review.
        with self.cache.lock:
            claims = self.cache.db.execute(
                "SELECT url,error FROM queue WHERE error LIKE 'claimed by %'"
            ).fetchall()
        for url, error in claims:
            try:
                os.kill(int(error.removeprefix("claimed by ")), 0)
            except ProcessLookupError:
                with self.cache.lock:
                    self.cache.db.execute(
                        "UPDATE queue SET submitted=NULL,error=NULL WHERE url=? AND error=?",
                        (url, error),
                    )
                    self.cache.db.commit()
            except (ValueError, PermissionError):
                continue

    def claim(self):
        with self.cache.lock:
            row = self.cache.db.execute(
                "UPDATE queue SET submitted=?,error=? WHERE url=("
                "SELECT url FROM queue WHERE submitted IS NULL AND due<=? ORDER BY due LIMIT 1"
                ") RETURNING url",
                (time.time(), f"claimed by {os.getpid()}", time.time()),
            ).fetchone()
            self.cache.db.commit()
            return row[0] if row else None

    def finish(self, url, error, *, release=False):
        with self.cache.lock:
            self.cache.db.execute(
                "UPDATE queue SET error=?"
                + (",submitted=NULL" if release else "")
                + " WHERE url=?",
                (error, url),
            )
            self.cache.db.commit()

    async def output(self, process):
        while chunk := await process.stdout.read(64 * 1024):
            await asyncio.to_thread(append_log, self.cache.root / "hooks.log", chunk)

    async def execute(self, url):
        error, started = None, False
        process, argv = None, None
        try:
            for command in self.commands:
                process = None
                argv = [argument.replace("{url}", url) for argument in command]
                await asyncio.to_thread(
                    self.report, url=url, command=argv, event="start"
                )
                await asyncio.to_thread(
                    self.finish, url, "started; inspect hooks.jsonl before resubmitting"
                )
                started = True
                # Shield process creation so cancellation cannot lose a child
                # between OS spawn and receiving its asyncio process handle.
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *argv,
                        cwd=self.cwd,
                        env=self.env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=os.name == "posix",
                    )
                )
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    process = await spawn
                    try:
                        await stop_process(process)
                    finally:
                        process._transport.close()
                    raise
                output = asyncio.create_task(self.output(process))
                waiter = asyncio.create_task(wait_exit(process))
                try:
                    done, _ = await asyncio.wait(
                        (output, waiter), return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                    code = await waiter
                finally:
                    try:
                        await stop_process(process)
                        # Cleanup alone is bounded: detached children can hold
                        # the pipe forever after the hook leader has finished.
                        try:
                            await asyncio.wait_for(asyncio.shield(output), 1)
                        except TimeoutError:
                            LOG.warning(
                                "Closing hook output still open after exit: %s", argv[0]
                            )
                    finally:
                        # StreamReader exposes no public close(). Closing its
                        # subprocess transport releases inherited output pipes.
                        process._transport.close()
                        output.cancel()
                        waiter.cancel()
                        await asyncio.gather(output, waiter, return_exceptions=True)
                await asyncio.to_thread(
                    self.report, url=url, command=argv, event="finish", returncode=code
                )
                if code:
                    raise RuntimeError(f"hook exited {code}: {argv[0]}")
        except asyncio.CancelledError:
            error = (
                "interrupted; inspect hooks.jsonl before resubmitting"
                if started
                else None
            )
            try:
                await asyncio.to_thread(
                    self.report,
                    url=url,
                    command=argv,
                    event="interrupted",
                    returncode=process.returncode if process else None,
                )
            except Exception:
                LOG.exception("Could not write hook interruption event")
            raise
        except Exception as exc:
            error = str(exc)
            LOG.exception("Hook failed for %s", url)
            try:
                await asyncio.to_thread(
                    self.report, url=url, event="error", error=error
                )
            except Exception:
                LOG.exception("Could not write hook error event")
        finally:
            try:
                await asyncio.to_thread(
                    self.finish, url, error, release=not started and error is None
                )
            except Exception:
                LOG.exception("Could not update hook queue")

    async def run(self):
        if not self.commands:
            await asyncio.Event().wait()
        try:
            await asyncio.to_thread(self.recover)
        except Exception:
            LOG.exception("Could not recover hook queue")
        while True:
            try:
                url = await asyncio.to_thread(self.claim)
                if url is not None:
                    await self.execute(url)
                    continue
            except Exception:
                LOG.exception("Hook queue failed")
            await asyncio.sleep(0.25)
