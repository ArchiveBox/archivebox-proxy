"""Install the proxy's existing CA through the platform's certificate tools."""

import hashlib
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def run(command, *, administrator=False):
    if administrator and os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise RuntimeError("Installing system certificate trust requires sudo")
        command = ["sudo", *command]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{Path(command[0]).name} failed: {detail}")


def install_ca(certificate):
    cert = x509.load_pem_x509_certificate(certificate.read_bytes())
    der = cert.public_bytes(serialization.Encoding.DER)
    # Distinct data directories have distinct authorities. A fingerprint avoids
    # replacing another proxy's trusted certificate when installing this one.
    name = "archivebox-proxy-" + hashlib.sha256(der).hexdigest()[:16]
    if sys.platform == "darwin":
        run(
            [
                "security",
                "add-trusted-cert",
                "-d",
                "-r",
                "trustRoot",
                "-k",
                "/Library/Keychains/System.keychain",
                str(certificate),
            ],
            administrator=True,
        )
        run(
            [
                "security",
                "verify-cert",
                "-c",
                str(certificate),
                "-p",
                "basic",
                "-l",
                "-L",
            ]
        )
    elif sys.platform.startswith("linux"):
        if shutil.which("update-ca-trust"):
            target = Path("/etc/pki/ca-trust/source/anchors") / (name + ".pem")
            refresh = ["update-ca-trust", "extract"]
            bundle = "/etc/pki/tls/certs/ca-bundle.crt"
        elif shutil.which("update-ca-certificates"):
            target = Path("/usr/local/share/ca-certificates") / (name + ".crt")
            refresh = ["update-ca-certificates"]
            bundle = "/etc/ssl/certs/ca-certificates.crt"
        else:
            raise RuntimeError(
                "Install your distribution's ca-certificates package, then run --install-ca again"
            )
        run(
            ["install", "-m", "0644", str(certificate), str(target)], administrator=True
        )
        run(refresh, administrator=True)
        # Check the refreshed OS bundle, not an inherited SSL_CERT_FILE override
        # that could point to the proxy's separately generated capture bundle.
        roots = ssl.create_default_context(cafile=bundle).get_ca_certs(binary_form=True)
        if der not in roots:
            raise RuntimeError("Certificate was not added to the system trust bundle")
    else:
        raise RuntimeError("Automatic certificate trust supports macOS and Linux")

    certutil = shutil.which("certutil")
    if not certutil and sys.platform == "darwin":
        for candidate in (
            "/opt/homebrew/opt/nss/bin/certutil",
            "/usr/local/opt/nss/bin/certutil",
        ):
            if os.access(candidate, os.X_OK):
                certutil = candidate
                break
    installed = 0
    if certutil:
        home = Path.home()
        profiles = [
            home / ".pki/nssdb",
            home / "snap/chromium/current/.pki/nssdb",
            Path("/etc/pki/nssdb"),
        ]
        for pattern in (
            ".mozilla/firefox/*",
            "snap/firefox/common/.mozilla/firefox/*",
            "Library/Application Support/Firefox/Profiles/*",
        ):
            profiles.extend(home.glob(pattern))
        # NSS keeps per-profile databases. Use its own import/validation tools
        # so existing Firefox/Chromium certificates and trust remain intact.
        for profile in sorted(set(profiles)):
            kind = "sql" if (profile / "cert9.db").is_file() else "dbm"
            certificate_db = profile / ("cert9.db" if kind == "sql" else "cert8.db")
            if not certificate_db.is_file():
                continue
            database = f"{kind}:{profile}"
            # Shared system profiles may need privilege to read their databases
            # as well as update them; validate with the same access as import.
            administrator = not os.access(profile, os.W_OK | os.X_OK) or not os.access(
                certificate_db, os.W_OK
            )
            run(
                [
                    certutil,
                    "-A",
                    "-d",
                    database,
                    "-t",
                    "C,,",
                    "-n",
                    name,
                    "-i",
                    str(certificate),
                ],
                administrator=administrator,
            )
            run(
                [certutil, "-V", "-d", database, "-u", "L", "-n", name],
                administrator=administrator,
            )
            installed += 1
    return installed
