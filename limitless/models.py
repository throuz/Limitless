"""Limitless 資料模型。

主要型別：
- LimitlessMarket：單一二元市場（YES/NO）
- LimitlessGroup：「marketType=group」事件，包含多個互斥子市場
- LimitlessOrderBook：訂單簿快照（API 只回 YES side，NO side 用 1-price 對稱推算）

CTF token 的 size 單位是 shares × 1e6（與 USDC decimals 對齊），1 share 結算後 = $1。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SHARE_DECIMALS = 1_000_000  # CTF token = shares × 1e6


@dataclass
class LimitlessMarket:
    """Limitless 上的單一二元市場。"""

    id: int
    slug: str
    title: str
    condition_id: str
    yes_token: str
    no_token: str
    prices: list[float]          # [yes, no]
    trade_type: str              # "clob" / "amm"
    market_type: str             # "single" / "group"
    status: str                  # "FUNDED" / ...
    expired: bool
    volume_usd: float
    end_date: str | None
    categories: list[str]
    is_poly_arbitrage: bool      # 對應 Polymarket 鏡像市場
    group_id: int | None
    fee_enabled: bool
    min_size_raw: int            # 最小單位（raw token amount，× 1e6 = shares）
    max_spread: float            # 做市最大可接受 spread
    last_trade_price: float | None
    midpoint: float | None       # mid 報價

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LimitlessMarket | None":
        tokens = raw.get("tokens") or {}
        yes_token = tokens.get("yes") or ""
        no_token = tokens.get("no") or ""
        if not yes_token or not no_token:
            return None

        meta = raw.get("metadata") or {}
        prices_raw = raw.get("prices") or [0.0, 0.0]
        try:
            prices = [float(p) for p in prices_raw]
        except (TypeError, ValueError):
            prices = [0.0, 0.0]

        try:
            vol = float(raw.get("volumeFormatted") or 0)
        except (TypeError, ValueError):
            vol = 0.0

        return cls(
            id=int(raw.get("id", 0)),
            slug=str(raw.get("slug", "")),
            title=str(raw.get("title", "")),
            condition_id=str(raw.get("conditionId") or ""),
            yes_token=yes_token,
            no_token=no_token,
            prices=prices,
            trade_type=str(raw.get("tradeType", "")),
            market_type=str(raw.get("marketType", "single")),
            status=str(raw.get("status", "")),
            expired=bool(raw.get("expired", False)),
            volume_usd=vol,
            end_date=raw.get("expirationDate"),
            categories=list(raw.get("categories") or []),
            is_poly_arbitrage=bool(meta.get("isPolyArbitrage", False)),
            group_id=raw.get("groupId"),
            fee_enabled=bool(meta.get("fee", False)),
            min_size_raw=int(meta.get("minSize") or 0),
            max_spread=float(meta.get("maxSpread") or 0),
            last_trade_price=raw.get("lastTradePrice"),
            midpoint=None,
        )

    @property
    def is_tradeable(self) -> bool:
        return (
            self.status == "FUNDED"
            and not self.expired
            and self.trade_type == "clob"  # AMM 不用 orderbook 套利
        )


@dataclass
class LimitlessGroup:
    """Limitless 上的 group 事件（marketType=group）— 多個互斥子市場。

    對應 Polymarket 的 negRisk 事件。同樣只有「事件下所有 markets 都未結算」時，
    互斥群組套利（ΣYES<$1）才成立。

    重要：API 的 `isPolyArbitrage` 旗標只放在 group 的 parent metadata，不在子市場。
    解析時必須把這個 flag 「下傳」到每個 child（在 `from_api` 內處理）。
    """

    id: int
    slug: str
    title: str
    neg_risk_market_id: str | None
    is_poly_arbitrage: bool = False
    markets: list[LimitlessMarket] = field(default_factory=list)
    expired: bool = False
    status: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LimitlessGroup":
        parent_is_poly_arb = bool((raw.get("metadata") or {}).get("isPolyArbitrage", False))
        g = cls(
            id=int(raw.get("id", 0)),
            slug=str(raw.get("slug", "")),
            title=str(raw.get("title", "")),
            neg_risk_market_id=raw.get("negRiskMarketId"),
            is_poly_arbitrage=parent_is_poly_arb,
            expired=bool(raw.get("expired", False)),
            status=str(raw.get("status", "")),
        )
        for sub in raw.get("markets", []) or []:
            m = LimitlessMarket.from_api(sub)
            if m is not None:
                # 把 parent 的 isPolyArbitrage 下傳到 child（child 自己沒這個 flag）
                if parent_is_poly_arb:
                    m.is_poly_arbitrage = True
                g.markets.append(m)
        return g

    @property
    def is_mutex_eligible(self) -> bool:
        """所有子市場可交易 + 未結算 → 才能視為真互斥套利候選。"""
        return (
            self.status == "FUNDED"
            and not self.expired
            and len(self.markets) >= 2
            and all(m.is_tradeable for m in self.markets)
        )


@dataclass(frozen=True)
class Level:
    """訂單簿一檔。size 是 raw CTF amount（shares × 1e6）。"""
    price: float
    size_raw: int

    @property
    def shares(self) -> float:
        return self.size_raw / SHARE_DECIMALS


@dataclass
class LimitlessOrderBook:
    """Limitless 訂單簿。

    重要：API 只回 YES side。要 NO side 透過對稱關係換算：
      - NO best ask = 1 - YES best bid
      - NO best bid = 1 - YES best ask
      - 換言之，買 NO @ p = 賣 YES @ (1-p)
    """

    token_id: str
    yes_bids: list[Level]   # YES 買單，價格遞減
    yes_asks: list[Level]   # YES 賣單，價格遞增
    midpoint: float | None
    adjusted_midpoint: float | None
    max_spread: float | None
    min_size_raw: int
    last_trade_price: float | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LimitlessOrderBook":
        def _parse(items: list[dict[str, Any]]) -> list[Level]:
            out: list[Level] = []
            for it in items or []:
                try:
                    out.append(Level(price=float(it["price"]), size_raw=int(it["size"])))
                except (KeyError, TypeError, ValueError):
                    continue
            return out

        return cls(
            token_id=str(raw.get("tokenId", "")),
            yes_bids=_parse(raw.get("bids", [])),
            yes_asks=_parse(raw.get("asks", [])),
            midpoint=raw.get("midpoint"),
            adjusted_midpoint=raw.get("adjustedMidpoint"),
            max_spread=float(raw["maxSpread"]) if raw.get("maxSpread") else None,
            min_size_raw=int(raw.get("minSize") or 0),
            last_trade_price=raw.get("lastTradePrice"),
        )

    # YES side accessors
    @property
    def yes_best_bid(self) -> Level | None:
        if not self.yes_bids:
            return None
        return max(self.yes_bids, key=lambda l: l.price)

    @property
    def yes_best_ask(self) -> Level | None:
        if not self.yes_asks:
            return None
        return min(self.yes_asks, key=lambda l: l.price)

    # NO side derived via 1-price symmetry
    @property
    def no_best_bid(self) -> Level | None:
        """買 NO 的「best bid」= 賣 YES 的 best ask 的反面。"""
        ya = self.yes_best_ask
        if ya is None:
            return None
        return Level(price=1.0 - ya.price, size_raw=ya.size_raw)

    @property
    def no_best_ask(self) -> Level | None:
        """買 NO 的「best ask」= 賣 YES 的 best bid 的反面。"""
        yb = self.yes_best_bid
        if yb is None:
            return None
        return Level(price=1.0 - yb.price, size_raw=yb.size_raw)

    def cost_to_buy_yes(self, target_shares: float) -> tuple[float, float] | None:
        """市價買 YES `target_shares` 股，回傳 (總成本 USDC, 實際成交股數)。"""
        return _cost_to_buy(self.yes_asks, target_shares)

    def cost_to_buy_no(self, target_shares: float) -> tuple[float, float] | None:
        """市價買 NO `target_shares` 股 = 市價賣 YES @ (1-price)。"""
        # 「買 NO」對應「YES 的 bid book，以 1-price 反算」
        # 你買 NO 一股的成本 = 1 - 你賣 YES 一股能拿到的價格
        if not self.yes_bids:
            return None
        sorted_bids = sorted(self.yes_bids, key=lambda l: -l.price)  # 從高價往低
        remaining = target_shares
        cost = 0.0
        filled = 0.0
        for lvl in sorted_bids:
            available = lvl.shares
            take = min(remaining, available)
            # 賣 YES 拿到 lvl.price/股 → 買 NO 成本 = (1 - lvl.price)/股
            cost += take * (1.0 - lvl.price)
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        return (cost, filled) if filled > 0 else None


def _cost_to_buy(asks: list[Level], target_shares: float) -> tuple[float, float] | None:
    if not asks:
        return None
    sorted_asks = sorted(asks, key=lambda l: l.price)
    remaining = target_shares
    cost = 0.0
    filled = 0.0
    for lvl in sorted_asks:
        available = lvl.shares
        take = min(remaining, available)
        cost += take * lvl.price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    return (cost, filled) if filled > 0 else None
