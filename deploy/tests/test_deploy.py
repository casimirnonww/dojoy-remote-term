import base64
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "deploy"))
sys.path.insert(0, str(REPO / "status"))
os.environ.setdefault("REMOTE_TERM_HOME", str(REPO))

import render_config  # noqa: E402
from hosts_config import load_hosts  # noqa: E402


def load_admin():
    path = REPO / "deploy" / "gateway" / "remote-term-admin"
    loader = importlib.machinery.SourceFileLoader("remote_term_admin", str(path))
    spec = importlib.util.spec_from_loader("remote_term_admin", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


admin = load_admin()


def hosts_with_placeholders(directory):
    """A copy of hosts.json whose two Tencent hosts have no address yet."""
    document = json.loads((REPO / "hosts.json").read_text(encoding="utf-8"))
    for host in document["hosts"]:
        if host["id"].startswith("tencent-"):
            host["ssh"]["host"] = "REPLACE_WITH_" + host["id"].upper().replace("-", "_") + "_IP"
    path = Path(directory) / "hosts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOfjYK2kIUwffyxuDh8gicevgdFkfio49xyfZlNqjwcq"


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.hosts = load_hosts(REPO / "hosts.json")
        self.files = render_config.render_all(self.hosts, "djai.djscz.com")

    def test_every_host_gets_a_location_env_and_ssh_entry(self):
        nginx, ssh = self.files["nginx/remote-term.conf"], self.files["ssh_config"]
        for host in self.hosts:
            with self.subTest(host=host["id"]):
                self.assertIn(f"location ^~ /{host['id']}/ {{", nginx)
                self.assertIn(f"proxy_pass http://unix:/run/remote-term-ttyd/{host['id']}/ttyd.sock;", nginx)
                self.assertIn(f"Host {host['id']}\n", ssh)
                self.assertIn(f"IdentityFile /etc/remote-term/keys/{host['id']}\n", ssh)
        self.assertEqual(self.files["aliases"].split(), [host["id"] for host in self.hosts])

    def test_nginx_has_no_basic_auth_or_unauthenticated_terminal(self):
        nginx = self.files["nginx/remote-term.conf"]
        self.assertNotIn("auth_basic", nginx)
        self.assertNotIn("@@", nginx)
        self.assertIn("auth_request /oauth2/auth;", nginx)
        self.assertIn("ssl_reject_handshake on;", nginx)
        self.assertIn('"~*^websocket:https://djai\\.djscz\\.com$" 0;', nginx)
        # Only the login flow, the 401 helper and the token-checked report endpoint skip login.
        skipped = re.findall(r"location ([^{]+)\{\n\s+auth_request off;", nginx)
        self.assertEqual([location.strip() for location in skipped],
                         ["/oauth2/", "= /oauth2/auth", "@remote_term_unauthorized", "= /api/report",
                          "= /api/enroll", "^~ /join/"])
        self.assertIn("alias /var/lib/remote-term-join/;", nginx)
        self.assertIn("charset utf-8;", nginx)

    def test_ssh_config_never_logs_in_as_root_and_pins_host_keys(self):
        ssh = self.files["ssh_config"]
        self.assertNotRegex(ssh, r"(?m)^\s*User root$")
        self.assertIn("StrictHostKeyChecking yes", ssh)
        self.assertIn("ClearAllForwardings yes", ssh)
        # Host-specific blocks must come before "Host *" (first value wins in ssh_config).
        self.assertGreater(ssh.index("Host *"), ssh.index("Host mac-local"))

    def test_committed_hosts_js_is_up_to_date(self):
        self.assertEqual(render_config.main(["--check-web", str(REPO / "web" / "hosts.js")]), 0)

    def test_placeholders_block_a_real_render(self):
        with tempfile.TemporaryDirectory() as out:
            hosts = str(hosts_with_placeholders(out))
            self.assertEqual(render_config.main(["--hosts", hosts, "--domain", "djai.djscz.com", "--out", out]), 1)
            self.assertEqual(render_config.main(
                ["--hosts", hosts, "--domain", "djai.djscz.com", "--out", out, "--allow-placeholders"]), 0)
            self.assertTrue((Path(out) / "nginx" / "remote-term.conf").exists())

    def test_public_mode_listens_on_443(self):
        listens = re.findall(r"(?m)^\s*listen ([^;]+);", self.files["nginx/remote-term.conf"])
        self.assertEqual(listens, ["80 default_server", "[::]:80 default_server", "443 ssl default_server",
                                   "[::]:443 ssl default_server", "80", "[::]:80", "443 ssl", "[::]:443 ssl"])

    def test_router_mode_listens_only_on_the_loopback_backend(self):
        nginx = render_config.render_all(self.hosts, "djai.djscz.com", "127.0.0.1:18443")["nginx/remote-term.conf"]
        listens = re.findall(r"(?m)^\s*listen ([^;]+);", nginx)
        self.assertEqual(listens, ["80 default_server", "[::]:80 default_server",
                                   "127.0.0.1:18443 ssl default_server", "80", "[::]:80", "127.0.0.1:18443 ssl"])
        self.assertIn("ssl_reject_handshake on;", nginx)
        for bad in ("0.0.0.0:18443", "18443", "192.0.2.1:443", "127.0.0.1:0", "127.0.0.1:70000"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render_config.render_all(self.hosts, "djai.djscz.com", bad)


class AdminHelperTests(unittest.TestCase):
    def test_public_key_parsing_accepts_option_prefixed_lines(self):
        expected = tuple(ED25519.split())
        self.assertEqual(admin.parse_public_key(ED25519), expected)
        self.assertEqual(admin.parse_public_key("restrict,pty " + ED25519 + " remote-term-fa"), expected)
        for bad in ("", "ssh-ed25519", "ssh-ed25519 not-base64!", "ssh-rsa " + ED25519.split()[1]):
            with self.subTest(bad=bad), self.assertRaises(admin.AdminError):
                admin.parse_public_key(bad)

    def test_key_lines_are_restricted(self):
        key_type, blob = ED25519.split()
        self.assertEqual(admin.terminal_key_line("fa", key_type, blob),
                         f"restrict,pty {ED25519} remote-term-fa")
        host = {"id": "mac-local", "ssh_port": 22223}
        self.assertEqual(admin.tunnel_key_line(host, key_type, blob),
                         f'restrict,port-forwarding,permitlisten="127.0.0.1:22223" {ED25519} '
                         "remote-term-tunnel-mac-local")

    def test_replacing_a_tagged_line_keeps_the_others(self):
        known = "fa ssh-ed25519 OLD\nvps ssh-ed25519 KEEP\n"
        self.assertEqual(admin.replace_tagged_line(known, "fa", "fa ssh-ed25519 NEW"),
                         "vps ssh-ed25519 KEEP\nfa ssh-ed25519 NEW\n")
        keys = "restrict ssh-ed25519 A remote-term-tunnel-mbp-dojoy\nrestrict ssh-ed25519 B remote-term-tunnel-mac-local\n"
        result = admin.replace_tagged_line(keys, "remote-term-tunnel-mac-local", "restrict ssh-ed25519 C remote-term-tunnel-mac-local")
        self.assertEqual(result.splitlines(), ["restrict ssh-ed25519 A remote-term-tunnel-mbp-dojoy",
                                               "restrict ssh-ed25519 C remote-term-tunnel-mac-local"])


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.hosts = {host["id"]: host for host in load_hosts(REPO / "hosts.json")}
        self.bundle = admin.build_bundle(REPO)
        self.key = "restrict,pty " + ED25519 + " remote-term-fa"

    def test_bundle_holds_exactly_the_join_files(self):
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(self.bundle)), mode="r:gz") as archive:
            self.assertEqual(sorted(archive.getnames()), sorted(admin.JOIN_BUNDLE))
            self.assertTrue(all(member.uid == 0 for member in archive.getmembers()))

    def render(self, host_id, **extra):
        return admin.build_join_script(self.hosts[host_id], "djai.djscz.com", "tok_en-123", self.key,
                                       self.bundle, **extra)

    def test_linux_and_mac_scripts_are_valid_bash_with_the_right_values(self):
        linux = self.render("fa", password_hash="$6$salt$hash")
        mac = self.render("mac-local", gateway_hostkey=ED25519)
        for script in (linux, mac):
            with self.subTest(script=script[:60]):
                result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("@@", script)
                self.assertIn("ENROLL_URL=https://djai.djscz.com/api/enroll", script)
                self.assertIn("TOKEN=tok_en-123", script)
        self.assertIn("KIND=linux", linux)
        self.assertIn("PASSWORD_HASH='$6$salt$hash'", linux)
        self.assertIn("TUNNEL_PORT=''", linux)
        self.assertIn("KIND=mac", mac)
        self.assertIn("SSH_USER=wanghui", mac)
        self.assertIn("TUNNEL_PORT=22223", mac)
        self.assertIn("PASSWORD_HASH=''", mac)
        self.assertIn(f"GATEWAY_HOSTKEY='{ED25519}'", mac)

    def test_values_are_shell_quoted(self):
        host = dict(self.hosts["fa"], name="x'; rm -rf / #")
        script = admin.build_join_script(host, "djai.djscz.com", "t", self.key, self.bundle)
        self.assertIn("HOST_NAME='x'\"'\"'; rm -rf / #'", script)
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_join_command(self):
        self.assertEqual(admin.join_command("djai.djscz.com", "ab12"),
                         "curl -fsSLo /tmp/dojoy-join.sh 'https://djai.djscz.com/join/ab12.sh' && bash /tmp/dojoy-join.sh")


