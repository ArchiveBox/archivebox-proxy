"""Exercise an installed wheel against live HTTPS without a source engine."""

import http.client
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

import archivebox_proxy


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(process, condition, description):
    deadline = time.monotonic() + 20
    while not condition():
        if process.poll() is not None:
            raise AssertionError(f"Proxy exited with {process.returncode}")
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        time.sleep(0.05)


def main():
    package = Path(archivebox_proxy.__file__).resolve().parent
    engine = package / "bin/archivebox-proxy-engine"
    assert engine.is_file() and os.access(engine, os.X_OK), engine
    subprocess.run([str(engine), "--help"], check=True, capture_output=True)
    cli = shutil.which("archivebox-proxy")
    assert cli, "Installed console entry point is missing"
    subprocess.run([cli, "--version"], check=True)

    with tempfile.TemporaryDirectory(prefix="archivebox-proxy-wheel-") as directory:
        root = Path(directory)
        data = root / "data"
        visitor, worker = free_port(), free_port()
        while visitor == worker:
            worker = free_port()
        # Do not inherit a developer's capture collection or transport settings.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PROXY_")
        }
        env.pop("ARCHIVEBOX_DATA", None)
        env.pop("WIREGUARD_ENDPOINT", None)
        console = root / "console.log"
        with console.open("w") as output:
            process = subprocess.Popen(
                [
                    cli,
                    "--http",
                    "--data",
                    str(data),
                    "--port",
                    str(visitor),
                    "--worker-port",
                    str(worker),
                    "--no-record",
                ],
                cwd=root,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_for(
                    process,
                    lambda: "ArchiveBox Proxy is ready" in console.read_text(),
                    "listeners",
                )
                context = ssl.create_default_context(cafile=str(data / "trust.pem"))

                def fetch(port):
                    # CONNECT exercises TLS trust with the bundled engine. Using
                    # an explicit connection also avoids ambient NO_PROXY rules.
                    connection = http.client.HTTPSConnection(
                        "127.0.0.1", port, context=context, timeout=30
                    )
                    connection.set_tunnel("news.ycombinator.com", 443)
                    try:
                        connection.request("GET", "/")
                        response = connection.getresponse()
                        return (
                            response.status,
                            dict(response.getheaders()),
                            response.read(),
                        )
                    finally:
                        connection.close()

                first = fetch(visitor)
                assert first[0] == 200 and b"Hacker News" in first[2], first[:2]
                # Streaming EOF precedes asynchronous persistence. Observe the
                # actual store event before asserting a warm-cache request.
                log = data / "proxy.log"
                wait_for(
                    process,
                    lambda: any(
                        "STORED visitor " in line
                        and " 200 GET https://news.ycombinator.com/" in line
                        for line in log.read_text().splitlines()
                    ),
                    "response persistence",
                )
                second = fetch(worker)
                assert second[0] == 200 and second[2] == first[2]
                headers = {key.lower(): value for key, value in second[1].items()}
                assert headers.get("x-archivebox-proxy-cache") == "HIT", headers
                print(
                    f"Installed wheel HTTPS capture and worker cache HIT: {len(first[2])} bytes"
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                    assert process.returncode == 0, process.returncode
                finally:
                    print(console.read_text())
                    if (data / "proxy.log").exists():
                        print((data / "proxy.log").read_text())


if __name__ == "__main__":
    main()
