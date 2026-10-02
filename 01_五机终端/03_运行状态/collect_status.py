#!/usr/bin/env python3
"""Collect six fixed hosts into one atomic status.json; Python standard library only."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


# id, display name, existing SSH alias. None means this VPS, without SSH.
HOSTS = (
    ("vps", "VPS", None),
    ("fa", "财务机", "fa"),
    ("tencent-new", "腾讯新机", "tencent-new"),
    ("tencent-main", "腾讯大总管", "tencent-main"),
    ("mbp-dojoy", "另一台 Mac", "mbp-dojoy"),
    ("mac-local", "本机 Mac", "mac-local"),
)
HOST_TIMEOUT_SECONDS = 8
REFRESH_INTERVAL_SECONDS = 30
MAX_WORKERS = 3
REQUIRED_METRICS = {
    "hostname", "os", "arch", "cpu_model", "cpu_cores", "cpu_percent",
    "memory_total_bytes", "memory_used_bytes", "disk_total_bytes", "disk_used_bytes",
    "uptime_seconds", "load_1",
}
OPTIONAL_METRICS = {
    "memory_note", "network_interface", "network_rx_bytes_per_second", "network_tx_bytes_per_second",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def valid_timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").utcoffset().total_seconds() == 0
    except (ValueError, AttributeError):
        return False


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_metrics(metrics):
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")
    if not REQUIRED_METRICS <= set(metrics) or set(metrics) - REQUIRED_METRICS - OPTIONAL_METRICS:
        raise ValueError("unexpected or missing metrics")
    for key in ("hostname", "os", "arch", "cpu_model"):
        if not isinstance(metrics[key], str) or not metrics[key].strip() or len(metrics[key]) > 512:
            raise ValueError("invalid text metric")
    if "memory_note" in metrics and (not isinstance(metrics["memory_note"], str)
                                    or len(metrics["memory_note"]) > 512):
        raise ValueError("invalid memory note")
    interface = metrics.get("network_interface")
    if interface is not None and (not isinstance(interface, str) or not interface.strip() or len(interface) > 128):
        raise ValueError("invalid network interface")
    for key in ("network_rx_bytes_per_second", "network_tx_bytes_per_second"):
        value = metrics.get(key)
        if value is not None and (not valid_number(value) or value < 0):
            raise ValueError("invalid network rate")
    for key in ("cpu_cores", "memory_total_bytes", "memory_used_bytes", "disk_total_bytes", "disk_used_bytes"):
        if type(metrics[key]) is not int or metrics[key] < 0:
            raise ValueError("invalid integer metric")
    if any(metrics[key] == 0 for key in ("cpu_cores", "memory_total_bytes", "disk_total_bytes")):
        raise ValueError("invalid total metric")
    for total, used in (("memory_total_bytes", "memory_used_bytes"), ("disk_total_bytes", "disk_used_bytes")):
        if metrics[used] > metrics[total]:
            raise ValueError("used exceeds total")
    if not valid_number(metrics["uptime_seconds"]) or metrics["uptime_seconds"] < 0:
        raise ValueError("invalid uptime")
    for key in ("cpu_percent", "load_1"):
        value = metrics[key]
        if value is not None and (not valid_number(value) or value < 0):
            raise ValueError("invalid optional metric")
    if metrics["cpu_percent"] is not None and metrics["cpu_percent"] > 100:
        raise ValueError("invalid cpu percentage")
    return dict(metrics)


def read_previous(output):
    try:
        document = json.loads(output.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("hosts"), list):
            return {}
        result = {}
        ids = {host[0] for host in HOSTS}
        for host in document["hosts"]:
            if not isinstance(host, dict) or host.get("id") not in ids:
                continue
            if not valid_timestamp(host.get("last_success_at")):
                continue
            try:
                metrics = validate_metrics(host.get("metrics"))
            except (ValueError, TypeError, OverflowError):
                continue
            result[host["id"]] = {"metrics": metrics, "last_success_at": host["last_success_at"]}
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def collect_host(host, probe_path, probe_source, previous):
    host_id, name, alias = host
    start = time.monotonic()
    old = previous.get(host_id, {})
    result = {
        "id": host_id, "name": name, "checked_at": None,
        "last_success_at": old.get("last_success_at"), "status": "error", "duration_ms": 0,
        "metrics": old.get("metrics"), "error": None,
    }
    if alias is None:
        argv = [sys.executable, str(probe_path)]
        probe_input = None
    else:
        argv = [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
            alias, "python3", "-",
        ]
        probe_input = probe_source
    try:
        process = subprocess.run(
            argv, input=probe_input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=HOST_TIMEOUT_SECONDS, check=False,
        )
        if process.returncode:
            result["status"] = "unreachable" if alias is not None and process.returncode == 255 else "error"
            result["error"] = "SSH连接失败。" if result["status"] == "unreachable" else "探针执行失败或目标缺少Python 3。"
        elif len(process.stdout) > 65536:
            result["error"] = "探针返回数据超出限制。"
        else:
            try:
                metrics = validate_metrics(json.loads(process.stdout))
            except (ValueError, TypeError, OverflowError):
                result["error"] = "探针返回的指标格式不完整或无效。"
            else:
                result.update(status="online", metrics=metrics, last_success_at=utc_now())
    except subprocess.TimeoutExpired:
        result["status"] = "unreachable" if alias is not None else "error"
        result["error"] = "采集超时（8秒）；下个周期重试。"
    except OSError:
        result["error"] = "无法启动本机采集进程。"
    except Exception:
        result["error"] = "采集失败；下个周期重试。"
    result["checked_at"] = utc_now()
    result["duration_ms"] = round((time.monotonic() - start) * 1000)
    return result


def collect_snapshot(probe_path, probe_source, previous):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        rows = list(pool.map(lambda host: collect_host(host, probe_path, probe_source, previous), HOSTS))
    return {
        "schema_version": 1, "generated_at": utc_now(),
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS, "hosts": rows,
    }


def atomic_write(output, document):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent,
            prefix="." + output.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/var/www/remote-term/status.json"))
    parser.add_argument("--probe", type=Path, default=Path(__file__).with_name("probe.py"))
    args = parser.parse_args(argv)
    try:
        probe_path = args.probe.resolve(strict=True)
        source = probe_path.read_text(encoding="utf-8")
        previous = read_previous(args.output)
        document = collect_snapshot(probe_path, source, previous)
        atomic_write(args.output, document)
    except Exception:
        print("运行状态快照未能发布；保留原文件。", file=sys.stderr)
        return 1
    online = sum(host["status"] == "online" for host in document["hosts"])
    print(f"运行状态快照已发布：{online}/{len(HOSTS)} 台采集成功。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
