#!/usr/bin/env python3
"""Collect this host's metrics and push them to the remote-term receiver; standard library only.

Runs unprivileged from a systemd timer (Linux) or a LaunchAgent (macOS). The host only
makes outbound HTTPS requests; nothing on the gateway can log in to it through this path.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import urllib.request

import probe


TOTAL_BUDGET_SECONDS = 25
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class AgentDeadline(Exception):
    pass


class ConfigError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would replay the bearer token to another URL; treat it as a failure instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


def check_url(url):
    parts = urlsplit(url or "")
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return url
    raise ConfigError("上报地址必须是 https://（仅本机回环地址允许 http://）。")


def read_token(token_file):
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            raise ConfigError("无法读取 token 文件。") from None
    else:
        token = os.environ.get("REMOTE_TERM_TOKEN", "").strip()
    if not token or any(char.isspace() for char in token) or len(token) > 512:
        raise ConfigError("缺少有效的上报 token。")
    return token


def build_report():
    start = time.monotonic()
    metrics = probe.collect_metrics()
    return {"metrics": metrics, "probe_ms": round((time.monotonic() - start) * 1000)}


def send_report(url, token, report, cafile=None, timeout=10):
    body = json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "User-Agent": "remote-term-agent/1",
    })
    handlers = [NoRedirect()]
    if urlsplit(url).scheme == "https":
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(request, timeout=timeout) as response:
        return response.status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("REMOTE_TERM_URL"),
                        help="report endpoint, e.g. https://djai.djscz.com/api/report (env REMOTE_TERM_URL)")
    parser.add_argument("--token-file", help="file holding the host token (default: env REMOTE_TERM_TOKEN)")
    parser.add_argument("--cafile", help="CA bundle for TLS verification (default: system store)")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the report instead of sending it")
    args = parser.parse_args(argv)

    def budget_exceeded(signum, frame):
        raise AgentDeadline()

    signal.signal(signal.SIGALRM, budget_exceeded)
    signal.setitimer(signal.ITIMER_REAL, TOTAL_BUDGET_SECONDS)
    try:
        if args.print_only:
            print(json.dumps(build_report(), ensure_ascii=False, allow_nan=False))
            return 0
        url = check_url(args.url)
        token = read_token(args.token_file)
        send_report(url, token, build_report(), args.cafile, args.timeout)
        return 0
    except ConfigError as error:
        print("配置错误：" + str(error), file=sys.stderr)
        return 2
    except HTTPError as error:
        print(f"上报被拒绝：HTTP {error.code}。", file=sys.stderr)
    except AgentDeadline:
        print(f"上报超时（{TOTAL_BUDGET_SECONDS} 秒）；下个周期重试。", file=sys.stderr)
    except (URLError, ssl.SSLError, OSError) as error:
        reason = getattr(error, "reason", error)
        kind = "TLS 校验失败" if isinstance(reason, ssl.SSLError) else "网络不可达"
        print(f"上报失败：{kind}；下个周期重试。", file=sys.stderr)
    except Exception:
        # Never echo command output, paths or other host details.
        print("采集失败；未上报。", file=sys.stderr)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return 1


if __name__ == "__main__":
    sys.exit(main())
