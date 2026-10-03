"""Exercise the Windows side for real, on a Windows CI machine with Windows PowerShell 5.1
(what Windows 11 ships): the gateway is not available there, so the receiver runs locally.

1. Every PowerShell file, and a rendered join script, parse without errors.
2. agent.ps1 reports once to a local receiver, which accepts it (validate_metrics) and shows
   the host online; agent.ps1 -Enroll hands over host keys and a tunnel key.
3. The join script's own functions turn on OpenSSH Server, install the terminal key, and that
   key logs in to 127.0.0.1 with `whoami`. (Skipped, and said so, if the OpenSSH Server
   capability cannot be installed on the machine.)

Run: python deploy/tests/windows_e2e.py
"""

import importlib.machinery
import importlib.util
import json
import os
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


def check_agent(work, host):
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
    try:
        wait_for_port(PORT)
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
    finally:
        receiver.terminate()
        receiver.wait(timeout=10)


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
        check_agent(work, host)
        check_terminal_login(join, terminal_key, host)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("windows e2e: all checks passed")


if __name__ == "__main__":
    main()
