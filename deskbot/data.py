"""Free Binance market data (public mirror, no API key) with a CSV cache."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from .config import DATA_DIR

KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/price"
COLUMNS = ["open", "high", "low", "close", "volume"]


def _get(url: str, params: dict, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def fetch_klines(symbol: str, interval: str, start: str | pd.Timestamp) -> pd.DataFrame:
    """Completed candles only: the still-open candle is dropped."""
    start_ms = int(pd.Timestamp(start).tz_localize(None).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    rows = []
    while True:
        batch = _get(KLINES_URL, {"symbol": symbol, "interval": interval,
                                  "startTime": start_ms, "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        start_ms = batch[-1][0] + 1
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "base_volume",
                                     "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "_"])
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = df[df["close_time"] < now_ms]
    out = pd.DataFrame({
        "open": df["open"].astype(float).values,
        "high": df["high"].astype(float).values,
        "low": df["low"].astype(float).values,
        "close": df["close"].astype(float).values,
        # quote volume (USDT) keeps volume comparable across coins
        "volume": df["quote_volume"].astype(float).values,
    }, index=pd.to_datetime(df["open_time"].values, unit="ms"))
    out.index.name = "date"
    return out


def load(symbol: str, interval: str = "1d", start: str = "2019-01-01", refresh: bool = True) -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{symbol}_{interval}.csv"
    cached = pd.read_csv(path, index_col="date", parse_dates=True) if path.exists() else None
    if cached is not None and not refresh:
        return cached
    if cached is not None and len(cached):
        fresh = fetch_klines(symbol, interval, cached.index[-1])
        df = pd.concat([cached[cached.index < cached.index[-1]], fresh])
    else:
        df = fetch_klines(symbol, interval, start)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(path)
    return df


def load_universe(symbols, interval="1d", start="2019-01-01", refresh=True) -> dict[str, pd.DataFrame]:
    return {s: load(s, interval, start, refresh) for s in symbols}


def last_prices(symbols) -> dict[str, float]:
    wanted = set(symbols)
    try:  # only the coins we need (the full list is thousands of tickers and takes seconds)
        data = _get(TICKER_URL, {"symbols": json.dumps(sorted(wanted), separators=(",", ":"))}, retries=1)
    except requests.RequestException:  # e.g. one symbol delisted: fall back to the full list
        data = _get(TICKER_URL, {})
    return {d["symbol"]: float(d["price"]) for d in data if d["symbol"] in wanted}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
