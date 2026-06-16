"""E1 harness — does COMPRESSION -> breakout-ACCEPTANCE precede a big move?

Causal, completed-bars-only. For each day (snapshots_ml_flat_v2, per-minute):
  1. compute compression features from OHLC (BB width, ATR & trend, range
     contraction, EMA9/20/50 spacing) — all trailing/causal.
  2. compression_score>=3 -> COMPRESSED state.
  3. ACCEPTANCE setup: recent compression + breakout close beyond the prior-10-bar
     range + the NEXT bar holds (doesn't return inside). Confirmed at j=i+1, dir=+/-1.
  4. measure forward N-bar move from j; HIT if |move|>=X pts. Base rate = same for
     ALL in-window bars. Lift = setup_hit / base_hit. Bonus: direction accuracy.

Prints a line PER MONTH as completed (flushed) so partial results are accessible,
then per-quarter + walk-forward (2020-2023 vs 2024) + the E1 verdict.

Run:  python3 compression_harness.py --root ~/parquet_data
"""
import argparse, glob, os, re, sys
import numpy as np, pandas as pd
from collections import defaultdict

WINDOW = ("09:45", "15:00")     # trade window (in-window only)
N = 10                          # forward horizon (bars/min)
XS = [100.0, 150.0]             # big-move thresholds (pts)
RANGE_LOOKBACK = 10             # breakout range = prior 10 bars
COMP_RECENT = 5                 # compression must occur within last 5 bars

def _ser(df, col):
    return pd.to_numeric(df[col], errors="coerce") if col in df.columns else pd.Series([np.nan]*len(df))

def features(df):
    df = df.copy()
    df["_ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["_ts"]).sort_values("_ts").reset_index(drop=True)
    c = _ser(df, "px_fut_close").values.astype(float)
    h = _ser(df, "px_fut_high").values.astype(float)
    l = _ser(df, "px_fut_low").values.astype(float)
    n = len(c)
    if n < 60:
        return None
    s = pd.Series(c)
    ema9  = s.ewm(span=9,  adjust=False).mean().values
    ema20 = s.ewm(span=20, adjust=False).mean().values
    sma20 = s.rolling(20).mean()
    std20 = s.rolling(20).std()
    bbw = (2.0 * std20 / sma20.replace(0, np.nan)).values          # bollinger width fraction
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).ewm(alpha=1/14, adjust=False).mean().values
    bbw_avg = pd.Series(bbw).rolling(20).mean().shift(1).values     # causal: prior-20 avg
    atr_avg = pd.Series(atr).rolling(20).mean().shift(1).values
    rng = h - l
    r10 = pd.Series(rng).rolling(10).mean().values
    r30 = pd.Series(rng).rolling(30).mean().values
    hm = df["_ts"].dt.strftime("%H:%M").values
    return dict(c=c, h=h, l=l, ema9=ema9, ema20=ema20, bbw=bbw, bbw_avg=bbw_avg,
                atr=atr, atr_avg=atr_avg, r10=r10, r30=r30, hm=hm, n=n)

def comp_score(f, i):
    sc = 0
    if f["bbw"][i] < f["bbw_avg"][i]: sc += 1
    if f["atr"][i] < f["atr_avg"][i]: sc += 1
    if f["r30"][i] and not np.isnan(f["r30"][i]) and f["r10"][i] < 0.6*f["r30"][i]: sc += 1
    if f["c"][i] and abs(f["ema9"][i]-f["ema20"][i])/f["c"][i] < 0.0008: sc += 1
    return sc

