"""Exercise the Mac join for real, on a macOS CI machine, the way a person runs it: the
rendered join script under bash 3.2 with the system's /usr/bin/python3, as a normal user.

The gateway is not available there, so this machine plays it: a local receiver behind TLS
(certificate from a throwaway CA added to /etc/ssl/cert.pem, the bundle the agent uses), the
gateway's name pointed at 127.0.0.1, and this machine's own sshd with a "tunnel" account.

1. The join script runs to the end: terminal key, tunnel LaunchAgent, report LaunchAgent,
   first report (the receiver shows the Mac online), enrollment (host keys + tunnel key).
2. It runs again at once, while the tunnel agent keeps failing and restarting (the gateway has
   not authorized its key yet), and with both agents switched off the way System Settings ->
   Login Items -> "Allow in the Background" (or launchctl disable) leaves them: what happens
   when someone pastes the command a second time.
3. With the tunnel key authorized, the tunnel opens its port and the terminal key logs in
   through it.
4. It runs a third time with the tunnel up, and the tunnel comes back.

Run (needs sudo, changes /etc/hosts and /etc/ssl/cert.pem; meant for a throwaway CI machine):
    python3 deploy/tests/mac_e2e.py
"""

import getpass
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "status"), str(REPO / "deploy")]
os.environ.setdefault("REMOTE_TERM_HOME", str(REPO))

DOMAIN = "djai-ci.test"
TLS_PORT = 18443
TUNNEL_PORT = 22299
USER = getpass.getuser()
DOMAIN_GUI = f"gui/{os.getuid()}"
LABELS = ("com.dojoy.remote-term-tunnel", "com.dojoy.remote-term-agent")
JOIN_PATH = Path("/tmp/dojoy-join.sh")  # where the pasted command saves it


def load_admin():
    path = REPO / "deploy" / "gateway" / "remote-term-admin"
    loader = importlib.machinery.SourceFileLoader("remote_term_admin", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("remote_term_admin", loader))
    loader.exec_module(module)
    return module


def run(command, check=True, **kwargs):
    print("$ " + " ".join(str(part) for part in command)[:300], flush=True)
    result = subprocess.run([str(part) for part in command], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", **kwargs)
    if result.stdout.strip() or result.stderr.strip():
        print(result.stdout, result.stderr, sep="\n", flush=True)
    if check and result.returncode != 0:
        raise SystemExit(f"failed with exit code {result.returncode}: {command[:3]}")
    return result


def sudo_append(path, text):
    run(["sudo", "/bin/sh", "-c", f'cat >> "{path}"'], input=text)


def make_certificate(work):
    config = work / "openssl.cnf"
    config.write_text(f"""[req]
distinguished_name = dn
prompt = no
[dn]
CN = DOJOY CI root
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[leaf]
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:{DOMAIN}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
""", encoding="ascii")
    openssl = "/usr/bin/openssl"
    run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-config", config,
         "-extensions", "ca", "-keyout", work / "ca.key", "-out", work / "ca.pem"])
    run([openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", config, "-subj", f"/CN={DOMAIN}",
         "-keyout", work / "leaf.key", "-out", work / "leaf.csr"])
    run([openssl, "x509", "-req", "-in", work / "leaf.csr", "-CA", work / "ca.pem", "-CAkey", work / "ca.key",
         "-CAcreateserial", "-days", "2", "-extfile", config, "-extensions", "leaf", "-out", work / "leaf.pem"])
    return work / "ca.pem"


