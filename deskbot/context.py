"""Free market context for the News and Sentiment agents. Every source is optional:
a failed fetch returns None/[] and the desk works with what it has."""

from __future__ import annotations

import email.utils
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
}
COIN_NAMES = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin", "bnb chain"], "XRP": ["xrp", "ripple"], "ADA": ["cardano", "ada"],
    "DOGE": ["dogecoin", "doge"], "AVAX": ["avalanche", "avax"], "LINK": ["chainlink", "link"],
    "DOT": ["polkadot", "dot"],
}
MACRO_WORDS = ["fed", "fomc", "rate", "inflation", "cpi", "etf", "sec", "tariff", "recession", "treasury"]
UA = {"User-Agent": "Mozilla/5.0 (ai-desk-bot research)"}


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return email.utils.parsedate_to_datetime(text).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_headlines(max_age_hours: int = 72) -> list[dict]:
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for source, url in RSS_FEEDS.items():
        try:
            r = requests.get(url, headers=UA, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError):
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            when = _parse_date(it.findtext("pubDate"))
            if not title or (when and when < cutoff):
                continue
            desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
            items.append({"source": source, "title": title, "url": (it.findtext("link") or "").strip(),
                          "published": when.isoformat() if when else None,
                          "summary": re.sub(r"\s+", " ", desc).strip()[:280]})
    items.sort(key=lambda x: x["published"] or "", reverse=True)
    return items


def headlines_for(coin: str, headlines: list[dict], limit: int = 8) -> tuple[list[dict], list[dict]]:
    """(coin-specific headlines, macro headlines)"""
    words = COIN_NAMES.get(coin, [coin.lower()])

    def hits(h, vocab):
        text = f" {h['title']} {h['summary']} ".lower()
        return any(re.search(rf"\b{re.escape(w)}\b", text) for w in vocab)

    coin_items = [h for h in headlines if hits(h, words)][:limit]
    macro = [h for h in headlines if hits(h, MACRO_WORDS) and h not in coin_items][:5]
    return coin_items, macro


def fear_greed() -> dict | None:
    try:
        data = requests.get("https://api.alternative.me/fng/?limit=7", timeout=15).json()["data"]
        return {"today": int(data[0]["value"]), "label": data[0]["value_classification"],
                "week": [int(d["value"]) for d in data]}
    except (requests.RequestException, KeyError, ValueError, IndexError):
        return None


def derivatives(symbol: str) -> dict | None:
    """Funding rate and open interest trend from Binance futures (may be unavailable in some regions)."""
    try:
        prem = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                            params={"symbol": symbol}, timeout=15).json()
        oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                          params={"symbol": symbol, "period": "1d", "limit": 8}, timeout=15).json()
        out = {"funding_rate_8h": float(prem["lastFundingRate"])}
        if isinstance(oi, list) and len(oi) >= 2:
            first, last = float(oi[0]["sumOpenInterestValue"]), float(oi[-1]["sumOpenInterestValue"])
            out["open_interest_change_7d"] = last / first - 1 if first else None
        return out
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None
