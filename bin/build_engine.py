#!/usr/bin/env python3
"""Build against the pinned Proxelar source plus the checked-in integration patch."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REVISION = "91bf33d21c6ff6daa984979310e9b54795f70e4c"
REPOSITORY = "https://github.com/emanuele-em/proxelar"


def source():
    patches = sorted((ROOT / "patches").glob("*.patch"))
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
    args = parser.parse_args()
    directory = source()
    # Honor rust-toolchain.toml even on hosts exporting an unrelated RUSTC.
    env = os.environ.copy()
    env["RUSTC"] = subprocess.check_output(
        ["rustup", "which", "rustc"], cwd=ROOT, text=True
    ).strip()
    cargo = subprocess.check_output(
        ["rustup", "which", "cargo"], cwd=ROOT, text=True
    ).strip()
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
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
    subprocess.run(command, cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    main()