def day_stats(df):
    f = features(df)
    if f is None: return None
    n = f["n"]; c=f["c"]; h=f["h"]; l=f["l"]; hm=f["hm"]
    inwin = np.array([WINDOW[0] <= t <= WINDOW[1] for t in hm])
    compressed = np.array([comp_score(f, i) >= 3 if i >= 30 else False for i in range(n)])
    # forward N-bar abs move per bar
    fwd = np.full(n, np.nan); fwd_signed = np.full(n, np.nan)
    for j in range(n - N):
        if not np.isnan(c[j]):
            seg = c[j+1:j+1+N]
            if len(seg) == N and not np.isnan(seg).any():
                fwd[j] = np.max(np.abs(seg - c[j]))
                fwd_signed[j] = c[j+N] - c[j]
    out = {"base_bars":0, "base_hit":{x:0 for x in XS}, "setups":0,
           "setup_hit":{x:0 for x in XS}, "dir_ok":0, "dir_tot":0}
    # base rate: all in-window bars with a forward move defined
    for j in range(n - N):
        if inwin[j] and not np.isnan(fwd[j]):
            out["base_bars"] += 1
            for x in XS:
                if fwd[j] >= x: out["base_hit"][x] += 1
    # setups: compression in last COMP_RECENT bars + breakout close beyond prior-10 range + next bar holds
    for i in range(RANGE_LOOKBACK+1, n - N - 1):
        if not inwin[i]: continue
        recent_comp = compressed[max(0,i-COMP_RECENT):i+1].any()
        if not recent_comp: continue
        rh = np.max(h[i-RANGE_LOOKBACK:i]); rl = np.min(l[i-RANGE_LOOKBACK:i])
        d = 0
        if c[i] > rh and l[i+1] >= rh: d = 1       # bull: close above + next bar holds above
        elif c[i] < rl and h[i+1] <= rl: d = -1    # bear: close below + next bar holds below
        if d == 0: continue
        j = i + 1                                   # confirmed bar
        if np.isnan(fwd[j]): continue
        out["setups"] += 1
        for x in XS:
            if fwd[j] >= x: out["setup_hit"][x] += 1
        if not np.isnan(fwd_signed[j]):
            out["dir_tot"] += 1
            if (fwd_signed[j] > 0 and d > 0) or (fwd_signed[j] < 0 and d < 0): out["dir_ok"] += 1
    return out

def add(agg, s):
    agg["base_bars"] += s["base_bars"]; agg["setups"] += s["setups"]
    agg["dir_ok"] += s["dir_ok"]; agg["dir_tot"] += s["dir_tot"]
    for x in XS:
        agg["base_hit"][x] += s["base_hit"][x]; agg["setup_hit"][x] += s["setup_hit"][x]

def new_agg():
    return {"base_bars":0,"setups":0,"dir_ok":0,"dir_tot":0,
            "base_hit":{x:0 for x in XS},"setup_hit":{x:0 for x in XS}}

def report(label, a):
    if a["base_bars"] == 0: return f"{label}: no data"
    parts = [f"{label:14} days_bars={a['base_bars']:6d} setups={a['setups']:5d}"]
    for x in XS:
        base = a["base_hit"][x]/a["base_bars"]
        setup = a["setup_hit"][x]/a["setups"] if a["setups"] else 0.0
        lift = (setup/base) if base>0 else 0.0
        parts.append(f"|{int(x)}pt base={base*100:4.1f}% setup={setup*100:4.1f}% lift={lift:4.2f}")
    diracc = a["dir_ok"]/a["dir_tot"]*100 if a["dir_tot"] else 0.0
    parts.append(f"| dir={diracc:4.1f}%(n={a['dir_tot']})")
    return " ".join(parts)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--root", required=True)
    a = ap.parse_args()
    files = sorted(glob.glob(f"{a.root}/snapshots_ml_flat_v2/**/*.parquet", recursive=True))
    print(f"files={len(files)}  window={WINDOW}  N={N}  X={XS}  (compression_score>=3, acceptance=close-beyond+next-bar-holds)", flush=True)
    print("=== PER-MONTH (incremental) ===", flush=True)
    by_month = defaultdict(new_agg); by_q = defaultdict(new_agg)
    wf = {"train(2020-2023)":new_agg(), "test(2024)":new_agg()}
    overall = new_agg()
    cur_month = None
    for fp in files:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", os.path.basename(fp))
        if not m: continue
        y, mo = m.group(1), m.group(2); ym = f"{y}-{mo}"; q = f"{y}Q{(int(mo)-1)//3+1}"
        if cur_month is not None and ym != cur_month:
            print("  " + report(cur_month, by_month[cur_month]), flush=True)
        cur_month = ym
        try:
            df = pd.read_parquet(fp)
            s = day_stats(df)
        except Exception:
            continue
        if s is None: continue
        for tgt in (by_month[ym], by_q[q], overall):
            add(tgt, s)
        add(wf["train(2020-2023)"] if y < "2024" else wf["test(2024)"], s)
    if cur_month is not None:
        print("  " + report(cur_month, by_month[cur_month]), flush=True)
    print("\n=== PER-QUARTER ===", flush=True)
    for q in sorted(by_q): print("  " + report(q, by_q[q]), flush=True)
    print("\n=== WALK-FORWARD ===", flush=True)
    for k in ("train(2020-2023)", "test(2024)"): print("  " + report(k, wf[k]), flush=True)
    print("\n=== OVERALL ===", flush=True)
    print("  " + report("ALL", overall), flush=True)
    print("\n=== E1 VERDICT (read the 2024 + per-quarter lift; >=1.5x stable = alive) ===", flush=True)

if __name__ == "__main__":
    main()
