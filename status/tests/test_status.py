import contextlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import agent
import hosts_config
import probe
import receiver
import status_schema


REPO = Path(__file__).resolve().parents[2]
METRICS = {
    "hostname": "fixture-host", "os": "Fixture OS", "arch": "arm64",
    "cpu_model": "Fixture CPU", "cpu_cores": 8, "cpu_percent": 12.5,
    "memory_total_bytes": 16000, "memory_used_bytes": 6000,
    "disk_total_bytes": 100000, "disk_used_bytes": 40000,
    "uptime_seconds": 123.4, "load_1": 0.5,
}
HOSTS = [
    {"id": "vps", "name": "VPS"},
    {"id": "fa", "name": "财务机"},
    {"id": "mac-local", "name": "本机 Mac"},
]


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class SchemaTests(TempDirTestCase):
    def test_unknown_values_are_null_but_invalid_numbers_are_rejected(self):
        optional_unknown = {**METRICS, "cpu_percent": None, "load_1": None}
        self.assertEqual(status_schema.validate_metrics(optional_unknown), optional_unknown)
        for field, value in (("cpu_percent", float("nan")), ("cpu_percent", 101),
                             ("memory_used_bytes", 17000), ("cpu_cores", True)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                status_schema.validate_metrics({**METRICS, field: value})

    def test_extra_or_missing_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            status_schema.validate_metrics({**METRICS, "unexpected": 1})
        incomplete = dict(METRICS)
        del incomplete["memory_total_bytes"]
        with self.assertRaises(ValueError):
            status_schema.validate_metrics(incomplete)

    def test_timestamps_must_be_utc_z(self):
        self.assertTrue(status_schema.valid_timestamp("2026-09-06T01:00:00Z"))
        for value in ("2026-09-06T01:00:00+08:00", "2026-09-06 01:00:00", None, 5):
            self.assertFalse(status_schema.valid_timestamp(value))

    def test_atomic_publish_leaves_old_file_intact_on_replace_failure(self):
        output = self.root / "status.json"
        output.write_text('{"old":true}\n')
        with patch.object(status_schema.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                status_schema.atomic_write(output, {"new": True})
        self.assertEqual(output.read_text(), '{"old":true}\n')
        self.assertEqual(list(self.root.glob(".status.json.*.tmp")), [])

    def test_atomic_publish_sets_mode_and_group(self):
        output = self.root / "state.json"
        status_schema.atomic_write(output, {"a": 1}, mode=0o640, gid=os.getgid())
        self.assertEqual(json.loads(output.read_text()), {"a": 1})
        self.assertEqual(output.stat().st_mode & 0o777, 0o640)
        self.assertEqual(output.stat().st_gid, os.getgid())


class ProbeTests(unittest.TestCase):
    def test_network_rates_use_elapsed_time_and_reject_counter_reset(self):
        rates = probe.network_rates("eth0", (10.0, 1000, 2000), (10.5, 1500, 4000))
        self.assertEqual(rates["network_rx_bytes_per_second"], 1000)
        self.assertEqual(rates["network_tx_bytes_per_second"], 4000)
        status_schema.validate_metrics({**METRICS, **rates})
        reset = probe.network_rates("eth0", (10.0, 1000, 2000), (10.5, 0, 0))
        self.assertIsNone(reset["network_rx_bytes_per_second"])
        self.assertIsNone(reset["network_tx_bytes_per_second"])
        unavailable = probe.network_rates(None, None, None)
        self.assertIsNone(unavailable["network_interface"])
        status_schema.validate_metrics({**METRICS, **unavailable})

    def test_mac_network_counters_select_link_row_without_double_counting(self):
        fixture = "\n".join([
            "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll",
            "en0 1500 <Link#7> 00:00:00:00:00:00 12 0 4096 20 0 8192 0",
            "en0 1500 192.0.2/24 192.0.2.1 12 - 4096 20 - 8192 -",
        ])
        with patch.object(probe, "command", return_value=fixture):
            counters = probe.mac_network_counters("en0")
        self.assertEqual(counters[1:], (4096, 8192))

    @unittest.skipUnless(os.uname().sysname in ("Linux", "Darwin"), "probe supports Linux and macOS only")
    def test_real_probe_output_passes_validation(self):
        metrics = probe.collect_metrics()
        self.assertEqual(status_schema.validate_metrics(metrics), metrics)


class HostsConfigTests(unittest.TestCase):
    def entry(self, **overrides):
        base = {"id": "fa", "name": "财务机", "meta": "fa", "kind": "linux", "ttyd_port": 7682,
                "ssh": {"host": "192.0.2.10", "port": 22, "user": "ops"}}
        base.update(overrides)
        return base

    def parse(self, *entries):
        return hosts_config.parse_hosts({"schema_version": 1, "hosts": list(entries)})

    def test_repository_hosts_file_is_valid(self):
        hosts = hosts_config.load_hosts(REPO / "hosts.json")
        self.assertEqual([host["id"] for host in hosts],
                         ["vps", "fa", "tencent-new", "tencent-main", "mbp-dojoy", "mac-local"])
        self.assertTrue(all(host["path"] == "/" + host["id"] + "/" for host in hosts))
        self.assertTrue(all(host["ssh_user"] != "root" for host in hosts))

    def test_invalid_entries_are_rejected(self):
        cases = {
            "duplicate id": (self.entry(), self.entry(ttyd_port=7683)),
            "root login": (self.entry(ssh={"host": "192.0.2.10", "port": 22, "user": "root"}),),
            "shared ttyd port": (self.entry(), self.entry(id="other")),
            "reserved port": (self.entry(ttyd_port=8790),),
            "bad id": (self.entry(id="../etc"),),
            "bad host": (self.entry(ssh={"host": "a b", "port": 22, "user": "ops"}),),
            "tunnel off loopback": (self.entry(tunnel=True),),
            "unknown field": (self.entry(extra=1),),
            "control char": (self.entry(name="a\nb"),),
        }
        for label, entries in cases.items():
            with self.subTest(label), self.assertRaises(hosts_config.HostsConfigError):
                self.parse(*entries)

    def test_placeholders_are_reported(self):
        hosts = self.parse(self.entry(ssh={"host": "REPLACE_WITH_IP", "port": 22, "user": "ops"}))
        self.assertEqual(hosts_config.placeholders(hosts), ["fa"])


class StatusStateTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.state_path = self.root / "state.json"
        self.state = receiver.StatusState(HOSTS, self.state_path, clock=self.clock)

    def row(self, host_id, state=None):
        document = (state or self.state).snapshot()
        return next(row for row in document["hosts"] if row["id"] == host_id)

    def test_never_reported_is_unreachable_without_metrics(self):
        row = self.row("fa")
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(row["error"], receiver.NEVER_REPORTED)
        self.assertIsNone(row["metrics"])
        self.assertIsNone(row["last_success_at"])

    def test_report_uses_server_time_and_expires_after_90_seconds(self):
        self.state.record_success("fa", METRICS, 1200)
        row = self.row("fa")
        self.assertEqual(row["status"], "online")
        self.assertEqual(row["last_success_at"], "2026-10-02T12:00:00Z")
        self.assertEqual(row["duration_ms"], 1200)
        self.clock.advance(89)
        self.assertEqual(self.row("fa")["status"], "online")
        self.clock.advance(1)
        row = self.row("fa")
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(row["error"], receiver.REPORT_OVERDUE)
        self.assertEqual(row["metrics"], METRICS)
        self.assertEqual(row["last_success_at"], "2026-10-02T12:00:00Z")

    def test_invalid_report_marks_error_and_next_success_clears_it(self):
        self.state.record_success("fa", METRICS, 0)
        self.clock.advance(30)
        self.state.record_failure("fa", receiver.REPORT_INVALID)
        row = self.row("fa")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["metrics"], METRICS)
        self.clock.advance(30)
        self.state.record_success("fa", METRICS, 0)
        self.assertEqual(self.row("fa")["status"], "online")

    def test_snapshot_has_schema_v1_and_host_order(self):
        document = self.state.snapshot()
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["refresh_interval_seconds"], 30)
        self.assertEqual(document["generated_at"], "2026-10-02T12:00:00Z")
        self.assertEqual([row["id"] for row in document["hosts"]], ["vps", "fa", "mac-local"])
        self.assertEqual(self.row("fa")["name"], "财务机")

    def test_state_survives_restart_and_ignores_invalid_history(self):
        self.state.record_success("fa", METRICS, 0)
        self.state.record_success("vps", METRICS, 0)
        saved = json.loads(self.state_path.read_text())
        saved["hosts"]["vps"]["metrics"] = {"hostname": "partial"}
        saved["hosts"]["intruder"] = saved["hosts"]["fa"]
        self.state_path.write_text(json.dumps(saved))
        restarted = receiver.StatusState(HOSTS, self.state_path, clock=self.clock)
        self.assertEqual(self.row("fa", restarted)["status"], "online")
        self.assertEqual(self.row("fa", restarted)["metrics"], METRICS)
        self.assertIsNone(self.row("vps", restarted)["metrics"])
        self.assertNotIn("intruder", restarted.records)
        self.clock.advance(120)
        self.assertEqual(self.row("fa", restarted)["status"], "unreachable")