class AddressTests(unittest.TestCase):
    def test_ssh_g_hostname(self):
        self.assertEqual(admin.parse_ssh_g_hostname("user root\nhostname 192.0.2.5\nport 22\n", "tencent-new"),
                         "192.0.2.5")
        # An alias with no Host block resolves to itself: unknown.
        self.assertIsNone(admin.parse_ssh_g_hostname("hostname tencent-new\n", "tencent-new"))
        self.assertEqual(admin.parse_ssh_g_hostname("hostname vm.example.com\n", "x"), "vm.example.com")

    def test_overrides_fill_placeholders_only(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = load_hosts(hosts_with_placeholders(directory))
        admin.apply_address_overrides(hosts, {"tencent-new": "192.0.2.9", "fa": "192.0.2.1",
                                              "tencent-main": "not an ip"})
        by_id = {host["id"]: host["ssh_host"] for host in hosts}
        self.assertEqual(by_id["tencent-new"], "192.0.2.9")
        self.assertEqual(by_id["fa"], "39.107.156.205")
        self.assertTrue(by_id["tencent-main"].startswith("REPLACE"))

    def test_all_host_key_types_are_pinned(self):
        text = "fa ssh-ed25519 OLD\nvps ssh-ed25519 KEEP\n"
        result = admin.replace_host_lines(text, "fa", ["fa ssh-ed25519 NEW", "fa ecdsa-sha2-nistp256 NEW2"])
        self.assertEqual(result.splitlines(), ["vps ssh-ed25519 KEEP", "fa ssh-ed25519 NEW",
                                               "fa ecdsa-sha2-nistp256 NEW2"])


FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$@" >> "$OUT/curl.argv"
if [[ " $* " == *" -K - "* ]]; then cat >> "$OUT/curl.config"; fi
cat "$FAKE_TGZ"
"""
FAKE_INSTALL = r"""#!/usr/bin/env bash
{ printf '%s\n' "$@"; env; } > "$OUT/install.log"
"""


class BootstrapTests(unittest.TestCase):
    """deploy/bootstrap.sh with a stub curl: the token reaches GitHub only through curl's stdin."""

    TOKEN = "github_pat_11TESTONLY0123456789abcdefXYZ"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.out = self.tmp / "out"
        self.out.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("curl", FAKE_CURL), ("id", "#!/bin/sh\necho 0\n")):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        package = self.tmp / "pkg" / "owner-repo-abc123" / "deploy" / "gateway"
        package.mkdir(parents=True)
        (package / "install_gateway.sh").write_text(FAKE_INSTALL)
        (package / "install_gateway.sh").chmod(0o755)
        self.tgz = self.tmp / "fake.tgz"
        with tarfile.open(self.tgz, "w:gz") as archive:
            archive.add(self.tmp / "pkg" / "owner-repo-abc123", arcname="owner-repo-abc123")
        self.env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "OUT": str(self.out),
                    "FAKE_TGZ": str(self.tgz), "DOJOY_SOURCE": str(self.tmp / "src")}

    def run_bootstrap(self, **extra):
        return subprocess.run(["bash", str(REPO / "deploy" / "bootstrap.sh")], env=dict(self.env, **extra),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def test_token_goes_only_to_curl_stdin(self):
        result = self.run_bootstrap(DOJOY_GITHUB_TOKEN=self.TOKEN)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = (self.out / "curl.argv").read_text()
        self.assertIn("https://api.github.com/repos/casimirnonww/dojoy-remote-term/tarball/main", argv)
        self.assertIn("-L\n", argv)
        self.assertNotIn(self.TOKEN, argv)
        self.assertEqual((self.out / "curl.config").read_text(),
                         f'header = "Authorization: Bearer {self.TOKEN}"\n')
        install = (self.out / "install.log").read_text()
        self.assertIn("--github-user\ncasimirnonww\n", install)
        self.assertNotIn(self.TOKEN, install)
        self.assertNotIn("DOJOY_GITHUB_TOKEN", install)
        self.assertTrue((self.tmp / "src" / "deploy" / "gateway" / "install_gateway.sh").exists())

    def test_without_token_downloads_the_public_tarball(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / "curl.argv").read_text().splitlines()[-1],
                         "https://codeload.github.com/casimirnonww/dojoy-remote-term/tar.gz/refs/heads/main")
        self.assertFalse((self.out / "curl.config").exists())

    def test_malformed_token_is_refused_before_any_download(self):
        result = self.run_bootstrap(DOJOY_GITHUB_TOKEN='abc"\nheader = "X: y')
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.out / "curl.argv").exists())

    def test_readme_shows_the_same_one_line_command(self):
        header = (REPO / "deploy" / "bootstrap.sh").read_text()
        line = re.search(r"^#   (curl -fsSL https://raw\.githubusercontent\.com/\S+ \| bash)$", header, re.M).group(1)
        self.assertIn(f"```\n{line}\n```", (REPO / "README.md").read_text())
        # The private-repo variant stays documented in the script.
        self.assertRegex(header, r"(?m)^#   read -rsp .*DOJOY_GITHUB_TOKEN=\"\$T\" bash /root/dojoy-bootstrap\.sh; unset T$")


