"""套利機會偵測。

兩種主要型態：

1. **same_market**：同一個二元市場中，買 YES + 買 NO 的總成本 < $1。
   理論上不該存在，但在流動性低或快速波動時偶有出現。

2. **mutex_group**：同一事件下多個互斥市場（多選一），全部 YES 的買入成本
   加總 < $1 → 把每個都買一股，其中必定恰有一個結算為 $1。
   注意：只有 `neg_risk=True` 的事件**真正**互斥；否則「兩個都發生」是可能的。

兩種型態都會回傳 `Opportunity`，附帶可下單的最大資金與預期利潤。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from .clients import ClobClient, GammaClient
from .models import Event, Market, OrderBook


OpportunityType = Literal["same_market", "mutex_group"]


@dataclass
class Leg:
    """套利組合中的一條腿（買進某 token 一定數量）。"""
    market_question: str
    token_id: str
    side: str               # "YES" 或 "NO"
    price: float            # 加權平均成交價
    shares: float           # 預計買入股數
    notional: float         # 預計花費 USDC

    @property
    def slug_hint(self) -> str:
        """方便人類辨識的簡短描述。"""
        q = self.market_question
        if len(q) > 60:
            q = q[:57] + "..."
        return f"{self.side} | {q}"


@dataclass
class Opportunity:
    type: OpportunityType
    event_id: str | None
    event_title: str | None
    legs: list[Leg]
    cost_per_set: float       # 買一「組」需多少 USDC
    guaranteed_payout: float  # 一組能保證拿回多少 USDC（多選一通常是 $1）
    max_sets: float           # 受限於 orderbook 深度，最多買幾組
    fee_drag: float           # 預估手續費扣除（USDC，每組）
    neg_risk: bool

    @property
    def edge_per_set(self) -> float:
        """每組的淨利潤（已扣費）。"""
        return self.guaranteed_payout - self.cost_per_set - self.fee_drag

    @property
    def edge_pct(self) -> float:
        """每組淨利潤佔成本的比例。"""
        if self.cost_per_set <= 0:
            return 0.0
        return self.edge_per_set / self.cost_per_set

    @property
    def total_edge(self) -> float:
        """全倉位的預估獲利。"""
        return self.edge_per_set * self.max_sets

    @property
    def required_capital(self) -> float:
        return self.cost_per_set * self.max_sets


def _best_ask_fill(book: OrderBook, target_shares: float) -> tuple[float, float] | None:
    """模擬市價買 `target_shares` 股，回傳 (加權均價, 實際成交股數)。"""
    res = book.cost_to_buy(target_shares)
    if res is None:
        return None
    cost, filled = res
    if filled <= 0:
        return None
    return cost / filled, filled


def _scan_same_market(
    market: Market,
    yes_book: OrderBook | None,
    no_book: OrderBook | None,
    *,
    probe_shares: float = 100.0,
    min_edge_per_set: float = 0.002,
) -> Opportunity | None:
    """在單一二元市場找 YES + NO 買入總和 < $1 的機會。"""
    if not yes_book or not no_book:
        return None
    yes_best = yes_book.best_ask
    no_best = no_book.best_ask
    if not yes_best or not no_best:
        return None

    # 快速過濾：用 best ask 估算，沒希望就放棄
    quick_total = yes_best.price + no_best.price
    if quick_total >= 1.0 - min_edge_per_set:
        return None

    # 計算可買幾組（受限於兩邊 orderbook 深度與想下的金額）
    max_shares = min(
        sum(l.size for l in yes_book.asks),
        sum(l.size for l in no_book.asks),
    )
    target = min(max_shares, probe_shares)
    if target <= 0:
        return None

    yes_fill = _best_ask_fill(yes_book, target)
    no_fill = _best_ask_fill(no_book, target)
    if not yes_fill or not no_fill:
        return None

    yes_price, yes_filled = yes_fill
    no_price, no_filled = no_fill
    sets = min(yes_filled, no_filled)
    if sets <= 0:
        return None

    cost_per_set = yes_price + no_price
    fee_drag = (yes_price + no_price) * market.fee_rate  # 保守估
    if cost_per_set + fee_drag >= 1.0 - min_edge_per_set:
        return None

    return Opportunity(
        type="same_market",
        event_id=market.event_id,
        event_title=market.event_title,
        legs=[
            Leg(
                market_question=market.question,
                token_id=market.yes_token,
                side="YES",
                price=yes_price,
                shares=sets,
                notional=yes_price * sets,
            ),
            Leg(
                market_question=market.question,
                token_id=market.no_token,
                side="NO",
                price=no_price,
                shares=sets,
                notional=no_price * sets,
            ),
        ],
        cost_per_set=cost_per_set,
        guaranteed_payout=1.0,
        max_sets=sets,
        fee_drag=fee_drag,
        neg_risk=market.neg_risk,
    )


def _scan_mutex_group(
    event: Event,
    books: dict[str, OrderBook],
    *,
    probe_shares: float = 100.0,
    min_edge_per_set: float = 0.002,
) -> Opportunity | None:
    """同事件下多個互斥市場，買全部 YES 是否 < $1。

    **關鍵正確性條件**：事件下必須**所有**市場都仍未結算。
    一旦有 market 已 closed/resolved（即使是 NO 結算），剩下的開放市場
    就不再具有「保證恰一個拿 $1」的特性 — 中獎的可能已是已結算的市場。
    """
    if not event.is_multi_outcome:
        return None
    if not event.neg_risk:
        # 非 neg_risk 的多市場事件未必互斥（可能同時發生）。略過以避免假套利。
        return None

    # 整個事件必須完全未結算才能用互斥群組套利
    if any(m.closed for m in event.markets):
        return None
    # 且每個市場都要可交易（否則無法湊滿一組）
    if any(not m.is_tradeable for m in event.markets):
        return None

    tradeable = event.markets
    if len(tradeable) < 2:
        return None

    # 取每個市場 YES token 的 orderbook
    yes_books: list[tuple[Market, OrderBook]] = []
    for m in tradeable:
        b = books.get(m.yes_token)
        if b is None or not b.asks:
            return None  # 缺資料就不算
        yes_books.append((m, b))

    # 快速估算：best ask 加總
    quick_total = sum(b.best_ask.price for _, b in yes_books if b.best_ask)
    if quick_total >= 1.0 - min_edge_per_set:
        return None

    # 受限於最差那一檔的可買股數
    max_shares = min(sum(l.size for l in b.asks) for _, b in yes_books)
    target = min(max_shares, probe_shares)
    if target <= 0:
        return None

    fills = []
    for m, b in yes_books:
        f = _best_ask_fill(b, target)
        if not f:
            return None
        fills.append((m, b, f[0], f[1]))

    sets = min(f[3] for f in fills)
    if sets <= 0:
        return None

    legs: list[Leg] = []
    cost_per_set = 0.0
    for m, b, price, filled in fills:
        cost_per_set += price
        legs.append(
            Leg(
                market_question=m.question,
                token_id=m.yes_token,
                side="YES",
                price=price,
                shares=sets,
                notional=price * sets,
            )
        )

    # 平均費用率
    avg_fee = sum(m.fee_rate for m, *_ in fills) / max(len(fills), 1)
    fee_drag = cost_per_set * avg_fee
    if cost_per_set + fee_drag >= 1.0 - min_edge_per_set:
        return None

    return Opportunity(
        type="mutex_group",
        event_id=event.id,
        event_title=event.title,
        legs=legs,
        cost_per_set=cost_per_set,
        guaranteed_payout=1.0,
        max_sets=sets,
        fee_drag=fee_drag,
        neg_risk=True,
    )


async def scan(
    *,
    max_events: int = 200,
    min_liquidity: float = 1000.0,
    min_edge_per_set: float = 0.002,
    probe_shares: float = 100.0,
) -> list[Opportunity]:
    """主入口。同步擷取 → 平行抓 orderbook → 掃描。"""

    async with GammaClient() as gamma, ClobClient() as clob:
        events = await gamma.fetch_active_events(max_events=max_events)

        # 用於 same-market：流動性過濾後的個別市場
        sm_markets: list[Market] = []
        # 用於 mutex group：保留事件的「完整原始市場集合」（含已關閉者）
        # 但僅當事件整體有意義（>=2 個市場、neg_risk）時才考慮
        mutex_candidates: list[Event] = []

        for ev in events:
            for m in ev.markets:
                if m.is_tradeable and m.liquidity >= min_liquidity:
                    sm_markets.append(m)
            if ev.neg_risk and len(ev.markets) >= 2 and all(
                m.is_tradeable for m in ev.markets
            ):
                mutex_candidates.append(ev)

        # 拉所有需要的 orderbook
        all_tokens: set[str] = set()
        for m in sm_markets:
            all_tokens.add(m.yes_token)
            all_tokens.add(m.no_token)
        for ev in mutex_candidates:
            for m in ev.markets:
                all_tokens.add(m.yes_token)

        if not all_tokens:
            return []

        books = await clob.fetch_books(all_tokens)

    # 同步掃描
    opps: list[Opportunity] = []
    for m in sm_markets:
        opp = _scan_same_market(
            m,
            books.get(m.yes_token),
            books.get(m.no_token),
            probe_shares=probe_shares,
            min_edge_per_set=min_edge_per_set,
        )
        if opp:
            opps.append(opp)

    for ev in mutex_candidates:
        opp = _scan_mutex_group(
            ev, books,
            probe_shares=probe_shares,
            min_edge_per_set=min_edge_per_set,
        )
        if opp:
            opps.append(opp)

    opps.sort(key=lambda o: o.total_edge, reverse=True)
    return opps
