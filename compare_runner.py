#!/usr/bin/env python3
"""
compare_runner.py — one-parameter-at-a-time benchmark tester, mean±std output.

Binary interface:
    ./mpmc_mutex_cv.exe P C items burst
Output:
    duration_ms,throughput/s

This script:
  • Builds two binaries
  • Defines a BASE configuration
  • Sweeps ONE parameter at a time (P, C, items, burst)
  • Each test repeated REPEATS times
  • Outputs mean ± stddev comparison tables
"""

import subprocess
import shutil
from pathlib import Path
from typing import List, Tuple, Dict
import math
import pandas as pd
import matplotlib.pyplot as plt
import tabulate as tb

ROOT = Path(__file__).parent
GPP = shutil.which("g++") or "g++"

DATA_DIR = ROOT / "data"
if not DATA_DIR.exists():
    DATA_DIR.mkdir()

MUTEX_SRC = ROOT / "mpmc_mutex_cv.cpp"
ATOMIC_SRC = ROOT / "mpmc_atomic_wait.cpp"
MUTEX_EXE = ROOT / "mpmc_mutex_cv.exe"
ATOMIC_EXE = ROOT / "mpmc_atomic_wait.exe"

# these control column names in tables
P="P"; C="C"; ITEMS_PER_PRODUCER="I"; B="B"

# ----------------------------------------------------------------------
# Utility
# ----------------------------------------------------------------------

def build(src: Path, out: Path) -> None:
    cmd = [GPP, "-O2", "-std=c++20", str(src), "-o", str(out)]
    print("[build]", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)

def run_binary(exe: Path, params: Tuple[int,int,int,int]) -> Dict[str,float]:
    """Run binary with parameters (P,C,items,burst). Parse CSV output."""
    cmd = [str(exe), *map(str, params)]
    out = subprocess.check_output(cmd, text=True, cwd=ROOT)
    # second line is numeric row
    dur, thr = out.splitlines()[1].split(',')
    return {"DurationMs": float(dur), "Throughput": float(thr)}

def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0

def stddev(xs: List[float]) -> float:
    if len(xs) <= 1:
        return 0
    m = mean(xs)
    return math.sqrt(sum((x - m)**2 for x in xs) / len(xs))

# ----------------------------------------------------------------------
# Output for paper
# ----------------------------------------------------------------------

def make_dataframe(results_cv, results_aw, changing_param):
    # fragile... must match order in main()
    param_idx = { P:0, C:1, ITEMS_PER_PRODUCER: 2, B: 3 }[changing_param] 

    THR_CV = "CV Thru."
    THR_AW = "AW Thru."
    RATIO = "AW/CV"

    data = { changing_param: [], THR_CV: [], THR_AW: [] } # RATIO col is added thru pandas
    
    data[changing_param] = [ row['params'][param_idx] for row in results_aw.values() ]
    data[THR_CV] = [ row['ThrMean'] for row in results_cv.values() ]
    data[THR_AW] = [ row['ThrMean'] for row in results_aw.values() ]

    df = pd.DataFrame.from_dict(data)
    df[RATIO] = df[THR_AW] / df[THR_CV]
    return df

def save_table(df, filename_prefix):
    output_file = DATA_DIR / (filename_prefix + ".tex")
    table = tb.tabulate(df, showindex=False, headers='keys', tablefmt='latex_booktabs')
    with open(output_file, 'w') as f:
        f.write(table)

def save_plot(df, filename_prefix):
    output_file = DATA_DIR / (filename_prefix + ".png")
    x_col = df.columns[0]
    df.plot(x=x_col, y=["CV Thru.", "AW Thru."], marker="o", title=f"Throughput vs. {x_col}")
    plt.ylabel("Throughput")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(str(output_file))

# ----------------------------------------------------------------------
# Console output
# ----------------------------------------------------------------------

def print_block(title: str, results_cv, results_aw, repeats: int):
    print(f"\n===== {title} =====")
    print(f"=== Aggregated over {repeats} runs ===")

    W = 118  # table width
    print("-" * W)
    print("COMPARISON: mutex+cv (mean±std) vs atomic_wait (mean±std)")
    print("-" * W)

    # Column layout
    # Test  P  C  Burst  items/C | DurCV  ±  DurAW | ThrCV ± ThrAW | AW/CV
    header = (
        f"{'Test':<4} "
        f"{'P':>3} {'C':>3} {'B':>3} {'items/P':>10} | "
        f"{'DurCV':>8} {'±':>5} {'DurAW':>8} {'±':>5} | "
        f"{'ThrCV':>10} {'±':>5} {'ThrAW':>10} {'±':>5} | "
        f"{'AW/CV':>6}"
    )
    print(header)
    print("-" * W)

    sum_cv = sum_aw = 0.0

    for tid in sorted(results_cv.keys()):
        a = results_cv[tid]
        b = results_aw[tid]

        P, C, itemsPerProducer, B = a["params"]

        dcv, scv = a["DurationMean"], a["DurationStd"]
        daw, saw = b["DurationMean"], b["DurationStd"]

        tcv, tsc = a["ThrMean"], a["ThrStd"]
        taw, tsa = b["ThrMean"], b["ThrStd"]

        sum_cv += tcv
        sum_aw += taw
        ratio = taw / tcv if tcv > 0 else float("nan")

        print(
            f"{tid:<4} "
            f"{P:>3} {C:>3} {B:>3} {itemsPerProducer:>10} | "
            f"{dcv:8.0f} {scv:>5.2f} {daw:8.0f} {saw:>5.2f} | "
            f"{int(tcv):>10,} {int(tsc):>5,} {int(taw):>10,} {int(tsa):>5,} | "
            f"{ratio:6.2f}"
        )

    print("-" * W)
    print(f"Aggregate throughput ratio (AW/CV): {sum_aw / sum_cv:.2f}")
    print()

