#!/usr/bin/env python3
"""Build and run bench_ondemand for several CMake presets and compare them.

By default it compares the GCC release build (preset "rel") against the Clang
release build (preset "rel-clang"). The first preset is the baseline: the
speedup column is baseline_time / preset_time, so > 1.00x means faster.

Examples:
  scripts/run-benchmarks.py
  scripts/run-benchmarks.py --filter 'simdjson_ondemand' --repetitions 10
  scripts/run-benchmarks.py --cpus ''                 # no taskset pinning
  scripts/run-benchmarks.py --skip-build --skip-run   # re-print last results
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = "bench_ondemand"


def run(cmd, **kwargs):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def build_dir(preset):
    # Matches "binaryDir": "build-${presetName}" in CMakePresets.json.
    return ROOT / f"build-{preset}"


def results_file(preset):
    return build_dir(preset) / f"{TARGET}-results.json"


def build(preset):
    run(["cmake", "--preset", preset], cwd=ROOT)
    run(["cmake", "--build", build_dir(preset), "--target", TARGET], cwd=ROOT)


def bench(preset, args):
    exe = build_dir(preset) / "benchmark" / TARGET
    cmd = ["taskset", "-c", args.cpus] if args.cpus else []
    cmd += [
        exe,
        f"--benchmark_out={results_file(preset)}",
        "--benchmark_out_format=json",
    ]
    if args.filter:
        cmd.append(f"--benchmark_filter={args.filter}")
    if args.min_time:
        cmd.append(f"--benchmark_min_time={args.min_time}")
    if args.repetitions > 1:
        cmd += [
            f"--benchmark_repetitions={args.repetitions}",
            "--benchmark_report_aggregates_only=true",
        ]
    run(cmd, cwd=exe.parent)


def load(preset):
    """Return {benchmark name: entry}, using the median when repeated.

    When repeated, entry["cv"] holds the coefficient of variation of real_time.
    """
    path = results_file(preset)
    if not path.exists():
        sys.exit(f"error: {path} does not exist, run without --skip-run")
    data = json.loads(path.read_text())
    results = {}
    cvs = {}
    for b in data["benchmarks"]:
        if b.get("error_occurred"):
            continue
        name = b["run_name"].removesuffix("/manual_time")
        if b.get("run_type") == "aggregate":
            if b.get("aggregate_name") == "cv":
                cvs[name] = b["real_time"]
            if b.get("aggregate_name") != "median":
                continue
        results[name] = b
    for name, cv in cvs.items():
        if name in results:
            results[name]["cv"] = cv
    return results


def to_ns(entry):
    scale = {"ns": 1, "us": 1e3, "ms": 1e6, "s": 1e9}[entry.get("time_unit", "ns")]
    return entry["real_time"] * scale


def fmt_time(ns):
    for unit, div in (("s", 1e9), ("ms", 1e6), ("us", 1e3)):
        if ns >= div:
            return f"{ns / div:.2f} {unit}"
    return f"{ns:.0f} ns"


def fmt_gbs(entry):
    bps = entry.get("bytes_per_second")
    return f"{bps / 1e9:.2f}" if bps else "-"


def fmt_cv(entry):
    cv = entry.get("cv")
    return f"{cv * 100:.1f}%" if cv is not None else "-"


def print_table(presets, results):
    base = presets[0]
    names = list(results[base])
    for p in presets[1:]:
        names += [n for n in results[p] if n not in names]
    show_cv = any("cv" in e for r in results.values() for e in r.values())

    header = ["benchmark"]
    for i, p in enumerate(presets):
        header += [f"{p} time", f"{p} GB/s"]
        if show_cv:
            header.append(f"{p} cv")
        if i:
            header.append(f"{p} speedup")

    rows = []
    for name in names:
        row = [name]
        base_entry = results[base].get(name)
        for i, p in enumerate(presets):
            entry = results[p].get(name)
            row += [fmt_time(to_ns(entry)), fmt_gbs(entry)] if entry else ["-", "-"]
            if show_cv:
                row.append(fmt_cv(entry) if entry else "-")
            if i:
                if entry and base_entry:
                    row.append(f"{to_ns(base_entry) / to_ns(entry):.2f}x")
                else:
                    row.append("-")
        rows.append(row)

    widths = [max(len(r[c]) for r in [header] + rows) for c in range(len(header))]
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def line(row, colorize=False):
        cells = []
        for c, (cell, w) in enumerate(zip(row, widths)):
            text = cell.ljust(w) if c == 0 else cell.rjust(w)
            if colorize and cell.endswith("x") and header[c].endswith("speedup"):
                ratio = float(cell[:-1])
                if ratio >= 1.05:
                    text = f"\033[32m{text}\033[0m"
                elif ratio <= 0.95:
                    text = f"\033[31m{text}\033[0m"
            cells.append(text)
        return " | ".join(cells)

    print()
    print(line(header))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(line(row, color))
    print(f"\nspeedup = {base} time / preset time (> 1.00x: faster than {base})")
    if show_cv:
        print("time/GB/s are medians; cv = coefficient of variation of the time")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "presets", nargs="*", default=["rel", "rel-clang"],
        help="configure presets to compare, first is the baseline (default: rel rel-clang)",
    )
    parser.add_argument("--filter", help="regex passed to --benchmark_filter")
    parser.add_argument("--min-time", help="passed to --benchmark_min_time, e.g. 0.5s")
    parser.add_argument(
        "--repetitions", type=int, default=5,
        help="repeat each benchmark and compare medians (default: 5)",
    )
    parser.add_argument(
        "--cpus", default="2",
        help="CPU list passed to 'taskset -c' (default: 2); empty string disables "
        "pinning. Note: the THREADED benchmarks are serialized when pinned to one CPU",
    )
    parser.add_argument("--skip-build", action="store_true", help="do not configure/build")
    parser.add_argument(
        "--skip-run", action="store_true", help="only print the table from previous results"
    )
    args = parser.parse_args()

    if len(args.presets) < 2:
        parser.error("need at least two presets to compare")

    try:
        if not args.skip_build:
            for p in args.presets:
                build(p)
        if not args.skip_run:
            for p in args.presets:
                bench(p, args)
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: command failed with exit code {e.returncode}")

    print_table(args.presets, {p: load(p) for p in args.presets})


if __name__ == "__main__":
    main()
