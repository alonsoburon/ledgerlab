#!/usr/bin/env python3
"""Render publication-quality charts from LedgerLab evaluation results.

Reads a JSON list of run/sweep reports (as written by
``scripts/run_benchmarks.py`` or served by ``GET /evaluations``) and writes
PNG figures suitable for embedding in the README.

Run it with the plotting dependencies without installing them globally::

    uv run --with matplotlib --with seaborn --with pandas \
        scripts/plot_results.py --input docs/sample-results.json --output docs/figures
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

DISPLAY = {"python": "Python", "rust": "Rust", "c": "C", "cobol": "COBOL hybrid"}
PALETTE = {"python": "#1f3a5f", "rust": "#b54708", "c": "#067647", "cobol": "#6941c6"}
FALLBACK = ["#1f3a5f", "#b54708", "#067647", "#6941c6", "#b42318"]

sns.set_theme(style="whitegrid", context="talk")
plt.rcParams.update({"figure.dpi": 150, "savefig.bbox": "tight", "axes.titlesize": 14, "axes.titleweight": "bold"})


def engine_name(engine):
    return DISPLAY.get(engine, engine.title())


def load(path):
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, list) else data.get("evaluations", [])


def latest_session_runs(reports):
    latest = {}
    for report in reports:  # reports are newest-first; keep the first per engine
        if report.get("kind") == "sweep" or report.get("profile") == "raw":
            continue
        if report.get("results"):
            latest.setdefault(report["engine"], report)
    return latest


def sweeps_by_profile(reports, profile):
    """Curves per engine for a profile, preferring adaptive capacity reports."""
    sweeps = {}
    for report in reports:
        if report.get("profile") != profile or report.get("kind") not in ("sweep", "capacity"):
            continue
        current = sweeps.get(report["engine"])
        if current is None or (current.get("kind") != "capacity" and report.get("kind") == "capacity"):
            sweeps[report["engine"]] = report
    return sweeps


def capacity_by_engine(reports):
    """Most recent adaptive capacity result per (engine, profile)."""
    latest = {}
    for report in reports:
        if report.get("kind") == "capacity":
            latest.setdefault((report["engine"], report["profile"]), report)
    return latest


def save(fig, output, name):
    target = Path(output) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target)
    plt.close(fig)
    print(f"wrote {target}")


def plot_peak_throughput(sweeps, output):
    if not sweeps:
        return
    engines, peaks, peak_users = [], [], []
    for engine, sweep in sweeps.items():
        best = max(sweep["curve"], key=lambda entry: entry["throughput_rps"])
        engines.append(engine)
        peaks.append(best["throughput_rps"])
        peak_users.append(best["users"])
    labels = [engine_name(engine) for engine in engines]
    fig, ax = plt.subplots(figsize=(9, 4.6))
    sns.barplot(x=labels, y=peaks, hue=labels, palette=[PALETTE.get(engine, "#1f3a5f") for engine in engines], legend=False, ax=ax)
    ax.set_title("Peak transfer throughput by engine (raw write path)")
    ax.set_xlabel("")
    ax.set_ylabel("transfers / s")
    for index, (value, users) in enumerate(zip(peaks, peak_users)):
        ax.text(index, value, f"{value:,.0f}\n@{users} writers", ha="center", va="bottom", fontsize=10)
    save(fig, output, "throughput_by_engine.png")


def plot_latency(runs, output):
    if not runs:
        return
    engines = list(runs)
    labels = [engine_name(engine) for engine in engines]
    frame = pd.DataFrame({
        "engine": labels,
        "p50": [runs[engine]["results"]["latency_ms"]["p50"] for engine in engines],
        "p95": [runs[engine]["results"]["latency_ms"]["p95"] for engine in engines],
        "p99": [runs[engine]["results"]["latency_ms"]["p99"] for engine in engines],
    })
    melted = frame.melt(id_vars="engine", var_name="percentile", value_name="ms")
    order = ["p50", "p95", "p99"]
    melted["percentile"] = pd.Categorical(melted["percentile"], order, ordered=True)
    fig, ax = plt.subplots(figsize=(9, 4.6))
    sns.barplot(data=melted, x="engine", y="ms", hue="percentile", palette=["#98a2b3", "#b54708", "#b42318"], ax=ax)
    ax.set_title("Session request latency by percentile")
    ax.set_xlabel("")
    ax.set_ylabel("milliseconds")
    ax.legend(title="")
    for container in ax.containers:
        ax.bar_label(container, fmt="%.2f", fontsize=9)
    save(fig, output, "latency_by_engine.png")


def plot_raw_saturation(sweeps, output, slo):
    if not sweeps:
        return
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for index, (engine, sweep) in enumerate(sweeps.items()):
        color = PALETTE.get(engine, FALLBACK[index % len(FALLBACK)])
        curve = sorted(sweep["curve"], key=lambda entry: entry["users"])
        users = [entry["users"] for entry in curve]
        rps = [entry["throughput_rps"] for entry in curve]
        p95 = [entry["latency_ms"]["p95"] for entry in curve]
        axes[0].plot(users, rps, marker="o", color=color, label=engine_name(engine))
        axes[1].plot(users, p95, marker="o", color=color, label=engine_name(engine))
        failures = [entry for entry in curve if not entry["slo"]["passed"]]
        axes[1].scatter([entry["users"] for entry in failures], [entry["latency_ms"]["p95"] for entry in failures], marker="x", s=90, color="#b42318", zorder=5)
    axes[0].set_title("Raw write path: throughput")
    axes[0].set_xlabel("concurrent writers")
    axes[0].set_ylabel("transfers / s")
    axes[0].set_xscale("log", base=2)
    axes[1].set_title("Raw write path: p95 latency")
    axes[1].set_xlabel("concurrent writers")
    axes[1].set_ylabel("milliseconds")
    axes[1].set_xscale("log", base=2)
    axes[1].axhline(slo["p95_ms"], color="#b42318", linestyle="--", linewidth=1.2, label=f"p95 SLO {slo['p95_ms']:.0f} ms")
    axes[1].legend()
    for ax in axes:
        ax.set_xticks(sorted({entry["users"] for sweep in sweeps.values() for entry in sweep["curve"]}))
        ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    fig.suptitle("Saturation of the SQLite write path (raw profile)", fontweight="bold")
    save(fig, output, "raw_saturation.png")


def plot_capacity_heatmap(sweeps, output, slo):
    if not sweeps:
        return
    by_engine = {engine: {entry["users"]: entry["latency_ms"]["p95"] for entry in sweep["curve"]} for engine, sweep in sweeps.items()}
    columns = sorted({level for values in by_engine.values() for level in values})
    index = [engine_name(engine) for engine in by_engine]
    rows = [[by_engine[engine].get(level) for level in columns] for engine in by_engine]
    frame = pd.DataFrame(rows, index=index, columns=[str(level) for level in columns])
    fig, ax = plt.subplots(figsize=(9, 1.2 + 0.7 * len(index)))
    sns.heatmap(frame, annot=True, fmt=".0f", cmap="Oranges", cbar_kws={"label": "p95 ms"}, linewidths=0.5, linecolor="white", ax=ax)
    ax.set_title(f"p95 latency by concurrency · raw profile (SLO {slo['p95_ms']:.0f} ms)")
    ax.set_xlabel("concurrent writers")
    ax.set_ylabel("")
    save(fig, output, "capacity_heatmap.png")


def plot_capacity_summary(reports, output, slo):
    capacity = capacity_by_engine(reports)
    if not capacity:
        return
    engines = []
    for engine, profile in capacity:
        if engine not in engines:
            engines.append(engine)
    rows = []
    annotations = {}
    for engine in engines:
        for profile in ("session", "raw"):
            report = capacity.get((engine, profile))
            if not report:
                continue
            rows.append({"engine": engine_name(engine), "profile": profile, "capacity": report["capacity_users"]})
            ceil = report.get("first_failing_users") is None
            annotations[(engine_name(engine), profile)] = ("≥" if ceil else "") + str(report["capacity_users"])
    frame = pd.DataFrame(rows)
    fig, ax = plt.subplots(figsize=(9, 4.8))
    sns.barplot(data=frame, x="engine", y="capacity", hue="profile", hue_order=["session", "raw"], palette=["#1f3a5f", "#98a2b3"], ax=ax)
    ax.set_title("Concurrent capacity within SLO by engine")
    ax.set_xlabel("")
    ax.set_ylabel("virtual users")
    ax.legend(title="profile")
    for container, profile in zip(ax.containers, ("session", "raw")):
        values = [annotations.get((engine_name(engine), profile), "") for engine in engines]
        ax.bar_label(container, labels=values, fontsize=10)
    ax.text(0.0, -0.24, f"≥ = still met every SLO at the test ceiling · p95 ≤ {slo['p95_ms']:.0f} ms · p99 ≤ {slo['p99_ms']:.0f} ms · errors ≤ {slo['error_rate'] * 100:.1f}%",
            transform=ax.transAxes, fontsize=9, color="#667085")
    save(fig, output, "capacity_by_engine.png")


def plot_inmemory(runs, output):
    data = {engine: report for engine, report in runs.items() if report.get("memory_core")}
    if not data:
        return
    labels = [engine_name(engine) for engine in data]
    values = [data[engine]["memory_core"]["throughput_ops_per_second"] / 1e6 for engine in data]
    fig, ax = plt.subplots(figsize=(9, 4.6))
    sns.barplot(x=labels, y=values, hue=labels, palette=[PALETTE.get(engine, "#1f3a5f") for engine in data], legend=False, ax=ax)
    ax.set_title("In-memory transfer-core throughput (no HTTP or SQLite)")
    ax.set_xlabel("")
    ax.set_ylabel("millions of operations / s")
    for index, value in enumerate(values):
        ax.text(index, value, f"{value:.2f}M", ha="center", va="bottom", fontsize=10)
    save(fig, output, "inmemory_core.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="docs/sample-results.json")
    parser.add_argument("--output", default="docs/figures")
    args = parser.parse_args()
    reports = load(args.input)
    runs = latest_session_runs(reports)
    raw = sweeps_by_profile(reports, "raw")
    slo = next((sweep.get("slo") for sweep in raw.values()), {"p95_ms": 500.0, "p99_ms": 1000.0, "error_rate": 0.01})
    print(f"Loaded {len(reports)} reports · {len(runs)} session runs · {len(raw)} raw sweeps")
    plot_capacity_summary(reports, args.output, slo)
    plot_peak_throughput(raw, args.output)
    plot_latency(runs, args.output)
    plot_inmemory(runs, args.output)
    plot_raw_saturation(raw, args.output, slo)
    plot_capacity_heatmap(raw, args.output, slo)


if __name__ == "__main__":
    main()
