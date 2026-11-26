"""Run independent Proxelar listeners against one cache and optional hooks."""

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import signal
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .server import Controller

LOG = logging.getLogger(__name__)


def argv(value):
    command = json.loads(value)
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
    ):
        raise argparse.ArgumentTypeError(
            "expected a nonempty JSON array of command arguments"
        )
    if not command[0]:
        raise argparse.ArgumentTypeError("command executable cannot be empty")
    return command


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument(
        "--engine",
        default=shutil.which("archivebox-proxy-engine")
        or str(
            Path(__file__).resolve().parents[1] / "target/debug/archivebox-proxy-engine"
        ),
    )
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--worker-bind", default="127.0.0.1")
    p.add_argument("--worker-port", type=int, default=8082)
    p.add_argument(
        "--mode",
        choices=["forward", "socks5", "wireguard", "dns", "reverse", "udp"],
        default="forward",
    )
    p.add_argument("--target")
    p.add_argument("--wireguard-endpoint")
    p.add_argument("--dns-upstream", default="1.1.1.1:53")
    p.add_argument(
        "--policy", choices=["auto", "record", "replay", "offline"], default="auto"
    )
    p.add_argument(
        "--archive", type=Path, help="read-only ArchiveBox collection for fallback"
    )
    p.add_argument("--cache-gb", type=float, default=50)
    p.add_argument("--body-limit", type=int, default=64 * 1024**2)
    p.add_argument(
        "--hook",
        type=argv,
        action="append",
        default=[],
        help="JSON argv array; {url} is substituted literally",
    )
    p.add_argument(
        "--archivebox-dir",
        type=Path,
        help="enable archivebox add hook in this initialized collection",
    )
    p.add_argument(
        "--archivebox-command",
        type=argv,
        default=["archivebox"],
        help='JSON argv prefix for ArchiveBox, e.g. ["uv","run","--project","/src/archivebox","archivebox"]',
    )
    p.add_argument(
        "--worker-proxy", help="worker proxy URL reachable by hook processes"
    )
    for name, help_text in (
        ("record", "enqueue navigations and execute configured capture hooks"),
        ("cache", "read and populate the native response cache"),
        ("replay", "use the ArchiveBox collection as a replay fallback"),
    ):
        p.add_argument(
            f"--{name}",
            action=argparse.BooleanOptionalAction,
            default=True,
            help=help_text,
        )
    # argparse converts string defaults through each option's existing type;
    # explicitly supplied flags take precedence over their environment defaults.
    for action in p._actions:
        if action.dest in ("help", "hook"):
            continue
        key = "PROXY_" + action.dest.upper()
        value = os.environ.get(key)
        if value is None and action.dest == "archivebox_dir":
            value = os.environ.get("ARCHIVEBOX_DATA")
        if value is None and action.dest == "wireguard_endpoint":
            value = os.environ.get("WIREGUARD_ENDPOINT")
        if value is not None and (
            value or action.dest in ("record", "cache", "replay")
        ):
            action.default = value
    # Repeated CLI --hook arguments replace PROXY_HOOK, rather than silently
    # executing both environment and command-line side effects.
    p.set_defaults(hook=None)
    return p


def address(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def hook_environment(directory, ca_dir, proxy):
    env = dict(os.environ)
    env.update(
        {
            key: proxy
            for key in (
                "http_proxy",
                "https_proxy",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "all_proxy",
            )
        }
    )
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    env["NODE_EXTRA_CA_CERTS"] = str(ca_dir / "proxelar-ca.pem")
    env["NODE_USE_ENV_PROXY"] = "1"
    for key in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    ):
        env[key] = str(directory / "trust.pem")
    # Keep established system roots as well as our CA, including on platforms
    # whose ssl module has no default PEM path (e.g. system certificate stores).
    context = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if paths.cafile:
        context.load_verify_locations(cafile=paths.cafile)
    if paths.capath:
        # OpenSSL loads capath roots lazily, so get_ca_certs() alone can export
        # an empty bundle. Materialize each PEM before adding the proxy's CA.
        for path in Path(paths.capath).iterdir():
            if path.is_file():
                try:
                    context.load_verify_locations(cafile=str(path))
                except ssl.SSLError:
                    pass  # Certificate directories can also contain CRLs.
    certificates = [
        ssl.DER_cert_to_PEM_cert(cert).encode()
        for cert in context.get_ca_certs(binary_form=True)
    ]
    if not certificates:
        raise RuntimeError("No system CA roots loaded; configure SSL_CERT_FILE")
    (directory / "trust.pem").write_bytes(
        b"".join(certificates) + (ca_dir / "proxelar-ca.pem").read_bytes()
    )
    wgetrc = directory / "wgetrc"
    wgetrc.write_text(
        f"use_proxy = on\nhttp_proxy = {proxy}\nhttps_proxy = {proxy}\nca_certificate = {directory / 'trust.pem'}\n"
    )
    env["WGETRC"] = str(wgetrc)
    return env


