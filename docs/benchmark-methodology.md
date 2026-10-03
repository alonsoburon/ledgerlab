# Benchmark methodology

Each managed evaluation builds one engine, starts it against a disposable SQLite WAL database on a free loopback port, verifies protocol conformance, runs a load profile, checks ledger invariants, and tears the engine down. Build time, server startup, runtime versions, and machine details are recorded with every result.

## Workload profiles

**`session` — realistic virtual users (default).** Each virtual user models a banking client session and repeats it for the duration:

1. read the account balance (`GET /accounts/{id}`),
2. think for 3–7 seconds,
3. read a statement page (`GET /ledger?account_id=...&limit=20`),
4. think for 3–8 seconds,
5. with 25% probability, post a transfer (amount 1–2000 minor units) and, with 5% probability, immediately replay the same idempotency key to exercise idempotent retry,
6. think for 5–15 seconds, then loop.

Users own a shared account pool (`max(8, users)`, capped at 128) so writes contend realistically instead of hammering one pair. A virtual user offers roughly 0.1 requests/second, so throughput is *offered* load, not a server ceiling. This profile answers "how many concurrent customers are served within the SLO".

**`raw` — write-path ceiling.** A saturated transfer loop with no think time and no read mix. It measures the maximum persisted transfers per second and the queueing behaviour of the write path. It is never reported as user capacity.

**In-memory transfer core.** A separate request runs inside the engine: workers own independent account pairs and repeatedly apply the funds check and debit/credit without HTTP or SQLite. Results are reported in operations/second and must not be compared with API req/s. The COBOL authorization routine is guarded by a process mutex because concurrent calls into the current GCC COBOL runtime were not safe in this harness, so its core result includes serialized calls.

## Service-level objectives

A run passes only if **all** thresholds hold (defaults follow common API budgets):

| Metric | Default |
|---|---:|
| p95 latency | < 500 ms |
| p99 latency | < 1000 ms |
| error rate | < 1% |

A **capacity run** (`POST /evaluations/{engine}/capacity`) warms the engine, starts at a concurrency level, doubles it until an SLO fails, then binary-searches the interval between the last passing and first failing level. It reports the highest level that still meets every SLO (the capacity) and the first level that breaches one. A fixed **sweep** (`/sweep`) accepts explicit levels instead.

Because a short pass is not enough, the capacity run then **confirms** the reported level with a longer **soak** window (`confirm_seconds`, default `max(30, 5 × level seconds)`); if the sustained run breaches an SLO it steps down to the next lower passing level and re-confirms. This mirrors the finding that a level can pass a two-minute test and fail a five-minute one. Warm-up runs an unmeasured pass before the search so thread pools and caches reach steady state. For a publishable number, still repeat the whole run and prefer an isolated load-generator host.

## Recorded data and invariants

Every result preserves: implementation and compiler/runtime version, build and startup times, protocol version, host OS/architecture, profile, account pool, operation mix, worker count, warm-up and measured duration, successful/failed counts, error rate, throughput, p50/p95/p99 overall and per operation, SLO outcome, ledger-invariant outcomes, and **resource attribution** — CPU seconds and peak RSS for the engine and for the load generator, so a report shows which side saturated first rather than guessing.

Account balances are cached on the account row and updated in the same transaction as the immutable journal entries. After the load stops, the harness reconciles every benchmark account's cached balance against its journal sum and verifies that every transfer has exactly two balanced entries. A run with a failed invariant is never treated as a valid performance result.

## Fair comparison rules

Compare only conforming runs with the same profile, dataset, account pool, operation mix, persistence guarantees, resource limits, and hardware. Run one engine at a time on an otherwise idle host; warm up and repeat at each level, then report distributions alongside latency percentiles and errors. Keep the in-memory core and the API results separate. C and COBOL use a synchronous libevent callback while Python and Rust process requests on worker threads, so the raw sweep includes server concurrency architecture as well as language/runtime; COBOL additionally includes the shared C HTTP/SQLite adapter.

## Limitations

The load generator currently runs on the same host as the engine, so at high raw concurrency the client competes for CPU and can become the bottleneck; publishable numbers require a separate generator host with low, documented latency. SQLite is the reference store for all engines, so the raw write path is serialized by the database regardless of language. Results are implementation-stack measurements, not a universal language ranking.
