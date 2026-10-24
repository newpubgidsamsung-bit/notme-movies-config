
#!/usr/bin/env python3
"""Refresh unsigned proxy candidates without editing signed sites.json."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

OUTPUT = Path(os.environ.get("NOTME_PROXY_OUTPUT", "proxy-pool.json"))
POOL_LIMIT = max(8, min(80, int(os.environ.get("NOTME_PROXY_LIMIT", "40"))))
MIN_POOL = max(3, min(POOL_LIMIT, int(os.environ.get("NOTME_MIN_POOL", "8"))))
SOURCE_LIMIT = max(10, min(100, int(os.environ.get("NOTME_SOURCE_LIMIT", "60"))))
TIMEOUT = (8, 12)
UA = "NotmeMovies-ProxyPool/3.0"

SOURCES = (
    (
        "ProxyScrape HTTP",
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies&proxy_format=protocolipport"
        "&format=text&protocol=http&country=all"
    ),
    (
        "HProxy",
        "https://raw.githubusercontent.com/hproxy-com/"
        "free-proxy-list/main/live.txt"
    ),
    (
        "GeoNode",
        "https://proxylist.geonode.com/api/proxy-list"
        f"?page=1&limit={SOURCE_LIMIT}"
        "&sort_by=responseTime&sort_type=asc"
    ),
)

HOSTPORT_RE = re.compile(
    r"^(?:(https?|socks5?|socks)://)?([^:/\s]+):(\d{1,5})$",
    re.I
)


def allowed_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return isinstance(ip, ipaddress.IPv4Address) and ip.is_global
    except ValueError:
        return False


def normalize_proxy(value: str, default_scheme: str = "http") -> str | None:
    match = HOSTPORT_RE.fullmatch((value or "").strip())
    if not match:
        return None

    scheme = (match.group(1) or default_scheme).lower()
    if scheme in {"socks5", "socks4"}:
        scheme = "socks"

    if scheme not in {"http", "https", "socks"}:
        return None

    host = match.group(2)
    port = int(match.group(3))

    if not 1 <= port <= 65535 or not allowed_ip(host):
        return None

    return f"{scheme}://{host}:{port}"


def request(url: str) -> requests.Response:
    last = None

    for attempt in range(2):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": UA, "Accept": "*/*"},
                timeout=TIMEOUT,
            )

            if response.status_code == 429 and attempt == 0:
                retry = int(response.headers.get("Retry-After", "5") or "5")
                time.sleep(min(12, max(2, retry)))
                continue

            response.raise_for_status()
            return response

        except requests.RequestException as exc:
            last = exc
            if attempt == 0:
                time.sleep(2)

    raise RuntimeError(f"Source request failed: {last}")


def parse_text(value: str) -> list[str]:
    out = []

    for raw in value.splitlines():
        proxy = normalize_proxy(raw)
        if proxy and proxy not in out:
            out.append(proxy)

        if len(out) >= SOURCE_LIMIT:
            break

    return out


def parse_geonode(payload: dict) -> list[str]:
    out = []

    for row in payload.get("data") or []:
        host = str(row.get("ip") or "").strip()
        port = str(row.get("port") or "").strip()
        protocols = [
            str(x).lower()
            for x in (row.get("protocols") or [])
        ]

        if "https" in protocols:
            scheme = "https"
        elif "http" in protocols:
            scheme = "http"
        elif any(x.startswith("socks") for x in protocols):
            scheme = "socks"
        else:
            scheme = "http"

        proxy = normalize_proxy(f"{scheme}://{host}:{port}")
        if proxy and proxy not in out:
            out.append(proxy)

        if len(out) >= SOURCE_LIMIT:
            break

    return out


def fetch_candidates() -> list[str]:
    merged = set()

    for name, url in SOURCES:
        try:
            response = request(url)

            if "geonode.com" in url:
                values = parse_geonode(response.json())
            else:
                values = parse_text(response.text)

            merged.update(values[:SOURCE_LIMIT])
            print(f"{name}: accepted {len(values[:SOURCE_LIMIT])} candidates")

        except Exception as exc:
            print(f"WARNING: {name} skipped: {exc}", file=sys.stderr)

    return sorted(merged)[:POOL_LIMIT]


def main() -> int:
    if OUTPUT.resolve().name == "sites.json":
        print("REFUSING to edit signed sites.json", file=sys.stderr)
        return 2

    candidates = fetch_candidates()

    if len(candidates) < MIN_POOL:
        print(
            f"Only {len(candidates)} candidates (< {MIN_POOL}); "
            "keeping previous pool"
        )
        return 0

    try:
        prior = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior = {}

    if (
        prior.get("schema") == 1
        and prior.get("proxyServers") == candidates
    ):
        print("Proxy candidate pool unchanged.")
        return 0

    payload = {
        "schema": 1,
        "updatedAt": int(time.time() * 1000),
        "proxyServers": candidates,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(
        f"Published {len(candidates)} candidates to {OUTPUT}; "
        "sites.json untouched"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
