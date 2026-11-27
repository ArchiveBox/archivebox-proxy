#!/usr/bin/env python3
"""Build against the pinned Proxelar source plus the checked-in integration patch."""

import argparse
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src/archivebox_proxy/engine"
TOOLCHAIN = "1.99.1"
REVISION = "91bf33d21c6ff6daa984979310e9b54795f70e4c"
REPOSITORY = "https://github.com/emanuele-em/proxelar"


def source():
    patches = sorted((ENGINE / "patches").glob("*.patch"))
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in patches)).hexdigest()[:16]
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    cache = cache / "archivebox-proxy" / "proxelar"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{REVISION}-{digest}"
    if destination.is_dir():
        return destination
    # A patch digest selects a distinct source tree, so patch updates never
    # mutate dependencies while another build is using an earlier revision.
    with tempfile.TemporaryDirectory(prefix="download-", dir=cache) as temporary:
        temporary = Path(temporary)
        archive = temporary / "source.tar.gz"
        with (
            urllib.request.urlopen(
                f"https://codeload.github.com/emanuele-em/proxelar/tar.gz/{REVISION}"
            ) as response,
            archive.open("wb") as target,
        ):
            shutil.copyfileobj(response, target)
        with tarfile.open(archive) as tar:
            tar.extractall(temporary, filter="data")
        extracted = temporary / f"proxelar-{REVISION}"
        for patch in patches:
            subprocess.run(
                ["git", "apply", "--check", str(patch)], cwd=extracted, check=True
            )
            subprocess.run(["git", "apply", str(patch)], cwd=extracted, check=True)
        try:
            extracted.rename(destination)
        except FileExistsError:
            pass  # Another build finished preparing the identical source tree.
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--locked", action="store_true")
    parser.add_argument("--clippy", action="store_true")
    parser.add_argument(
        "--fmt", action="store_true", help="Check native source formatting"
    )
    args = parser.parse_args()
    # Pin native build tools here, without a root toolchain file affecting
    # ordinary Python development or the rest of a shared workspace.
    installed = subprocess.run(
        ["rustup", "which", "--toolchain", TOOLCHAIN, "cargo"],
        check=False,
        capture_output=True,
        text=True,
    )
    if installed.returncode:
        subprocess.run(
            ["rustup", "toolchain", "install", TOOLCHAIN, "--profile", "minimal"],
            check=True,
        )
    if args.fmt or args.clippy:
        subprocess.run(
            [
                "rustup",
                "component",
                "add",
                "--toolchain",
                TOOLCHAIN,
                "rustfmt" if args.fmt else "clippy",
            ],
            check=True,
        )
    env = os.environ.copy()
    env["RUSTUP_TOOLCHAIN"] = TOOLCHAIN
    # Cargo runs inside the nested crate. Preserve a caller's relative target
    # directory instead of accidentally creating build files in package source.
    env["CARGO_TARGET_DIR"] = str(
        Path(env.get("CARGO_TARGET_DIR", ROOT / "target")).expanduser().resolve()
    )
    env["RUSTC"] = subprocess.check_output(
        ["rustup", "which", "--toolchain", TOOLCHAIN, "rustc"], text=True
    ).strip()
    cargo = subprocess.check_output(
        ["rustup", "which", "--toolchain", TOOLCHAIN, "cargo"], text=True
    ).strip()
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
    if args.fmt:
        subprocess.run([cargo, "fmt", "--check"], cwd=ENGINE, env=env, check=True)
        return
    directory = source()
    command = [cargo, "clippy" if args.clippy else "build"]
    for package in ("proxyapi", "proxyapi_models", "proxelar-proto"):
        command += [
            "--config",
            f'patch."{REPOSITORY}".{package}.path="{directory / package}"',
        ]
    if args.locked:
        command.append("--locked")
    if args.release:
        command.append("--release")
    if args.clippy:
        command += ["--all-targets", "--", "-D", "warnings"]
    subprocess.run(command, cwd=ENGINE, env=env, check=True)


if __name__ == "__main__":
    main()