async def chrome_environment(env, ca_dir, proxy):
    chrome = json.loads(env.get("CHROME_ARGS_EXTRA", "[]"))
    if not isinstance(chrome, list) or any(not isinstance(arg, str) for arg in chrome):
        raise ValueError("CHROME_ARGS_EXTRA must be a JSON array of strings")
    # A stale no-proxy/PAC flag takes precedence over --proxy-server in Chrome,
    # so the capture browser must not inherit a conflicting routing choice.
    chrome = [
        arg
        for arg in chrome
        if arg not in ("--no-proxy-server", "--proxy-auto-detect")
        and not arg.startswith(("--proxy-server=", "--proxy-pac-url="))
    ]
    chrome.append("--proxy-server=" + proxy)
    # Chrome does not consistently consult SSL_CERT_FILE. Trust precisely this
    # proxy's CA SPKI for the hook browser, preserving normal certificate checks.
    proc = await asyncio.create_subprocess_exec(
        "openssl",
        "x509",
        "-in",
        str(ca_dir / "proxelar-ca.pem"),
        "-pubkey",
        "-noout",
        stdout=asyncio.subprocess.PIPE,
    )
    pem, _ = await proc.communicate()
    if proc.returncode:
        raise RuntimeError("cannot read Proxelar CA public key")
    proc = await asyncio.create_subprocess_exec(
        "openssl",
        "pkey",
        "-pubin",
        "-outform",
        "DER",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    der, _ = await proc.communicate(pem)
    if proc.returncode:
        raise RuntimeError("cannot encode Proxelar CA public key")
    pin = base64.b64encode(hashlib.sha256(der).digest()).decode()
    pins = [
        arg.split("=", 1)[1]
        for arg in chrome
        if arg.startswith("--ignore-certificate-errors-spki-list=")
    ]
    chrome = [
        arg
        for arg in chrome
        if not arg.startswith("--ignore-certificate-errors-spki-list=")
    ]
    chrome.append("--ignore-certificate-errors-spki-list=" + ",".join([*pins, pin]))
    env["CHROME_ARGS_EXTRA"] = json.dumps(chrome)


async def wait_listener(process, host, port):
    """Confirm the actual listener opened; existing CA files are not readiness."""
    connect_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    async with asyncio.timeout(10):
        while True:
            if process.returncode is not None:
                raise RuntimeError(f"proxy engine exited {process.returncode}")
            try:
                _, writer = await asyncio.open_connection(connect_host, port)
            except OSError:
                await asyncio.sleep(0.05)
            else:
                writer.close()
                await writer.wait_closed()
                # Yield once so a bind failure cannot hide behind a different
                # process that already owns this port.
                await asyncio.sleep(0.05)
                if process.returncode is not None:
                    raise RuntimeError(f"proxy engine exited {process.returncode}")
                return


async def engine_logs(process, ready=False):
    """Use Proxelar's post-bind log for UDP without sending upstream probes."""
    while line := await process.stdout.readline():
        print(line.decode(errors="replace").rstrip(), flush=True)
        if ready and b"proxy listening on " in line:
            return
    if ready:
        raise RuntimeError(f"proxy engine exited {await process.wait()}")


async def serve(args):
    args.data = args.data.resolve()
    args.data.mkdir(parents=True, exist_ok=True)
    os.chmod(args.data, 0o700)
    ca_dir = args.data / "ca"
    token = secrets.token_urlsafe(32)
    hooks = list(args.hook)
    if args.archivebox_dir and args.record:
        if not (args.archivebox_dir / "index.sqlite3").is_file():
            raise ValueError(
                "--archivebox-dir must be an initialized ArchiveBox collection"
            )
        hooks.append([*args.archivebox_command, "add", "--depth=0", "{url}"])
    proxy_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(
        args.worker_bind, args.worker_bind
    )
    proxy = args.worker_proxy or "http://" + address(proxy_host, args.worker_port)
    parsed = urlsplit(proxy)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port == 0
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "--worker-proxy must be an HTTP proxy URL without credentials or a path"
        )
    controller = Controller(
        args.data,
        token,
        archive=args.archive,
        policy=args.policy,
        max_bytes=int(args.cache_gb * 1024**3),
        body_limit=args.body_limit,
        record=args.record,
        caching=args.cache,
        replay=args.replay,
        hooks=hooks,
        hook_cwd=args.archivebox_dir.resolve() if args.archivebox_dir else None,
    )
    runner = web.AppRunner(controller.app())
    processes, tasks, log_tasks = [], [], []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        if args.replay:
            await asyncio.to_thread(controller.archive.refresh)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        controller.base = f"http://127.0.0.1:{port}"
        # The worker always initializes the shared CA first, including when the
        # visitor listener is DNS/UDP and would never initialize a CA itself.
        for mode, bind, port, worker in [
            ("forward", args.worker_bind, args.worker_port, True),
            (args.mode, args.bind, args.port, False),
        ]:
            command = [
                args.engine,
                "--backend",
                controller.base,
                "--ca-dir",
                str(ca_dir),
                "--bind",
                address(bind, port),
                "--mode",
                mode,
                "--body-limit",
                str(args.body_limit),
                "--dns-upstream",
                args.dns_upstream,
            ]
            if worker:
                command.append("--worker")
            elif args.target:
                command += ["--target", args.target]
            if mode == "wireguard":
                command += [
                    "--wireguard-endpoint",
                    args.wireguard_endpoint or address(args.bind, port),
                ]
            tcp = mode in ("forward", "socks5") or (
                mode == "reverse" and not args.target.startswith("http3://")
            )
            engine_env = {**os.environ, "ARCHIVEBOX_PROXY_BACKEND_TOKEN": token}
            if not tcp:
                engine_env["RUST_LOG"] = ",".join(
                    filter(None, [engine_env.get("RUST_LOG"), "proxyapi::proxy=info"])
                )
            process = await asyncio.create_subprocess_exec(
                *command,
                env=engine_env,
                stdout=None if tcp else asyncio.subprocess.PIPE,
            )
            processes.append(process)
            if tcp:
                await wait_listener(process, bind, port)
            else:
                async with asyncio.timeout(10):
                    await engine_logs(process, ready=True)
                log_tasks.append(asyncio.create_task(engine_logs(process)))
        env = await asyncio.to_thread(hook_environment, args.data, ca_dir, proxy)
        await chrome_environment(env, ca_dir, proxy)
        controller.hook_env = env
        tasks = [
            asyncio.create_task(controller.maintenance()),
            asyncio.create_task(controller.run_hooks()),
        ]
        tasks += [asyncio.create_task(process.wait()) for process in processes]
        tasks.append(asyncio.create_task(stop.wait()))
        LOG.info(
            "Proxy on %s; worker proxy %s; CA %s",
            address(args.bind, args.port),
            proxy,
            ca_dir / "proxelar-ca.pem",
        )
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            for task in done:
                task.result()
            raise RuntimeError("proxy or controller stopped unexpectedly")
    finally:
        # Stop hooks first, while their worker proxy is still available for a
        # clean shutdown. HookRunner terminates the complete subprocess group.
        for task in tasks[:2]:
            task.cancel()
        await asyncio.gather(*tasks[:2], return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.send_signal(signal.SIGINT)
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in tasks[2:]:
            task.cancel()
        await asyncio.gather(*tasks[2:], return_exceptions=True)
        await asyncio.gather(*log_tasks, return_exceptions=True)
        await runner.cleanup()
        # Cancelling an asyncio.to_thread await does not stop its SQLite/file
        # work. Let maintenance and request threads finish before closing DBs.
        await loop.shutdown_default_executor()
        controller.cache.close()
        controller.archive.db.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    options = parser()
    args = options.parse_args()
    for action in options._actions:
        if action.choices and getattr(args, action.dest) not in action.choices:
            options.error(
                f"PROXY_{action.dest.upper()} must be one of: {', '.join(action.choices)}"
            )
    for name in ("record", "cache", "replay"):
        value = getattr(args, name)
        if isinstance(value, str):
            if value.lower() not in (
                "true",
                "false",
                "1",
                "0",
                "yes",
                "no",
                "on",
                "off",
            ):
                options.error(f"PROXY_{name.upper()} must be true or false")
            setattr(args, name, value.lower() in ("true", "1", "yes", "on"))
    if args.hook is None:
        try:
            args.hook = (
                [argv(os.environ["PROXY_HOOK"])] if os.environ.get("PROXY_HOOK") else []
            )
        except (ValueError, argparse.ArgumentTypeError) as exc:
            options.error(f"PROXY_HOOK: {exc}")
    if (
        args.body_limit <= 0
        or args.cache_gb <= 0
        or not 0 < args.port < 65536
        or not 0 < args.worker_port < 65536
    ):
        raise SystemExit("Use positive cache limits and ports between 1 and 65535")
    if args.port == args.worker_port and args.bind == args.worker_bind:
        raise SystemExit("Use distinct visitor and worker listener addresses")
    if args.mode in ("reverse", "udp") and not args.target:
        raise SystemExit("--target is required for reverse and UDP modes")
    if args.mode == "wireguard" and not args.wireguard_endpoint:
        raise SystemExit("--wireguard-endpoint is required for WireGuard mode")
    try:
        asyncio.run(serve(args))
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
