#!/usr/bin/env python3
"""Read one Linux/macOS host and emit metrics JSON. Writes no files; needs no privileges."""

import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time


class ProbeDeadline(Exception):
    pass


def command(argv, timeout=3):
    result = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=timeout, check=True,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    return result.stdout.strip()


def linux_cpu_ticks():
    with open("/proc/stat", encoding="ascii") as handle:
        values = [int(value) for value in handle.readline().split()[1:9]]
    if len(values) < 4:
        raise ValueError("cpu counters unavailable")
    # guest/guest_nice are already included in user/nice; do not add them twice.
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def linux_default_interface():
    try:
        routes = []
        for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
            row = line.split()
            if len(row) >= 8 and row[1] == "00000000" and int(row[3], 16) & 1:
                routes.append((int(row[6]), row[0]))
        return min(routes)[1] if routes else None
    except (OSError, ValueError):
        return None


def linux_network_counters(interface):
    if interface is None:
        return None
    try:
        for line in Path("/proc/net/dev").read_text(encoding="ascii").splitlines():
            if ":" not in line:
                continue
            name, data = line.split(":", 1)
            if name.strip() == interface:
                values = data.split()
                return time.monotonic(), int(values[0]), int(values[8])
    except (OSError, ValueError, IndexError):
        pass
    return None


def mac_default_interface():
    try:
        route = command(["/sbin/route", "-n", "get", "default"], timeout=1)
        match = re.search(r"^\s*interface:\s*([A-Za-z0-9_.-]+)\s*$", route, re.M)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def mac_network_counters(interface):
    if interface is None:
        return None
    try:
        lines = command(["/usr/sbin/netstat", "-ibn", "-I", interface], timeout=1).splitlines()
        columns = lines[0].split()
        rx_index, tx_index = columns.index("Ibytes"), columns.index("Obytes")
        for line in lines[1:]:
            row = line.split()
            # Only the link-layer row: address-specific rows can repeat interface counters.
            if row and row[0].rstrip("*") == interface and any(value.startswith("<Link#") for value in row):
                return time.monotonic(), int(row[rx_index]), int(row[tx_index])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return None


def network_rates(interface, before, after):
    metrics = {
        "network_interface": interface,
        "network_rx_bytes_per_second": None,
        "network_tx_bytes_per_second": None,
    }
    if before is None or after is None or after[0] <= before[0]:
        return metrics
    elapsed = after[0] - before[0]
    rx, tx = after[1] - before[1], after[2] - before[2]
    if rx < 0 or tx < 0:
        return metrics
    metrics["network_rx_bytes_per_second"] = round(rx / elapsed, 1)
    metrics["network_tx_bytes_per_second"] = round(tx / elapsed, 1)
    return metrics


def linux_memory():
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0]) * 1024
    total = values["MemTotal"]
    if "MemAvailable" in values:
        available = values["MemAvailable"]
        note = "Linux：已用 = MemTotal − MemAvailable（含内核可回收内存估计）。"
    else:
        available = sum(values.get(key, 0) for key in
                        ("MemFree", "Buffers", "Cached", "SReclaimable"))
        available -= values.get("Shmem", 0)
        note = "Linux兼容估计：可用 = MemFree + Buffers + Cached + SReclaimable − Shmem。"
    available = min(total, max(0, available))
    return total, total - available, note


def linux_os_name():
    path = Path("/etc/os-release")
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                value = line.partition("=")[2].strip().strip('"\'')
                if value:
                    return value
    return "Linux " + platform.release()


def linux_metrics():
    interface = linux_default_interface()
    before_network = linux_network_counters(interface)
    before_total, before_idle = linux_cpu_ticks()
    time.sleep(0.3)
    after_total, after_idle = linux_cpu_ticks()
    after_network = linux_network_counters(interface)
    delta = after_total - before_total
    cpu_percent = None if delta <= 0 else round(
        min(100.0, max(0.0, 100 * (1 - (after_idle - before_idle) / delta))), 1
    )
    cpu_info = {}
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            cpu_info.setdefault(key.strip(), value.strip())
    model = (cpu_info.get("model name") or cpu_info.get("Hardware")
             or cpu_info.get("Processor") or platform.processor())
    if not model:
        raise ValueError("cpu model unavailable")
    total, used, note = linux_memory()
    return {
        "os": linux_os_name(), "cpu_model": model,
        "cpu_cores": os.cpu_count(), "cpu_percent": cpu_percent,
        "memory_total_bytes": total, "memory_used_bytes": used,
        "memory_note": note,
        "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0]),
        **network_rates(interface, before_network, after_network),
    }


