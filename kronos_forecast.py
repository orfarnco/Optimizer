"""Price forecasts with the Kronos foundation model.

Kronos (https://github.com/shiyu-coder/Kronos, MIT) is a pre-trained model for
financial candlestick (OHLCV) data. This module downloads recent candles from
Yahoo Finance, runs several Kronos sample paths and summarises them into a
median forecast with an uncertainty band.

CLI usage:
    python kronos_forecast.py --symbol NQ1! --timeframe 15 --horizon 32
"""

from __future__ import annotations

import argparse
import functools
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

MODELS = {
    "mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k", 2048),
    "small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base", 512),
    "base": ("NeoQuasar/Kronos-base", "NeoQuasar/Kronos-Tokenizer-base", 512),
}

# TradingView timeframe -> (yfinance interval, max history period, pandas freq)
TIMEFRAMES = {
    "1": ("1m", "7d", "1min"),
    "5": ("5m", "60d", "5min"),
    "15": ("15m", "60d", "15min"),
    "30": ("30m", "60d", "30min"),
    "60": ("60m", "730d", "60min"),
    "240": ("60m", "730d", "240min"),  # resampled from 1h candles
    "1D": ("1d", "10y", "1D"),
    "1W": ("1wk", "max", "7D"),
}

# Common TradingView symbols -> Yahoo Finance tickers
SYMBOL_ALIASES = {
    "NQ1!": "NQ=F", "MNQ1!": "MNQ=F", "ES1!": "ES=F", "MES1!": "MES=F",
    "YM1!": "YM=F", "RTY1!": "RTY=F", "CL1!": "CL=F", "GC1!": "GC=F",
    "SI1!": "SI=F", "NG1!": "NG=F", "ZB1!": "ZB=F", "ZN1!": "ZN=F",
    "6E1!": "6E=F", "BTC1!": "BTC=F",
    "SPX": "^GSPC", "NDX": "^NDX", "DJI": "^DJI", "VIX": "^VIX",
}
FOREX = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"}
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "LTC"}


def to_yahoo_symbol(symbol: str) -> str:
    """Map a TradingView-style symbol (e.g. NQ1!, EURUSD, BTCUSD) to Yahoo Finance."""
    s = symbol.strip().upper()
    if ":" in s:  # drop exchange prefix, e.g. CME_MINI:NQ1!
        s = s.split(":", 1)[1]
    if s in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[s]
    if s.endswith("1!"):
        return s[:-2] + "=F"
    if len(s) == 6 and s[:3] in FOREX and s[3:] in FOREX:
        return f"{s}=X"
    for quote in ("USDT", "USD"):
        if s.endswith(quote) and s[: -len(quote)] in CRYPTO:
            return f"{s[: -len(quote)]}-USD"
    return s


def fetch_candles(symbol: str, timeframe: str, lookback: int) -> pd.DataFrame:
    """Download the latest `lookback` closed candles as a DataFrame with a
    `timestamps` column and open/high/low/close/volume/amount columns."""
    import yfinance as yf

    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {list(TIMEFRAMES)}")
    interval, period, freq = TIMEFRAMES[timeframe]
    ticker = to_yahoo_symbol(symbol)
    raw = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
    if raw is None or raw.empty:
        raise ValueError(f"No data returned from Yahoo Finance for {ticker!r}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].copy()
    if timeframe == "240":
        df = df.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        )
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0).astype(float)

    # The last intraday bar is usually still forming; drop it so the model sees closed candles.
    if timeframe not in ("1D", "1W") and len(df) > 1:
        df = df.iloc[:-1]

    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    df.index = idx
    slots = trading_slots(df.index, timeframe)
    df = df.tail(lookback)
    df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
    df = df.reset_index(names="timestamps")
    df.attrs.update(ticker=ticker, freq=freq, slots=slots)
    return df


def _slot_key(t: pd.Timestamp, timeframe: str):
    if timeframe == "1W":
        return None
    if timeframe == "1D":
        return t.weekday()
    return (t.weekday(), t.hour, t.minute)


def trading_slots(index: pd.DatetimeIndex, timeframe: str) -> frozenset:
    """Set of (weekday, hour, minute) slots in which the instrument actually traded,
    so forecast timestamps skip closed sessions (nights, weekends, daily breaks)."""
    return frozenset(_slot_key(t, timeframe) for t in index)


