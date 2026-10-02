"""Shared metric validation and atomic file publishing; Python standard library only."""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile


REQUIRED_METRICS = {
    "hostname", "os", "arch", "cpu_model", "cpu_cores", "cpu_percent",
    "memory_total_bytes", "memory_used_bytes", "disk_total_bytes", "disk_used_bytes",
    "uptime_seconds", "load_1",
}
OPTIONAL_METRICS = {
    "memory_note", "network_interface", "network_rx_bytes_per_second", "network_tx_bytes_per_second",
}


def format_timestamp(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_now():
    return format_timestamp(datetime.now(timezone.utc))


def parse_timestamp(value):
    """Return a timezone-aware datetime for a valid UTC 'Z' timestamp, else None."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.utcoffset().total_seconds() == 0 else None


def valid_timestamp(value):
    return parse_timestamp(value) is not None


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


def atomic_write(output, document, mode=0o644, gid=None):
    output = Path(output)
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
        os.chmod(temporary, mode)
        if gid is not None:
            os.chown(temporary, -1, gid)
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
