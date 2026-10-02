#!/usr/bin/env python3
"""Accept per-host metric reports and serve status.json; Python standard library only.

Each host pushes its own metrics with a host-specific bearer token. The gateway
never logs in to the hosts to collect anything, so it holds no credentials for them.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sys
import threading

from hosts_config import HostsConfigError, load_hosts
from status_schema import atomic_write, format_timestamp, parse_timestamp, valid_number, validate_metrics


SCHEMA_VERSION = 1
REFRESH_INTERVAL_SECONDS = 30
FRESHNESS = timedelta(seconds=90)
CLOCK_SKEW = timedelta(seconds=10)
MAX_BODY_BYTES = 65536
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

NEVER_REPORTED = "尚未收到上报。"
REPORT_OVERDUE = "超过 90 秒未收到上报。"
REPORT_INVALID = "上报的指标格式不完整或无效。"


def utc_clock():
    return datetime.now(timezone.utc)


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """Per-host token hashes; the plaintext token is shown once and never stored."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._signature = None
        self._digests = {}

    def _read(self):
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        tokens = document.get("tokens") if isinstance(document, dict) else None
        if not isinstance(tokens, dict):
            raise ValueError("token file must contain a tokens object")
        return {host_id: digest for host_id, digest in tokens.items()
                if isinstance(host_id, str) and isinstance(digest, str) and SHA256_HEX.match(digest)}

    def digests(self):
        with self._lock:
            try:
                stat = self.path.stat()
                signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except FileNotFoundError:
                signature = None
            if signature != self._signature:
                try:
                    self._digests = self._read()
                except (OSError, ValueError):
                    # Fail closed: an unreadable token file accepts nobody.
                    self._digests = {}
                self._signature = signature
            return dict(self._digests)

    def identify(self, token):
        if not token:
            return None
        digest = token_digest(token)
        match = None
        for host_id, stored in self.digests().items():
            # Compare against every entry so timing does not depend on the position of a match.
            if hmac.compare_digest(digest, stored):
                match = host_id
        return match

    def _write(self, digests):
        gid = self.path.stat().st_gid if self.path.exists() else None
        atomic_write(self.path, {"tokens": dict(sorted(digests.items()))}, mode=0o640, gid=gid)

    def issue(self, host_id):
        token = secrets.token_urlsafe(32)
        digests = self._read()
        digests[host_id] = token_digest(token)
        self._write(digests)
        return token

    def revoke(self, host_id):
        digests = self._read()
        removed = digests.pop(host_id, None) is not None
        if removed:
            self._write(digests)
        return removed


class StatusState:
    def __init__(self, hosts, state_path, clock=utc_clock):
        self.hosts = [(host["id"], host["name"]) for host in hosts]
        self.state_path = Path(state_path) if state_path else None
        self.clock = clock
        self._lock = threading.Lock()
        self.records = {host_id: self._empty() for host_id, _ in self.hosts}
        self._load()

    @staticmethod
    def _empty():
        return {"last_success_at": None, "metrics": None, "checked_at": None, "error": None, "duration_ms": 0}

    def _load(self):
        if not self.state_path:
            return
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
            saved = document.get("hosts") if document.get("schema_version") == SCHEMA_VERSION else None
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(saved, dict):
            return
        for host_id, record in saved.items():
            if host_id not in self.records or not isinstance(record, dict):
                continue
            if parse_timestamp(record.get("last_success_at")) is None:
                continue
            try:
                metrics = validate_metrics(record.get("metrics"))
            except (ValueError, TypeError, OverflowError):
                continue
            restored = self._empty()
            restored.update(last_success_at=record["last_success_at"], metrics=metrics)
            if parse_timestamp(record.get("checked_at")) is not None:
                restored["checked_at"] = record["checked_at"]
            self.records[host_id] = restored

    def _persist(self):
        if self.state_path:
            atomic_write(self.state_path, {"schema_version": SCHEMA_VERSION, "hosts": self.records})

    def record_success(self, host_id, metrics, duration_ms):
        with self._lock:
            now = format_timestamp(self.clock())
            self.records[host_id] = {
                "last_success_at": now, "metrics": metrics, "checked_at": now,
                "error": None, "duration_ms": duration_ms,
            }
            self._persist()

    def record_failure(self, host_id, error):
        with self._lock:
            record = self.records[host_id]
            record.update(checked_at=format_timestamp(self.clock()), error=error)
            self._persist()

    def _fresh(self, value, now):
        moment = parse_timestamp(value)
        return moment is not None and -CLOCK_SKEW <= now - moment < FRESHNESS

    def snapshot(self):
        with self._lock:
            now = self.clock()
            rows = []
            for host_id, name in self.hosts:
                record = self.records[host_id]
                if record["error"] and self._fresh(record["checked_at"], now):
                    status, error = "error", record["error"]
                elif record["error"] is None and self._fresh(record["last_success_at"], now):
                    status, error = "online", None
                elif record["checked_at"] is None:
                    status, error = "unreachable", NEVER_REPORTED
                else:
                    status, error = "unreachable", REPORT_OVERDUE
                rows.append({
                    "id": host_id, "name": name, "checked_at": record["checked_at"],
                    "last_success_at": record["last_success_at"], "status": status,
                    "duration_ms": record["duration_ms"], "metrics": record["metrics"], "error": error,
                })
            return {
                "schema_version": SCHEMA_VERSION, "generated_at": format_timestamp(now),
                "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS, "hosts": rows,
            }