def future_timestamps(history: pd.Series, freq: str, horizon: int,
                      slots: frozenset | None = None, timeframe: str = "") -> pd.Series:
    """Timestamps for the next `horizon` candles. With `slots`, only trading-session
    slots seen in the history are used; otherwise weekends are skipped."""
    step = pd.Timedelta(freq)
    use_slots = bool(slots) and timeframe != "1W"

    def is_open(t):
        if use_slots:
            return _slot_key(t, timeframe) in slots
        return freq == "7D" or t.weekday() < 5

    stamps, t = [], pd.Timestamp(history.iloc[-1])
    for _ in range(horizon * 2000):  # safety bound
        if len(stamps) == horizon:
            break
        t = t + step
        if is_open(t):
            stamps.append(t)
    if len(stamps) < horizon:  # slots too sparse; fall back to plain stepping
        return future_timestamps(history, freq, horizon)
    return pd.Series(stamps, name="timestamps")


@functools.lru_cache(maxsize=3)
def load_predictor(model_size: str = "small", device: str | None = None):
    from kronos_model import Kronos, KronosPredictor, KronosTokenizer

    model_id, tokenizer_id, max_context = MODELS[model_size]
    tokenizer = KronosTokenizer.from_pretrained(tokenizer_id)
    model = Kronos.from_pretrained(model_id)
    return KronosPredictor(model, tokenizer, device=device, max_context=max_context)


@dataclass
class ForecastResult:
    symbol: str
    ticker: str
    timeframe: str
    model_size: str
    history: pd.DataFrame        # timestamps + OHLCV used as context
    paths: np.ndarray            # (samples, horizon) predicted closes
    forecast: pd.DataFrame       # timestamps, median/mean/p10/p90 close, mean OHLC
    created_at: datetime

    @property
    def last_close(self) -> float:
        return float(self.history["close"].iloc[-1])

    def summary(self) -> dict:
        final = self.paths[:, -1]
        last = self.last_close
        median = float(np.median(final))
        return {
            "symbol": self.symbol,
            "ticker": self.ticker,
            "timeframe": self.timeframe,
            "model": f"Kronos-{self.model_size}",
            "last_bar": str(self.history["timestamps"].iloc[-1]),
            "last_close": last,
            "horizon_bars": int(self.paths.shape[1]),
            "target_time": str(self.forecast["timestamps"].iloc[-1]),
            "median_close": median,
            "change_pct": (median / last - 1) * 100,
            "p10_close": float(np.percentile(final, 10)),
            "p90_close": float(np.percentile(final, 90)),
            "prob_up": float((final > last).mean()),
            "samples": int(self.paths.shape[0]),
            "direction": "UP" if median > last else "DOWN",
        }


def run_forecast(
    symbol: str = "NQ1!",
    timeframe: str = "15",
    horizon: int = 32,
    lookback: int = 400,
    samples: int = 20,
    model_size: str = "small",
    temperature: float = 1.0,
    top_p: float = 0.9,
    seed: int | None = 42,
    history: pd.DataFrame | None = None,
    verbose: bool = False,
) -> ForecastResult:
    """Forecast the next `horizon` candles. Each of the `samples` paths is an
    independent Kronos sample; the spread between them is the uncertainty band."""
    import torch

    if seed is not None:
        torch.manual_seed(seed)
        np.random.seed(seed)

    predictor = load_predictor(model_size)
    lookback = min(lookback, predictor.max_context)
    if history is None:
        history = fetch_candles(symbol, timeframe, lookback)
    else:
        history = history.tail(lookback).reset_index(drop=True)
    if len(history) < 50:
        raise ValueError(f"Only {len(history)} candles available; need at least 50")

    freq = history.attrs.get("freq") or TIMEFRAMES[timeframe][2]
    x_df = history[["open", "high", "low", "close", "volume", "amount"]]
    x_ts = history["timestamps"]
    y_ts = future_timestamps(x_ts, freq, horizon, history.attrs.get("slots"), timeframe)

    preds = predictor.predict_batch(
        df_list=[x_df] * samples,
        x_timestamp_list=[x_ts] * samples,
        y_timestamp_list=[y_ts] * samples,
        pred_len=horizon,
        T=temperature,
        top_p=top_p,
        sample_count=1,
        verbose=verbose,
    )
    paths = np.stack([p["close"].to_numpy() for p in preds])
    mean_ohlc = sum(p[["open", "high", "low", "close", "volume"]].to_numpy() for p in preds) / samples

    forecast = pd.DataFrame({
        "timestamps": y_ts.to_numpy(),
        "median_close": np.median(paths, axis=0),
        "mean_close": paths.mean(axis=0),
        "p10_close": np.percentile(paths, 10, axis=0),
        "p90_close": np.percentile(paths, 90, axis=0),
        "mean_open": mean_ohlc[:, 0],
        "mean_high": mean_ohlc[:, 1],
        "mean_low": mean_ohlc[:, 2],
        "mean_volume": mean_ohlc[:, 4],
    })
    return ForecastResult(
        symbol=symbol,
        ticker=history.attrs.get("ticker", to_yahoo_symbol(symbol)),
        timeframe=timeframe,
        model_size=model_size,
        history=history,
        paths=paths,
        forecast=forecast,
        created_at=datetime.now(),
    )


