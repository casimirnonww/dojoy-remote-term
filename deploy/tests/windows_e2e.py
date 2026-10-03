"""Exercise the Windows side for real, on a Windows CI machine with Windows PowerShell 5.1
(what Windows 11 ships): the gateway is not available there, so the receiver runs locally.

1. Every PowerShell file, and a rendered join script, parse without errors.
2. agent.ps1 reports once to a local receiver, which accepts it (validate_metrics) and shows
   the host online; agent.ps1 -Enroll hands over host keys and a tunnel key.
3. The join script's own functions turn on OpenSSH Server, install the terminal key, and that
   key logs in to 127.0.0.1 with `whoami`. (Skipped, and said so, if the OpenSSH Server
   capability cannot be installed on the machine.)
4. The lock-down a fresh OpenSSH install gets (127.0.0.1 only, keys only) is valid for sshd.
5. The whole join script runs, pointed at the local receiver and with this machine's own sshd
   standing in for the gateway: scheduled tasks as SYSTEM, the tunnel key's ACL, the first
   report, the enrollment, and then a real tunnel: the SYSTEM-run ssh opens the forwarded
   port, and the terminal key logs in through it.

Run: python deploy/tests/windows_e2e.py
"""

import importlib.machinery
import importlib.util
import json
import os
import secrets
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "status"), str(REPO / "deploy")]
os.environ.setdefault("REMOTE_TERM_HOME", str(REPO))
from receiver import TokenStore  # noqa: E402

POWERSHELL = "powershell.exe"  # Windows PowerShell 5.1, not pwsh
OPENSSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH"
PORT = 18790
USER = os.environ.get("USERNAME", "runneradmin")


def load_admin():
    path = REPO / "deploy" / "gateway" / "remote-term-admin"
    loader = importlib.machinery.SourceFileLoader("remote_term_admin", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("remote_term_admin", loader))
    loader.exec_module(module)
    return module