def mac_metrics():
    def sysctl(key):
        return command(["/usr/sbin/sysctl", "-n", key])

    total = int(sysctl("hw.memsize"))
    cores = int(sysctl("hw.logicalcpu"))
    model = sysctl("machdep.cpu.brand_string")
    boot = re.search(r"\bsec\s*=\s*(\d+)", sysctl("kern.boottime"))
    if not boot or not model:
        raise ValueError("system information unavailable")

    interface = mac_default_interface()
    before_network = mac_network_counters(interface)
    # The second top sample covers a one-second interval instead of boot history.
    top = command(["/usr/bin/top", "-l", "2", "-s", "1", "-n", "0", "-stats", "pid"], timeout=4)
    after_network = mac_network_counters(interface)
    samples = re.findall(r"CPU usage:[^\n]*?([0-9.]+)% idle", top)
    cpu_percent = None
    if len(samples) >= 2:
        idle = float(samples[-1])
        if math.isfinite(idle) and 0 <= idle <= 100:
            cpu_percent = round(100 - idle, 1)

    vm_stat = command(["/usr/bin/vm_stat"])
    page_size = re.search(r"page size of (\d+) bytes", vm_stat)
    if not page_size:
        raise ValueError("memory page size unavailable")
    pages = {key.strip(): int(value) for key, value in
             re.findall(r"^([^:\n]+):\s+(\d+)\.?\s*$", vm_stat, re.M)}
    # macOS has no MemAvailable equivalent; inactive pages are reclaimable estimates.
    available = sum(pages[key] for key in
                    ("Pages free", "Pages inactive", "Pages speculative")) * int(page_size.group(1))
    available = min(total, max(0, available))
    return {
        "os": "macOS " + command(["/usr/bin/sw_vers", "-productVersion"]),
        "cpu_model": model, "cpu_cores": cores, "cpu_percent": cpu_percent,
        "memory_total_bytes": total, "memory_used_bytes": total - available,
        "memory_note": "macOS估计：可用 = (free + inactive + speculative) × 页大小；非活动页未必能立即回收，与活动监视器口径不同。",
        "uptime_seconds": max(0.0, time.time() - int(boot.group(1))),
        **network_rates(interface, before_network, after_network),
    }


def collect_metrics():
    system = platform.system()
    if system == "Linux":
        metrics = linux_metrics()
    elif system == "Darwin":
        metrics = mac_metrics()
    else:
        raise ValueError("unsupported operating system")
    disk = shutil.disk_usage("/")
    try:
        load = os.getloadavg()[0]
        load = round(load, 2) if math.isfinite(load) and load >= 0 else None
    except OSError:
        load = None
    metrics.update({
        "hostname": socket.gethostname(), "arch": platform.machine(),
        "disk_total_bytes": disk.total, "disk_used_bytes": disk.used,
        "uptime_seconds": round(metrics["uptime_seconds"], 1), "load_1": load,
    })
    if not metrics["cpu_cores"] or not metrics["hostname"] or not metrics["arch"]:
        raise ValueError("required metrics unavailable")
    return metrics


def main():
    print(json.dumps(collect_metrics(), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    def budget_exceeded(signum, frame):
        raise ProbeDeadline("probe budget exceeded")

    try:
        # Bound the work so a stuck system command cannot hang the caller.
        signal.signal(signal.SIGALRM, budget_exceeded)
        signal.setitimer(signal.ITIMER_REAL, 7.0)
        main()
    except Exception:
        # Never forward subprocess stderr, command details or a target's private data.
        print("运行状态探针失败；未返回完整指标。", file=sys.stderr)
        sys.exit(1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
