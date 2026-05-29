"""Polymarket 鯨魚資料 + 跟單訊號。

資料來源：data-api.polymarket.com（公開、無需 API key）
- `/trades` — 近期成交（全平台 stream）
- `/positions?user=X` — 某錢包當下所有部位（含 cashPnl, realizedPnl）
- `/value?user=X` — 錢包當下組合總價值

跟單流程：
1. `discover_active_wallets`：從 `/trades` 拿近期成交、提取活躍 proxyWallet
2. `score_wallets`：對每個 wallet 算 alpha score（已實現報酬率 + 量級）
3. `top_whales`：篩出最值得跟的 N 個
4. `watch_trades`：監控這些 wallet 的新 trade、產生 signals
5. `cross_to_limitless`：若該題目 LM 有鏡像市場 → 產生可執行的 OrderRequest

⚠️ 高 ROI 不保證持續、過去 alpha 不等於未來 alpha；這是「相對 less random」訊號而不是必勝指標。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx


DATA_API = "https://data-api.polymarket.com"


@dataclass
class Trade:
    """單筆 Polymarket 成交。"""
    proxy_wallet: str
    side: str             # "BUY" / "SELL"
    asset_token: str      # CTF token ID
    condition_id: str
    size: float           # 股數
    price: float
    timestamp: int
    title: str
    slug: str
    event_slug: str
    outcome: str          # "Yes" / "No" / 球隊名等
    outcome_index: int    # 0 = YES, 1 = NO (typically)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Trade":
        return cls(
            proxy_wallet=str(raw.get("proxyWallet", "")).lower(),
            side=str(raw.get("side", "")),
            asset_token=str(raw.get("asset", "")),
            condition_id=str(raw.get("conditionId", "")),
            size=float(raw.get("size", 0) or 0),
            price=float(raw.get("price", 0) or 0),
            timestamp=int(raw.get("timestamp", 0) or 0),
            title=str(raw.get("title", "")),
            slug=str(raw.get("slug", "")),
            event_slug=str(raw.get("eventSlug", "")),
            outcome=str(raw.get("outcome", "")),
            outcome_index=int(raw.get("outcomeIndex", 0) or 0),
        )

    @property
    def notional(self) -> float:
        return self.size * self.price


@dataclass
class Position:
    """單筆當下未平倉部位。"""
    proxy_wallet: str
    asset_token: str
    condition_id: str
    size: float
    avg_price: float
    initial_value: float
    current_value: float
    cash_pnl: float          # 未實現 P&L (current - initial)
    percent_pnl: float
    total_bought: float
    realized_pnl: float      # 已實現
    percent_realized_pnl: float
    title: str
    outcome: str
    redeemable: bool

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Position":
        return cls(
            proxy_wallet=str(raw.get("proxyWallet", "")).lower(),
            asset_token=str(raw.get("asset", "")),
            condition_id=str(raw.get("conditionId", "")),
            size=float(raw.get("size", 0) or 0),
            avg_price=float(raw.get("avgPrice", 0) or 0),
            initial_value=float(raw.get("initialValue", 0) or 0),
            current_value=float(raw.get("currentValue", 0) or 0),
            cash_pnl=float(raw.get("cashPnl", 0) or 0),
            percent_pnl=float(raw.get("percentPnl", 0) or 0),
            total_bought=float(raw.get("totalBought", 0) or 0),
            realized_pnl=float(raw.get("realizedPnl", 0) or 0),
            percent_realized_pnl=float(raw.get("percentRealizedPnl", 0) or 0),
            title=str(raw.get("title", "")),
            outcome=str(raw.get("outcome", "")),
            redeemable=bool(raw.get("redeemable", False)),
        )


@dataclass
class WhaleScore:
    """某 wallet 的整體 alpha 評分。"""
    proxy_wallet: str
    portfolio_value: float
    total_unrealized_pnl: float
    total_realized_pnl: float
    total_bought: float            # 累計交易量（衡量資歷）
    n_positions: int
    realized_roi_pct: float        # **dollar-weighted** 已實現 ROI
    total_roi_pct: float           # 含未實現的整體 ROI（更全面但會隨市價波動）

    @property
    def total_pnl(self) -> float:
        return self.total_unrealized_pnl + self.total_realized_pnl

    @property
    def alpha_score(self) -> float:
        """綜合評分：dollar-weighted ROI × log(交易量級)。

        - dollar-weighted ROI 避免「玩 10 筆小單虧 -100% 拉低平均」的偏差
        - log(total_bought) 給有經驗的鯨魚加分（避免「賺一次大運氣」混進來）
        """
        from math import log
        if self.total_bought < 1000:  # 太少交易、不可靠
            return -1
        # 用「已實現 + 70% × 未實現」當穩健 ROI 估計
        # 純已實現太少資料、純未實現會隨市價亂跳
        blended_pnl = self.total_realized_pnl + 0.7 * self.total_unrealized_pnl
        blended_roi = (blended_pnl / self.total_bought) * 100
        return blended_roi * log(1 + self.total_bought / 1000)


# ---------------- HTTP client ----------------

class PolymarketDataClient:
    """data-api.polymarket.com client。"""

    def __init__(self, timeout: float = 30.0, max_concurrency: int = 8):
        self._client = httpx.AsyncClient(
            base_url=DATA_API,
            timeout=timeout,
            headers={"User-Agent": "polymkt/0.1", "Accept": "application/json"},
        )
        self._sem = asyncio.Semaphore(max_concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def recent_trades(self, limit: int = 500) -> list[Trade]:
        r = await self._client.get("/trades", params={"limit": limit})
        r.raise_for_status()
        return [Trade.from_api(x) for x in r.json()]

    async def positions(self, wallet: str) -> list[Position]:
        async with self._sem:
            try:
                r = await self._client.get(
                    "/positions", params={"user": wallet, "limit": 500}
                )
                if r.status_code != 200:
                    return []
                return [Position.from_api(x) for x in r.json()]
            except httpx.HTTPError:
                return []

    async def portfolio_value(self, wallet: str) -> float:
        async with self._sem:
            try:
                r = await self._client.get("/value", params={"user": wallet})
                if r.status_code != 200:
                    return 0.0
                data = r.json()
                if not data:
                    return 0.0
                return float(data[0].get("value", 0) or 0)
            except httpx.HTTPError:
                return 0.0


# ---------------- 主流程 ----------------

# 過濾掉 noise：超高頻 BTC/SOL/ETH N-min 市場（不適合跟單）
_HF_SLUG_PATTERNS = (
    "btc-updown-5m-", "btc-updown-15m-", "btc-updown-1h-",
    "eth-updown-5m-", "eth-updown-15m-", "eth-updown-1h-",
    "sol-updown-5m-", "sol-updown-15m-", "sol-updown-1h-",
    "doge-updown-", "xrp-updown-", "bnb-updown-", "hype-updown-",
)


def _is_high_frequency(trade: Trade) -> bool:
    s = trade.event_slug.lower()
    return any(s.startswith(p) for p in _HF_SLUG_PATTERNS)


async def discover_active_wallets(
    client: PolymarketDataClient,
    trades_limit: int = 500,
    min_trade_notional: float = 100.0,
    exclude_high_frequency: bool = True,
) -> dict[str, int]:
    """從近期 trades 提取活躍 wallets，回傳 {wallet: trade_count}。"""
    trades = await client.recent_trades(limit=trades_limit)
    counts: dict[str, int] = {}
    for t in trades:
        if exclude_high_frequency and _is_high_frequency(t):
            continue
        if t.notional < min_trade_notional:
            continue
        counts[t.proxy_wallet] = counts.get(t.proxy_wallet, 0) + 1
    return counts


async def score_wallet(
    client: PolymarketDataClient,
    wallet: str,
) -> WhaleScore | None:
    """對單一 wallet 計算 alpha score。"""
    positions, value = await asyncio.gather(
        client.positions(wallet),
        client.portfolio_value(wallet),
    )

    if not positions and value == 0:
        return None

    total_unrealized = sum(p.cash_pnl for p in positions)
    total_realized = sum(p.realized_pnl for p in positions)
    total_bought = sum(p.total_bought for p in positions)

    # Dollar-weighted ROI（避免小單偏差）
    realized_roi = (total_realized / total_bought * 100) if total_bought > 0 else 0.0
    total_roi = ((total_realized + total_unrealized) / total_bought * 100) if total_bought > 0 else 0.0

    return WhaleScore(
        proxy_wallet=wallet,
        portfolio_value=value,
        total_unrealized_pnl=total_unrealized,
        total_realized_pnl=total_realized,
        total_bought=total_bought,
        n_positions=len(positions),
        realized_roi_pct=realized_roi,
        total_roi_pct=total_roi,
    )


async def top_whales(
    *,
    trades_limit: int = 1000,
    min_trade_notional: float = 200.0,
    min_portfolio_value: float = 5000.0,
    min_total_bought: float = 10000.0,
    max_score_concurrent: int = 8,
    top_n: int = 20,
) -> list[WhaleScore]:
    """完整 pipeline：發現候選 → 評分 → 排序。"""
    async with PolymarketDataClient(max_concurrency=max_score_concurrent) as c:
        active = await discover_active_wallets(
            c, trades_limit=trades_limit,
            min_trade_notional=min_trade_notional,
        )
        # Score 所有活躍錢包（並發）
        wallets = list(active.keys())
        scored = await asyncio.gather(*(score_wallet(c, w) for w in wallets))

    candidates = [
        s for s in scored
        if s is not None
        and s.portfolio_value >= min_portfolio_value
        and s.total_bought >= min_total_bought
    ]
    candidates.sort(key=lambda s: s.alpha_score, reverse=True)
    return candidates[:top_n]


# ---------------- 跟單訊號 ----------------

@dataclass
class WhaleSignal:
    """來自鯨魚的跟單訊號。"""
    whale: str
    whale_alpha_score: float
    trade: Trade
    # 是否在 Limitless 有對應的可下單市場
    lm_slug: str | None = None
    lm_yes_token: str | None = None
    lm_no_token: str | None = None
    suggested_lm_side: str | None = None      # "BUY YES" / "BUY NO"

    @property
    def is_actionable(self) -> bool:
        return self.lm_slug is not None and self.suggested_lm_side is not None


async def find_whale_signals(
    whales: Iterable[WhaleScore],
    *,
    since_timestamp: int = 0,
    trades_limit: int = 1000,
    min_trade_notional: float = 500.0,
) -> list[WhaleSignal]:
    """掃近期 trades 看哪些是被追蹤鯨魚的新動作。

    `since_timestamp`：只看這個 epoch 之後的 trades（避免重複跟）。
    """
    whale_map = {w.proxy_wallet: w for w in whales}
    if not whale_map:
        return []

    async with PolymarketDataClient() as c:
        trades = await c.recent_trades(limit=trades_limit)

    signals: list[WhaleSignal] = []
    for t in trades:
        if t.proxy_wallet not in whale_map:
            continue
        if t.timestamp < since_timestamp:
            continue
        if _is_high_frequency(t):
            continue
        if t.notional < min_trade_notional:
            continue
        whale = whale_map[t.proxy_wallet]
        signals.append(WhaleSignal(
            whale=t.proxy_wallet,
            whale_alpha_score=whale.alpha_score,
            trade=t,
        ))

    # 同 condition_id 多筆只留最大那筆（最強 conviction）
    by_cid: dict[str, WhaleSignal] = {}
    for s in signals:
        key = f"{s.whale}:{s.trade.condition_id}:{s.trade.side}"
        if key not in by_cid or s.trade.notional > by_cid[key].trade.notional:
            by_cid[key] = s
    return list(by_cid.values())


# ---------------- Limitless 市場對應 ----------------

async def attach_limitless_markets(
    signals: list[WhaleSignal],
) -> list[WhaleSignal]:
    """對每個訊號嘗試找 LM 上的對應市場（用 crossarb 一樣的 token 比對邏輯）。

    成功配對的 signal 會被填入 `lm_slug`、`lm_yes_token`、`lm_no_token`、
    `suggested_lm_side`，使 `is_actionable` 變 True。
    """
    if not signals:
        return signals

    # 延遲匯入避免循環
    from .crossarb import _content_tokens, _strict_question_match
    from .limitless.client import LimitlessClient

    async with LimitlessClient() as lm_c:
        lm_singles, lm_groups = await lm_c.fetch_active_markets(max_markets=1000)

    # 建立 LM 市場索引：所有可交易市場（含 group 子市場）
    lm_pool: list[tuple[str, str, str, str]] = []  # (title, slug, yes_token, no_token)
    for m in lm_singles:
        if m.is_tradeable:
            lm_pool.append((m.title, m.slug, m.yes_token, m.no_token))
    for g in lm_groups:
        for sub in g.markets:
            if sub.is_tradeable:
                lm_pool.append((sub.title, sub.slug, sub.yes_token, sub.no_token))

    for s in signals:
        # 試嚴格 token 相等
        best_match = None
        for title, slug, yes_tok, no_tok in lm_pool:
            if _strict_question_match(s.trade.title, title) >= 1.0:
                best_match = (slug, yes_tok, no_tok)
                break
        if best_match is None:
            # 嘗試以 outcome（小組賽選項名）配對 group children
            outcome_norm = _content_tokens(s.trade.outcome)
            if outcome_norm:
                for title, slug, yes_tok, no_tok in lm_pool:
                    if _content_tokens(title) == outcome_norm:
                        best_match = (slug, yes_tok, no_tok)
                        break
        if best_match is None:
            continue

        s.lm_slug, s.lm_yes_token, s.lm_no_token = best_match
        # 鏡像方向：鯨魚買 YES → 我們在 LM 也買 YES
        # outcome_index 0 = YES, 1 = NO
        if s.trade.side == "BUY":
            if s.trade.outcome_index == 0:
                s.suggested_lm_side = "BUY YES"
            else:
                s.suggested_lm_side = "BUY NO"
        else:  # 鯨魚 SELL 通常意味要「平倉」，不一定值得跟（先標但不下單）
            s.suggested_lm_side = None

    return signals