def powershell(command, check=True, env=None):
    print(f"$ {command[:200]}", flush=True)
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-Command", command], capture_output=True, text=True, env=env,
                            encoding="utf-8", errors="replace")
    print(result.stdout, result.stderr, sep="\n", flush=True)
    if check and result.returncode != 0:
        raise SystemExit(f"PowerShell failed with exit code {result.returncode}")
    return result


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def keypair(path):
    subprocess.run([str(OPENSSH / "ssh-keygen.exe"), "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                   check=True)
    return Path(str(path) + ".pub")


def render_join(admin, work, host):
    terminal = keypair(work / "terminal_key")
    gateway = keypair(work / "gateway_key").read_text().strip()
    script = admin.build_windows_join_script(host, "gateway.example", "test-token-" + "x" * 30,
                                             terminal.read_text(), REPO, gateway)
    path = work / "join.ps1"
    path.write_text(script, encoding="utf-8")  # the BOM is part of the text
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "join script lost its BOM"
    return path, work / "terminal_key"


def check_syntax(paths):
    files = ", ".join(quote(path) for path in paths)
    powershell(
        "$failed = $false; foreach ($f in @(" + files + ")) { $errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$errors); "
        "if ($errors) { $failed = $true; $errors | ForEach-Object { Write-Output ($f + ': ' + $_.ToString()) } } "
        "else { Write-Output ('parsed ' + $f) } }; if ($failed) { exit 1 }")


def wait_for_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise SystemExit("receiver did not start")


def start_receiver(work, host):
    hosts = work / "hosts.json"
    hosts.write_text(json.dumps({"schema_version": 1, "hosts": [{
        "id": host["id"], "name": host["name"], "meta": host["meta"], "kind": "windows", "tunnel": True,
        "ssh": {"host": "127.0.0.1", "port": host["ssh_port"], "user": host["ssh_user"]}}]}), encoding="utf-8")
    tokens = work / "tokens.json"
    token = TokenStore(tokens).issue(host["id"])
    token_file = work / "token"
    token_file.write_text(token + "\n", encoding="ascii")
    receiver = subprocess.Popen([sys.executable, str(REPO / "status" / "receiver.py"), "--hosts", str(hosts),
                                 "--tokens", str(tokens), "serve", "--state", str(work / "state.json"),
                                 "--port", str(PORT), "--enroll-dir", str(work / "enroll")])
    wait_for_port(PORT)
    return receiver, token, token_file


def check_agent(work, host, token_file):
    agent = REPO / "deploy" / "windows" / "agent.ps1"
    base = f"http://127.0.0.1:{PORT}"
    powershell(f"& {quote(agent)} -Url '{base}/api/report' -TokenFile {quote(token_file)} -Once; "
               "exit $LASTEXITCODE")
    status = json.loads(urllib.request.urlopen(f"{base}/status.json", timeout=10).read())
    row = next(row for row in status["hosts"] if row["id"] == host["id"])
    print(json.dumps(row, ensure_ascii=False, indent=2))
    assert row["status"] == "online", row
    metrics = row["metrics"]
    assert metrics["memory_total_bytes"] > 0 and metrics["disk_total_bytes"] > 0, metrics
    assert metrics["cpu_cores"] >= 1 and metrics["arch"] in ("x86_64", "arm64"), metrics

    # A wrong token must be refused, and the agent must say so with a failing exit code.
    bad = work / "bad-token"
    bad.write_text("wrong-token-" + "y" * 30, encoding="ascii")
    result = powershell(f"& {quote(agent)} -Url '{base}/api/report' -TokenFile {quote(bad)} -Once; "
                        "exit $LASTEXITCODE", check=False)
    assert result.returncode == 1, "agent accepted a refused report"
    # Plain http to another host is refused before anything is sent.
    result = powershell(f"& {quote(agent)} -Url 'http://gateway.example/api/report' "
                        f"-TokenFile {quote(token_file)} -Once; exit $LASTEXITCODE", check=False)
    assert result.returncode != 0, "agent sent the token over plain http"

    host_keys = [keypair(work / name) for name in ("host_ed25519", "host_ecdsa")]
    tunnel = keypair(work / "tunnel")
    keys = ", ".join(quote(path) for path in host_keys)
    powershell(f"& {quote(agent)} -Url '{base}/api/enroll' -TokenFile {quote(token_file)} -Enroll "
               f"-HostKeyFiles @({keys}) -TunnelKeyFile {quote(tunnel)}; exit $LASTEXITCODE")
    enrollment = json.loads((work / "enroll" / f"{host['id']}.json").read_text(encoding="utf-8"))
    assert len(enrollment["host_keys"]) == 2 and enrollment["tunnel_key"].startswith("ssh-ed25519 "), enrollment
    print("agent report and enrollment: OK")


def check_terminal_login(join, terminal_key, host):
    env = dict(os.environ, DOJOY_JOIN_LIBRARY_ONLY="1")
    result = powershell(f". {quote(join)}; Install-OpenSshServer", check=False, env=env)
    if result.returncode != 0:
        print("SKIPPED: OpenSSH Server could not be installed on this machine; key login not tested.")
        return
    public = (Path(str(terminal_key) + ".pub")).read_text().split()
    line = f"restrict,pty {public[0]} {public[1]} remote-term-{host['id']}"
    # Twice: a second run must replace its own line, not add another.
    for _ in range(2):
        powershell(f". {quote(join)}; Install-TerminalKey '{line}' 'remote-term-{host['id']}'", env=env)
    keys_file = Path(os.environ["ProgramData"]) / "ssh" / "administrators_authorized_keys"
    raw = keys_file.read_bytes()
    assert not raw.startswith(b"\xff\xfe") and raw.count(f"remote-term-{host['id']}".encode()) == 1, raw
    acl = subprocess.run(["icacls", str(keys_file)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    print(acl)
    login = subprocess.run([str(OPENSSH / "ssh.exe"), "-i", str(terminal_key), "-o", "BatchMode=yes",
                            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
                            f"{USER}@127.0.0.1", "whoami"], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
    print(login.stdout, login.stderr)
    assert login.returncode == 0 and USER.lower() in login.stdout.lower(), "terminal key login failed"
    print("terminal key login: OK")


def check_sshd_lockdown(join, work):
    original = Path(os.environ["ProgramData"]) / "ssh" / "sshd_config"
    if not original.exists():
        print("SKIPPED: no sshd_config on this machine; lock-down not tested.")
        return
    copy = work / "sshd_config"
    shutil.copyfile(original, copy)
    env = dict(os.environ, DOJOY_JOIN_LIBRARY_ONLY="1")
    for _ in range(2):  # applying it twice must not stack the header
        powershell(f". {quote(join)}; Protect-SshdConfig {quote(copy)}", env=env)
    text = copy.read_text(encoding="ascii")
    assert text.count("ListenAddress 127.0.0.1") == 1, text[:300]
    sshd = str(OPENSSH / "sshd.exe")
    subprocess.run([sshd, "-t", "-f", str(copy)], check=True)
    effective = subprocess.run([sshd, "-T", "-f", str(copy)], capture_output=True, text=True, check=True).stdout
    assert "listenaddress 127.0.0.1:22" in effective and "passwordauthentication no" in effective, effective
    print("sshd lock-down: OK")


def wait_for_tcp(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(1)
    return False


def check_full_join(admin, work, host, token):
    terminal = keypair(work / "full_terminal_key")
    ssh_dir = Path(os.environ["ProgramData"]) / "ssh"
    # This machine's sshd plays the gateway: the tunnel pins its host key.
    gateway = " ".join((ssh_dir / "ssh_host_ed25519_key.pub").read_text().split()[:2])
    script = admin.build_windows_join_script(host, "127.0.0.1", token, terminal.read_text(), REPO, gateway)
    base = f"http://127.0.0.1:{PORT}"
    # The gateway is not here: send the report and the enrollment to the local receiver.
    for name in ("report", "enroll"):
        assert f"'https://127.0.0.1/api/{name}'" in script
        script = script.replace(f"'https://127.0.0.1/api/{name}'", f"'{base}/api/{name}'")
    path = work / "full-join.ps1"
    path.write_text(script, encoding="utf-8")
    tasks = "'DOJOY remote-term tunnel', 'DOJOY remote-term agent'"
    try:
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        print(result.stdout, result.stderr, sep="\n")
        assert result.returncode == 0, "the join script failed"
        listing = powershell(f"Get-ScheduledTask -TaskName {tasks} | ForEach-Object {{ "
                             "$_.TaskName + ' | ' + $_.State + ' | ' + $_.Principal.UserId }").stdout
        assert listing.count("SYSTEM") == 2, listing
        key = Path(os.environ["ProgramData"]) / "remote-term" / "tunnel_key"
        acl = subprocess.run(["icacls", str(key)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        print(acl)
        entries = [line for line in acl.splitlines() if ":(" in line]
        assert len(entries) == 2 and "Administrators" in acl and "SYSTEM" in acl, acl
        assert USER.lower() not in acl.lower(), "the tunnel key is still open to the account that made it"
        enrollment = json.loads((work / "enroll" / f"{host['id']}.json").read_text(encoding="utf-8"))
        tunnel_public = " ".join(Path(str(key) + ".pub").read_text().split()[:2])
        assert enrollment["tunnel_key"] == tunnel_public, (enrollment, tunnel_public)
        assert len(enrollment["host_keys"]) >= 1, enrollment
        print("full join: OK")

        # A "tunnel" account like the gateway's, allowed to forward with the generated tunnel key.
        password = "Dj!" + secrets.token_urlsafe(18) + "9aZ"
        subprocess.run(["net", "user", "tunnel", password, "/add"], check=True, capture_output=True)
        subprocess.run(["net", "localgroup", "Administrators", "tunnel", "/add"], check=True, capture_output=True)
        env = dict(os.environ, DOJOY_JOIN_LIBRARY_ONLY="1")
        powershell(f". {quote(path)}; Install-TerminalKey 'restrict,port-forwarding {tunnel_public} "
                   "remote-term-tunnel-ci' 'remote-term-tunnel-ci'", env=env)
        assert wait_for_tcp(host["ssh_port"], 90), (
            "the SYSTEM-run tunnel never opened its port; tunnel log:\n"
            + (Path(os.environ["ProgramData"]) / "remote-term" / "tunnel.log").read_text(errors="replace"))
        login = subprocess.run([str(OPENSSH / "ssh.exe"), "-p", str(host["ssh_port"]), "-i", str(work / "full_terminal_key"),
                                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
                                f"{USER}@127.0.0.1", "whoami"], capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace")
        print(login.stdout, login.stderr)
        assert login.returncode == 0 and USER.lower() in login.stdout.lower(), "login through the tunnel failed"
        print("tunnel: OK")
    finally:
        powershell(f"Get-ScheduledTask -TaskName {tasks} -ErrorAction SilentlyContinue | "
                   "ForEach-Object { Stop-ScheduledTask -InputObject $_; $_ } | "
                   "Unregister-ScheduledTask -Confirm:$false", check=False)
        subprocess.run(["net", "user", "tunnel", "/delete"], capture_output=True)


def main():
    # The runner's console code page cannot print the Chinese test host name.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    admin = load_admin()
    work = Path(tempfile.mkdtemp(prefix="dojoy-win-"))
    host = {"id": "win-ci", "name": "测试 Windows '引号'", "meta": "CI", "kind": "windows",
            "ssh_host": "127.0.0.1", "ssh_port": 22299, "ssh_user": USER, "tunnel": True, "path": "/win-ci/"}
    try:
        join, terminal_key = render_join(admin, work, host)
        check_syntax([REPO / "deploy" / "windows" / "agent.ps1", REPO / "deploy" / "windows" / "tunnel.ps1", join])
        receiver, token, token_file = start_receiver(work, host)
        try:
            check_agent(work, host, token_file)
            check_terminal_login(join, terminal_key, host)
            check_sshd_lockdown(join, work)
            check_full_join(admin, work, host, token)
        finally:
            receiver.terminate()
            receiver.wait(timeout=10)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("windows e2e: all checks passed")


if __name__ == "__main__":
    main()
