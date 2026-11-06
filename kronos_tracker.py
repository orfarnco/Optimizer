"""Live forward-test of Kronos on several 24/7 assets.

Every candle close it forecasts each symbol, logs the prediction, and once the
horizon has passed scores it against what actually happened. Because the
predictions are logged before the outcome exists, the scoreboard is an honest
out-of-sample test of whether Kronos has a directional signal.

    python kronos_tracker.py --symbols BTCUSDT.P ETHUSDT.P SOLUSDT.P --timeframe 15 --horizon 4
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import kronos_forecast as kf

DEFAULT_SYMBOLS = ["BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P", "XRPUSDT.P", "BNBUSDT.P", "DOGEUSDT.P"]
COLS = ["open", "high", "low", "close", "volume", "amount"]


def forecast_all(symbols, timeframe, horizon, lookback, samples, model_size="small"):
    """One batched Kronos run over all symbols. Returns a list of prediction rows."""
    predictor = kf.load_predictor(model_size)
    lookback = min(lookback, predictor.max_context)
    hists = {}
    for sym in symbols:
        try:
            hists[sym] = kf.fetch_candles(sym, timeframe, lookback)
        except Exception as exc:
            print(f"  ! {sym}: {exc}")
    df_l, x_l, y_l, order = [], [], [], []
    for sym, h in hists.items():
        y = kf.future_timestamps(h["timestamps"], h.attrs["freq"], horizon, continuous=True)
        for _ in range(samples):
            df_l.append(h[COLS]); x_l.append(h["timestamps"]); y_l.append(y)
        order.append((sym, h, y))
    out = predictor.predict_batch(df_l, x_l, y_l, pred_len=horizon, T=1.0, top_p=0.9,
                                  sample_count=1, verbose=False)
    rows = []
    for n, (sym, h, y) in enumerate(order):
        finals = np.array([o["close"].iloc[-1] for o in out[n * samples:(n + 1) * samples]])
        last = float(h["close"].iloc[-1])
        median = float(np.median(finals))
        rows.append({
            "made_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym, "timeframe": timeframe, "horizon": horizon,
            "last_bar": h["timestamps"].iloc[-1], "target_bar": y.iloc[-1],
            "last_close": last, "pred_median": median,
            "pred_ret_pct": (median / last - 1) * 100,
            "p10": float(np.percentile(finals, 10)), "p90": float(np.percentile(finals, 90)),
            "prob_up": float((finals > last).mean()),
            "actual_close": np.nan, "actual_ret_pct": np.nan, "hit": np.nan,
        })
    return rows


def score_pending(log: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Fill actual outcomes for predictions whose target candle has closed."""
    pending = log[log["actual_close"].isna()]
    for sym in pending["symbol"].unique():
        try:
            h = kf.fetch_candles(sym, timeframe, 300)
        except Exception:
            continue
        closes = dict(zip(pd.to_datetime(h["timestamps"]), h["close"]))
        for i in pending.index[pending["symbol"] == sym]:
            target = pd.Timestamp(log.at[i, "target_bar"])
            if target in closes:
                actual = float(closes[target])
                last = log.at[i, "last_close"]
                log.at[i, "actual_close"] = actual
                log.at[i, "actual_ret_pct"] = (actual / last - 1) * 100
                log.at[i, "hit"] = float(np.sign(actual - last) == np.sign(log.at[i, "pred_median"] - last))
    return log


def scoreboard(log: pd.DataFrame) -> pd.DataFrame:
    done = log.dropna(subset=["hit"])
    if done.empty:
        return pd.DataFrame()

    def stats(g):
        strong = g[(g["prob_up"] >= 0.7) | (g["prob_up"] <= 0.3)]
        return pd.Series({
            "n": len(g),
            "hit_rate": g["hit"].mean(),
            "majority_baseline": max((g["actual_ret_pct"] > 0).mean(), (g["actual_ret_pct"] < 0).mean()),
            "strong_n": len(strong),
            "strong_hit": strong["hit"].mean() if len(strong) else np.nan,
            "dir_return_pct": (np.sign(g["pred_ret_pct"]) * g["actual_ret_pct"]).sum(),
        })

    board = done.groupby("symbol").apply(stats, include_groups=False)
    board.loc["ALL"] = stats(done)
    return board


def seconds_to_next_close(timeframe: str, delay: int = 5) -> float:
    step = pd.Timedelta(kf.TIMEFRAMES[timeframe][2]).total_seconds()
    now = time.time()
    return step - (now % step) + delay


def main():
    ap = argparse.ArgumentParser(description="Live Kronos forward-test on several assets")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--timeframe", default="15", choices=list(kf.TIMEFRAMES))
    ap.add_argument("--horizon", type=int, default=4)
    ap.add_argument("--lookback", type=int, default=400)
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--log", default="kronos_forecasts/tracker_log.csv")
    ap.add_argument("--cycles", type=int, default=0, help="Stop after N cycles (0 = run forever)")
    args = ap.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = pd.read_csv(log_path) if log_path.exists() else pd.DataFrame()
    cycle = 0
    while True:
        cycle += 1
        t0 = time.time()
        rows = forecast_all(args.symbols, args.timeframe, args.horizon, args.lookback, args.samples)
        log = pd.concat([log, pd.DataFrame(rows)], ignore_index=True)
        log = score_pending(log, args.timeframe)
        log.to_csv(log_path, index=False)

        print(f"\n=== cycle {cycle} · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · {time.time() - t0:.0f}s ===")
        for r in rows:
            arrow = "▲" if r["pred_ret_pct"] > 0 else "▼"
            print(f"  {r['symbol']:<11} {r['last_close']:>12,.4f} → {r['pred_median']:>12,.4f} "
                  f"{arrow} {r['pred_ret_pct']:+.3f}%  P(up) {r['prob_up']:.0%}  (target {r['target_bar']})")
        board = scoreboard(log)
        if not board.empty:
            print("  --- scoreboard (scored predictions) ---")
            print(board.round(3).to_string())
        if args.cycles and cycle >= args.cycles:
            break
        time.sleep(seconds_to_next_close(args.timeframe))


if __name__ == "__main__":
    main()
