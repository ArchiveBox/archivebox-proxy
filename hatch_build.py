"""Include the native transport in platform wheels; installation only unpacks it."""

import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        machine = platform.machine().lower()
        if sys.platform == "darwin" and machine in ("arm64", "x86_64"):
            system = "macos"
        elif sys.platform == "linux" and machine in ("aarch64", "x86_64"):
            system = "linux"
        else:
            raise RuntimeError(
                f"Unsupported wheel build platform: {sys.platform}/{machine}"
            )

        root = Path(self.root)
        supplied = os.environ.get("PROXY_ENGINE")
        if supplied:
            # Docker already builds the same pinned engine in its native stage.
            # An explicit path reuses that artifact without a second compilation.
            engine = Path(supplied).expanduser().resolve()
        else:
            env = os.environ.copy()
            if system == "macos":
                env.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")
            # Keep wheel builds separate from developer builds with different
            # deployment targets, so older cached objects cannot inflate the ABI.
            target = Path(
                env.get("CARGO_TARGET_DIR", root / "target" / "wheel")
            ).resolve()
            env["CARGO_TARGET_DIR"] = str(target)
            subprocess.run(
                [
                    sys.executable,
                    str(root / "bin/build_engine.py"),
                    "--locked",
                    "--release",
                ],
                cwd=root,
                env=env,
                check=True,
            )
            engine = target / "release" / "archivebox-proxy-engine"
        if not engine.is_file() or not os.access(engine, os.X_OK):
            raise RuntimeError(f"Native engine is missing or not executable: {engine}")
        subprocess.run([str(engine), "--help"], check=True, capture_output=True)

        if system == "macos":
            # Tag the actual Mach-O minimum rather than the builder's OS version.
            # This also handles an explicitly supplied binary honestly.
            metadata = subprocess.check_output(["otool", "-l", str(engine)], text=True)
            versions = re.findall(r"\bminos\s+(\d+)\.(\d+)", metadata)
            versions += re.findall(
                r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+)\.(\d+)",
                metadata,
            )
            if not versions:
                raise RuntimeError(
                    "Cannot determine the bundled engine's macOS minimum"
                )
            major, minor = max((int(a), int(b)) for a, b in versions)
            platform_tag = f"macosx_{major}_{minor}_{machine}"
        else:
            # Linux release jobs audit/repair this native tag in manylinux before
            # publishing; no Python C ABI is involved in this executable.
            platform_tag = f"linux_{machine}"
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform_tag}"
        build_data["force_include"][str(engine)] = (
            "archivebox_proxy/bin/archivebox-proxy-engine"
        )
