"""Walk-forward backtest of Kronos direction forecasts.

At each test point the model only sees candles up to that point, forecasts
`horizon` candles ahead, and the forecast is compared with what happened.
A simple strategy is simulated: go long when P(up) >= threshold, short when
P(up) <= 1 - threshold, hold for `horizon` candles, pay `fee` per side.

    python kronos_backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --timeframe 15 \
        --horizon 4 --step 12 --tests 120

Results are appended per test point to a CSV, so an interrupted run can resume.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

import kronos_forecast as kf

COLS = ["open", "high", "low", "close", "volume", "amount"]


def backtest_symbol(symbol, timeframe, horizon, step, tests, context, samples, out_csv,
                    batch_points=8, model_size="small"):
    predictor = kf.load_predictor(model_size)
    need = context + (tests - 1) * step + horizon
    df = kf.fetch_candles(symbol, timeframe, need)
    starts = [context + i * step for i in range(tests) if context + i * step + horizon <= len(df)]

    done = set()
    if out_csv.exists():
        prev = pd.read_csv(out_csv)
        done = set(prev.loc[prev["symbol"] == symbol, "t"].astype(str))
    starts = [s for s in starts if str(df["timestamps"].iloc[s - 1]) not in done]

    for j in range(0, len(starts), batch_points):
        chunk = starts[j:j + batch_points]
        dfl, xl, yl = [], [], []
        for s in chunk:
            h, f = df.iloc[s - context:s], df.iloc[s:s + horizon]
            for _ in range(samples):
                dfl.append(h[COLS].reset_index(drop=True))
                xl.append(h["timestamps"].reset_index(drop=True))
                yl.append(f["timestamps"].reset_index(drop=True))
        out = predictor.predict_batch(dfl, xl, yl, pred_len=horizon, T=1.0, top_p=0.9,
                                      sample_count=1, verbose=False)
        rows = []
        for n, s in enumerate(chunk):
            last = float(df["close"].iloc[s - 1])
            actual = float(df["close"].iloc[s + horizon - 1])
            finals = np.array([o["close"].iloc[-1] for o in out[n * samples:(n + 1) * samples]])
            rows.append({
                "symbol": symbol, "timeframe": timeframe, "horizon": horizon,
                "t": str(df["timestamps"].iloc[s - 1]), "last": last, "actual": actual,
                "pred_median": float(np.median(finals)), "prob_up": float((finals > last).mean()),
            })
        pd.DataFrame(rows).to_csv(out_csv, mode="a", header=not out_csv.exists(), index=False)


def summarize(res: pd.DataFrame, threshold: float = 0.7, fee_pct: float = 0.04) -> pd.DataFrame:
    """Per-symbol stats. fee_pct is per side (Binance USDⓈ-M taker ≈ 0.04-0.05%)."""
    res = res.copy()
    res["ret"] = (res["actual"] / res["last"] - 1) * 100
    res["pred_ret"] = (res["pred_median"] / res["last"] - 1) * 100
    res["hit"] = np.sign(res["ret"]) == np.sign(res["pred_ret"])
    res["pos"] = np.where(res["prob_up"] >= threshold, 1, np.where(res["prob_up"] <= 1 - threshold, -1, 0))
    res["trade_pnl"] = np.where(res["pos"] != 0, res["pos"] * res["ret"] - 2 * fee_pct, 0.0)

    def stats(g):
        tr = g[g["pos"] != 0]
        n = len(g)
        hit = g["hit"].mean()
        # binomial z-score of hit rate vs 50%
        z = (hit - 0.5) / np.sqrt(0.25 / n) if n else np.nan
        return pd.Series({
            "tests": n,
            "hit_rate": hit,
            "z_vs_50": z,
            "up_share": (g["ret"] > 0).mean(),
            "corr": np.corrcoef(g["ret"], g["pred_ret"])[0, 1] if n > 2 else np.nan,
            "trades": len(tr),
            "trade_hit": (np.sign(tr["ret"]) == tr["pos"]).mean() if len(tr) else np.nan,
            "pnl_gross_%": (tr["pos"] * tr["ret"]).sum(),
            "pnl_net_%": tr["trade_pnl"].sum(),
            "buy_hold_%": g["ret"].sum(),
        })

    board = res.groupby("symbol").apply(stats, include_groups=False)
    board.loc["ALL"] = stats(res)
    return board


def main():
    ap = argparse.ArgumentParser(description="Walk-forward Kronos backtest")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--timeframe", default="15", choices=list(kf.TIMEFRAMES))
    ap.add_argument("--horizon", type=int, default=4)
    ap.add_argument("--step", type=int, default=12, help="Candles between test points")
    ap.add_argument("--tests", type=int, default=120, help="Test points per symbol")
    ap.add_argument("--context", type=int, default=400)
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--threshold", type=float, default=0.7, help="P(up) needed to trade")
    ap.add_argument("--fee", type=float, default=0.04, help="Fee per side in percent")
    ap.add_argument("--out", default="kronos_forecasts/backtest.csv")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        t0 = time.time()
        backtest_symbol(sym, args.timeframe, args.horizon, args.step, args.tests,
                        args.context, args.samples, out)
        print(f"{sym} done in {time.time() - t0:.0f}s", flush=True)

    res = pd.read_csv(out)
    res = res[(res["timeframe"].astype(str) == args.timeframe) & (res["horizon"] == args.horizon)]
    print(f"\n=== Kronos backtest · TF {args.timeframe} · horizon {args.horizon} · "
          f"threshold {args.threshold} · fee {args.fee}%/side ===")
    print(f"period: {res['t'].min()} .. {res['t'].max()}")
    print(summarize(res, args.threshold, args.fee).round(3).to_string())


if __name__ == "__main__":
    main()
