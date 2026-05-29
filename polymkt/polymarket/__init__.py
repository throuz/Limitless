"""Polymarket 子套件 — 讀取分析 / Oracle 資料源。

設計定位:
- 台灣 close-only 限制 → 我們**只讀** Polymarket,不下單
- 用途:當作 Limitless 做市的「公平價 oracle」(`make-market --oracle pm`)
- 用途:鯨魚追蹤(Polymarket data API 抓全平台交易 + 高 ROI 錢包)
- 用途:Cross-arb 訊號(PM ↔ LM 價差,訊號交易而非套利)

主要交易場在 [polymkt.limitless](../limitless)。這裡的所有 client/scanner/whales
都是**輔助工具**,不直接動 USDC。
"""

from .clients import ClobClient, GammaClient
from .models import Event, Market, OrderBook
from .scanner import scan as pm_scan
from .whales import (
    PolymarketDataClient,
    attach_limitless_markets,
    find_whale_signals,
    score_wallet,
    top_whales,
)

__all__ = [
    "ClobClient", "GammaClient",
    "Event", "Market", "OrderBook",
    "pm_scan",
    "PolymarketDataClient",
    "attach_limitless_markets", "find_whale_signals", "score_wallet", "top_whales",
]
