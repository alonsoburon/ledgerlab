"""Deterministic, engine-neutral load generator for LedgerLab.

Two workload profiles are supported:

``session``
    A closed-loop model of a banking client. Every virtual user repeatedly
    reads its balance, reads a statement page, and occasionally initiates a
    transfer (with a realistic idempotent retry). Think time between steps is
    drawn from ranges, so the offered load per user is low and human-like
    instead of a tight request loop. This is the profile used to find how many
    concurrent customers a stack serves within an SLO.

``raw``
    A saturated transfer path used only to measure the ceiling of the write
    endpoint. It has no think time and no read mix, so it is reported
    separately and never presented as real user capacity.

Virtual users are asyncio coroutines, not OS threads, so tens of thousands of
concurrent clients can be offered from one process; a user is only connected
while its request is in flight, so idle think time costs almost nothing. The
runner records per-operation latency, throughput, error rate and ledger
invariants, then evaluates explicit service-level objectives
(``--slo-p95-ms``, ``--slo-p99-ms``, ``--slo-error-rate``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

DEFAULT_SLO = {"p95_ms": 500.0, "p99_ms": 1000.0, "error_rate": 0.01}
SESSION_THINK = {"balance_read": (3.0, 7.0), "statement_read": (3.0, 8.0), "transfer": (5.0, 15.0)}
TRANSFER_PROBABILITY = 0.25
IDEMPOTENT_RETRY_PROBABILITY = 0.05
MAX_TRANSFER_MINOR = 2_000


def request(url, method="GET", payload=None, headers=None, timeout=30):
    """Synchronous JSON request, used for setup and verification steps."""
    data = json.dumps(payload).encode() if payload is not None else None
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except (ValueError, TypeError):
            body = {}
        return exc.code, body
    except Exception as exc:  # transport failure: report as status 0
        return 0, {"error": {"code": "transport_error", "message": str(exc)}}


async def async_request(host, port, method, path, payload=None):
    """Minimal HTTP/1.1 client with one connection per request.

    A connection per request keeps idle virtual users from holding server
    connections (and, for thread-per-connection servers, threads). Responses
    are drained by Content-Length, falling back to read-to-EOF.
    """
    writer = None
    try:
        reader, writer = await asyncio.open_connection(host, port)
        body = json.dumps(payload).encode() if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}", "Accept: application/json", "Connection: close"]
        if payload is not None:
            lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(body)}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await writer.drain()
        status_line = await reader.readline()
        parts = status_line.split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        length = None
        while True:
            header = await reader.readline()
            if header in (b"\r\n", b"\n", b""):
                break
            if header.lower().startswith(b"content-length:"):
                try:
                    length = int(header.split(b":", 1)[1].strip())
                except ValueError:
                    length = None
        if length is not None:
            await reader.readexactly(length)
        else:
            await reader.read()
        return status, {}
    except Exception as exc:
        return 0, {"error": {"code": "transport_error", "message": str(exc)}}
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


class Recorder:
    """Thread-safe accumulator for per-operation latency samples and errors."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latencies = {}
        self.successes = 0
        self.errors = 0
        self.error_samples = []

    def record(self, operation, latency_ms, ok, detail=None):
        with self._lock:
            self._latencies.setdefault(operation, []).append(latency_ms)
            if ok:
                self.successes += 1
            else:
                self.errors += 1
                if len(self.error_samples) < 8:
                    self.error_samples.append({"operation": operation, "detail": detail})

    def summary(self):
        attempts = self.successes + self.errors
        with self._lock:
            combined = [value for values in self._latencies.values() for value in values]
            by_operation = {
                operation: {
                    "samples": len(values),
                    "p50": percentile(values, 0.50),
                    "p95": percentile(values, 0.95),
                    "p99": percentile(values, 0.99),
                    "max": round(max(values), 3) if values else None,
                }
                for operation, values in sorted(self._latencies.items())
            }
        return {
            "successes": self.successes,
            "errors": self.errors,
            "attempts": attempts,
            "error_rate": round(self.errors / attempts, 6) if attempts else 0.0,
            "latency_ms": {"p50": percentile(combined, 0.50), "p95": percentile(combined, 0.95), "p99": percentile(combined, 0.99)},
            "latency_by_operation": by_operation,
            "error_samples": self.error_samples,
        }


async def _sleep_until(deadline, seconds):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    await asyncio.sleep(min(seconds, remaining))
    return time.monotonic() < deadline


