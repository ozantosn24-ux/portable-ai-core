#!/usr/bin/env python3
"""Closed-loop HTTP load client for the compose drill.

Runs INSIDE a throwaway container on the drill network, so the host needs no Python,
no httpx, and no TLS trust store changes. The only dependency is httpx (pinned by
scripts/load.sh when it builds the throwaway image).

Design notes, because they change what the numbers mean:

* CLOSED LOOP. `--concurrency N` means N workers, each sending the next request only
  after the previous one returned. This measures what the stack does when N clients
  wait on it; it is NOT an open-loop arrival-rate generator, so `rps` here is a RESULT,
  not an input. A saturated service therefore shows up as falling rps and rising
  latency, never as a growing backlog inside this client.
* Latency percentiles are computed over requests that produced an HTTP RESPONSE, at any
  status. Requests that raised (timeout, connection reset, TLS failure) are counted
  separately in `errors_by_exception` and their durations are reported separately in
  `exception_latency_ms` - mixing a 10 s timeout into a p99 of successful responses
  would quietly turn a failure into a "slow request".
* Connections are keep-alive and capped at `--concurrency`, so TLS handshakes happen
  during warm-up, not during the measured window. That is a deliberate choice: this
  measures request handling, not handshake cost. See "not measured" in LOAD-*.md.
* Warm-up requests (default 1) are sent and DISCARDED before the clock starts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import httpx


def percentile(values_sorted: list[float], q: float) -> float | None:
    """Nearest-rank percentile. No interpolation, no numpy."""
    if not values_sorted:
        return None
    k = math.ceil(q / 100.0 * len(values_sorted)) - 1
    k = min(max(k, 0), len(values_sorted) - 1)
    return values_sorted[k]


async def worker(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    deadline: float,
    lat_ms: list[float],
    exc_lat_ms: list[float],
    statuses: Counter,
    excs: Counter,
    budget: list[int],
) -> None:
    method = args.method.upper()
    body = args.body.encode() if args.body else None
    while True:
        if args.requests and budget[0] <= 0:
            return
        if time.monotonic() >= deadline:
            return
        if args.requests:
            budget[0] -= 1
        t0 = time.perf_counter()
        try:
            r = await client.request(method, args.url, content=body)
            dt = (time.perf_counter() - t0) * 1000.0
            lat_ms.append(dt)
            statuses[str(r.status_code)] += 1
        except Exception as exc:  # noqa: BLE001 - every failure class is data here
            dt = (time.perf_counter() - t0) * 1000.0
            exc_lat_ms.append(dt)
            name = type(exc).__name__
            excs[name] += 1


async def run(args: argparse.Namespace) -> dict:
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    timeout = httpx.Timeout(args.timeout)
    verify = args.cacert if args.cacert else True

    lat_ms: list[float] = []
    exc_lat_ms: list[float] = []
    statuses: Counter = Counter()
    excs: Counter = Counter()

    async with httpx.AsyncClient(
        verify=verify, limits=limits, timeout=timeout, follow_redirects=False
    ) as client:
        # Warm-up: pay the TLS handshake and any first-request cost outside the window.
        warm_ok, warm_err = 0, ""
        for _ in range(args.warmup):
            try:
                r = await client.request(args.method.upper(), args.url)
                warm_ok = r.status_code
            except Exception as exc:  # noqa: BLE001
                warm_err = f"{type(exc).__name__}: {exc}"

        started_wall = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t_start = time.monotonic()
        deadline = t_start + args.duration if args.duration else float("inf")
        budget = [args.requests or 0]

        tasks = [
            asyncio.create_task(
                worker(client, args, deadline, lat_ms, exc_lat_ms, statuses, excs, budget)
            )
            for _ in range(args.concurrency)
        ]
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - t_start

    lat_sorted = sorted(lat_ms)
    responses = len(lat_ms)
    exceptions = len(exc_lat_ms)
    total = responses + exceptions
    non_2xx = sum(c for s, c in statuses.items() if not s.startswith("2"))

    return {
        "label": args.label,
        "url": args.url,
        "method": args.method.upper(),
        "concurrency": args.concurrency,
        "warmup_requests": args.warmup,
        "warmup_last_status": warm_ok,
        "warmup_error": warm_err,
        "started_utc": started_wall,
        "duration_s": round(elapsed, 2),
        "requests": total,
        "responses": responses,
        "status_2xx": sum(c for s, c in statuses.items() if s.startswith("2")),
        "errors_non_2xx": non_2xx,
        "errors_exceptions": exceptions,
        "errors_total": non_2xx + exceptions,
        "errors_by_status": dict(sorted(statuses.items())),
        "errors_by_exception": dict(sorted(excs.items())),
        "rps": round(total / elapsed, 2) if elapsed > 0 else None,
        "latency_ms": {
            "p50": round(percentile(lat_sorted, 50), 2) if lat_sorted else None,
            "p95": round(percentile(lat_sorted, 95), 2) if lat_sorted else None,
            "p99": round(percentile(lat_sorted, 99), 2) if lat_sorted else None,
            "max": round(lat_sorted[-1], 2) if lat_sorted else None,
            "min": round(lat_sorted[0], 2) if lat_sorted else None,
            "mean": round(sum(lat_sorted) / len(lat_sorted), 2) if lat_sorted else None,
        },
        "exception_latency_ms": {
            "p50": round(percentile(sorted(exc_lat_ms), 50), 2) if exc_lat_ms else None,
            "max": round(max(exc_lat_ms), 2) if exc_lat_ms else None,
        },
        "client": {
            "httpx": httpx.__version__,
            "python": sys.version.split()[0],
            "http_version": "HTTP/1.1 (h2 not installed)",
            "timeout_s": args.timeout,
            "keepalive": True,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--method", default="GET")
    p.add_argument("--body", default="")
    p.add_argument("--concurrency", type=int, required=True)
    p.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = use --requests")
    p.add_argument("--requests", type=int, default=0, help="total request budget; 0 = use --duration")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--cacert", default=os.environ.get("DRILL_CA", ""))
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--label", default="unlabelled")
    args = p.parse_args()

    if not args.duration and not args.requests:
        p.error("one of --duration or --requests is required")

    result = asyncio.run(run(args))
    json.dump(result, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
