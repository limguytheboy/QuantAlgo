"""
run_optimization.py  v8
-----------------------
4-phase optimizer for TrendlineBreakStrategy.
Replicates run_backtest.bat exactly per run:

  docker run --rm
    -v  Data     → /Lean/Data
    -v  MyAlgos  → /MyAlgos
    -v  config_N → /Lean/Launcher/bin/Debug/config.json   (unique per run)
    -v  log_N    → /Lean/Launcher/bin/Debug/TrendlineBreakStrategy-log.txt

Each parallel run gets its own config + log file — no collisions.
"""

import subprocess, json, itertools, copy, os, re, time, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────────────────────────────────────
LEAN_ROOT   = r"C:\Users\user\Desktop\VisualCode\Quant\Lean Engine\Lean"
DATA_DIR    = os.path.join(LEAN_ROOT, "Data")
ALGOS_DIR   = os.path.join(LEAN_ROOT, "MyAlgos")
CONFIG_BASE = os.path.join(LEAN_ROOT, "Launcher", "config.json")
RESULTS_DIR = os.path.join(LEAN_ROOT, "OptimizationResults")
TEMP_DIR    = os.path.join(LEAN_ROOT, "OptTemp")

DOCKER_IMAGE     = "quantconnect/lean:latest"
STRATEGY_NAME    = "TrendlineBreakStrategy"
CONTAINER_CONFIG = "/Lean/Launcher/bin/Debug/config.json"
CONTAINER_LOG    = f"/Lean/Launcher/bin/Debug/{STRATEGY_NAME}-log.txt"

MAX_PARALLEL = 8
TIMEOUT_SECS = 360

# ─────────────────────────────────────────────────────────────────────────────
#  PHASE
# ─────────────────────────────────────────────────────────────────────────────
PHASE = 1

PHASE1_BEST = {}
PHASE2_BEST = {}
PHASE3_BEST = {}

GRIDS = {
    1: {
        "bull_sl_atr_buffer":   [1.0, 1.2, 1.5],
        "bear_sl_atr_buffer":   [1.3, 1.5, 1.8],
        "bull_fib_tp_mult":     [1.0, 1.2, 1.5],
        "bear_fib_tp_mult":     [0.7, 0.9, 1.1],
        "min_rr":               [2.0, 2.5, 3.0],
        "bear_vol_spike_mult":  [1.1, 1.3, 1.6],
    },
    2: {
        "be_dynamic_ratio":     [0.30, 0.40, 0.50],
        "partial_tp_rr":        [1.5,  2.0,  2.5],
        "partial_close_pct":    [0.40, 0.50, 0.60],
        "trail_sl_follow_r":    [0.50, 0.75, 1.00],
        "retest_atr_band":      [0.40, 0.60, 0.80],
        "retest_max_bars":      [8,    12,   18],
    },
    3: {
        "min_touches":           [5,    6,    7],
        "max_touch_cluster":     [2,    3,    4],
        "touch_atr_mult":        [0.15, 0.20, 0.25],
        "min_trendline_bars":    [15,   20,   25],
        "min_swing_atr_mult":    [3.0,  4.0,  5.0],
        "htf_min_bars_below_ema":[8,    12,   20],
    },
    4: {
        "breakeven_trigger_rr":  [1.0, 1.5, 2.0],
        "loss_cooldown_bars":    [15,  25,  35],
        "fakeout_cooldown_bars": [5,   8,   12],
        "loss_streak_n":         [2,   3,   4],
        "loss_streak_scale":     [0.4, 0.5, 0.6],
    },
}

ALWAYS_FIXED = {
    "symbol":              "SOLUSDT",
    "market":              "binance",
    "timeframe":           "5min",
    "use_retest":          "true",
    "use_trail_sl":        "true",
    "use_breakeven":       "true",
    "use_partial_tp":      "true",
    "bearish_require_all": "true",
    "require_all_filters": "false",
    "max_drawdown_pct":    "0.20",
    "risk_pct":            "0.005",
    "bull_vol_spike_mult": "1.8",
}

MIN_TRADES  = 5
WARN_TRADES = 20

TIER_NAMES = {
    1: "Entry Quality  (SL dist, TP target, filters)",
    2: "Trade Management  (BE, partial, trail, retest)",
    3: "Trendline Detection  (touches, spacing, spans)",
    4: "Risk & Cooldowns  (streak, pause, BE floor)",
}

for _d in [RESULTS_DIR, TEMP_DIR]:
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception as _e:
        print(f"WARNING: could not create {_d}: {_e}")