async def _issue(recorder, operation, host, port, method, path, payload=None):
    started = time.perf_counter()
    status, body = await async_request(host, port, method, path, payload)
    recorder.record(operation, (time.perf_counter() - started) * 1000, 200 <= status < 300, {"status": status, "body": body})
    return status, body


async def _session_user(host, port, index, accounts, users, duration, ramp, recorder, seed):
    rng = random.Random(f"{seed}-{index}")
    home = accounts[index % len(accounts)]
    sequence = 0
    await asyncio.sleep((index / users) * ramp)  # spread arrivals instead of a thundering herd
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        await _issue(recorder, "balance_read", host, port, "GET", f"/accounts/{home}")
        if not await _sleep_until(deadline, rng.uniform(*SESSION_THINK["balance_read"])):
            break
        await _issue(recorder, "statement_read", host, port, "GET", f"/ledger?account_id={home}&limit=20")
        if not await _sleep_until(deadline, rng.uniform(*SESSION_THINK["statement_read"])):
            break
        if rng.random() < TRANSFER_PROBABILITY:
            target = home
            while target == home:
                target = rng.choice(accounts)
            payload = {"idempotency_key": f"bench-{seed}-{index}-{sequence}", "from_account": home, "to_account": target, "amount_minor": rng.randint(1, MAX_TRANSFER_MINOR), "currency": "USD"}
            status, _ = await _issue(recorder, "transfer", host, port, "POST", "/transfers", payload)
            if status == 201 and rng.random() < IDEMPOTENT_RETRY_PROBABILITY:
                await _issue(recorder, "idempotent_replay", host, port, "POST", "/transfers", payload)
            sequence += 1
        if not await _sleep_until(deadline, rng.uniform(*SESSION_THINK["transfer"])):
            break


async def _raw_user(host, port, index, accounts, users, duration, ramp, recorder, seed):
    source, target = accounts[index % 2], accounts[(index + 1) % 2]
    await asyncio.sleep((index / users) * ramp)
    deadline = time.monotonic() + duration
    operation = 0
    failures = 0
    while time.monotonic() < deadline:
        from_account, to_account = (source, target) if operation % 2 == 0 else (target, source)
        payload = {"idempotency_key": f"raw-{seed}-{index}-{operation}", "from_account": from_account, "to_account": to_account, "amount_minor": 1, "currency": "USD"}
        status, _ = await _issue(recorder, "transfer", host, port, "POST", "/transfers", payload)
        failures = failures + 1 if not 200 <= status < 300 else 0
        if failures >= 5:
            break
        operation += 1


def run_profile(profile, base_url, accounts, users, seconds, recorder, seed):
    parsed = urlparse(base_url)
    host, port = parsed.hostname, parsed.port or 80
    ramp = min(3.0, seconds * 0.4)
    worker = _session_user if profile == "session" else _raw_user

    async def drive():
        await asyncio.gather(*(worker(host, port, index, accounts, users, seconds, ramp, recorder, seed) for index in range(users)))

    started = time.perf_counter()
    asyncio.run(drive())
    return time.perf_counter() - started


def create_and_fund_accounts(base, tag, count, seed_balance):
    accounts = [f"bench-{tag}-{index:03d}" for index in range(count)]
    for account in accounts:
        status, body = request(base + "/accounts", "POST", {"id": account, "currency": "USD"})
        if status != 201:
            raise SystemExit(f"Could not create benchmark account {account}: HTTP {status} {body}")
    funder = "ledgerlab-funder"
    for account in accounts:
        status, body = request(base + "/transfers", "POST", {"idempotency_key": f"bench-fund-{tag}-{account}", "from_account": funder, "to_account": account, "amount_minor": seed_balance, "currency": "USD"})
        if status != 201:
            raise SystemExit(f"Funding requires a funded {funder!r} account (HTTP {status}: {body})")
    return accounts


