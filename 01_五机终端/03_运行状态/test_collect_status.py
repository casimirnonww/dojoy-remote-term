import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import collect_status as collector
import probe


METRICS = {
    "hostname": "fixture-host", "os": "Fixture OS", "arch": "arm64",
    "cpu_model": "Fixture CPU", "cpu_cores": 8, "cpu_percent": 12.5,
    "memory_total_bytes": 16000, "memory_used_bytes": 6000,
    "disk_total_bytes": 100000, "disk_used_bytes": 40000,
    "uptime_seconds": 123.4, "load_1": 0.5,
}
LAST_SUCCESS = "2026-09-06T01:00:00Z"


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.probe = self.root / "fixture_probe.py"
        self.probe.write_text("print(" + repr(json.dumps(METRICS)) + ")\n")

    def test_local_probe_runs_and_returns_online_metrics(self):
        row = collector.collect_host(collector.HOSTS[0], self.probe, "", {})
        self.assertEqual(row["status"], "online")
        self.assertEqual(row["metrics"], METRICS)
        self.assertIsNone(row["error"])
        self.assertTrue(collector.valid_timestamp(row["last_success_at"]))

    def test_ssh_failure_keeps_last_metrics_without_claiming_online(self):
        previous = {"fa": {"metrics": METRICS, "last_success_at": LAST_SUCCESS}}
        source = self.probe.read_text()
        with patch.object(collector.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "")) as run:
            row = collector.collect_host(collector.HOSTS[1], self.probe, source, previous)
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(row["metrics"], METRICS)
        self.assertEqual(row["last_success_at"], LAST_SUCCESS)
        self.assertIsNotNone(row["error"])
        argv = run.call_args.args[0]
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=5", "ConnectionAttempts=1"):
            self.assertIn(option, argv)
        self.assertEqual(argv[-3:], ["fa", "python3", "-"])
        self.assertEqual(run.call_args.kwargs["input"], source)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_missing_metric_preserves_previous_success(self):
        incomplete = dict(METRICS)
        del incomplete["memory_total_bytes"]
        self.probe.write_text("print(" + repr(json.dumps(incomplete)) + ")\n")
        previous = {"vps": {"metrics": METRICS, "last_success_at": LAST_SUCCESS}}
        row = collector.collect_host(collector.HOSTS[0], self.probe, "", previous)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["metrics"], METRICS)
        self.assertEqual(row["last_success_at"], LAST_SUCCESS)

    def test_local_mac_uses_its_ssh_alias_instead_of_collecting_the_vps(self):
        source = self.probe.read_text()
        host = next(host for host in collector.HOSTS if host[0] == "mac-local")
        with patch.object(collector.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(METRICS))) as run:
            row = collector.collect_host(host, self.probe, source, {})
        self.assertEqual(row["id"], "mac-local")
        self.assertEqual(row["name"], "本机 Mac")
        self.assertEqual(row["status"], "online")
        self.assertEqual(run.call_args.args[0][-3:], ["mac-local", "python3", "-"])
        self.assertEqual(run.call_args.kwargs["input"], source)

    def test_local_mac_disconnect_preserves_only_its_own_previous_metrics(self):
        host = next(host for host in collector.HOSTS if host[0] == "mac-local")
        local_metrics = {**METRICS, "hostname": "fixture-local-mac"}
        previous = {
            "mac-local": {"metrics": local_metrics, "last_success_at": LAST_SUCCESS},
            "mbp-dojoy": {"metrics": METRICS, "last_success_at": LAST_SUCCESS},
        }
        with patch.object(collector.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "")):
            row = collector.collect_host(host, self.probe, "", previous)
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(row["metrics"], local_metrics)
        self.assertEqual(row["last_success_at"], LAST_SUCCESS)

    def test_offline_without_history_has_null_metrics(self):
        with patch.object(collector.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "")):
            row = collector.collect_host(collector.HOSTS[4], self.probe, "", {})
        self.assertEqual(row["status"], "unreachable")
        self.assertIsNone(row["metrics"])
        self.assertIsNone(row["last_success_at"])

    def test_timeout_does_not_publish_success(self):
        self.probe.write_text("import time\ntime.sleep(1)\n")
        with patch.object(collector, "HOST_TIMEOUT_SECONDS", 0.05):
            row = collector.collect_host(collector.HOSTS[0], self.probe, "", {})
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["metrics"])
        self.assertLess(row["duration_ms"], 900)

    def test_atomic_publish_leaves_old_file_intact_on_replace_failure(self):
        output = self.root / "status.json"
        output.write_text('{"old":true}\n')
        with patch.object(collector.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                collector.atomic_write(output, {"new": True})
        self.assertEqual(output.read_text(), '{"old":true}\n')
        self.assertEqual(list(self.root.glob(".status.json.*.tmp")), [])

    def test_atomic_publish_is_readable_json_and_validates_saved_history(self):
        output = self.root / "status.json"
        document = {"schema_version": 1, "hosts": [
            {"id": "vps", "metrics": METRICS, "last_success_at": LAST_SUCCESS},
            {"id": "fa", "metrics": {"hostname": "partial"}, "last_success_at": LAST_SUCCESS},
        ]}
        collector.atomic_write(output, document)
        self.assertEqual(json.loads(output.read_text()), document)
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.assertEqual(set(collector.read_previous(output)), {"vps"})

    def test_unknown_values_are_null_but_invalid_numbers_are_rejected(self):
        optional_unknown = {**METRICS, "cpu_percent": None, "load_1": None}
        self.assertEqual(collector.validate_metrics(optional_unknown), optional_unknown)
        for field, value in (("cpu_percent", float("nan")), ("cpu_percent", 101),
                             ("memory_used_bytes", 17000), ("cpu_cores", True)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                collector.validate_metrics({**METRICS, field: value})

    def test_fixed_host_snapshot_never_exceeds_three_parallel_processes(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def fake_run(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return subprocess.CompletedProcess([], 0, json.dumps(METRICS))

        with patch.object(collector.subprocess, "run", side_effect=fake_run):
            result = collector.collect_snapshot(self.probe, self.probe.read_text(), {})
        self.assertEqual([row["id"] for row in result["hosts"]],
                         ["vps", "fa", "tencent-new", "tencent-main", "mbp-dojoy", "mac-local"])
        self.assertEqual(result["refresh_interval_seconds"], 30)
        self.assertEqual(maximum, 3)
        self.assertTrue(all(row["status"] == "online" for row in result["hosts"]))

    def test_network_rates_use_elapsed_time_and_reject_counter_reset(self):
        rates = probe.network_rates("eth0", (10.0, 1000, 2000), (10.5, 1500, 4000))
        self.assertEqual(rates["network_rx_bytes_per_second"], 1000)
        self.assertEqual(rates["network_tx_bytes_per_second"], 4000)
        collector.validate_metrics({**METRICS, **rates})
        reset = probe.network_rates("eth0", (10.0, 1000, 2000), (10.5, 0, 0))
        self.assertIsNone(reset["network_rx_bytes_per_second"])
        self.assertIsNone(reset["network_tx_bytes_per_second"])
        unavailable = probe.network_rates(None, None, None)
        self.assertIsNone(unavailable["network_interface"])
        collector.validate_metrics({**METRICS, **unavailable})

    def test_mac_network_counters_select_link_row_without_double_counting(self):
        fixture = "\n".join([
            "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll",
            "en0 1500 <Link#7> 00:00:00:00:00:00 12 0 4096 20 0 8192 0",
            "en0 1500 192.0.2/24 192.0.2.1 12 - 4096 20 - 8192 -",
        ])
        with patch.object(probe, "command", return_value=fixture):
            counters = probe.mac_network_counters("en0")
        self.assertEqual(counters[1:], (4096, 8192))


if __name__ == "__main__":
    unittest.main()
