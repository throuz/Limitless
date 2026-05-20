"""Polymarket 資料模型。

把 Gamma / CLOB API 的 JSON 收斂成型別友善的 dataclass。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _parse_json_field(value: Any) -> Any:
    """Gamma API 把陣列以 JSON 字串塞在欄位裡（例 outcomes、clobTokenIds）。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


@dataclass
class Market:
    """單一二元市場（YES / NO）。"""

    id: str
    condition_id: str
    question: str
    slug: str
    outcomes: list[str]
    outcome_prices: list[float]
    clob_token_ids: list[str]  # [YES_token, NO_token]
    active: bool
    closed: bool
    accepting_orders: bool
    enable_order_book: bool
    neg_risk: bool
    liquidity: float
    volume_24hr: float
    end_date: str | None
    fee_rate: float = 0.0  # 0 對絕大多數市場；finance_prices_fees 類型可能有 0.04
    event_id: str | None = None
    event_title: str | None = None
    group_item_title: str | None = None  # 多選一事件中的此選項標題

    @classmethod
    def from_gamma(cls, raw: dict[str, Any], event_id: str | None = None,
                   event_title: str | None = None) -> "Market | None":
        """從 Gamma API `markets[]` 內 JSON 物件建立。"""
        token_ids = _parse_json_field(raw.get("clobTokenIds", "[]"))
        if not token_ids or len(token_ids) < 2:
            return None

        outcomes = _parse_json_field(raw.get("outcomes", '["Yes","No"]'))
        prices_raw = _parse_json_field(raw.get("outcomePrices", '["0","0"]'))
        try:
            prices = [float(p) for p in prices_raw]
        except (TypeError, ValueError):
            prices = [0.0, 0.0]

        fee_schedule = raw.get("feeSchedule") or {}
        fee_rate = float(fee_schedule.get("rate", 0.0)) if fee_schedule else 0.0

        return cls(
            id=str(raw.get("id", "")),
            condition_id=str(raw.get("conditionId", "")),
            question=str(raw.get("question", "")),
            slug=str(raw.get("slug", "")),
            outcomes=outcomes,
            outcome_prices=prices,
            clob_token_ids=token_ids,
            active=bool(raw.get("active", False)),
            closed=bool(raw.get("closed", False)),
            accepting_orders=bool(raw.get("acceptingOrders", False)),
            enable_order_book=bool(raw.get("enableOrderBook", False)),
            neg_risk=bool(raw.get("negRisk", False)),
            liquidity=float(raw.get("liquidityClob") or raw.get("liquidity") or 0),
            volume_24hr=float(raw.get("volume24hrClob") or raw.get("volume24hr") or 0),
            end_date=raw.get("endDate"),
            fee_rate=fee_rate,
            event_id=event_id,
            event_title=event_title,
            group_item_title=raw.get("groupItemTitle"),
        )

    @property
    def yes_token(self) -> str:
        return self.clob_token_ids[0]

    @property
    def no_token(self) -> str:
        return self.clob_token_ids[1]

    @property
    def is_tradeable(self) -> bool:
        return (
            self.active
            and not self.closed
            and self.accepting_orders
            and self.enable_order_book
            and len(self.clob_token_ids) == 2
        )


@dataclass
class Event:
    """事件 — 一個事件可包含多個 Market（多選一）。"""

    id: str
    slug: str
    title: str
    neg_risk: bool
    markets: list[Market] = field(default_factory=list)

    @classmethod
    def from_gamma(cls, raw: dict[str, Any]) -> "Event":
        ev = cls(
            id=str(raw.get("id", "")),
            slug=str(raw.get("slug", "")),
            title=str(raw.get("title", "")),
            neg_risk=bool(raw.get("negRisk", False)),
        )
        for m_raw in raw.get("markets", []) or []:
            m = Market.from_gamma(m_raw, event_id=ev.id, event_title=ev.title)
            if m is not None:
                ev.markets.append(m)
        return ev

    @property
    def is_multi_outcome(self) -> bool:
        """多選一事件（同一事件包含 >1 個互斥的 YES/NO 市場）。"""
        return len(self.markets) > 1


@dataclass(frozen=True)
class Level:
    """訂單簿一檔。"""
    price: float
    size: float


@dataclass
class OrderBook:
    """單一 token 的訂單簿快照。"""

    token_id: str
    bids: list[Level]  # 由低到高
    asks: list[Level]  # 由高到低
    timestamp_ms: int

    @classmethod
    def from_clob(cls, raw: dict[str, Any]) -> "OrderBook":
        def _parse(items: list[dict[str, Any]]) -> list[Level]:
            out: list[Level] = []
            for it in items or []:
                try:
                    out.append(Level(price=float(it["price"]), size=float(it["size"])))
                except (KeyError, TypeError, ValueError):
                    continue
            return out

        return cls(
            token_id=str(raw.get("asset_id", "")),
            bids=_parse(raw.get("bids", [])),
            asks=_parse(raw.get("asks", [])),
            timestamp_ms=int(raw.get("timestamp", 0) or 0),
        )

    @property
    def best_bid(self) -> Level | None:
        """最高買價（最佳賣出價）。Polymarket bids 升序 → 取最後一個。"""
        if not self.bids:
            return None
        return max(self.bids, key=lambda l: l.price)

    @property
    def best_ask(self) -> Level | None:
        """最低賣價（最佳買入價）。"""
        if not self.asks:
            return None
        return min(self.asks, key=lambda l: l.price)

    def cost_to_buy(self, target_shares: float) -> tuple[float, float] | None:
        """模擬市價買入 `target_shares` 股的總成本與實際成交數量。

        回傳 (total_cost, filled_shares)；若流動性不足只回傳部分成交。
        若完全沒有 ask，回傳 None。
        """
        if not self.asks:
            return None
        sorted_asks = sorted(self.asks, key=lambda l: l.price)
        remaining = target_shares
        cost = 0.0
        filled = 0.0
        for lvl in sorted_asks:
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        return cost, filled
