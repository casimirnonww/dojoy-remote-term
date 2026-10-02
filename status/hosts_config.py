"""Load and validate hosts.json, the single source of truth for every host entry."""

import ipaddress
import json
from pathlib import Path
import re


HOST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
PLACEHOLDER_PREFIX = "REPLACE"
RESERVED_PORTS = {4180, 8790}
KINDS = {"linux", "mac"}


class HostsConfigError(ValueError):
    pass


def _label(value, field, host_id):
    if not isinstance(value, str) or not value.strip() or len(value) > 40:
        raise HostsConfigError(f"{host_id}: {field} must be 1-40 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HostsConfigError(f"{host_id}: {field} must not contain control characters")
    return value.strip()


def _port(value, field, host_id, minimum=1):
    if type(value) is not int or not minimum <= value <= 65535:
        raise HostsConfigError(f"{host_id}: {field} must be an integer between {minimum} and 65535")
    return value


def _ssh_host(value, host_id):
    if not isinstance(value, str) or not value:
        raise HostsConfigError(f"{host_id}: ssh.host is required")
    if value.startswith(PLACEHOLDER_PREFIX):
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not HOSTNAME.match(value):
        raise HostsConfigError(f"{host_id}: ssh.host must be an IP address or hostname")
    return value


def parse_hosts(document):
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise HostsConfigError("hosts.json must have schema_version 1")
    entries = document.get("hosts")
    if not isinstance(entries, list) or not entries:
        raise HostsConfigError("hosts.json must list at least one host")
    hosts, ids, ttyd_ports, ssh_ports = [], set(), set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise HostsConfigError("each host must be an object")
        host_id = entry.get("id")
        if not isinstance(host_id, str) or not HOST_ID.match(host_id):
            raise HostsConfigError(f"invalid host id: {host_id!r}")
        if host_id in ids:
            raise HostsConfigError(f"duplicate host id: {host_id}")
        unknown = set(entry) - {"id", "name", "meta", "kind", "ttyd_port", "ssh", "tunnel"}
        if unknown:
            raise HostsConfigError(f"{host_id}: unknown fields {sorted(unknown)}")
        kind = entry.get("kind")
        if kind not in KINDS:
            raise HostsConfigError(f"{host_id}: kind must be one of {sorted(KINDS)}")
        ttyd_port = _port(entry.get("ttyd_port"), "ttyd_port", host_id, minimum=1024)
        if ttyd_port in ttyd_ports or ttyd_port in RESERVED_PORTS:
            raise HostsConfigError(f"{host_id}: ttyd_port {ttyd_port} is already used")
        ssh = entry.get("ssh")
        if not isinstance(ssh, dict) or set(ssh) != {"host", "port", "user"}:
            raise HostsConfigError(f"{host_id}: ssh must have exactly host, port and user")
        user = ssh["user"]
        if not isinstance(user, str) or not SSH_USER.match(user) or user == "root":
            raise HostsConfigError(f"{host_id}: ssh.user must be a valid non-root account")
        tunnel = entry.get("tunnel", False)
        if not isinstance(tunnel, bool):
            raise HostsConfigError(f"{host_id}: tunnel must be true or false")
        host = {
            "id": host_id,
            "name": _label(entry.get("name"), "name", host_id),
            "meta": _label(entry.get("meta"), "meta", host_id),
            "kind": kind,
            "path": "/" + host_id + "/",
            "ttyd_port": ttyd_port,
            "ssh_host": _ssh_host(ssh["host"], host_id),
            "ssh_port": _port(ssh["port"], "ssh.port", host_id),
            "ssh_user": user,
            "tunnel": tunnel,
        }
        if tunnel:
            if host["ssh_host"] != "127.0.0.1" or host["ssh_port"] < 1024:
                raise HostsConfigError(f"{host_id}: a tunnel host must use 127.0.0.1 and a port >= 1024")
            if host["ssh_port"] in ssh_ports:
                raise HostsConfigError(f"{host_id}: tunnel port {host['ssh_port']} is already used")
            ssh_ports.add(host["ssh_port"])
        ids.add(host_id)
        ttyd_ports.add(ttyd_port)
        hosts.append(host)
    return hosts


def load_hosts(path):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HostsConfigError(f"cannot read {path}: {error}") from None
    return parse_hosts(document)


def placeholders(hosts):
    return [host["id"] for host in hosts if host["ssh_host"].startswith(PLACEHOLDER_PREFIX)]