def build_fixed(phase):
    fixed = dict(ALWAYS_FIXED)
    if phase >= 2: fixed.update({k: str(v) for k, v in PHASE1_BEST.items()})
    if phase >= 3: fixed.update({k: str(v) for k, v in PHASE2_BEST.items()})
    if phase >= 4: fixed.update({k: str(v) for k, v in PHASE3_BEST.items()})
    return fixed


def load_base_config():
    with open(CONFIG_BASE, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
    lines = raw.splitlines()
    cleaned = []
    for line in lines:
        result = []; in_string = False; escape = False; i = 0
        while i < len(line):
            ch = line[i]
            if escape:
                result.append(ch); escape = False
            elif ch == "\\" and in_string:
                result.append(ch); escape = True
            elif ch == '"':
                in_string = not in_string; result.append(ch)
            elif ch == "/" and not in_string and i+1 < len(line) and line[i+1] == "/":
                break
            else:
                result.append(ch)
            i += 1
        cleaned.append("".join(result))
    raw = "\n".join(cleaned)
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    raw = raw.replace("\r", "").replace("\x00", "")
    return json.loads(raw)


def write_temp_config(base_cfg, fixed, grid_params, run_id):
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("parameters", {})
    cfg["parameters"].update(fixed)
    cfg["parameters"].update({k: str(v) for k, v in grid_params.items()})
    path = os.path.join(TEMP_DIR, f"config_{run_id:04d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path


def make_empty_log(run_id):
    path = os.path.join(TEMP_DIR, f"log_{run_id:04d}.txt")
    open(path, "w").close()
    return path


def parse_stats(output):
    stats = {}
    for line in output.splitlines():
        if "STATISTICS::" not in line:
            continue
        parts = line.split("STATISTICS::")[-1].strip()
        m = re.match(r"^(.+?)\s{2,}(.+)$", parts)
        if m:
            stats[m.group(1).strip()] = m.group(2).strip()
        else:
            t = parts.rsplit(None, 1)
            if len(t) == 2:
                stats[t[0].strip()] = t[1].strip()
    return stats


def to_f(stats, key, default=float("-inf")):
    try:
        return float(re.sub(r"[%$,\u20ae\s]", "", stats.get(key, "")).strip())
    except Exception:
        return default


def to_i(stats, key):
    try:
        return int(re.sub(r"[^0-9]", "", stats.get(key, "0")))
    except Exception:
        return 0


def fmt_params(params):
    return "  ".join(f"{k}={v}" for k, v in params.items())


def run_backtest(run_id, total, grid_params, fixed, base_cfg):
    config_path = write_temp_config(base_cfg, fixed, grid_params, run_id)
    log_path    = make_empty_log(run_id)
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{DATA_DIR}:/Lean/Data",
        "-v", f"{ALGOS_DIR}:/MyAlgos",
        "-v", f"{config_path}:{CONTAINER_CONFIG}",
        "-v", f"{log_path}:{CONTAINER_LOG}",
        DOCKER_IMAGE,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_SECS,
        )
        output = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        output = f"TIMEOUT after {TIMEOUT_SECS}s"
    except FileNotFoundError:
        output = "ERROR: docker not found"
    except Exception as e:
        output = f"ERROR: {e}"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            output += "\n" + f.read()
    except Exception:
        pass
    for p in [config_path, log_path]:
        try: os.remove(p)
        except Exception: pass
    return run_id, grid_params, parse_stats(output), output


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR,    exist_ok=True)

    grid    = GRIDS[PHASE]
    fixed   = build_fixed(PHASE)
    keys    = list(grid.keys())
    combos  = list(itertools.product(*grid.values()))
    total   = len(combos)
    est_min = (total * (TIMEOUT_SECS // 3)) // MAX_PARALLEL // 60

    print(f"\n{'='*72}")
    print(f"  TrendlineBreak Optimizer v8    Phase {PHASE}/4: {TIER_NAMES[PHASE]}")
    print(f"{'-'*72}")
    print(f"  Results      : {RESULTS_DIR}")
    print(f"  Combos       : {total}   (~{est_min} min at {MAX_PARALLEL} parallel)")
    print(f"  Parallel     : {MAX_PARALLEL}   Timeout: {TIMEOUT_SECS}s")
    print(f"  Started      : {datetime.now().strftime('%H:%M:%S')}")

    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if r.returncode != 0:
            print("\n  ERROR: Docker not running."); return
        print(f"  Docker       : OK")
    except FileNotFoundError:
        print("\n  ERROR: docker not found."); return

    try:
        base_cfg = load_base_config()
        print(f"  Config       : OK")
    except Exception as e:
        print(f"\n  ERROR loading config: {e}"); return

    if PHASE >= 2 and PHASE1_BEST:
        print(f"\n  Locked Phase 1:")
        for k, v in PHASE1_BEST.items(): print(f"    {k:<32} = {v}")
    if PHASE >= 3 and PHASE2_BEST:
        print(f"\n  Locked Phase 2:")
        for k, v in PHASE2_BEST.items(): print(f"    {k:<32} = {v}")
    if PHASE >= 4 and PHASE3_BEST:
        print(f"\n  Locked Phase 3:")
        for k, v in PHASE3_BEST.items(): print(f"    {k:<32} = {v}")

    print(f"\n  Fixed:"); [print(f"    {k:<32} = {v}") for k, v in ALWAYS_FIXED.items()]
    print(f"\n  Grid ({total} combos):");  [print(f"    {k:<32} : {list(vs)}") for k, vs in grid.items()]
    print(f"{'='*72}\n")

    results   = []
    all_runs  = []
    completed = 0
    submitted = 0
    start_t   = time.time()
    lock      = threading.Lock()

    def print_progress(extra=""):
        done      = completed
        in_flight = min(submitted - done, MAX_PARALLEL)
        queued    = max(0, total - done - in_flight)
        pct       = done / total * 100
        bar       = chr(9608) * int(pct/2) + chr(9617) * (50 - int(pct/2))
        elapsed   = time.time() - start_t
        eta_str   = ""
        if done > 0:
            eta_sec = (elapsed / done) * (total - done)
            h, rem  = divmod(int(eta_sec), 3600)
            m, s    = divmod(rem, 60)
            eta_str = f"  ETA {h:02d}:{m:02d}:{s:02d}" if h else f"  ETA {m:02d}:{s:02d}"
        print(
            f"\r  [{bar}] {pct:5.1f}%"
            f"  {done}/{total} done  {in_flight}/{MAX_PARALLEL} running  {queued} queued"
            f"  elapsed {int(elapsed)//60:02d}:{int(elapsed)%60:02d}{eta_str}"
            + (f"  {extra}" if extra else ""),
            end="", flush=True
        )

    print(f"  Launching {MAX_PARALLEL} parallel containers...\n")
    print_progress()

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        futures = {}
        for i, combo in enumerate(combos):
            run_id = i + 1
            params = dict(zip(keys, combo))
            future = executor.submit(run_backtest, run_id, total, params, fixed, base_cfg)
            futures[future] = (run_id, params)
            with lock:
                submitted += 1
            print_progress()

        for future in as_completed(futures):
            run_id, params = futures[future]
            try:
                run_id, params, stats, output = future.result()
            except Exception as e:
                stats = {}; output = str(e)

            sharpe     = to_f(stats, "Sharpe Ratio")
            trades     = to_i(stats, "Total Orders")
            net_profit = to_f(stats, "Net Profit",  0.0)
            drawdown   = to_f(stats, "Drawdown",    0.0)
            win_rate   = to_f(stats, "Win Rate",    0.0)
            expectancy = to_f(stats, "Expectancy",  0.0)
            avg_win    = stats.get("Average Win",  "N/A")
            avg_loss   = stats.get("Average Loss", "N/A")

            if "TIMEOUT"  in output:               flag = "[TIMEOUT]"
            elif "ERROR"  in output and not stats: flag = "[DOCKER ERROR]"
            elif trades < MIN_TRADES:              flag = "[INVALID]"
            elif trades < WARN_TRADES:             flag = "[LOW TRADES]"
            else:                                  flag = ""

            valid = flag not in ("[TIMEOUT]", "[DOCKER ERROR]") and trades >= MIN_TRADES

            record = {
                "run_id": run_id, "phase": PHASE, "params": params, "flag": flag,
                "sharpe": sharpe, "trades": trades, "net_profit": net_profit,
                "drawdown": drawdown, "win_rate": win_rate,
                "expectancy": expectancy, "avg_win": avg_win, "avg_loss": avg_loss,
            }

            # Save per-run log
            try:
                lp = os.path.join(RESULTS_DIR, f"run_p{PHASE}_{run_id:04d}.txt")
                with open(lp, "w", encoding="utf-8") as f:
                    f.write(f"Phase {PHASE}  Run {run_id}/{total}\n")
                    f.write(f"Flag   : {flag or 'OK'}\n")
                    f.write(f"Params : {json.dumps(params)}\n")
                    f.write(f"Fixed  : {json.dumps(fixed)}\n\n")
                    f.write("Statistics:\n")
                    for k, v in stats.items(): f.write(f"  {k}: {v}\n")
                    f.write(f"\n--- RAW output (last 10000 chars) ---\n{output[-10000:]}")
            except Exception as e:
                print(f"\n  WARNING: log write failed run {run_id}: {e}")

            with lock:
                completed += 1
                all_runs.append(record)
                if valid:
                    results.append(record)

            print(f"\r{' '*140}", end="")
            print(
                f"\r  [{run_id:>3}/{total}] {'OK' if valid else '--'}"
                f"  Sharpe={sharpe:>7.3f}  trades={trades:>4}"
                f"  profit={net_profit:>+7.2f}%  DD={drawdown:>5.1f}%"
                f"  WR={win_rate:>5.1f}%"
                + (f"  {flag}" if flag else "")
            )
            print(f"           {fmt_params(params)}")
            best_str = f"best={max(results, key=lambda x: x['sharpe'])['sharpe']:.3f}" if results else ""
            print_progress(best_str)

    print()

    n_ok      = sum(1 for r in all_runs if not r["flag"])
    n_low     = sum(1 for r in all_runs if r["flag"] == "[LOW TRADES]")
    n_invalid = sum(1 for r in all_runs if r["flag"] == "[INVALID]")
    n_error   = sum(1 for r in all_runs if "ERROR" in r["flag"] or "TIMEOUT" in r["flag"])

    print(f"\n{'='*72}")
    print(f"  Phase {PHASE} complete  |  {datetime.now().strftime('%H:%M:%S')}")
    print(f"  OK={n_ok}  Low-trade={n_low}  Invalid={n_invalid}  Errors={n_error}")
    print(f"{'='*72}\n")

    if not results:
        print("  No valid results. All below MIN_TRADES threshold.")
        return

    ranked = sorted(results, key=lambda x: x["sharpe"], reverse=True)

    print(f"  TOP 15  (Phase {PHASE}):\n")
    print(f"  {'#':<4} {'Sharpe':>7} {'Trades':>7} {'Profit%':>9} {'DD%':>6} {'WR%':>6} {'Expect':>8}  Params")
    print(f"  {'-'*120}")
    for i, r in enumerate(ranked[:15], 1):
        print(
            f"  {i:<4} {r['sharpe']:>7.3f} {r['trades']:>7} {r['net_profit']:>+9.2f} "
            f"{r['drawdown']:>6.1f} {r['win_rate']:>6.1f} {r['expectancy']:>+8.3f}  "
            f"{fmt_params(r['params'])}"
            + (f"  {r['flag']}" if r["flag"] else "")
        )

    best = ranked[0]
    print(f"\n{'='*72}")
    print(f"  BEST run #{best['run_id']}  — paste into PHASE{PHASE}_BEST:\n")
    print(f"  PHASE{PHASE}_BEST = {{")
    for k, v in best["params"].items():
        print(f"      \"{k}\": {repr(v)},")
    print(f"  }}\n")
    print(f"  Sharpe={best['sharpe']:.4f}  Profit={best['net_profit']:+.2f}%  DD={best['drawdown']:.1f}%  WR={best['win_rate']:.1f}%  Trades={best['trades']}")
    print(f"{'='*72}\n")

    print(f"  SENSITIVITY:\n")
    for key in grid.keys():
        groups = {}
        for r in results:
            v = r["params"][key]
            groups.setdefault(v, []).append(r["sharpe"])
        avg_by_val  = {v: sum(s)/len(s) for v, s in groups.items()}
        sorted_vals = sorted(avg_by_val.items(), key=lambda x: x[1], reverse=True)
        vmin = min(avg_by_val.values()); vmax = max(avg_by_val.values()); span = vmax - vmin
        impact = "HIGH IMPACT" if span > 0.15 else ("medium" if span > 0.05 else "low")
        print(f"  {key}  [{impact}  span={span:.3f}]:")
        for v, avg in sorted_vals:
            bar    = chr(9608) * max(0, int((avg - vmin) / max(0.001, span) * 30))
            marker = " <- best" if v == sorted_vals[0][0] else ""
            print(f"    {str(v):<10} n={len(groups[v]):>3}  avg_sharpe={avg:>7.3f}  {bar}{marker}")
        print()

    if PHASE < 4:
        print(f"  NEXT: copy PHASE{PHASE}_BEST above → set PHASE={PHASE+1} → run again\n")

    board_path = os.path.join(RESULTS_DIR, f"leaderboard_phase{PHASE}.json")
    all_path   = os.path.join(RESULTS_DIR, f"all_runs_phase{PHASE}.json")
    with open(board_path, "w", encoding="utf-8") as f: json.dump(ranked,   f, indent=2)
    with open(all_path,   "w", encoding="utf-8") as f: json.dump(all_runs, f, indent=2)
    print(f"  Leaderboard  -> {board_path}")
    print(f"  All runs     -> {all_path}")
    print(f"  Per-run logs -> {RESULTS_DIR}\n")


if __name__ == "__main__":
    main()