ROUTER = r"""
# Public 443 SNI router {kept as is}
map $ssl_preread_server_name $tls_backend {
    hostnames;
    "~^b-[0-9a-f]{36}\.base\.example\.com$" work_https;
    app.example.com       app_https;
    djai.djscz.com        terminal_https;
    default               terminal_https;
}

map $ssl_preread_protocol $backend {
    ""      plain_dashboard;
    default $tls_backend;
}

upstream terminal_https {
    server 127.0.0.1:18443;
}

upstream app_https {
    server 127.0.0.1:18091;
}

server {
    listen 0.0.0.0:443;
    listen [::]:443;
    proxy_pass $backend;
    ssl_preread on;
}
"""


class RouterTests(unittest.TestCase):
    def backend(self, text, domain="djai.djscz.com"):
        return admin.sni_router_backend(domain, [admin.strip_nginx_comments(text)])

    def test_finds_the_backend_the_router_sends_this_domain_to(self):
        self.assertEqual(self.backend(ROUTER), "127.0.0.1:18443")
        direct = ROUTER.replace("djai.djscz.com        terminal_https;", "djai.djscz.com 127.0.0.1:19000;")
        self.assertEqual(self.backend(direct), "127.0.0.1:19000")
        self.assertEqual(self.backend(ROUTER.replace("server 127.0.0.1:18443", "server localhost:18443")),
                         "127.0.0.1:18443")

    def test_no_stream_server_on_443_means_public_mode(self):
        self.assertIsNone(self.backend(ROUTER.replace(":443;", ":8443;")))
        self.assertIsNone(admin.sni_router_backend("djai.djscz.com", []))

    def test_unusable_routers_are_refused(self):
        cases = {
            "domain not routed": ROUTER.replace("djai.djscz.com        terminal_https;\n", ""),
            "public upstream": ROUTER.replace("server 127.0.0.1:18443", "server 192.0.2.1:443"),
            "two upstream servers": ROUTER.replace("server 127.0.0.1:18443;",
                                                   "server 127.0.0.1:18443;\n    server 127.0.0.1:18444;"),
            "proxy protocol": ROUTER.replace("ssl_preread on;", "ssl_preread on;\n    proxy_protocol on;"),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(admin.AdminError):
                self.backend(text)

    def test_stream_blocks_and_their_includes_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stream.d").mkdir()
            (root / "stream.d" / "router.conf").write_text(ROUTER, encoding="utf-8")
            (root / "nginx.conf").write_text(
                "events {}\nhttp {\n    include sites-enabled/*;\n}\n"
                "# stream { listen 443; }\nstream {\n    include stream.d/*.conf;\n}\n", encoding="utf-8")
            texts = admin.stream_config_texts(root / "nginx.conf")
            self.assertEqual(admin.sni_router_backend("djai.djscz.com", texts), "127.0.0.1:18443")
            (root / "nginx.conf").write_text("events {}\nhttp {}\n", encoding="utf-8")
            self.assertEqual(admin.stream_config_texts(root / "nginx.conf"), [])


class LegacyNginxTests(unittest.TestCase):
    SITES = {
        "conf.d/old-terminal.conf": "server {\n    listen 18443 ssl;\n    server_name _;\n}\n",
        "conf.d/root-shell.conf": "server {\n    listen 18444 ssl;\n    server_name djai.djscz.com;\n}\n",
        "sites-enabled/default": "server {\n    listen 80 default_server;\n}\n",
        "sites-enabled/brand.conf": "server {\n    listen 127.0.0.1:18091 ssl;\n"
                                    "    server_name brand.example.com djai.djscz.com.example.net;\n}\n",
        "sites-enabled/map-only.conf": "map $http_x $y {\n    default 0;\n}\n",
        "sites-enabled/commented.conf": "server {\n    # listen 443 ssl;\n    listen 127.0.0.1:18090;\n}\n",
        "sites-enabled/remote-term.conf": "server {\n    listen 443 ssl;\n    server_name djai.djscz.com;\n}\n",
    }

    def test_only_sites_that_clash_are_moved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, text in self.SITES.items():
                (root / relative).parent.mkdir(exist_ok=True)
                (root / relative).write_text(text, encoding="utf-8")
            (root / "sites-enabled" / "gone").symlink_to(root / "nowhere")
            moved = admin.move_conflicting_sites(
                "djai.djscz.com", {80, 443, 18443}, root / "backup",
                (str(root / "conf.d" / "*.conf"), str(root / "sites-enabled" / "*")))
            self.assertEqual(sorted(path.name for path, _ in moved),
                             ["default", "gone", "old-terminal.conf", "root-shell.conf"])
            self.assertEqual(sorted(path.name for path in (root / "backup").iterdir()),
                             ["default", "gone", "old-terminal.conf", "root-shell.conf"])
            self.assertTrue((root / "backup" / "gone").is_symlink())
            self.assertEqual(sorted(path.name for path in (root / "sites-enabled").iterdir()),
                             ["brand.conf", "commented.conf", "map-only.conf", "remote-term.conf"])

    def test_listen_ports(self):
        for spec, port in (("80", 80), ("[::]:443", 443), ("127.0.0.1:18443", 18443), ("127.0.0.1", 80),
                           ("[::1]", 80), ("unix:/run/x.sock", None)):
            with self.subTest(spec=spec):
                self.assertEqual(admin.listen_port(spec), port)

    def test_only_public_enrollment_addresses_fill_placeholders(self):
        for value, public in (("124.221.128.129", True), ("127.0.0.1", False), ("10.0.0.5", False),
                              ("::1", False), ("", False), (None, False), ("not an ip", False)):
            with self.subTest(value=value):
                self.assertEqual(admin.is_public_ip(value), public)


if __name__ == "__main__":
    unittest.main()
