#!/usr/bin/env python3
"""Drive a running LedgerLab manager through a reproducible set of evaluations.

Start the manager first (``python3 -m ledgerlab``), then run::

    python3 scripts/run_benchmarks.py --base-url http://127.0.0.1:8090

The script asks the manager for every available engine, runs a realistic
session benchmark plus an adaptive capacity run for the session and raw
profiles, and writes the collected reports to ``--output``. It is safe to
re-run; each evaluation builds and tears down its own isolated engine and
database.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_SLO = {"p95_ms": 500.0, "p99_ms": 1000.0, "error_rate": 0.01}


def api(base, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def run_job(base, engine, kind, body, label):
    suffix = {"sweep": "/sweep", "capacity": "/capacity"}.get(kind, "")
    path = f"/evaluations/{engine}{suffix}"
    status, accepted = api(base, path, "POST", body)
    if status != 202:
        raise SystemExit(f"{engine} {label}: manager rejected the job (HTTP {status}: {accepted})")
    job_id = accepted["job_id"]
    last = ""
    while True:
        status, job = api(base, f"/evaluations/jobs/{job_id}")
        line = f"{engine:6s} {label:22s} {job.get('phase', '?'):32s} {job.get('progress', 0):3d}%"
        if line != last:
            print(line, flush=True)
            last = line
        if job.get("status") == "completed":
            return job["result"]
        if job.get("status") == "failed":
            raise SystemExit(f"{engine} {label} failed: {job.get('error')}")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--output", default="docs/sample-results.json")
    parser.add_argument("--engines", help="Comma-separated engine ids (default: all available)")
    parser.add_argument("--session-users", type=int, default=32)
    parser.add_argument("--session-seconds", type=int, default=10)
    parser.add_argument("--raw-max", type=int, default=1024, help="Max concurrent writers for the raw capacity run")
    parser.add_argument("--session-max", type=int, default=2048, help="Max concurrent sessions for the session capacity run")
    parser.add_argument("--confirm-seconds", type=int, default=30, help="Confirmation/soak window used to verify the reported capacity")
    parser.add_argument("--quick", action="store_true", help="Shorter durations for a fast smoke run")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    status, catalog = api(base, "/evaluations/engines")
    if status != 200:
        raise SystemExit(f"Manager not reachable at {base}")
    available = [engine["id"] for engine in catalog["engines"] if engine["available"]]
    engines = [item.strip() for item in args.engines.split(",")] if args.engines else available
    unknown = [item for item in engines if item not in available]
    if unknown:
        raise SystemExit(f"Unavailable engines: {', '.join(unknown)}")

    session_seconds = 4 if args.quick else args.session_seconds
    level_seconds = 3 if args.quick else 5
    raw_seconds = 3 if args.quick else 4
    results = []
    for engine in engines:
        print(f"=== {engine} ===", flush=True)
        results.append(run_job(base, engine, "run", {"profile": "session", "users": args.session_users, "seconds": session_seconds, "slo": DEFAULT_SLO}, "session run"))
        results.append(run_job(base, engine, "capacity", {"profile": "raw", "seconds": raw_seconds, "start": 64, "max_users": args.raw_max, "confirm_seconds": args.confirm_seconds, "slo": DEFAULT_SLO}, "raw capacity"))
        results.append(run_job(base, engine, "capacity", {"profile": "session", "seconds": level_seconds, "start": 64, "max_users": args.session_max, "confirm_seconds": args.confirm_seconds, "slo": DEFAULT_SLO}, "session capacity"))

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print(f"\nWrote {len(results)} reports to {args.output}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
