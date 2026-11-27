"""Prepare the native executable, build with uv, and pack a platform wheel."""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def platform_tag(engine):
    machine = platform.machine().lower()
    if sys.platform == "linux" and machine in ("aarch64", "x86_64"):
        return f"linux_{machine}"
    if sys.platform == "darwin" and machine in ("arm64", "x86_64"):
        # Read the binary's minimum OS, not the builder's newer macOS version.
        metadata = subprocess.check_output(["otool", "-l", str(engine)], text=True)
        versions = re.findall(r"\bminos\s+(\d+)\.(\d+)", metadata)
        versions += re.findall(
            r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+)\.(\d+)", metadata
        )
        if not versions:
            raise RuntimeError("Cannot determine the bundled engine's macOS minimum")
        major, minor = max((int(a), int(b)) for a, b in versions)
        return f"macosx_{major}_{minor}_{machine}"
    raise RuntimeError(f"Unsupported wheel build platform: {sys.platform}/{machine}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine", type=Path, help="Use an already built native executable"
    )
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.engine:
        engine = args.engine.expanduser().resolve()
    else:
        env = os.environ.copy()
        env.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")
        target = Path(env.get("CARGO_TARGET_DIR", ROOT / "target/wheel")).resolve()
        env["CARGO_TARGET_DIR"] = str(target)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "bin/build_engine.py"),
                "--locked",
                "--release",
            ],
            env=env,
            check=True,
        )
        engine = target / "release/archivebox-proxy-engine"
    if not engine.is_file() or not os.access(engine, os.X_OK):
        raise RuntimeError(f"Native engine is missing or not executable: {engine}")
    subprocess.run([str(engine), "--help"], check=True, capture_output=True)
    tag = f"py3-none-{platform_tag(engine)}"
    destination = args.out_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    # uv_build deliberately has no native compilation hooks or platform tags.
    # Stage the executable without modifying the source checkout, then use the
    # standard wheel tools to write platlib metadata and regenerate RECORD.
    # The intermediate any wheel stays temporary and is never distributed.
    with tempfile.TemporaryDirectory(
        prefix="archivebox-proxy-wheel-build-"
    ) as temporary:
        stage = Path(temporary) / "source"
        stage.mkdir()
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, stage / name)
        package = stage / "src/archivebox_proxy"
        shutil.copytree(
            ROOT / "src/archivebox_proxy",
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy2(engine, package / "engine/archivebox-proxy-engine")
        intermediate = Path(temporary) / "built"
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(intermediate), str(stage)],
            check=True,
        )
        wheel = next(intermediate.glob("*.whl"))
        unpacked = Path(temporary) / "unpacked"
        wheel_command = ["uvx", "--from", "wheel==0.48.0", "wheel"]
        subprocess.run(
            [*wheel_command, "unpack", str(wheel), "--dest", str(unpacked)], check=True
        )
        directory = next(unpacked.iterdir())
        metadata = next(directory.glob("*.dist-info/WHEEL"))
        text = metadata.read_text()
        if "Root-Is-Purelib: true" not in text or "Tag: py3-none-any" not in text:
            raise RuntimeError(
                "Unexpected uv wheel metadata; refusing to mislabel the binary"
            )
        metadata.write_text(
            text.replace("Root-Is-Purelib: true", "Root-Is-Purelib: false").replace(
                "Tag: py3-none-any", "Tag: " + tag
            )
        )
        subprocess.run(
            [*wheel_command, "pack", str(directory), "--dest-dir", str(destination)],
            check=True,
        )


if __name__ == "__main__":
    main()