# ----------------------------------------------------------------------
# Core test runner
# ----------------------------------------------------------------------

def run_sweep(test_list: List[Tuple[int,int,int,int]], repeats: int):
    """Run a list of parameter tuples."""
    cv_res = {}
    aw_res = {}

    for tid, params in enumerate(test_list, 1):
        P, C, itemsPerProducer, burst = params

        cv_dur = []
        cv_thr = []
        aw_dur = []
        aw_thr = []

        for i in range(repeats):
            o = run_binary(MUTEX_EXE, params)
            cv_dur.append(o["DurationMs"])
            cv_thr.append(o["Throughput"])

            o = run_binary(ATOMIC_EXE, params)
            aw_dur.append(o["DurationMs"])
            aw_thr.append(o["Throughput"])

        cv_res[tid] = {
            "params": params,
            "DurationMean": mean(cv_dur),
            "DurationStd": stddev(cv_dur),
            "ThrMean": mean(cv_thr),
            "ThrStd": stddev(cv_thr),
        }
        aw_res[tid] = {
            "params": params,
            "DurationMean": mean(aw_dur),
            "DurationStd": stddev(aw_dur),
            "ThrMean": mean(aw_thr),
            "ThrStd": stddev(aw_thr),
        }

    return cv_res, aw_res

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    REPEATS = 5

    # ------------------------------------------
    # BASE CONFIG — everything varies from this
    # ------------------------------------------
    BASE = (8, 8, 1_000, 10)
    base_P, base_C, base_items, base_burst = BASE

    # Sweeps modifying one param at a time
    SWEEP_P = [(p, base_C, base_items, base_burst) for p in [2, 4, 8, 16, 32]]
    SWEEP_C = [(base_P, c, base_items, base_burst) for c in [2, 4, 8, 16, 32]]
    SWEEP_ITEMS = [(base_P, base_C, it, base_burst) for it in [100, 1_000, 5_000, 10_000]]
    SWEEP_BURST = [(base_P, base_C, base_items, b) for b in [1, 2, 4, 8, 16, 32]]

    # ------------------------------------------
    # Build binaries
    # ------------------------------------------
    build(MUTEX_SRC, MUTEX_EXE)
    build(ATOMIC_SRC, ATOMIC_EXE)

    # ------------------------------------------
    # Run sweeps and print results
    # ------------------------------------------

    print()

    # 1) Producer sweep
    print("Testing with varying P")
    cv, aw = run_sweep(SWEEP_P, REPEATS)
    dataframe = make_dataframe(cv, aw, P)
    save_table(dataframe, "producers_sweep")
    save_plot(dataframe, "producers_sweep")
    print_block("Producer Sweep (vary P)", cv, aw, REPEATS)

    # 2) Consumer sweep
    print("Testing with varying C")
    cv, aw = run_sweep(SWEEP_C, REPEATS)
    dataframe = make_dataframe(cv, aw, C)
    save_table(dataframe, "consumers_sweep")
    save_plot(dataframe, "consumers_sweep")
    print_block("Consumer Sweep (vary C)", cv, aw, REPEATS)

    # 3) Items-per-producer sweep
    print("Testing with varying items/producer")
    cv, aw = run_sweep(SWEEP_ITEMS, REPEATS)
    dataframe = make_dataframe(cv, aw, ITEMS_PER_PRODUCER)
    save_table(dataframe, "items_sweep")
    save_plot(dataframe, "items_sweep")
    print_block("Items-per-Producer Sweep (vary items/producer)", cv, aw, REPEATS)

    # 4) Burst sweep
    print("Testing with varying burst")
    cv, aw = run_sweep(SWEEP_BURST, REPEATS)
    dataframe = make_dataframe(cv, aw, B)
    save_table(dataframe, "bursts_sweep")
    save_plot(dataframe, "bursts_sweep")
    print_block("Burst Sweep (vary burst)", cv, aw, REPEATS)


if __name__ == "__main__":
    main()