def serve_tls(argv):
    """Child process: the receiver, speaking TLS itself (nginx's job on the real gateway)."""
    cert, key = argv[:2]
    import receiver
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    class TLSServer(receiver.ThreadingHTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            return context.wrap_socket(connection, server_side=True), address

    receiver.ThreadingHTTPServer = TLSServer
    receiver.main(argv[2:])


def wait_for_tcp(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(1)
    return False


def start_receiver(work, host, ca):
    from receiver import TokenStore
    hosts = work / "hosts.json"
    hosts.write_text(json.dumps({"schema_version": 1, "hosts": [{
        "id": host["id"], "name": host["name"], "meta": host["meta"], "kind": "mac", "tunnel": True,
        "ssh": {"host": "127.0.0.1", "port": host["ssh_port"], "user": host["ssh_user"]}}]}), encoding="utf-8")
    tokens = work / "tokens.json"
    token = TokenStore(tokens).issue(host["id"])
    receiver = subprocess.Popen([sys.executable, __file__, "serve-tls", work / "leaf.pem", work / "leaf.key",
                                 "--hosts", hosts, "--tokens", tokens, "serve", "--state", work / "state.json",
                                 "--port", str(TLS_PORT), "--enroll-dir", work / "enroll"])
    if not wait_for_tcp(TLS_PORT, 20):
        raise SystemExit("receiver did not start")
    # The name, the certificate and the CA bundle all line up, as they do for the real gateway.
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    urllib.request.urlopen(f"https://{DOMAIN}:{TLS_PORT}/status.json", timeout=10, context=context).read()
    return receiver, token


def status_row(host_id):
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    status = json.loads(urllib.request.urlopen(f"https://{DOMAIN}:{TLS_PORT}/status.json", timeout=10,
                                               context=context).read())
    return next(row for row in status["hosts"] if row["id"] == host_id)


def ensure_sshd():
    if wait_for_tcp(22, 1):
        return
    run(["sudo", "systemsetup", "-f", "-setremotelogin", "on"], check=False)
    if not wait_for_tcp(22, 10):
        run(["sudo", "launchctl", "load", "-w", "/System/Library/LaunchDaemons/ssh.plist"], check=False)
    if not wait_for_tcp(22, 20):
        raise SystemExit("could not turn on Remote Login (sshd) on this machine")


def gateway_hostkey():
    path = Path("/etc/ssh/ssh_host_ed25519_key.pub")
    if not path.exists():
        # macOS makes its host keys on the first connection.
        run(["/usr/bin/ssh-keyscan", "-t", "ed25519", "127.0.0.1"], check=False)
    return " ".join(path.read_text().split()[:2])


def start_old_tunnel():
    """A stand-in for the old tunnel the join retires (it logged in to the old VPS as root)."""
    label = "com.dojoy.reverse-ssh-hermes"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>/bin/sleep</string><string>3600</string></array>
  <key>KeepAlive</key><true/>
</dict>
</plist>
""", encoding="utf-8")
    run(["launchctl", "bootstrap", DOMAIN_GUI, plist])
    return label


def render_join(admin, work, host, token):
    run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "terminal_key"])
    key_type, blob = (work / "terminal_key.pub").read_text().split()[:2]
    script = admin.build_join_script(host, DOMAIN, token, admin.terminal_key_line(host["id"], key_type, blob),
                                     admin.build_bundle(REPO), "", gateway_hostkey())
    # The real gateway answers on 443 through nginx; here the receiver has its own port.
    assert script.count(f"https://{DOMAIN}/api/") == 2, "report/enroll URLs not found in the join script"
    return script.replace(f"https://{DOMAIN}/api/", f"https://{DOMAIN}:{TLS_PORT}/api/")


def run_join(script, attempt):
    print(f"\n===== join run {attempt} =====", flush=True)
    JOIN_PATH.write_text(script, encoding="utf-8")
    result = subprocess.run(["/bin/bash", str(JOIN_PATH)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300, stdin=subprocess.DEVNULL)
    print(result.stdout, result.stderr, sep="\n", flush=True)
    assert result.returncode == 0, f"join run {attempt} failed with exit code {result.returncode}"
    assert "完成" in result.stdout, f"join run {attempt} did not finish"
    assert not JOIN_PATH.exists(), "the join script did not remove itself (it holds the token)"


def disable_agents():
    """What switching the items off under Login Items (or launchctl disable) leaves behind."""
    for label in LABELS:
        run(["launchctl", "bootout", f"{DOMAIN_GUI}/{label}"], check=False)
        run(["launchctl", "disable", f"{DOMAIN_GUI}/{label}"])
    # For the record: a plain bootstrap (what the join did before) of a switched-off agent.
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABELS[0]}.plist"
    result = run(["launchctl", "bootstrap", DOMAIN_GUI, plist], check=False)
    print(f"plain bootstrap of a switched-off agent: exit {result.returncode}", flush=True)


def check_installed(work, host):
    for label in LABELS:
        run(["launchctl", "print", f"{DOMAIN_GUI}/{label}"], check=True)
    disabled = run(["launchctl", "print-disabled", DOMAIN_GUI]).stdout
    for label in LABELS:
        assert not re.search(rf'"{re.escape(label)}" => (disabled|true)', disabled), f"{label} is still switched off"
    keys = (Path.home() / ".ssh" / "authorized_keys").read_text()
    assert keys.count(f"remote-term-{host['id']}") == 1, keys
    row = status_row(host["id"])
    print(json.dumps(row, ensure_ascii=False, indent=2))
    assert row["status"] == "online", row
    metrics = row["metrics"]
    assert metrics["memory_total_bytes"] > 0 and metrics["disk_total_bytes"] > 0, metrics
    assert metrics["arch"] in ("arm64", "x86_64"), metrics
    enrollment = json.loads((work / "enroll" / f"{host['id']}.json").read_text(encoding="utf-8"))
    tunnel_public = " ".join((Path.home() / ".ssh" / "id_ed25519_remote_term_tunnel.pub").read_text().split()[:2])
    assert enrollment["tunnel_key"] == tunnel_public, (enrollment, tunnel_public)
    assert len(enrollment["host_keys"]) >= 1, enrollment


def create_tunnel_account(admin, host):
    """The gateway's "tunnel" account: may only listen on this Mac's port."""
    key_type, blob = (Path.home() / ".ssh" / "id_ed25519_remote_term_tunnel.pub").read_text().split()[:2]
    home = "/Users/tunnel"
    run(["sudo", "dscl", ".", "-create", "/Users/tunnel"])
    for attribute in (["UserShell", "/bin/bash"], ["RealName", "tunnel"], ["UniqueID", "599"],
                      ["PrimaryGroupID", "20"], ["NFSHomeDirectory", home]):
        run(["sudo", "dscl", ".", "-create", "/Users/tunnel", *attribute])
    run(["sudo", "dscl", ".", "-passwd", "/Users/tunnel", "Dj!" + secrets.token_hex(8) + "9aZ"])
    run(["sudo", "mkdir", "-p", f"{home}/.ssh"])
    run(["sudo", "/bin/sh", "-c", f'cat > "{home}/.ssh/authorized_keys"'],
        input=admin.tunnel_key_line(host, key_type, blob) + "\n")
    run(["sudo", "chown", "-R", "599:20", home])
    run(["sudo", "chmod", "700", f"{home}/.ssh"])
    run(["sudo", "chmod", "600", f"{home}/.ssh/authorized_keys"])
    # Remote Login may be limited to members of com.apple.access_ssh.
    for user in ("tunnel", USER):
        run(["sudo", "dseditgroup", "-o", "edit", "-a", user, "-t", "user", "com.apple.access_ssh"], check=False)


def check_tunnel_login(work, host):
    if not wait_for_tcp(host["ssh_port"], 120):
        log = Path.home() / "Library" / "Logs" / "remote-term-tunnel.log"
        raise SystemExit("the tunnel never opened its port; tunnel log:\n"
                         + (log.read_text(errors="replace") if log.exists() else "(no log)"))
    # A tunnel being replaced may still hold the port for a moment.
    for attempt in range(6):
        login = run(["/usr/bin/ssh", "-p", str(host["ssh_port"]), "-i", work / "terminal_key", "-o", "BatchMode=yes",
                     "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null", f"{USER}@127.0.0.1", "id -un"], check=False, timeout=60)
        if login.returncode == 0:
            break
        time.sleep(10)
    assert login.returncode == 0 and login.stdout.strip() == USER, "login through the tunnel failed"
    print("tunnel login: OK", flush=True)


def cleanup():
    for label in LABELS + ("com.dojoy.reverse-ssh-hermes",):
        subprocess.run(["launchctl", "bootout", f"{DOMAIN_GUI}/{label}"], capture_output=True)
    subprocess.run(["sudo", "dscl", ".", "-delete", "/Users/tunnel"], capture_output=True)
    subprocess.run(["sudo", "rm", "-rf", "/Users/tunnel"], capture_output=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve-tls":
        serve_tls(sys.argv[2:])
        return
    admin = load_admin()
    work = Path(tempfile.mkdtemp(prefix="dojoy-mac-"))
    host = {"id": "mac-ci", "name": "测试 Mac '引号'", "meta": "CI", "kind": "mac", "ssh_host": "127.0.0.1",
            "ssh_port": TUNNEL_PORT, "ssh_user": USER, "tunnel": True, "path": "/mac-ci/"}
    run(["/usr/bin/python3", "--version"])
    run(["sw_vers"])
    ensure_sshd()
    ca = make_certificate(work)
    sudo_append("/etc/hosts", f"\n127.0.0.1 {DOMAIN}\n")
    sudo_append("/etc/ssl/cert.pem", "\n" + ca.read_text())
    receiver, token = start_receiver(work, host, ca)
    try:
        script = render_join(admin, work, host, token)
        old = start_old_tunnel()
        run_join(script, 1)
        check_installed(work, host)
        assert run(["launchctl", "print", f"{DOMAIN_GUI}/{old}"], check=False).returncode != 0, \
            "the old tunnel is still running"
        # Pasted again straight away, while the tunnel agent keeps failing and restarting, and
        # with both agents switched off in between.
        disable_agents()
        run_join(script, 2)
        check_installed(work, host)
        create_tunnel_account(admin, host)
        check_tunnel_login(work, host)
        # And once more with the tunnel up: it must come back.
        run_join(script, 3)
        check_installed(work, host)
        check_tunnel_login(work, host)
    finally:
        receiver.terminate()
        receiver.wait(timeout=10)
        cleanup()
        shutil.rmtree(work, ignore_errors=True)
    print("mac e2e: all checks passed")


if __name__ == "__main__":
    main()
