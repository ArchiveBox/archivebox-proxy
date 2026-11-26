"""Browse through a caching proxy and archive your visits with ArchiveBox."""

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import secrets
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
from importlib.metadata import version
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from platformdirs import user_data_path

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


def default_engine():
    bundled = Path(__file__).resolve().parent / "bin/archivebox-proxy-engine"
    if bundled.is_file():
        return str(bundled)
    # Editable source checkouts use their local build; installed wheels always
    # carry the executable and never download or compile anything at startup.
    source = Path(__file__).resolve().parents[1]
    development = source / "target/debug/archivebox-proxy-engine"
    if (source / "Cargo.toml").is_file() and development.is_file():
        return str(development)
    # Retain existing source installations that placed their compiled executable
    # on PATH, while preferring the matching executable shipped in each wheel.
    return shutil.which("archivebox-proxy-engine") or str(bundled)


def parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        usage="%(prog)s [--http | --socks | --wireguard | --dns | --reverse | --udp] [options]",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"ArchiveBox Proxy {version('archivebox-proxy')}",
    )
    connection = p.add_argument_group("Proxy")
    transports = connection.add_mutually_exclusive_group()
    for flag, mode, description in (
        ("http", "forward", "HTTP and HTTPS proxy (default)"),
        ("socks", "socks5", "SOCKS5 proxy"),
        (
            "wireguard",
            "wireguard",
            "WireGuard tunnel with an importable client configuration",
        ),
        ("dns", "dns", "DNS proxy"),
        ("reverse", "reverse", "Reverse proxy to --target URL"),
        ("udp", "udp", "UDP proxy to --target HOST:PORT"),
    ):
        transports.add_argument(
            f"--{flag}", dest="mode", action="store_const", const=mode, help=description
        )
    transports.add_argument(
        "--mode",
        choices=["forward", "socks5", "wireguard", "dns", "reverse", "udp"],
        help=argparse.SUPPRESS,
    )
    p.set_defaults(mode="forward")
    connection.add_argument(
        "--bind",
        default="127.0.0.1",
        metavar="ADDRESS",
        help="Listen on this address (default: 127.0.0.1)",
    )
    connection.add_argument(
        "--port", type=int, default=8080, help="Listen on this port (default: 8080)"
    )
    connection.add_argument(
        "--target",
        metavar="ADDRESS",
        help="Upstream URL for --reverse, or HOST:PORT for --udp",
    )
    connection.add_argument(
        "--wireguard-endpoint",
        metavar="HOST:PORT",
        help="Address your WireGuard client connects to",
    )
    connection.add_argument(
        "--dns-upstream",
        default="1.1.1.1:53",
        metavar="IP:PORT",
        help="DNS resolver (default: 1.1.1.1:53)",
    )
    certificates = connection.add_mutually_exclusive_group()
    certificates.add_argument(
        "--install-ca",
        nargs="?",
        const=True,
        type=Path,
        metavar="FILE",
        help="Trust FILE or initialize and trust this proxy's CA, then exit",
    )
    certificates.add_argument(
        "--export-ca",
        nargs="?",
        const=Path("-"),
        type=Path,
        metavar="FILE",
        help="Export the existing CA certificate to FILE or stdout and exit",
    )

    archivebox = p.add_argument_group("ArchiveBox")
    archivebox.add_argument(
        "--archivebox",
        "--archivebox-dir",
        dest="archivebox_dir",
        type=Path,
        metavar="DIRECTORY",
        help="Capture visits in this initialized ArchiveBox collection",
    )
    archivebox.add_argument(
        "--persona", metavar="NAME", help="Use this ArchiveBox persona for captures"
    )
    archivebox.add_argument(
        "--archive",
        type=Path,
        metavar="DIRECTORY",
        help="Replay from this collection (default: the capture collection)",
    )
    archivebox.add_argument(
        "--record",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Record navigations and run configured capture hooks (default: enabled)",
    )

    cache = p.add_argument_group("Cache")
    cache.add_argument(
        "--data",
        type=Path,
        default=user_data_path("archivebox-proxy", appauthor=False),
        metavar="DIRECTORY",
        help="Store cache, certificates and logs here (default: your user data directory)",
    )
    cache.add_argument(
        "--cache-gb",
        type=float,
        default=50,
        metavar="GIB",
        help="Maximum disk cache size (default: 50 GiB)",
    )
    cache.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Read and populate the response cache (default: enabled)",
    )
    cache.add_argument(
        "--replay",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Replay responses from the ArchiveBox collection (default: enabled)",
    )
    cache.add_argument(
        "--policy",
        choices=["auto", "record", "replay", "offline"],
        default="auto",
        help="Response lookup policy (default: auto)",
    )

    advanced = p.add_argument_group("Advanced")
    advanced.add_argument(
        "--hook",
        type=argv,
        action="append",
        metavar="ARGV",
        help="Run an additional navigation hook (JSON argument array)",
    )
    advanced.add_argument(
        "--archivebox-command",
        type=argv,
        default=["archivebox"],
        help=argparse.SUPPRESS,
    )
    advanced.add_argument("--engine", default=default_engine(), help=argparse.SUPPRESS)
    advanced.add_argument(
        "--worker-bind",
        default="127.0.0.1",
        metavar="ADDRESS",
        help="Capture listener address (default: 127.0.0.1)",
    )
    advanced.add_argument(
        "--worker-port",
        type=int,
        default=8082,
        metavar="PORT",
        help="Capture listener port (default: 8082)",
    )
    advanced.add_argument(
        "--worker-proxy",
        metavar="URL",
        help="Capture proxy URL reachable by hook processes",
    )
    advanced.add_argument(
        "--body-limit",
        type=int,
        default=64 * 1024**2,
        metavar="BYTES",
        help="Maximum captured request/response size (default: 64 MiB)",
    )
    advanced.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Show request and connection logs",
    )
    # argparse converts string defaults through each option's existing type;
    # explicitly supplied flags take precedence over their environment defaults.
    for action in p._actions:
        if action.dest in ("help", "version", "hook", "install_ca"):
            continue
        key = "PROXY_" + action.dest.upper()
        value = os.environ.get(key)
        if value is None and action.dest == "archivebox_dir":
            value = os.environ.get("ARCHIVEBOX_DATA")
        if value is None and action.dest == "wireguard_endpoint":
            value = os.environ.get("WIREGUARD_ENDPOINT")
        if value is not None and (
            value or action.dest in ("record", "cache", "replay", "verbose")
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
    certificate = x509.load_pem_x509_certificate(
        (ca_dir / "proxelar-ca.pem").read_bytes()
    )
    der = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
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
                raise RuntimeError(
                    f"Proxy process exited with status {process.returncode}"
                )
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
                    raise RuntimeError(
                        f"Proxy process exited with status {process.returncode}"
                    )
                return


async def engine_logs(process, ready=False):
    """Use Proxelar's post-bind log for UDP without sending upstream probes."""
    while line := await process.stdout.readline():
        message = line.decode(errors="replace").rstrip()
        if message.startswith("Error:"):
            LOG.error("%s", message.removeprefix("Error:").strip())
        else:
            LOG.info("%s", message)
        if ready and b"proxy listening on " in line:
            return
    if ready:
        raise RuntimeError(f"Proxy process exited with status {await process.wait()}")


def startup(args, proxy, ca_dir):
    endpoint = address(args.bind, args.port)
    schemes = {
        "forward": "http",
        "socks5": "socks5h",
        "dns": "dns",
        "wireguard": "wireguard",
        "udp": "udp",
    }
    scheme = (
        urlsplit(args.target).scheme if args.mode == "reverse" else schemes[args.mode]
    )
    labels = {
        "forward": "HTTP proxy",
        "socks5": "SOCKS5 proxy",
        "wireguard": "WireGuard",
        "dns": "DNS proxy",
        "reverse": "Reverse proxy",
        "udp": "UDP proxy",
    }
    lines = [
        "",
        "ArchiveBox Proxy is ready",
        "",
        f"  {labels[args.mode]}: {scheme}://{endpoint}",
        f"  Data: {args.data}",
        f"  Logs: {args.data / 'proxy.log'}",
    ]
    if args.mode not in ("dns", "udp"):
        lines.append(f"  Certificate: {ca_dir / 'proxelar-ca.pem'}")
    if args.mode == "wireguard":
        lines.extend(
            [
                f"  Client configuration: {ca_dir / 'proxelar-wg.conf'}",
                f"  Endpoint: {args.wireguard_endpoint}",
            ]
        )
    if args.archivebox_dir and args.record:
        lines.append(f"  ArchiveBox: {args.archivebox_dir}")
        if args.persona:
            lines.append(f"  Persona: {args.persona}")
        lines.append(f"  Capture proxy: {proxy}")
    if args.archive and args.replay:
        lines.append(f"  Archive replay: {args.archive}")
    lines.append("")
    if args.mode == "forward":
        lines.append(f"Set your browser's HTTP and HTTPS proxy to {endpoint}.")
    elif args.mode == "socks5":
        lines.append(f"Set your browser's SOCKS5 proxy to {endpoint}.")
    elif args.mode == "wireguard":
        lines.append(
            "Import the client configuration in WireGuard and activate the tunnel."
        )
    elif args.mode == "dns":
        lines.append(f"Send DNS queries to {endpoint} (upstream: {args.dns_upstream}).")
    elif args.mode in ("reverse", "udp"):
        lines.append(f"Requests are forwarded to {args.target}.")
    if args.mode not in ("dns", "udp"):
        command = ["archivebox-proxy", "--install-ca"]
        if args.data != user_data_path("archivebox-proxy", appauthor=False).resolve():
            command += ["--data", str(args.data)]
        lines.append("Trust HTTPS certificates: " + shlex.join(command))
    print("\n".join(lines) + "\n", flush=True)


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
        command = [*args.archivebox_command, "add", "--depth=0"]
        if args.persona:
            command += ["--persona", args.persona]
        hooks.append([*command, "{url}"])
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
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(process)
            if tcp:
                log_tasks.append(asyncio.create_task(engine_logs(process)))
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
        startup(args, proxy, ca_dir)
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
    options = parser()
    args = options.parse_args()
    if args.install_ca is not None:
        from .trust import install_ca

        try:
            if args.install_ca is True:
                executable = shutil.which(args.engine)
                if not executable:
                    raise RuntimeError(
                        "ArchiveBox Proxy is missing its executable. Reinstall archivebox-proxy for this platform."
                    )
                directory = args.data.expanduser().resolve()
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
                result = subprocess.run(
                    [executable, "--init-ca", "--ca-dir", str(directory / "ca")],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(
                        "Could not initialize certificate: "
                        + (result.stderr or result.stdout).strip()
                    )
                certificate = directory / "ca/proxelar-ca.pem"
            else:
                # A remote proxy's exported CA must be trusted as-is; generating
                # a different local CA would not trust that proxy's connections.
                certificate = args.install_ca.expanduser().resolve()
            print("Installing the proxy certificate authority...", flush=True)
            profiles = install_ca(certificate)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Certificate installed and trusted.\nCertificate: {certificate}")
        if profiles:
            print(f"Browser certificate databases updated: {profiles}")
        return
    if args.export_ca is not None:
        certificate = args.data.expanduser().resolve() / "ca/proxelar-ca.pem"
        # Export only the existing public certificate, even when the proxy is
        # stopped or its executable is unavailable. Never create a second CA.
        try:
            pem = certificate.read_bytes()
        except FileNotFoundError:
            raise SystemExit(
                f"Certificate not found: {certificate}\n"
                "Start ArchiveBox Proxy and wait for 'ArchiveBox Proxy is ready'."
            ) from None
        except OSError as exc:
            raise SystemExit(f"Could not read certificate: {exc}") from exc
        try:
            if args.export_ca == Path("-"):
                sys.stdout.buffer.write(pem)
            else:
                args.export_ca.expanduser().write_bytes(pem)
        except OSError as exc:
            raise SystemExit(f"Could not export certificate: {exc}") from exc
        return
    for action in options._actions:
        if action.choices and getattr(args, action.dest) not in action.choices:
            options.error(
                f"PROXY_{action.dest.upper()} must be one of: {', '.join(action.choices)}"
            )
    for name in ("record", "cache", "replay", "verbose"):
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
        if args.bind in ("0.0.0.0", "::"):
            raise SystemExit(
                "Set --wireguard-endpoint HOST:PORT so clients can reach this proxy"
            )
        args.wireguard_endpoint = address(args.bind, args.port)
    if args.persona and not args.archivebox_dir:
        raise SystemExit("--persona requires --archivebox DIRECTORY")
    if args.archivebox_dir:
        args.archivebox_dir = args.archivebox_dir.expanduser().resolve()
    if args.archive is None:
        args.archive = args.archivebox_dir
    elif args.archive:
        args.archive = args.archive.expanduser().resolve()
    executable = shutil.which(args.engine)
    if not executable:
        raise SystemExit(
            "ArchiveBox Proxy is missing its executable. Reinstall archivebox-proxy for this platform."
        )
    args.engine = executable
    args.data = args.data.expanduser().resolve()
    args.data.mkdir(parents=True, exist_ok=True)
    os.chmod(args.data, 0o700)
    file_log = RotatingFileHandler(
        args.data / "proxy.log", maxBytes=16 * 1024**2, backupCount=1
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if args.verbose else logging.WARNING)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_log.setFormatter(formatter)
    console.setFormatter(formatter)
    # Per-request logs stay available without synchronous file writes on the
    # proxy event loop, or overwhelming the normal connection instructions.
    log_queue = queue.SimpleQueue()
    listener = QueueListener(log_queue, file_log, console, respect_handler_level=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[QueueHandler(log_queue)],
    )
    listener.start()
    try:
        asyncio.run(serve(args))
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        raise SystemExit(f"{exc}\nLogs: {args.data / 'proxy.log'}") from exc
    finally:
        listener.stop()
        file_log.close()


if __name__ == "__main__":
    main()
