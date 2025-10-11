#!/usr/bin/env python3
"""
compare_runner.py

Always rebuilds both MPMC queue implementations (mutex+cv and atomic_wait), runs them,
parses their per-test summary output, and prints a side-by-side comparison table plus
an aggregate throughput ratio. No arguments required.

Requirements:
  - Python 3.x on PATH
  - g++ (MinGW / similar) supporting -std=c++20 and -pthread
  - Source files: mpmc_mutex_cv.cpp, mpmc_atomic_wait.cpp

Usage:
  python compare_runner.py

Outputs (example):
  ------------------------------------------------------------------------------
  COMPARISON: mutex+cv vs atomic_wait
  ------------------------------------------------------------------------------
  Test    P   C  Burst | DurCV(ms) DurAW(ms) |  ThrCV/s   ThrAW/s | AW/CV  CV/AW
  ...
  Aggregate throughput ratio (AW/CV): X.YZ

Notes:
  - The mutex+cv implementation prints capacity in its Config line; atomic version omits capacity.
  - Parser tolerates presence/absence of capacity= segment.
  - Both binaries are assumed to emit only the per-test summary blocks (no verbose logs) as per the latest code state.
"""
from __future__ import annotations

import subprocess
import re
import shutil
from pathlib import Path
from typing import Dict, Any, List

ROOT = Path(__file__).parent
GPP = shutil.which("g++") or "g++"  # fall back to plain name

MUTEX_SRC = ROOT / "mpmc_mutex_cv.cpp"
ATOMIC_SRC = ROOT / "mpmc_atomic_wait.cpp"
MUTEX_EXE = ROOT / "mpmc_mutex_cv.exe"
ATOMIC_EXE = ROOT / "mpmc_atomic_wait.exe"

TEST_HEADER_RE = re.compile(r"^===\s+Test\s+(\d+)\s+===\s*$")
CONFIG_RE = re.compile(r"^Config:\s+(\d+)\s+producers,\s+(\d+)\s+consumers,\s+(?:capacity=\d+,\s+)?burst=(\d+)\s*$")
DURATION_RE = re.compile(r"^Duration:\s+([0-9]+(?:\.[0-9]+)?)\s+ms\s*$")
THROUGHPUT_RE = re.compile(r"^Throughput:\s+([0-9]+(?:\.[0-9]+)?)\s+items/sec\s*$")
FINAL_SUMMARY_HEADER_RE = re.compile(r"^FINAL SUMMARY ")
ROW_FINAL_RE = re.compile(r"^(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([0-9]+(?:\.[0-9]+)?)\s+([0-9]+)\s+([0-9]+)\s*$")

class BuildError(RuntimeError):
    pass

def build(source: Path, output: Path) -> None:
    cmd = [GPP, "-std=c++20", "-O2", "-pthread", str(source), "-o", str(output)]
    print(f"[build] {' '.join(cmd)}")
    try:
        subprocess.check_call(cmd, cwd=ROOT)
    except subprocess.CalledProcessError as e:
        raise BuildError(f"Build failed for {source.name} (exit {e.returncode})") from e


def run_binary(exe: Path) -> str:
    print(f"[run] {exe.name}")
    try:
        out = subprocess.check_output([str(exe)], cwd=ROOT, stderr=subprocess.STDOUT, text=True)
        return out
    except subprocess.CalledProcessError as e:
        print(e.output)
        raise RuntimeError(f"Execution failed: {exe.name} (exit {e.returncode})") from e


