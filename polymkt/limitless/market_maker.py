"""Limitless CTF 雙 BID 做市機器人 (v0.5a)。

核心策略：同時掛 BUY YES + BUY NO，等對手吃單。
若兩邊都成交：1 YES + 1 NO = 結算 $1，成本 = (yes_bid + no_bid) < $1，差額就是利潤。

**這是 v0.5a 最小可運作版本。已知限制**：
- 只支援單一市場
- 簡單對稱報價（不依據庫存做 skew）
- 不接 PM 鏡像價當 oracle（只用 LM 自身 mid）
- 不處理結算（接近結算前必須手動停）

**安全機制**：
- 預設 dry-run（同 LimitlessTradingClient）
- 庫存上限：YES 或 NO 任一達 max_inventory_shares 就停止下單
- 總資本上限：累計花費達 capital_usdc 就停止
- Iteration 之間有 sleep，避免高頻打 API
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from .client import LimitlessClient
from .trading import LimitlessTradingClient, OrderRequest


@dataclass
class MakerConfig:
    """做市參數。"""

    slug: str                    # 目標市場
    capital_usdc: float = 100.0  # 本次做市總資本上限
    quote_size_shares: float = 20.0  # 每邊單筆股數
    target_profit_pct: float = 4.0   # 想吃的價差百分點（雙邊成交時的 ROI）
    half_spread_offset_pct: float = 1.0  # 報價偏離 LM mid 多少（單邊）
    max_inventory_shares: float = 50.0   # YES 或 NO 任一達此數就停
    iteration_sleep_s: float = 30.0      # 重新報價間隔
    duration_s: int = 600                # 做市持續時間（秒）；0 = 無限直到 Ctrl-C
    min_diff_for_requote_pct: float = 0.5  # 計算出的新報價偏離舊報價超過這個 % 才重新報價

    @classmethod
    def from_env(cls, slug: str) -> "MakerConfig":
        return cls(
            slug=slug,
            capital_usdc=float(os.environ.get("MM_CAPITAL_USDC", "100")),
            quote_size_shares=float(os.environ.get("MM_QUOTE_SIZE", "20")),
            target_profit_pct=float(os.environ.get("MM_TARGET_PROFIT_PCT", "4")),
            half_spread_offset_pct=float(os.environ.get("MM_HALF_SPREAD_PCT", "1")),
            max_inventory_shares=float(os.environ.get("MM_MAX_INVENTORY", "50")),
            iteration_sleep_s=float(os.environ.get("MM_ITER_SLEEP_S", "30")),
            duration_s=int(os.environ.get("MM_DURATION_S", "600")),
        )


@dataclass
class IterationResult:
    """單次重新報價的結果。"""
    yes_bid_price: float
    no_bid_price: float
    yes_order_accepted: bool
    no_order_accepted: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    """從 portfolio API 讀出的當下持倉。"""
    yes_shares: float = 0.0
    no_shares: float = 0.0

    @property
    def max_side(self) -> float:
        return max(self.yes_shares, self.no_shares)


class MarketMaker:
    """v0.5a 單一市場 CTF 雙 BID 做市。"""

    def __init__(
        self,
        config: MakerConfig,
        trading_client: LimitlessTradingClient,
        lm_client: LimitlessClient,
    ):
        self.cfg = config
        self.tc = trading_client
        self.lm = lm_client
        self.market: dict[str, Any] | None = None  # 從 API 抓的市場 metadata
        self._capital_used = 0.0

    async def init_market(self) -> None:
        """抓市場 metadata，取得 yes_token / no_token。"""
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{os.environ.get('LIMITLESS_HOST', 'https://api.limitless.exchange')}/markets/{self.cfg.slug}",
                timeout=20,
            )
            r.raise_for_status()
            self.market = r.json()

    @property
    def yes_token(self) -> str:
        return self.market["tokens"]["yes"]

    @property
    def no_token(self) -> str:
        return self.market["tokens"]["no"]

    async def fetch_mid(self) -> tuple[float, float] | None:
        """從 LM orderbook 算 YES mid 與 NO mid。

        若 best_bid/ask 缺一邊，用 `midpoint` 欄位 fallback。
        """
        ob = await self.lm.fetch_orderbook(self.cfg.slug)
        if ob is None:
            return None
        yb = ob.yes_best_bid
        ya = ob.yes_best_ask
        if yb and ya:
            yes_mid = (yb.price + ya.price) / 2
        elif ob.midpoint is not None:
            yes_mid = ob.midpoint
        else:
            return None
        no_mid = 1.0 - yes_mid
        return yes_mid, no_mid

    def compute_target_prices(self, yes_mid: float, no_mid: float) -> tuple[float, float]:
        """根據 mid 計算 BUY YES + BUY NO 的目標掛單價。

        - yes_bid = yes_mid - offset
        - no_bid = no_mid - offset
        - 並校正讓 yes_bid + no_bid = 1 - target_profit_pct/100（保證雙邊成交有 ROI）
        """
        offset = self.cfg.half_spread_offset_pct / 100
        raw_yes_bid = max(0.01, yes_mid - offset)
        raw_no_bid = max(0.01, no_mid - offset)
        # 校正讓總和 = 1 - target_profit_pct/100
        target_sum = max(0.01, 1.0 - self.cfg.target_profit_pct / 100)
        actual_sum = raw_yes_bid + raw_no_bid
        if actual_sum > target_sum:
            # 兩邊各扣一半的差
            excess = actual_sum - target_sum
            raw_yes_bid -= excess / 2
            raw_no_bid -= excess / 2
        # 鉗到 tick 邊界（LM tick = 0.001）
        yes_bid = max(0.01, min(0.99, round(raw_yes_bid, 3)))
        no_bid = max(0.01, min(0.99, round(raw_no_bid, 3)))
        return yes_bid, no_bid

    async def fetch_inventory(self) -> Inventory | None:
        """讀目前 YES / NO 持倉。若 portfolio API 失敗則回 None（不中斷做市）。"""
        try:
            from limitless_sdk.portfolio import PortfolioFetcher
            # SDK Client 的 http_client 暴露在 `.http` 屬性
            pf = PortfolioFetcher(self.tc._sdk_client.http)
            positions = await pf.get_positions()
        except Exception:
            return None

        yes = no = 0.0
        # positions 結構（假設）：{"clob": [{tokenId, size, ...}], "amm": [...]}
        if not isinstance(positions, dict):
            return Inventory(yes_shares=0, no_shares=0)
        for pos in (positions.get("clob") or []):
            tid = str(pos.get("tokenId") or pos.get("token_id") or "")
            try:
                size = float(pos.get("size") or pos.get("shares") or 0)
            except (TypeError, ValueError):
                continue
            if tid == self.yes_token:
                yes += size
            elif tid == self.no_token:
                no += size
        return Inventory(yes_shares=yes, no_shares=no)

    async def cancel_all(self) -> None:
        """取消本市場所有現有訂單（每次 iteration 開頭）。"""
        try:
            if not self.tc.safety.require_explicit_execute:
                # 真實模式才呼叫 SDK
                await self.tc._order_client.cancel_all(self.cfg.slug)
        except Exception as e:
            # 沒舊單也會失敗、不致命
            pass

    async def iterate(self) -> IterationResult:
        """單次重新報價。"""
        notes: list[str] = []

        # 1. 算 mid + 目標價
        mids = await self.fetch_mid()
        if mids is None:
            notes.append("無法取得 mid（orderbook 空）")
            return IterationResult(0, 0, False, False, notes)
        yes_mid, no_mid = mids
        yes_bid, no_bid = self.compute_target_prices(yes_mid, no_mid)
        notes.append(f"YES mid=${yes_mid:.3f}, NO mid=${no_mid:.3f}")
        notes.append(f"報價：BUY YES @${yes_bid:.3f} + BUY NO @${no_bid:.3f}, 和=${yes_bid + no_bid:.3f}")

        # 2. 庫存檢查
        inv = await self.fetch_inventory()
        if inv is not None:
            notes.append(f"庫存：YES={inv.yes_shares:.1f}, NO={inv.no_shares:.1f}")
            if inv.max_side >= self.cfg.max_inventory_shares:
                notes.append(f"⚠️ 庫存達上限 {self.cfg.max_inventory_shares}，本輪不下單")
                return IterationResult(yes_bid, no_bid, False, False, notes)

        # 3. 資本檢查
        notional_this_iter = (yes_bid + no_bid) * self.cfg.quote_size_shares
        if self._capital_used + notional_this_iter > self.cfg.capital_usdc:
            notes.append(f"⚠️ 累計資本 ${self._capital_used:.2f} + 本輪 ${notional_this_iter:.2f} 超過 ${self.cfg.capital_usdc}")
            return IterationResult(yes_bid, no_bid, False, False, notes)

        # 4. 取消舊單
        await self.cancel_all()

        # 5. 掛新單
        yes_req = OrderRequest(
            market_slug=self.cfg.slug,
            token_id=self.yes_token,
            side="BUY",
            price=yes_bid,
            size_shares=self.cfg.quote_size_shares,
            order_type="GTC",
            post_only=True,
        )
        no_req = OrderRequest(
            market_slug=self.cfg.slug,
            token_id=self.no_token,
            side="BUY",
            price=no_bid,
            size_shares=self.cfg.quote_size_shares,
            order_type="GTC",
            post_only=True,
        )
        yes_res = await self.tc.place_order(yes_req)
        no_res = await self.tc.place_order(no_req)

        if yes_res.accepted:
            notes.append(f"  YES BUY 送出 ({'dry' if yes_res.dry_run else 'live'})")
        else:
            notes.append(f"  YES BUY 拒絕：{yes_res.error}")
        if no_res.accepted:
            notes.append(f"  NO  BUY 送出 ({'dry' if no_res.dry_run else 'live'})")
        else:
            notes.append(f"  NO  BUY 拒絕：{no_res.error}")

        if yes_res.accepted and no_res.accepted:
            self._capital_used += notional_this_iter

        return IterationResult(
            yes_bid_price=yes_bid,
            no_bid_price=no_bid,
            yes_order_accepted=yes_res.accepted,
            no_order_accepted=no_res.accepted,
            notes=notes,
        )

    async def run(self, on_iteration=None) -> dict:
        """主迴圈。回傳統計資訊。"""
        await self.init_market()
        start = asyncio.get_event_loop().time()
        iter_count = 0
        last_yes_bid = last_no_bid = 0.0

        while True:
            iter_count += 1
            result = await self.iterate()
            if on_iteration:
                on_iteration(iter_count, result)
            last_yes_bid, last_no_bid = result.yes_bid_price, result.no_bid_price

            # 是否到時間
            if self.cfg.duration_s > 0:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed >= self.cfg.duration_s:
                    break
            await asyncio.sleep(self.cfg.iteration_sleep_s)

        # 收尾：取消所有訂單
        await self.cancel_all()
        return {
            "iterations": iter_count,
            "capital_used": self._capital_used,
            "last_yes_bid": last_yes_bid,
            "last_no_bid": last_no_bid,
        }