def parse_report(body):
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) - {"metrics", "probe_ms"}:
        raise ValueError("unexpected report fields")
    probe_ms = payload.get("probe_ms", 0)
    if not valid_number(probe_ms) or not 0 <= probe_ms <= 60000:
        raise ValueError("invalid probe duration")
    return validate_metrics(payload.get("metrics")), round(probe_ms)


class Handler(BaseHTTPRequestHandler):
    server_version = "remote-term-receiver"
    sys_version = ""
    timeout = 10

    def _send(self, code, body=b"", content_type="application/json; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        return self.path.split("?", 1)[0]

    def do_GET(self):
        route = self._route()
        if route == "/status.json":
            document = self.server.state.snapshot()
            self._send(200, json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        elif route == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
        else:
            self._send(404)

    do_HEAD = do_GET

    def do_POST(self):
        if self._route() != "/api/report":
            self._send(404)
            return
        if self.headers.get("Transfer-Encoding"):
            self._send(411)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411)
            return
        if length > MAX_BODY_BYTES:
            self._send(413)
            self.close_connection = True
            return
        if length <= 0:
            self._send(400)
            return
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(415)
            self.close_connection = True
            return
        scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
        host_id = self.server.tokens.identify(token.strip()) if scheme.lower() == "bearer" else None
        if host_id is None or host_id not in self.server.state.records:
            self._send(401, headers=(("WWW-Authenticate", "Bearer"),))
            self.close_connection = True
            return
        body = self.rfile.read(length)
        try:
            metrics, probe_ms = parse_report(body)
        except (ValueError, TypeError, OverflowError, UnicodeDecodeError):
            self.server.state.record_failure(host_id, REPORT_INVALID)
            self._send(400)
            return
        self.server.state.record_success(host_id, metrics, probe_ms)
        self._send(204)

    def log_request(self, code="-", size="-"):
        # Successful reads and reports arrive every few seconds; only log what needs attention.
        if isinstance(code, int) and code < 400:
            return
        super().log_request(code, size)


def make_server(state, tokens, bind="127.0.0.1", port=8790):
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.state = state
    server.tokens = tokens
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hosts", type=Path, default=Path("/opt/remote-term/hosts.json"))
    parser.add_argument("--tokens", type=Path, default=Path("/etc/remote-term/agent-tokens.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the HTTP receiver")
    serve.add_argument("--state", type=Path, default=Path("/var/lib/remote-term/state.json"))
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8790)
    issue = commands.add_parser("issue-token", help="create or replace a host's report token and print it once")
    issue.add_argument("host_id")
    revoke = commands.add_parser("revoke-token", help="remove a host's report token")
    revoke.add_argument("host_id")
    args = parser.parse_args(argv)

    try:
        hosts = load_hosts(args.hosts)
    except HostsConfigError as error:
        print(f"hosts.json invalid: {error}", file=sys.stderr)
        return 2
    tokens = TokenStore(args.tokens)
    if args.command in ("issue-token", "revoke-token"):
        if args.host_id not in {host["id"] for host in hosts}:
            print(f"unknown host id: {args.host_id}", file=sys.stderr)
            return 2
        if args.command == "issue-token":
            print(tokens.issue(args.host_id))
        elif not tokens.revoke(args.host_id):
            print(f"no token for {args.host_id}", file=sys.stderr)
            return 1
        return 0

    server = make_server(StatusState(hosts, args.state), tokens, args.bind, args.port)
    print(f"remote-term receiver listening on {args.bind}:{args.port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