def plot_forecast(result: ForecastResult, context_bars: int = 120):
    """Matplotlib figure: recent closes, median forecast and P10-P90 band."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hist = result.history.tail(context_bars).reset_index(drop=True)
    fc = result.forecast
    n_hist, n_fc = len(hist), len(fc)
    # Plot by bar index (like TradingView) so closed sessions/weekends leave no gaps.
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(range(n_hist), hist["close"], color="#1f77b4", lw=1.4, label="History (close)")
    x = list(range(n_hist - 1, n_hist + n_fc))
    last = result.last_close
    ax.fill_between(x, [last, *fc["p10_close"]], [last, *fc["p90_close"]],
                    color="#ff7f0e", alpha=0.2, label="P10–P90")
    ax.plot(x, [last, *fc["median_close"]], color="#d62728", lw=1.8, label="Median forecast")
    ax.axvline(n_hist - 1, color="gray", ls="--", lw=0.8)
    stamps = list(hist["timestamps"]) + list(fc["timestamps"])
    ticks = np.linspace(0, len(stamps) - 1, 8).round().astype(int)
    fmt = "%m-%d" if result.timeframe in ("1D", "1W") else "%m-%d %H:%M"
    ax.set_xticks(ticks)
    ax.set_xticklabels([pd.Timestamp(stamps[i]).strftime(fmt) for i in ticks])
    s = result.summary()
    ax.set_title(f"Kronos forecast · {result.symbol} ({result.ticker}) · TF {result.timeframe} · "
                 f"{s['direction']} {s['change_pct']:+.2f}% · P(up) {s['prob_up']:.0%}")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left")
    ax.set_xlim(0, len(stamps) - 1)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def main():
    parser = argparse.ArgumentParser(description="Kronos price forecast")
    parser.add_argument("--symbol", default="NQ1!")
    parser.add_argument("--timeframe", default="15", choices=list(TIMEFRAMES))
    parser.add_argument("--horizon", type=int, default=32, help="Candles to forecast")
    parser.add_argument("--lookback", type=int, default=400, help="Context candles (max 512 for small/base)")
    parser.add_argument("--samples", type=int, default=20, help="Number of sampled paths")
    parser.add_argument("--model", default="small", choices=list(MODELS))
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="kronos_forecasts", help="Output folder for CSV/PNG")
    args = parser.parse_args()

    result = run_forecast(args.symbol, args.timeframe, args.horizon, args.lookback, args.samples,
                          args.model, args.temperature, args.top_p, args.seed, verbose=True)
    s = result.summary()
    print("\n=== Kronos forecast ===")
    for k, v in s.items():
        print(f"{k:>14}: {v:.4f}" if isinstance(v, float) else f"{k:>14}: {v}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"kronos_{to_yahoo_symbol(args.symbol).replace('=', '_').replace('^', '')}_{args.timeframe}_{result.created_at:%Y%m%d_%H%M}"
    result.forecast.to_csv(out / f"{stem}.csv", index=False)
    plot_forecast(result).savefig(out / f"{stem}.png", dpi=130)
    print(f"\nSaved {out / (stem + '.csv')} and {out / (stem + '.png')}")


if __name__ == "__main__":
    main()
