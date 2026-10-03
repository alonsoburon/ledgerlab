# Benchmark methodology

Each managed evaluation builds one engine, starts it against a throwaway SQLite WAL database on a free loopback port, checks protocol conformance, runs a load profile, checks the ledger invariants, and tears the engine down. Build time, startup time, runtime versions and machine details are recorded with every result.

## Workload profiles

`session` is the default and models a person using the app. Each virtual user repeats this loop for the whole run:

1. read the account balance (`GET /accounts/{id}`),
2. wait 3 to 7 seconds,
3. read a statement page (`GET /ledger?account_id=...&limit=20`),
4. wait 3 to 8 seconds,
5. with 25% probability, post a transfer (1 to 2000 minor units) and, with 5% probability, immediately replay the same idempotency key,
6. wait 5 to 15 seconds, then loop.

Users draw from a shared account pool (`max(8, users)`, capped at 128) so writes actually contend instead of hammering a single pair. A virtual user offers about 0.1 requests/second, so the throughput here is offered load, not a server ceiling. This profile answers "how many concurrent customers fit inside the SLO".

`raw` is a saturated transfer loop with no think time and no reads. It measures the maximum persisted transfers per second and how the write path queues. It is never reported as a user count.

The in-memory core runs inside the engine as a separate request: each worker owns an independent account pair and applies the funds check and debit/credit over and over with no HTTP and no SQLite. It is reported in operations/second and isn't comparable to API req/s. The COBOL authorization routine is wrapped in a process mutex because concurrent calls into the current GCC COBOL runtime weren't safe, so its core result includes serialized calls.

## Service-level objectives

A run passes only if every threshold holds:

| Metric | Default |
|---|---:|
| p95 latency | < 500 ms |
| p99 latency | < 1000 ms |
| error rate | < 1% |

A capacity run (`POST /evaluations/{engine}/capacity`) starts at a concurrency level, doubles it until an SLO fails, then binary-searches between the last passing and first failing level. It reports the highest level that met every SLO and the first level that broke one. A fixed sweep (`/sweep`) takes explicit levels instead.

A short pass isn't enough to call something capacity, so the capacity run then confirms its level with a longer soak (`confirm_seconds`, default `max(30, 5 × level seconds)`). If the sustained run breaks an SLO it steps down to the next lower passing level and confirms again. This is there because a level can pass two minutes and fail five. The warm-up is an unmeasured pass before the search, so thread pools and caches reach steady state. Even so, repeat the run and use a separate load-generator host for anything you'd publish.

## Recorded data and invariants

Every result preserves implementation and runtime version, build and startup time, protocol version, host OS and architecture, profile, account pool, operation mix, worker count, warm-up and measured duration, success and failure counts, error rate, throughput, p50/p95/p99 overall and per operation, the SLO outcome, the invariant outcomes, and CPU seconds plus peak RSS for both the engine and the load generator so you can tell which side saturated.

Balances are cached on the account row and updated in the same transaction as the immutable journal entries. After the load stops, every benchmark account's cached balance is reconciled against its journal sum and every transfer is checked for exactly two balanced entries. A run that fails an invariant is never treated as a valid performance result.

## Comparing fairly

Compare only conforming runs with the same profile, dataset, account pool, operation mix, persistence guarantees, resource limits and hardware. Run one engine at a time on an idle host, warm up and repeat at each level, and report distributions alongside latency percentiles and errors. Keep the in-memory core and the API results separate. C and COBOL use a synchronous libevent callback while Python and Rust use worker threads, so the raw sweep also measures the server's concurrency architecture, not just the language; COBOL additionally includes the shared C HTTP/SQLite adapter.

## Limitations

The load generator runs on the same host as the engine, so at high raw concurrency the client competes for CPU and can become the bottleneck; publishable numbers need a separate generator host with low, documented latency. SQLite is the reference store for all engines, so the raw write path is serialized by the database regardless of language. Results are whole-stack measurements, not a universal ranking of languages.