def evaluate_slo(summary, thresholds):
    observed = {"p95_ms": summary["latency_ms"]["p95"], "p99_ms": summary["latency_ms"]["p99"], "error_rate": summary["error_rate"]}
    failures = []
    if observed["p95_ms"] is not None and observed["p95_ms"] > thresholds["p95_ms"]:
        failures.append("p95_latency")
    if observed["p99_ms"] is not None and observed["p99_ms"] > thresholds["p99_ms"]:
        failures.append("p99_latency")
    if observed["error_rate"] > thresholds["error_rate"]:
        failures.append("error_rate")
    return {"thresholds": thresholds, "observed": observed, "passed": not failures, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description="Realistic-load benchmark for a LedgerLab engine")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--profile", choices=("session", "raw"), default="session", help="session = virtual banking users, raw = saturated write path")
    parser.add_argument("--users", type=int, default=10, help="Concurrent virtual users")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--warmup-seconds", type=float, default=0.0, help="Unmeasured warm-up pass before the measured window")
    parser.add_argument("--account-pool", type=int, default=0, help="Shared account pool size (0 = auto)")
    parser.add_argument("--seed-balance", type=int, default=10**8, help="Minor units placed in each benchmark account (small enough that a long sweep cannot drain the funder)")
    parser.add_argument("--slo-p95-ms", type=float, default=DEFAULT_SLO["p95_ms"])
    parser.add_argument("--slo-p99-ms", type=float, default=DEFAULT_SLO["p99_ms"])
    parser.add_argument("--slo-error-rate", type=float, default=DEFAULT_SLO["error_rate"])
    parser.add_argument("--implementation", default="python", help="Implementation label stored in the report")
    parser.add_argument("--json", dest="json_path", help="Write complete result JSON to this path")
    args = parser.parse_args()
    if args.users < 1 or args.seconds <= 0 or args.warmup_seconds < 0:
        parser.error("users must be positive, seconds must be greater than zero, and warmup must not be negative")
    if args.account_pool and args.account_pool < 2:
        parser.error("account pool must be at least 2")

    base = args.base_url.rstrip("/")
    tag = uuid.uuid4().hex[:12]
    thresholds = {"p95_ms": args.slo_p95_ms, "p99_ms": args.slo_p99_ms, "error_rate": args.slo_error_rate}
    pool = args.account_pool or (2 if args.profile == "raw" else min(128, max(8, args.users)))
    accounts = create_and_fund_accounts(base, tag, pool, args.seed_balance)
    before = {account: request(base + "/accounts/" + account)[1].get("balance_minor") for account in accounts}

    recorder = Recorder()
    if args.warmup_seconds > 0:
        # Unmeasured warm-up pass so thread pools, caches and JIT reach a steady
        # state before anything is recorded (as in a soak-style capacity test).
        run_profile(args.profile, base, accounts, args.users, args.warmup_seconds, Recorder(), f"{tag}-warm")
    wall_seconds = run_profile(args.profile, base, accounts, args.users, args.seconds, recorder, tag)
    # Throughput is measured over the intended window, not over wall time that
    # includes coroutine setup.
    window = float(args.seconds)

    ledger_status, ledger = request(base + "/metrics")
    after = {account: request(base + "/accounts/" + account)[1].get("balance_minor") for account in accounts}
    summary = recorder.summary()
    conserved = all(before[account] is not None and after[account] is not None for account in accounts) and sum(after.values()) == sum(before.values())
    slo = evaluate_slo(summary, thresholds)
    operations = summary["latency_by_operation"]
    writes = operations.get("transfer", {}).get("samples", 0)

    result = {
        "implementation": args.implementation,
        "protocol_version": 1,
        "profile": args.profile,
        "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "python": platform.python_version()},
        "workload": {
            "profile": args.profile,
            "users": args.users,
            "requested_seconds": args.seconds,
            "warmup_seconds": round(args.warmup_seconds, 3),
            "actual_seconds": round(window, 3),
            "wall_seconds": round(wall_seconds, 3),
            "account_pool": len(accounts),
            "operation_mix": "balance_read + statement_read + transfer" if args.profile == "session" else "transfer only (saturated)",
            "think_time_seconds": SESSION_THINK if args.profile == "session" else None,
        },
        "results": {
            "successes": summary["successes"],
            "errors": summary["errors"],
            "attempts": summary["attempts"],
            "error_rate": summary["error_rate"],
            "throughput_rps": round(summary["successes"] / window, 2) if window else 0.0,
            "writes_per_second": round(writes / window, 2) if window else 0.0,
            "latency_ms": summary["latency_ms"],
            "latency_by_operation": summary["latency_by_operation"],
        },
        "slo": slo,
        "ledger_check": {"http_status": ledger_status, **ledger},
        "invariant_check": {
            "benchmark_accounts_conserved": conserved,
            "all_transfers_have_two_balanced_entries": ledger_status == 200 and ledger.get("invalid_transfer_groups") == 0,
            "account_balances_match_journal": ledger_status == 200 and ledger.get("balance_mismatch_accounts") == 0,
            "account_pool": len(accounts),
        },
        "error_samples": summary["error_samples"],
        "note": "Session profile models think-time virtual users and is the capacity figure; raw profile only measures the write ceiling. Results are implementation-stack measurements, not a universal language ranking.",
    }
    encoded = json.dumps(result, indent=2)
    print(encoded)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")


if __name__ == "__main__":
    main()