def parse_results(text: str) -> Dict[int, Dict[str, Any]]:
    results: Dict[int, Dict[str, Any]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = TEST_HEADER_RE.match(line)
        if m:
            tid = int(m.group(1))
            current = results.setdefault(tid, {
                "Test": tid,
                "Producers": None,
                "Consumers": None,
                "Burst": None,
                "DurationMs": None,
                "Throughput": None,
            })
            continue
        if current is None:
            continue
        m = CONFIG_RE.match(line)
        if m:
            current["Producers"], current["Consumers"], current["Burst"] = map(int, m.groups())
            continue
        m = DURATION_RE.match(line)
        if m:
            current["DurationMs"] = float(m.group(1))
            continue
        m = THROUGHPUT_RE.match(line)
        if m:
            current["Throughput"] = float(m.group(1))
            continue
    return results

def parse_final_table(text: str) -> Dict[int, Dict[str, Any]]:
    """Parse the new consolidated final summary table format.
    Expected rows (after header):
    Test  P  C  Capacity  Burst  Duration(ms)  Throughput/s  FinalQ
    """
    results: Dict[int, Dict[str, Any]] = {}
    in_table = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if FINAL_SUMMARY_HEADER_RE.search(line):
            in_table = True
            continue
        if not in_table:
            continue
        # Detect end of table by Aggregate line
        if line.startswith("Aggregate throughput"):
            break
        m = ROW_FINAL_RE.match(line.strip())
        if m:
            test, prod, cons, capacity, burst, dur, thr, finalq = m.groups()
            tid = int(test)
            results[tid] = {
                "Test": tid,
                "Producers": int(prod),
                "Consumers": int(cons),
                "Burst": int(burst),
                "DurationMs": float(dur),
                "Throughput": float(thr),
                # Capacity / FinalQ stored but not strictly needed by comparison
                "_Capacity": int(capacity),
                "_FinalQ": int(finalq),
            }
    return results


def format_number(val: float, decimals: int = 2) -> str:
    if val is None:
        return "-"
    return f"{val:.{decimals}f}"


def int_fmt(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{int(val):,}".replace(",", "_")  # use underscore to avoid locale surprises


def print_comparison(cv: Dict[int, Dict[str, Any]], aw: Dict[int, Dict[str, Any]]) -> None:
    all_tests = sorted(set(cv.keys()) & set(aw.keys()))
    print("\n" + "-" * 94)
    print("COMPARISON: mutex+cv vs atomic_wait")
    print("-" * 94)
    header = f"{'Test':<6} {'P':>3} {'C':>3} {'Burst':>5} | {'DurCV(ms)':>9} {'DurAW(ms)':>9} | {'ThrCV/s':>9} {'ThrAW/s':>9} | {'AW/CV':>5} {'CV/AW':>5}"
    print(header)
    print("-" * 94)

    sum_thr_cv = 0.0
    sum_thr_aw = 0.0

    for t in all_tests:
        a = cv[t]; b = aw[t]
        P = b['Producers'] or a['Producers']
        C = b['Consumers'] or a['Consumers']
        B = b['Burst'] or a['Burst']
        dcv = a['DurationMs']; daw = b['DurationMs']
        tcv = a['Throughput']; taw = b['Throughput']
        if tcv: sum_thr_cv += tcv
        if taw: sum_thr_aw += taw
        speed = (dcv / daw) if (dcv and daw) else float('nan')
        inv = (daw / dcv) if (dcv and daw) else float('nan')
        print(f"{t:<6} {P:>3} {C:>3} {B:>5} | {format_number(dcv):>9} {format_number(daw):>9} | {int_fmt(tcv):>9} {int_fmt(taw):>9} | {format_number(speed):>5} {format_number(inv):>5}")
    print("-" * 94)
    if sum_thr_cv > 0:
        ratio = sum_thr_aw / sum_thr_cv if sum_thr_cv else float('nan')
        print(f"\nAggregate throughput ratio (AW/CV): {ratio:.2f}")


def mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0

def stddev(values: List[float]) -> float:
    if not values:
        return 0.0
    m = mean(values)
    var = sum((x - m) ** 2 for x in values) / len(values)
    return var ** 0.5


def main() -> None:
    REPEATS = 5
    # 1) Rebuild both binaries
    build(MUTEX_SRC, MUTEX_EXE)
    build(ATOMIC_SRC, ATOMIC_EXE)

    # 2) Run both binaries REPEATS times and collect outputs
    cv_runs: List[str] = []
    aw_runs: List[str] = []
    for i in range(REPEATS):
        print(f"\n[run {i+1}/{REPEATS}] mutex+cv")
        cv_runs.append(run_binary(MUTEX_EXE))
        print(f"[run {i+1}/{REPEATS}] atomic_wait")
        aw_runs.append(run_binary(ATOMIC_EXE))

    # 3) Parse runs into per-test aggregated stats
    # We'll parse final-table format; fall back to line-based parsing if needed
    def aggregate_runs(runs: List[str]) -> Dict[int, Dict[str, Any]]:
        per_test: Dict[int, Dict[str, Any]] = {}
        for out in runs:
            parsed = parse_final_table(out)
            if not parsed:
                parsed = parse_results(out)
            for tid, row in parsed.items():
                entry = per_test.setdefault(tid, {
                    'Test': tid,
                    'Producers': row.get('Producers'),
                    'Consumers': row.get('Consumers'),
                    'Burst': row.get('Burst'),
                    'Durations': [],
                    'Throughputs': [],
                })
                entry['Durations'].append(row.get('DurationMs', 0.0))
                entry['Throughputs'].append(row.get('Throughput', 0.0))
        # Convert to mean/stddev
        summary: Dict[int, Dict[str, Any]] = {}
        for tid, v in per_test.items():
            summary[tid] = {
                'Test': tid,
                'Producers': v['Producers'],
                'Consumers': v['Consumers'],
                'Burst': v['Burst'],
                'DurationMs': mean(v['Durations']),
                'DurationStdMs': stddev(v['Durations']),
                'Throughput': mean(v['Throughputs']),
                'ThroughputStd': stddev(v['Throughputs']),
            }
        return summary

    cv_res = aggregate_runs(cv_runs)
    aw_res = aggregate_runs(aw_runs)

    # 4) Print comparison (adapted to show mean/stddev)
    print('\n=== AGGREGATED RESULTS (means over {} runs) ==='.format(REPEATS))
    all_tests = sorted(set(cv_res.keys()) & set(aw_res.keys()))
    print('\n' + '-' * 120)
    print('COMPARISON: mutex+cv (mean±std) vs atomic_wait (mean±std)')
    print('-' * 120)
    hdr = f"{'Test':<6} {'P':>3} {'C':>3} {'Burst':>5} | {'DurCV(ms)':>12} {'±':>2} {'DurAW(ms)':>12} {'±':>2} | {'ThrCV/s':>10} {'±':>5} {'ThrAW/s':>10} {'±':>5} | {'AW/CV':>6}"
    print(hdr)
    print('-' * 120)
    for t in all_tests:
        a = cv_res[t]; b = aw_res[t]
        P = b['Producers'] or a['Producers']
        C = b['Consumers'] or a['Consumers']
        B = b['Burst'] or a['Burst']
        dcv = a['DurationMs']; sd_cv = a['DurationStdMs']
        daw = b['DurationMs']; sd_aw = b['DurationStdMs']
        tcv = a['Throughput']; s_tc = a['ThroughputStd']
        taw = b['Throughput']; s_ta = b['ThroughputStd']
        ratio = (taw / tcv) if (tcv and taw) else float('nan')
        print(f"{t:<6} {P:>3} {C:>3} {B:>5} | {dcv:12.2f} {sd_cv:2.2f} {daw:12.2f} {sd_aw:2.2f} | {int(tcv):10,} {int(s_tc):5,} {int(taw):10,} {int(s_ta):5,} | {ratio:6.2f}")
    print('-' * 120)

if __name__ == "__main__":
    main()
