# Architecture

A small dual-entry ledger, implemented in Rust, C, COBOL and Python, so the implementations can be compared under the same workload. The manager owns the dashboard, builds and starts engines, generates load, and stores results. Each engine gets the same HTTP requests and a fresh SQLite WAL database.

## Layout

```text
Browser UI / CLI load generator
              |
      LedgerLab HTTP gateway
       /       |       |      \
   Python    Rust      C     COBOL
   adapter   adapter adapter adapter
       \       |       |      /
        implementation-local ledger store
```

The manager serves the dashboard and the evaluation API. Picking an engine builds it if needed, starts it on a free loopback port with a throwaway database, waits for `/health`, checks protocol conformance, runs a load profile (`session` virtual users or `raw` write saturation), checks the ledger invariants, then stops the process and deletes the database. It can also run a concurrency sweep and report the highest level that still meets the SLOs. Build time and startup are reported separately.

Python is a threaded HTTP server. Rust uses `tiny_http` with one worker thread per request. C uses libevent with a synchronous event loop. The COBOL funds-authorization core is called from the shared C libevent/SQLite adapter. What gets compared is the whole stack and its concurrency strategy, not the language in isolation, and the COBOL numbers include C's transport and storage.

## Ledger model

- An account has a stable ID, a currency and a status.
- A transfer has a stable ID, an idempotency key, source, destination, positive amount, currency and posting timestamp.
- Each posted transfer writes exactly two immutable entries: one debit and one credit of equal amount sharing a transaction ID.
- Journal entries are authoritative. Each account also keeps a cached balance, updated in the same transaction as the two entries; the evaluation reconciles cached balances against journal sums after each run. A transfer can't push the source balance negative.
- Account creation and transfer commit are atomic. An idempotency key produces at most one posting; a replay returns the original result.
- Demo seed data is explicit and repeatable. Benchmarks use isolated accounts so runs are comparable.

Amounts are signed 64-bit integer minor units and currency is an ISO-style code. This is deliberately small: it exercises accounting mechanics, not a full banking product.

## Implementations

- Python: standard-library HTTP server and SQLite.
- Rust: `tiny_http`, `rusqlite`, `serde_json`, and UUID transfer IDs.
- C: libevent HTTP server, JSON-C and SQLite.
- COBOL: GCC `gcobol` does funds authorization and produces the signed debit/credit amounts; the C adapter handles HTTP and SQLite.

Every result records the engine and compiler version, build and startup time, profile, account pool, throughput, per-operation latency percentiles, error rate, SLO outcome, CPU/RSS for engine and generator, the ledger checks, and balance conservation. Before any performance number is accepted, the managed flow runs a conformance suite covering health identity, account/ledger reads, idempotent replay, idempotency conflict, insufficient funds and currency mismatch.

## Comparing fairly

Keep the request mix, dataset, account count, resource limits, persistence semantics, durability, warm-up, duration and machine constant, and report them with every run. Run one engine at a time on an idle host, repeat at least three times, and show distributions. Separate read-heavy, write-heavy and mixed workloads. Report latency percentiles and errors alongside throughput.

The in-memory core metric excludes HTTP and SQLite and uses independent account pairs, so it measures arithmetic and worker scaling, not API capacity. What you're comparing is a full stack: server threading, HTTP library, SQLite bindings, compiler and the colocated load generator all affect the numbers.

## Out of scope

Authentication, real customer data, external payment rails, regulatory claims, tax and accounting integrations, and production deployment. This is an educational benchmark over synthetic data.