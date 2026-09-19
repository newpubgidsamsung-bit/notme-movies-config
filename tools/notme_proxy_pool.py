#!/usr/bin/env python3
"""Low-impact NotmeMovies proxy-pool refresher.

Safety design:
- Fetches a small number of candidates from a few public list providers.
- Does NOT connect through or actively scan candidate proxies from GitHub runners.
- Deduplicates and validates syntax/IP ranges.
- Replaces proxyServers only for enabled managed users.
- Preserves the previous pool if too few candidates are fetched.
- Produces deterministic ordering so unchanged pools do not generate commits.

The Android app already remembers successful proxies, skips recently failed ones,
and moves through the assigned list automatically. That is where liveness failover
belongs; GitHub Actions is only used as a light candidate-list distributor.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests

CONFIG = Path(os.environ.get("NOTME_SITES_CONFIG", "sites.json"))
POOL_LIMIT = max(8, min(80, int(os.environ.get("NOTME_PROXY_LIMIT", "40"))))
MIN_POOL = max(3, min(POOL_LIMIT, int(os.environ.get("NOTME_MIN_POOL", "8"))))
SOURCE_LIMIT = max(10, min(100, int(os.environ.get("NOTME_SOURCE_LIMIT", "60"))))
TIMEOUT = (8, 12)
UA = "NotmeMovies-ProxyPool/2.0 (+configuration refresh; no proxy scanning)"

SOURCES = (
    ("ProxyScrape HTTP", "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=http&country=all"),
    ("HProxy", "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/live.txt"),
    ("GeoNode", f"https://proxylist.geonode.com/api/proxy-list?page=1&limit={SOURCE_LIMIT}&sort_by=responseTime&sort_type=asc"),
)

HOSTPORT_RE = re.compile(r"^(?:(https?|socks5?|socks)://)?([^:/\s]+):(\d{1,5})$", re.I)


def allowed_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostnames are allowed syntactically; providers normally return IPs.
        return bool(re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host))
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def normalize_proxy(value: str, default_scheme: str = "http") -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    m = HOSTPORT_RE.match(value)
    if not m:
        return None
    scheme = (m.group(1) or default_scheme).lower()
    if scheme == "socks5":
        scheme = "socks"
    if scheme not in {"http", "https", "socks"}:
        return None
    host = m.group(2).strip("[]")
    port = int(m.group(3))
    if port < 1 or port > 65535 or not allowed_ip(host):
        return None
    return f"{scheme}://{host}:{port}"


def request(url: str) -> requests.Response:
    last = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=TIMEOUT)
            if r.status_code == 429:
                retry = min(30, max(2, int(r.headers.get("Retry-After", "5") or "5")))
                if attempt == 0:
                    time.sleep(retry)
                    continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            if attempt == 0:
                time.sleep(2)
    raise RuntimeError(f"request failed: {last}")


def parse_text(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        proxy = normalize_proxy(raw)
        if proxy and proxy not in out:
            out.append(proxy)
        if len(out) >= SOURCE_LIMIT:
            break
    return out


def parse_geonode(payload: dict) -> list[str]:
    out: list[str] = []
    for item in payload.get("data") or []:
        host = str(item.get("ip") or "").strip()
        port = str(item.get("port") or "").strip()
        protocols = [str(x).lower() for x in (item.get("protocols") or [])]
        scheme = "https" if "https" in protocols else "http" if "http" in protocols else "socks" if any(x.startswith("socks") for x in protocols) else "http"
        proxy = normalize_proxy(f"{scheme}://{host}:{port}")
        if proxy and proxy not in out:
            out.append(proxy)
        if len(out) >= SOURCE_LIMIT:
            break
    return out


def fetch_candidates() -> list[str]:
    merged: set[str] = set()
    for name, url in SOURCES:
        try:
            r = request(url)
            if "geonode.com" in url:
                values = parse_geonode(r.json())
            else:
                values = parse_text(r.text)
            merged.update(values[:SOURCE_LIMIT])
            print(f"{name}: accepted {len(values[:SOURCE_LIMIT])} candidates")
        except Exception as exc:
            print(f"WARNING: {name} skipped: {exc}", file=sys.stderr)
    # Stable order prevents needless commits caused only by provider ordering.
    return sorted(merged)[:POOL_LIMIT]


def main() -> int:
    if not CONFIG.exists():
        print(f"ERROR: {CONFIG} not found", file=sys.stderr)
        return 2
    root = json.loads(CONFIG.read_text(encoding="utf-8"))
    users = root.get("proxyUsers")
    if not isinstance(users, list):
        print("No proxyUsers array; nothing to refresh.")
        return 0

    candidates = fetch_candidates()
    if len(candidates) < MIN_POOL:
        print(f"Only {len(candidates)} candidates fetched (< {MIN_POOL}); preserving existing pools.")
        return 0

    changed = 0
    for user in users:
        if not isinstance(user, dict) or not user.get("enabled", False):
            continue
        old = [str(x).strip() for x in (user.get("proxyServers") or []) if str(x).strip()]
        if old != candidates:
            user["proxyServers"] = candidates
            changed += 1

    if changed == 0:
        print("Enabled users already have the current candidate pool; no file change.")
        return 0

    root["updatedAt"] = int(time.time() * 1000)
    CONFIG.write_text(json.dumps(root, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {changed} enabled managed user(s) with {len(candidates)} candidates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
