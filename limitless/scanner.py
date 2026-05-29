"""Limitless 套利掃描器。

三種型態：

1. **same_market**：單一二元市場買 YES + 買 NO < $1
   注意 Limitless 的 NO 側由 YES orderbook 對稱推算。
2. **mutex_group**：group 事件下所有 YES 加總 < $1。
3. **near_arb**：未達閾值但 ΣYES 很接近 $1 的事件（教學/反向參考用）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from .client import LimitlessClient
from .models import LimitlessGroup, LimitlessMarket, LimitlessOrderBook, SHARE_DECIMALS


OpType = Literal["same_market", "mutex_group"]


@dataclass
class Leg:
    market_title: str
    slug: str
    token_id: str
    side: str           # "YES" / "NO"
    price: float        # 加權平均成交價
    shares: float
    notional: float

    @property
    def label(self) -> str:
        q = self.market_title
        if len(q) > 55:
            q = q[:52] + "..."
        return f"{self.side} | {q}"


@dataclass
class Opportunity:
    type: OpType
    title: str           # 事件或市場標題
    legs: list[Leg]
    cost_per_set: float
    payout: float        # 通常 $1
    max_sets: float
    fee_rate: float      # 平均費率
    is_poly_arbitrage: bool = False  # 來自 isPolyArbitrage 標記市場

    @property
    def fee_drag(self) -> float:
        return self.cost_per_set * self.fee_rate

    @property
    def edge_per_set(self) -> float:
        return self.payout - self.cost_per_set - self.fee_drag

    @property
    def edge_pct(self) -> float:
        if self.cost_per_set <= 0:
            return 0.0
        return self.edge_per_set / self.cost_per_set

    @property
    def total_edge(self) -> float:
        return self.edge_per_set * self.max_sets

    @property
    def required_capital(self) -> float:
        return self.cost_per_set * self.max_sets


# Limitless CLOB taker fee 一般估值（保守）。實際依市場 fee schedule 變化。
# 公開資料常見 0.4% ~ 3%，這裡用 0.4% 作為「fee_enabled=True 但未知細節」的下界估計。
DEFAULT_TAKER_FEE = 0.004


def _avg_price(book_method_result: tuple[float, float] | None) -> tuple[float, float] | None:
    if book_method_result is None:
        return None
    cost, filled = book_method_result
    if filled <= 0:
        return None
    return cost / filled, filled


def _scan_same_market(
    m: LimitlessMarket,
    book: LimitlessOrderBook | None,
    *,
    probe_shares: float,
    min_edge: float,
) -> Opportunity | None:
    if book is None:
        return None
    yes_ask = book.yes_best_ask
    no_ask = book.no_best_ask
    if yes_ask is None or no_ask is None:
        return None

    quick_total = yes_ask.price + no_ask.price
    if quick_total >= 1.0 - min_edge:
        return None

    # 兩邊可成交股數的上限：YES 的 ask 總量 vs NO 的 ask 總量
    # NO ask 來自 YES bid 對稱，所以股數 = sum of YES bid shares
    yes_share_cap = sum(l.shares for l in book.yes_asks)
    no_share_cap = sum(l.shares for l in book.yes_bids)
    cap = min(yes_share_cap, no_share_cap, probe_shares)
    if cap <= 0:
        return None

    yes_fill = _avg_price(book.cost_to_buy_yes(cap))
    no_fill = _avg_price(book.cost_to_buy_no(cap))
    if not yes_fill or not no_fill:
        return None

    yes_p, yes_n = yes_fill
    no_p, no_n = no_fill
    sets = min(yes_n, no_n)
    if sets <= 0:
        return None

    cost = yes_p + no_p
    fee_rate = DEFAULT_TAKER_FEE if m.fee_enabled else 0.0
    if cost + cost * fee_rate >= 1.0 - min_edge:
        return None

    return Opportunity(
        type="same_market",
        title=m.title,
        legs=[
            Leg(market_title=m.title, slug=m.slug, token_id=m.yes_token,
                side="YES", price=yes_p, shares=sets, notional=yes_p * sets),
            Leg(market_title=m.title, slug=m.slug, token_id=m.no_token,
                side="NO", price=no_p, shares=sets, notional=no_p * sets),
        ],
        cost_per_set=cost,
        payout=1.0,
        max_sets=sets,
        fee_rate=fee_rate,
        is_poly_arbitrage=m.is_poly_arbitrage,
    )


def _scan_mutex_group(
    g: LimitlessGroup,
    books: dict[str, LimitlessOrderBook],
    *,
    probe_shares: float,
    min_edge: float,
) -> Opportunity | None:
    if not g.is_mutex_eligible:
        return None

    # 收集每個子市場的 YES 訂單簿
    pairs: list[tuple[LimitlessMarket, LimitlessOrderBook]] = []
    for sm in g.markets:
        b = books.get(sm.slug)
        if b is None or not b.yes_asks:
            return None  # 缺資料就放棄
        pairs.append((sm, b))

    # 快速估算
    quick_total = 0.0
    for _, b in pairs:
        a = b.yes_best_ask
        if a is None:
            return None
        quick_total += a.price
    if quick_total >= 1.0 - min_edge:
        return None

    share_cap = min(sum(l.shares for l in b.yes_asks) for _, b in pairs)
    cap = min(share_cap, probe_shares)
    if cap <= 0:
        return None

    fills = []
    for sm, b in pairs:
        f = _avg_price(b.cost_to_buy_yes(cap))
        if not f:
            return None
        fills.append((sm, f[0], f[1]))

    sets = min(f[2] for f in fills)
    if sets <= 0:
        return None

    cost = sum(f[1] for f in fills)
    fees = sum(DEFAULT_TAKER_FEE if sm.fee_enabled else 0.0 for sm, *_ in fills) / len(fills)
    if cost + cost * fees >= 1.0 - min_edge:
        return None

    legs = [
        Leg(market_title=sm.title, slug=sm.slug, token_id=sm.yes_token,
            side="YES", price=p, shares=sets, notional=p * sets)
        for sm, p, _n in fills
    ]
    any_poly = any(sm.is_poly_arbitrage for sm, *_ in fills)

    return Opportunity(
        type="mutex_group",
        title=g.title,
        legs=legs,
        cost_per_set=cost,
        payout=1.0,
        max_sets=sets,
        fee_rate=fees,
        is_poly_arbitrage=any_poly,
    )


async def scan(
    *,
    max_markets: int = 1000,
    min_edge: float = 0.003,    # 0.3% 預設（要超過 1 個 tick + 些許 fee）
    probe_shares: float = 100.0,
    min_volume_usd: float = 100.0,
) -> list[Opportunity]:
    async with LimitlessClient() as c:
        singles, groups = await c.fetch_active_markets(max_markets=max_markets)

        # 過濾 single market：要可交易 + 有量
        sm = [m for m in singles if m.is_tradeable and m.volume_usd >= min_volume_usd]

        # group market：要 mutex 合格（所有子市場都未結算）
        mg = [g for g in groups if g.is_mutex_eligible]

        slugs_needed: set[str] = set()
        for m in sm:
            slugs_needed.add(m.slug)
        for g in mg:
            for sub in g.markets:
                slugs_needed.add(sub.slug)

        if not slugs_needed:
            return []

        books = await c.fetch_orderbooks(slugs_needed)

    opps: list[Opportunity] = []
    for m in sm:
        opp = _scan_same_market(m, books.get(m.slug),
                                probe_shares=probe_shares, min_edge=min_edge)
        if opp:
            opps.append(opp)
    for g in mg:
        opp = _scan_mutex_group(g, books,
                                probe_shares=probe_shares, min_edge=min_edge)
        if opp:
            opps.append(opp)

    opps.sort(key=lambda o: o.total_edge, reverse=True)
    return opps


@dataclass
class NearArb:
    """快達套利但未過閾值，供觀察與反向交易參考。"""
    kind: str               # "same_market" / "mutex_group"
    title: str
    sigma_or_total: float   # ΣYES 或 YES+NO 總和
    edge_pct: float         # >0 = 正向套利、<0 = 反向機會
    n_legs: int
    is_poly_arbitrage: bool = False


async def closest(
    *,
    max_markets: int = 1000,
    min_volume_usd: float = 100.0,
) -> tuple[list[NearArb], list[NearArb]]:
    """回傳 (same_market_rows, mutex_group_rows)，依 |edge| 排序。"""
    async with LimitlessClient() as c:
        singles, groups = await c.fetch_active_markets(max_markets=max_markets)
        sm = [m for m in singles if m.is_tradeable and m.volume_usd >= min_volume_usd]
        mg = [g for g in groups if g.is_mutex_eligible]

        slugs: set[str] = set()
        for m in sm:
            slugs.add(m.slug)
        for g in mg:
            for sub in g.markets:
                slugs.add(sub.slug)

        books = await c.fetch_orderbooks(slugs)

    sm_rows: list[NearArb] = []
    for m in sm:
        b = books.get(m.slug)
        if not b or not b.yes_best_ask or not b.no_best_ask:
            continue
        total = b.yes_best_ask.price + b.no_best_ask.price
        sm_rows.append(NearArb(
            kind="same_market",
            title=m.title,
            sigma_or_total=total,
            edge_pct=(1.0 - total) * 100,
            n_legs=2,
            is_poly_arbitrage=m.is_poly_arbitrage,
        ))

    mg_rows: list[NearArb] = []
    for g in mg:
        asks = []
        ok = True
        any_poly = False
        for sub in g.markets:
            b = books.get(sub.slug)
            if not b or not b.yes_best_ask:
                ok = False
                break
            asks.append(b.yes_best_ask.price)
            any_poly = any_poly or sub.is_poly_arbitrage
        if not ok:
            continue
        total = sum(asks)
        mg_rows.append(NearArb(
            kind="mutex_group",
            title=g.title,
            sigma_or_total=total,
            edge_pct=(1.0 - total) * 100,
            n_legs=len(asks),
            is_poly_arbitrage=any_poly,
        ))

    sm_rows.sort(key=lambda r: abs(r.edge_pct), reverse=True)
    mg_rows.sort(key=lambda r: abs(r.edge_pct), reverse=True)
    return sm_rows, mg_rows
