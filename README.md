# LedgerLab

The same banking ledger written four times (Python, Rust, C, COBOL) plus a load-testing tool that measures how many concurrent users each version can hold. The point is to answer one question: with a fixed amount of hardware and a transfer endpoint, where does it start to break?

Everything sits behind one HTTP contract. A transfer posts exactly one debit and one credit in a single transaction, amounts are integers (cents), the journal is immutable, and account balances are cached on the row and reconciled against the journal after every run.

![Saturation of the SQLite write path](docs/figures/raw_saturation.png)

## How it works

The evaluation manager builds the engine you pick, starts it on a free port with a throwaway SQLite database, checks it against the shared contract in `docs/protocol.md`, runs a workload, verifies the ledger invariants, and shuts it down. The COBOL engine is a COBOL authorization routine called from the same C HTTP/SQLite adapter, and results label it that way.

Two workloads:

- `session` is a person using the app. Read the balance, wait 3–7 seconds, read the statement, wait 3–8 seconds, maybe post a transfer (about a quarter of the turns do, with a realistic idempotent retry), wait 5–15 seconds, repeat. This is the number I care about: how many concurrent customers fit under the latency target.
- `raw` just hammers the transfer endpoint with no waiting, to see the write ceiling. It never gets reported as a user count.

A run passes only if p95 is under 500 ms, p99 under 1 s, and errors under 1%. To find capacity the manager warms the engine up, doubles the user count until something fails, binary-searches the last good point, then re-runs that level for much longer to make sure it actually holds. `docs/benchmark-methodology.md` has the details.

## Results

Everything below comes from `docs/sample-results.json`, regenerated with `make bench` and `make figures`. One machine, one workload, one shot per level. Treat it as a snapshot of this setup, not a ranking of programming languages.

Concurrent users under the SLO:

| Engine | Sessions | First breach | Writers | First breach |
|---|---:|---:|---:|---:|
| Python | 2,560 | 3,072 | 224 | 256 |
| Rust | 4,096 | 5,120 | 2,048 | no breach |
| C | 3,072 | 3,584 | 512 | 640 |
| COBOL hybrid | 2,048 | 3,584 | 512 | 640 |

![Concurrent capacity by engine](docs/figures/capacity_by_engine.png)

A short session run (32 users, 10 seconds) looks like this. Everything passes comfortably at that load; the in-memory core column measures the transfer arithmetic without HTTP or SQLite:

| Engine | req/s | p50 ms | p95 ms | p99 ms | core Mops/s |
|---|---:|---:|---:|---:|---:|
| Python | 8.0 | 2.68 | 3.28 | 3.56 | 0.38 |
| Rust | 8.1 | 2.06 | 2.36 | 2.84 | 3.70 |
| C | 8.1 | 2.19 | 2.43 | 2.54 | 3.98 |
| COBOL hybrid | 8.0 | 2.75 | 2.94 | 3.15 | 0.72 |

The raw write path saturates and then queueing takes over:

| Engine | Peak transfers/s | Capacity under SLO | First breach |
|---|---:|---:|---:|
| Python | 776 @ 192 | 224 | 256 |
| Rust | 7,476 @ 256 | 2,048 | no breach |
| C | 3,658 @ 1,024 | 512 | 640 |
| COBOL hybrid | 3,741 @ 64 | 512 | 640 |

![Latency by percentile](docs/figures/latency_by_engine.png)
![Peak throughput](docs/figures/throughput_by_engine.png)
![Saturation curves](docs/figures/raw_saturation.png)
![p95 by concurrency](docs/figures/capacity_heatmap.png)
![In-memory transfer core](docs/figures/inmemory_core.png)

## What I found

Rust used to show a weird profile: fast median, exploding p99, because it opened a new SQLite connection on every request and let writers fight over the lock. It now reuses one connection per worker in a fixed thread pool, so the tail is uniform like everyone else's, and it leads on both sessions and the raw write path.

The bottleneck is the single SQLite writer. Once it saturates, queueing drives latency up; a few thousand users in, the load generator sharing the box starts to matter too.

C and COBOL come out identical because the COBOL core runs through C's HTTP/SQLite adapter. The one place they differ is the in-memory core: COBOL's authorization routine needs a process mutex (the GCC runtime hangs under concurrent calls — I tested removing it and the server lost the connection), so its core number reads lower than C's.

Numbers move around a lot. Consecutive runs on this desktop have differed by roughly 2x in session capacity, so treat these tables as one sample of one machine, not a benchmark score.

## Running it

For the Python demo you only need Python 3.11+, no third-party packages. The other engines additionally need Rust/Cargo, a C compiler with `pkg-config`, libevent, JSON-C and SQLite dev files, and GCC's `gcobol`. They get built automatically the first time you evaluate them.

```sh
make demo PORT=8080     # or python3 -m ledgerlab
# open the printed URL, pick an engine, press "Find capacity"
```

From another terminal:

```sh
make bench    # run every engine, write docs/sample-results.json
make figures  # regenerate the charts, needs uv
```

Or call the load generator directly:

```sh
python3 -m ledgerlab.bench --base-url http://127.0.0.1:8080 \
    --profile session --users 32 --seconds 10 \
    --slo-p95-ms 500 --slo-p99-ms 1000 --slo-error-rate 0.01
```

## What's where

- `ledgerlab/` the Python reference service, the evaluation manager, and the load generator (`bench.py`).
- `web/` the dashboard, served by the reference service.
- `scripts/` the benchmark driver and the chart renderer.
- `implementations/` the Rust, C and COBOL engines.
- `docs/` architecture, protocol, methodology, sample results, figures.

## Caveats

The load generator runs on the same machine as the engine, so at high concurrency part of what you're measuring is client contention and the CPU numbers are approximate. Each capacity figure is one run with a soak at the end, and it's tail-sensitive, so repeat it before quoting it. For anything you'd publish, run the generator on a separate host. Authentication, real customer data, payment rails and production deployment are out of scope. This is synthetic data over SQLite.

## License

MIT, see [LICENSE](LICENSE).