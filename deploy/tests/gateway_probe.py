"""Reach the real gateway the way a Mac's join script does: the system's /usr/bin/python3 (its
own LibreSSL, not the keychain) with /etc/ssl/cert.pem, plus the tunnel's SSH port. Sends no
secrets: the report carries a made-up token, so the expected answer is HTTP 401.

Run with the Mac's system python:  /usr/bin/python3 deploy/tests/gateway_probe.py [domain]
"""

import json
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

DOMAIN = sys.argv[1] if len(sys.argv) > 1 else "djai.djscz.com"


def report(label, cafile):
    request = urllib.request.Request(f"https://{DOMAIN}/api/report", data=json.dumps({}).encode(),
                                     headers={"Authorization": "Bearer probe-" + "x" * 40,
                                              "Content-Type": "application/json"})
    try:
        context = ssl.create_default_context(cafile=cafile)
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            print(f"{label}: HTTP {response.status}")
    except urllib.error.HTTPError as error:
        print(f"{label}: HTTP {error.code} (401 = TLS and routing fine, token refused as expected)")
    except Exception as error:  # noqa: BLE001 - print whatever a Mac would hit
        print(f"{label}: FAILED {type(error).__name__}: {error}")


def tcp(port):
    try:
        with socket.create_connection((DOMAIN, port), timeout=10) as connection:
            banner = connection.recv(64).decode(errors="replace").strip() if port == 22 else ""
        print(f"tcp {port}: open {banner}")
    except OSError as error:
        print(f"tcp {port}: FAILED {error}")


def main():
    print(sys.version.split()[0], ssl.OPENSSL_VERSION)
    report("report with /etc/ssl/cert.pem", "/etc/ssl/cert.pem")
    report("report with the default store", None)
    tcp(443)
    tcp(22)
    try:
        chain = subprocess.run(["/usr/bin/openssl", "s_client", "-connect", f"{DOMAIN}:443", "-servername", DOMAIN,
                                "-showcerts", "-CAfile", "/etc/ssl/cert.pem"],
                               input="", capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"openssl: FAILED {error}")
        return
    for line in chain.stdout.splitlines():
        if line.lstrip().startswith(("s:", "i:", "Verify return code", "Protocol", "Cipher", "New,")) \
                or line.startswith((" s:", " i:", "depth=")):
            print("openssl:", line.strip())
    for line in chain.stderr.splitlines()[:10]:
        print("openssl:", line.strip())


if __name__ == "__main__":
    main()
