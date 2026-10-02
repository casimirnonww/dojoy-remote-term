import importlib.machinery
import importlib.util
import os
from pathlib import Path
import re
import sys
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
                         ["/oauth2/", "= /oauth2/auth", "@remote_term_unauthorized", "= /api/report"])

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
            self.assertEqual(render_config.main(["--domain", "djai.djscz.com", "--out", out]), 1)
            self.assertEqual(render_config.main(
                ["--domain", "djai.djscz.com", "--out", out, "--allow-placeholders"]), 0)
            self.assertTrue((Path(out) / "nginx" / "remote-term.conf").exists())


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


if __name__ == "__main__":
    unittest.main()