class ReceiverFixture(TempDirTestCase):
    def setUp(self):
        super().setUp()
        quiet = patch.object(receiver.Handler, "log_message", lambda *args: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.clock = FakeClock()
        self.tokens = receiver.TokenStore(self.root / "agent-tokens.json")
        self.token = self.tokens.issue("fa")
        self.state = receiver.StatusState(HOSTS, self.root / "state.json", clock=self.clock)
        self.server = receiver.make_server(self.state, self.tokens, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def request(self, path, body=None, headers=None, method=None):
        request = urllib.request.Request(self.base + path, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def report(self, payload, token=None, content_type="application/json"):
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        return self.request("/api/report", json.dumps(payload).encode(), headers, "POST")[0]

    def status_row(self, host_id):
        code, body = self.request("/status.json")
        self.assertEqual(code, 200)
        return next(row for row in json.loads(body)["hosts"] if row["id"] == host_id)


class ReceiverHttpTests(ReceiverFixture):
    def test_valid_token_reports_only_its_own_host(self):
        self.assertEqual(self.report({"metrics": METRICS, "probe_ms": 812}, self.token), 204)
        row = self.status_row("fa")
        self.assertEqual(row["status"], "online")
        self.assertEqual(row["duration_ms"], 812)
        self.assertEqual(self.status_row("vps")["status"], "unreachable")

    def test_missing_or_wrong_token_is_rejected(self):
        self.assertEqual(self.report({"metrics": METRICS}), 401)
        self.assertEqual(self.report({"metrics": METRICS}, "not-the-token"), 401)
        self.assertEqual(self.status_row("fa")["status"], "unreachable")

    def test_revoked_token_and_unknown_host_are_rejected(self):
        stray = self.tokens.issue("decommissioned")
        self.assertEqual(self.report({"metrics": METRICS}, stray), 401)
        self.assertTrue(self.tokens.revoke("fa"))
        self.assertEqual(self.report({"metrics": METRICS}, self.token), 401)

    def test_invalid_metrics_are_rejected_and_flagged(self):
        self.assertEqual(self.report({"metrics": {**METRICS, "cpu_percent": 250}}, self.token), 400)
        self.assertEqual(self.report({"metrics": METRICS, "extra": True}, self.token), 400)
        row = self.status_row("fa")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], receiver.REPORT_INVALID)

    def test_oversized_body_and_wrong_content_type_are_rejected(self):
        big = {"metrics": {**METRICS, "memory_note": "x" * (receiver.MAX_BODY_BYTES + 1)}}
        self.assertEqual(self.report(big, self.token), 413)
        self.assertEqual(self.report({"metrics": METRICS}, self.token, "text/plain"), 415)

    def test_routes(self):
        self.assertEqual(self.request("/healthz")[0], 200)
        self.assertEqual(self.request("/status.json?t=1")[0], 200)
        self.assertEqual(self.request("/index.html")[0], 404)
        self.assertEqual(self.request("/status.json", b"{}", {"Content-Type": "application/json"}, "POST")[0], 404)

    def test_token_file_holds_only_hashes_with_restricted_mode(self):
        content = (self.root / "agent-tokens.json").read_text()
        self.assertNotIn(self.token, content)
        self.assertIn(receiver.token_digest(self.token), content)
        self.assertEqual((self.root / "agent-tokens.json").stat().st_mode & 0o777, 0o640)


class AgentTests(ReceiverFixture):
    def run_agent(self, *argv, token=None):
        stderr = io.StringIO()
        environment = {"REMOTE_TERM_TOKEN": token if token is not None else self.token}
        with patch.dict(os.environ, environment), contextlib.redirect_stderr(stderr):
            code = agent.main(list(argv))
        return code, stderr.getvalue()

    def test_url_policy(self):
        self.assertEqual(agent.check_url("https://example.com/api/report"), "https://example.com/api/report")
        self.assertEqual(agent.check_url("http://127.0.0.1:8790/api/report"), "http://127.0.0.1:8790/api/report")
        for url in ("http://example.com/api/report", "ftp://example.com/", "", None, "https:///nohost"):
            with self.subTest(url=url), self.assertRaises(agent.ConfigError):
                agent.check_url(url)

    def test_token_must_be_present_and_single_word(self):
        for token in ("", "two words", "x" * 600):
            with self.subTest(token=token[:10]), patch.dict(os.environ, {"REMOTE_TERM_TOKEN": token}):
                with self.assertRaises(agent.ConfigError):
                    agent.read_token(None)
        token_file = self.root / "token"
        token_file.write_text(self.token + "\n")
        self.assertEqual(agent.read_token(token_file), self.token)

    def test_agent_pushes_real_metrics(self):
        with patch.object(agent.probe, "collect_metrics", return_value=dict(METRICS)):
            code, stderr = self.run_agent("--url", self.base + "/api/report")
        self.assertEqual((code, stderr), (0, ""))
        row = self.status_row("fa")
        self.assertEqual(row["status"], "online")
        self.assertEqual(row["metrics"], METRICS)

    def test_rejected_report_does_not_echo_the_token(self):
        with patch.object(agent.probe, "collect_metrics", return_value=dict(METRICS)):
            code, stderr = self.run_agent("--url", self.base + "/api/report", token="wrong-token-value")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", stderr)
        self.assertNotIn("wrong-token-value", stderr)

    def test_redirect_is_refused_without_forwarding_the_token(self):
        seen = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(204)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        port = target.server_address[1]

        class Redirect(Target):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/collect" % port)
                self.end_headers()

        redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, redirector):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/api/report" % redirector.server_address[1]
        with patch.object(agent.probe, "collect_metrics", return_value=dict(METRICS)):
            code, stderr = self.run_agent("--url", url)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 302", stderr)
        self.assertEqual(seen, [])

    def test_plain_http_to_remote_host_is_a_config_error(self):
        code, stderr = self.run_agent("--url", "http://example.com/api/report")
        self.assertEqual(code, 2)
        self.assertIn("https://", stderr)


if __name__ == "__main__":
    unittest.main()
