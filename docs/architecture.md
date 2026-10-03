# Architecture and implementation plan

## Goal

Provide a portable demonstration of a small dual-entry ledger and compare runnable implementations in Rust, C, COBOL, and Python. The manager owns the UI, build/lifecycle orchestration, load generator, and result history. Each isolated engine process receives the same HTTP requests and a fresh SQLite WAL database.

## Shape

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

The manager serves the UI and evaluation API. Selecting an engine builds it if needed, starts it on a free loopback port with a disposable database, waits for `/health`, verifies protocol conformance, runs a load profile (`session` realistic virtual users or `raw` write-path saturation), checks ledger invariants, terminates the process, and deletes the database. It can also run a concurrency sweep that reports the highest level still meeting the latency and error SLOs. Builds and server startup are reported separately.

Python is a threaded HTTP server. Rust uses `tiny_http` with one worker thread per accepted request. C uses libevent and a synchronous event loop. The COBOL arithmetic and funds-authorization core is called from the shared C libevent/SQLite adapter. These runs compare complete implementation stacks and concurrency strategies, not language syntax in isolation. The COBOL result especially includes C transport and storage work.

## Ledger model

- Account has stable ID, currency, and status.
- Transfer has stable ID, idempotency key, source, destination, positive amount, currency, and posting timestamp.
- Each posted transfer writes exactly two immutable entries: one debit and one credit with equal amounts and a shared transaction ID.
- Journal entries are authoritative. Each account also stores a cached balance, adjusted in the same database transaction as the two immutable entries; the evaluation reconciles cached balances against journal sums after each run. A transfer cannot make the source balance negative.
- Account creation and transfer commit are atomic. An idempotency key can produce at most one posting; replay returns the original result.
- Demo seed data is explicit and repeatable. Benchmark workloads create or reset isolated accounts so run history is comparable.

Amounts use signed 64-bit integer minor units; currency is an ISO-style code. This intentionally small model demonstrates accounting mechanics, not a complete banking product.

## Implementations and acceptance criteria

- Python: standard-library HTTP server and SQLite.
- Rust: `tiny_http`, `rusqlite`, `serde_json`, and UUID transfer IDs.
- C: libevent HTTP server, JSON-C, and SQLite.
- COBOL: GCC `gcobol` performs funds authorization and produces signed debit/credit amounts; the C adapter supplies HTTP and SQLite operations.
- Every result records engine/compiler version, build time, server startup time, profile, account pool, throughput, per-operation latency percentiles, error rate, SLO outcome, global ledger checks, and benchmark-account balance conservation. The managed flow runs an automated conformance suite for health identity, account/ledger reads, idempotent replay, idempotency conflict, insufficient funds, and currency mismatch before any performance result is accepted.

Follow-up: add configurable concurrency strategies and per-request resource limits; record host CPU/memory counters with each run; package native toolchains for other operating systems; support running the load generator on a separate host.

## Fair comparison rules

Keep request mix, dataset, account count, runtime limits, CPU/memory limits, persistence semantics, transaction durability, warm-up, duration, and machine constant. Report these with every run. Run engines one at a time on an otherwise idle host; repeat at least three times and show distributions. Separate read-heavy, write-heavy, and mixed workloads. Report latency percentiles and errors alongside throughput. The in-memory core metric excludes HTTP/SQLite and uses independent account pairs, so it is a diagnostic of arithmetic and worker scaling, not a substitute for API capacity. Results are implementation-stack measurements; server threading, HTTP libraries, SQLite bindings, compiler, and the colocated load generator all affect them.

## Out of scope for the first demo

Authentication, real customer information, external payment rails, regulatory claims, tax/accounting integrations, and production deployment. This is an educational benchmark with synthetic data.
