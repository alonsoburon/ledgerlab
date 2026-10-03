#!/usr/bin/env python3
"""Render charts from LedgerLab evaluation results.

Reads a JSON list of run/sweep reports (as written by
``scripts/run_benchmarks.py`` or served by ``GET /evaluations``) and writes
PNGs for the README.

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

sns.set_theme(style="whitegrid", context="notebook")
plt.rcParams.update({
    "figure.dpi": 130,
    "savefig.bbox": "tight",
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.titleweight": "normal",
    "axes.labelsize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9,
    "grid.color": "#e6e6e6",
    "grid.linewidth": 0.6,
})


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
    labels = [engine_name(engine) for engine in sweeps]
    peaks = [max(sweep["curve"], key=lambda e: e["throughput_rps"])["throughput_rps"] for sweep in sweeps.values()]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.bar(labels, peaks, color="#4c72b0")
    ax.set_title("peak transfers/s (raw write path)")
    ax.set_ylabel("transfers/s")
    ax.tick_params(axis="x", rotation=0)
    save(fig, output, "throughput_by_engine.png")


def plot_latency(runs, output):
    if not runs:
        return
    engines = list(runs)
    frame = pd.DataFrame({
        "engine": [engine_name(engine) for engine in engines],
        "p50": [runs[e]["results"]["latency_ms"]["p50"] for e in engines],
        "p95": [runs[e]["results"]["latency_ms"]["p95"] for e in engines],
        "p99": [runs[e]["results"]["latency_ms"]["p99"] for e in engines],
    }).melt(id_vars="engine", var_name="percentile", value_name="ms")
    frame["percentile"] = pd.Categorical(frame["percentile"], ["p50", "p95", "p99"], ordered=True)
    fig, ax = plt.subplots(figsize=(7, 3.6))
    sns.barplot(data=frame, x="engine", y="ms", hue="percentile", ax=ax)
    ax.set_title("session latency (32 users)")
    ax.set_xlabel("")
    ax.set_ylabel("ms")
    ax.legend(title="", ncol=3, loc="upper left")
    ax.tick_params(axis="x", rotation=0)
    save(fig, output, "latency_by_engine.png")


def plot_raw_saturation(sweeps, output, slo):
    if not sweeps:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for engine, sweep in sweeps.items():
        curve = sorted(sweep["curve"], key=lambda e: e["users"])
        users = [e["users"] for e in curve]
        rps = [e["throughput_rps"] for e in curve]
        p95 = [e["latency_ms"]["p95"] for e in curve]
        color = axes[0].plot(users, rps, marker="o", label=engine_name(engine))[0].get_color()
        axes[1].plot(users, p95, marker="o", color=color, label=engine_name(engine))
        bad = [e for e in curve if not e["slo"]["passed"]]
        axes[1].plot([e["users"] for e in bad], [e["latency_ms"]["p95"] for e in bad], "x", color=color, markersize=7)
    axes[0].set_title("throughput")
    axes[0].set_ylabel("transfers/s")
    axes[1].set_title("p95 latency")
    axes[1].set_ylabel("ms")
    axes[1].axhline(slo["p95_ms"], color="#888", linestyle="--", linewidth=1, label="p95 SLO")
    for ax in axes:
        ax.set_xlabel("concurrent writers")
        ax.set_xscale("log", base=2)
        ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    axes[1].legend()
    save(fig, output, "raw_saturation.png")


def plot_capacity_heatmap(sweeps, output, slo):
    if not sweeps:
        return
    by_engine = {engine: {e["users"]: e["latency_ms"]["p95"] for e in sweep["curve"]} for engine, sweep in sweeps.items()}
    columns = sorted({level for values in by_engine.values() for level in values})
    frame = pd.DataFrame(
        [[by_engine[engine].get(level) for level in columns] for engine in by_engine],
        index=[engine_name(engine) for engine in by_engine],
        columns=[str(level) for level in columns],
    )
    fig, ax = plt.subplots(figsize=(7, 1.2 + 0.6 * len(frame.index)))
    sns.heatmap(frame, annot=True, fmt=".0f", cmap="Blues", cbar_kws={"label": "p95 ms"}, linewidths=0.5, ax=ax)
    ax.set_title("p95 ms (raw profile)")
    ax.set_xlabel("concurrent writers")
    ax.set_ylabel("")
    save(fig, output, "capacity_heatmap.png")


def plot_capacity_summary(reports, output, slo):
    capacity = capacity_by_engine(reports)
    if not capacity:
        return
    engines = []
    for engine, _ in capacity:
        if engine not in engines:
            engines.append(engine)
    rows = []
    for engine in engines:
        for profile in ("session", "raw"):
            report = capacity.get((engine, profile))
            if report:
                rows.append({"engine": engine_name(engine), "profile": profile, "capacity": report["capacity_users"]})
    frame = pd.DataFrame(rows)
    fig, ax = plt.subplots(figsize=(7, 3.6))
    sns.barplot(data=frame, x="engine", y="capacity", hue="profile", hue_order=["session", "raw"], ax=ax)
    ax.set_title("users within SLO")
    ax.set_xlabel("")
    ax.set_ylabel("concurrent users")
    ax.legend(title="")
    ax.tick_params(axis="x", rotation=0)
    save(fig, output, "capacity_by_engine.png")


def plot_inmemory(runs, output):
    data = {engine: report for engine, report in runs.items() if report.get("memory_core")}
    if not data:
        return
    labels = [engine_name(engine) for engine in data]
    values = [data[engine]["memory_core"]["throughput_ops_per_second"] / 1e6 for engine in data]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.bar(labels, values, color="#4c72b0")
    ax.set_title("in-memory core")
    ax.set_ylabel("millions of ops/s")
    ax.tick_params(axis="x", rotation=0)
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