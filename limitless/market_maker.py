"""Limitless CTF 雙 BID 做市機器人 (v0.6)。

核心策略：同時掛 BUY YES + BUY NO，等對手吃單。
若兩邊都成交：1 YES + 1 NO = 結算 $1，成本 = (yes_bid + no_bid) < $1，差額就是利潤。

**v0.6 新增**：
- Microprice（用對手側 size 加權）取代簡單 mid，對流動方向反應更快
- 逆選擇偵測：fill 不對稱 / PM 快速漂移 / LM best ask 突跌 → 自動加寬或撤單
- 主動清庫存：庫存堆積時加掛 SELL 對應方向（maker premium）
- 結算事件偵測：距結算 < N 小時 → 取消所有單 + 市價清倉

**安全機制**（同 v0.5b）：
- 預設 dry-run（同 LimitlessTradingClient）
- 庫存上限：YES 或 NO 任一達 max_inventory_shares 就停止下單
- 總資本上限：累計花費達 capital_usdc 就停止
- Iteration 之間有 sleep，避免高頻打 API
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import LimitlessClient
from .trading import LimitlessTradingClient, OrderRequest
from . import pnl as pnl_db
from . import notify


@dataclass
class MakerConfig:
    """做市參數。"""

    slug: str                    # 目標市場
    capital_usdc: float = 100.0  # 本次做市總資本上限
    quote_size_shares: float = 20.0  # 每邊單筆股數
    target_profit_pct: float = 4.0   # 想吃的價差百分點（雙邊成交時的 ROI）
    half_spread_offset_pct: float = 1.0  # 報價偏離公平價多少（單邊）
    max_inventory_shares: float = 50.0   # YES 或 NO 任一達此數就停
    iteration_sleep_s: float = 30.0      # 重新報價間隔
    duration_s: int = 600                # 做市持續時間（秒）；0 = 無限直到 Ctrl-C
    min_diff_for_requote_pct: float = 0.5  # 計算出的新報價偏離舊報價超過這個 % 才重新報價

    # v0.5b：公平價來源
    oracle_mode: str = "lm"
    inventory_skew_pct: float = 0.5

    # v0.6：Microprice（依對手側 size 加權的公平價,更準）
    use_microprice: bool = True

    # v0.6：逆選擇偵測
    toxicity_window: int = 5                      # 看最近 N 輪
    toxicity_imbalance_threshold: float = 0.7     # YES/NO fill 不對稱比 > 此值 → 加寬該側
    toxicity_pm_velocity_threshold: float = 0.03  # PM mid N 輪內漂移 > $0.03 → 撤單觀望
    toxicity_ask_drop_threshold: float = 0.02     # LM best ask N 輪內下殺 > $0.02 → 撤對側
    toxicity_widen_multiplier: float = 2.0        # 偵測到 toxicity 時 spread × 此倍數
    toxicity_pull_on_pm_jump: bool = True         # PM 大跳直接撤

    # v0.6：主動清庫存
    unwind_inventory_pct: float = 0.6       # 庫存超過 max × 此比例 → 啟動 SELL
    unwind_premium_pct: float = 1.0         # SELL 報價在 mid 上方多少 pp
    unwind_size_shares: float | None = None  # SELL 單股數；None = quote_size_shares

    # v0.6：結算事件偵測
    emergency_close_hours: float = 24.0     # 距結算 < N 小時 → 強制清倉退場
    emergency_close_enabled: bool = True    # 關掉這個會跑到結算（高風險）

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
    # v0.6：SELL（清庫存）結果
    yes_sell_price: float = 0.0
    no_sell_price: float = 0.0
    yes_sell_accepted: bool = False
    no_sell_accepted: bool = False
    # v0.6：toxicity 與緊急狀態
    toxicity_score: float = 0.0
    emergency_close: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    """從 portfolio API 讀出的當下持倉。"""
    yes_shares: float = 0.0
    no_shares: float = 0.0

    @property
    def max_side(self) -> float:
        return max(self.yes_shares, self.no_shares)


@dataclass
class ToxicityState:
    """逆選擇偵測用的滾動歷史。"""
    yes_fills: deque = field(default_factory=lambda: deque(maxlen=10))
    no_fills: deque = field(default_factory=lambda: deque(maxlen=10))
    pm_mids: deque = field(default_factory=lambda: deque(maxlen=10))
    lm_best_asks: deque = field(default_factory=lambda: deque(maxlen=10))
    last_yes_inv: float = 0.0
    last_no_inv: float = 0.0


@dataclass
class ToxicityAssessment:
    """單輪 toxicity 評估結果。"""
    yes_spread_mult: float = 1.0   # YES 側 spread 加成
    no_spread_mult: float = 1.0    # NO 側 spread 加成
    pull_yes: bool = False         # 直接撤 YES 報價
    pull_no: bool = False          # 直接撤 NO 報價
    score: float = 0.0             # 整體 toxicity 分（debug）
    reasons: list[str] = field(default_factory=list)


class MarketMaker:
    """v0.6 單一市場 CTF 雙 BID 做市（+ 主動 SELL 清庫存 + toxicity 偵測 + 結算保護）。"""

    def __init__(
        self,
        config: MakerConfig,
        trading_client: LimitlessTradingClient,
        lm_client: LimitlessClient,
    ):
        self.cfg = config
        self.tc = trading_client
        self.lm = lm_client
        self.market: dict[str, Any] | None = None
        self._capital_used = 0.0
        self._tox = ToxicityState()
        self._tox.yes_fills.extend([0.0] * config.toxicity_window)
        self._tox.no_fills.extend([0.0] * config.toxicity_window)

    async def init_market(self) -> None:
        """抓市場 metadata，取得 yes_token / no_token / expirationDate。"""
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

    # ---------- 結算偵測 ----------

    def hours_to_resolution(self) -> float | None:
        """距結算還剩幾小時。沒設 expirationDate → None。"""
        if self.market is None:
            return None
        exp = self.market.get("expirationDate") or self.market.get("expiration_date")
        if not exp:
            return None
        s = str(exp)
        # ISO 先
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
        except Exception:
            pass
        # Limitless 人類可讀格式 "May 21, 2026" 等
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%b %d, %Y", "%B %d, %Y",
            "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
            except ValueError:
                continue
        return None

    def is_emergency_window(self) -> bool:
        if not self.cfg.emergency_close_enabled:
            return False
        hrs = self.hours_to_resolution()
        if hrs is None:
            return False
        return hrs <= self.cfg.emergency_close_hours

    # ---------- 公平價（含 microprice）----------

    async def fetch_mid(self) -> tuple[float, float] | None:
        """根據 oracle_mode 決定公平價。

        - "lm": LM microprice（v0.6 預設）或 simple mid
        - "pm": Polymarket 鏡像 mid（更可靠，但若無對應市場會 fallback 到 lm）
        - "blend": PM 60% + LM 40%
        """
        lm_mid = await self._fetch_lm_fair()
        if lm_mid is None:
            return None

        mode = self.cfg.oracle_mode.lower()
        if mode == "lm":
            return lm_mid

        pm_yes = await self._try_fetch_pm_mirror_yes()
        if pm_yes is None:
            return lm_mid

        if mode == "pm":
            return (pm_yes, 1.0 - pm_yes)
        if mode == "blend":
            yes = 0.6 * pm_yes + 0.4 * lm_mid[0]
            return (yes, 1.0 - yes)
        return lm_mid

    async def _fetch_lm_fair(self) -> tuple[float, float] | None:
        """LM 端的公平價：use_microprice=True 用 microprice，否則 simple mid。"""
        ob = await self.lm.fetch_orderbook(self.cfg.slug)
        if ob is None:
            return None

        yb = ob.yes_best_bid
        ya = ob.yes_best_ask

        # 更新 toxicity 歷史（best ask 軌跡）
        if ya is not None:
            self._tox.lm_best_asks.append(ya.price)

        if self.cfg.use_microprice and yb and ya and yb.size_raw > 0 and ya.size_raw > 0:
            # microprice: 對手側量越大 → 公平價越靠該側
            # 標準公式：(bid * ask_size + ask * bid_size) / (bid_size + ask_size)
            bs = yb.size_raw
            as_ = ya.size_raw
            yes_mid = (yb.price * as_ + ya.price * bs) / (bs + as_)
        elif yb and ya:
            yes_mid = (yb.price + ya.price) / 2
        elif ob.midpoint is not None:
            yes_mid = ob.midpoint
        else:
            return None

        # clamp
        yes_mid = max(0.01, min(0.99, yes_mid))
        return yes_mid, 1.0 - yes_mid

    async def _try_fetch_pm_mirror_yes(self) -> float | None:
        if not hasattr(self, "_pm_match_cache"):
            self._pm_match_cache = await self._compute_pm_mirror_yes()
        if self._pm_match_cache is None:
            return None
        pm_market_id = self._pm_match_cache
        from .polymarket.clients import GammaClient
        async with GammaClient() as g:
            try:
                r = await g._client.get(f"/markets/{pm_market_id}")
                r.raise_for_status()
                raw = r.json()
                from .polymarket.models import Market as PMMarket
                pm = PMMarket.from_gamma(raw if isinstance(raw, dict) else raw[0])
                if pm is None or not pm.outcome_prices:
                    return None
                yes = pm.outcome_prices[0]
                return yes if 0 < yes < 1 else None
            except Exception:
                return None

    async def _compute_pm_mirror_yes(self) -> str | None:
        if self.market is None:
            return None
        lm_title = self.market.get("title", "")
        from .crossarb import _strict_question_match
        from .polymarket.clients import GammaClient
        async with GammaClient() as g:
            evs = await g.fetch_active_events(max_events=300)
        for ev in evs:
            for pm in ev.markets:
                if pm.is_tradeable and _strict_question_match(lm_title, pm.question) >= 1.0:
                    return pm.id
        return None

    # ---------- Toxicity 偵測 ----------

    def _record_fill(self, inv: Inventory | None) -> None:
        """根據庫存變化推估本輪 fill。庫存只增不減（假設我們是 maker BUY）→
        無法區分「掛單成交」vs「外部成交」，但對於我們本身只下 BUY 的場景夠用。"""
        if inv is None:
            self._tox.yes_fills.append(0.0)
            self._tox.no_fills.append(0.0)
            return
        dy = max(0.0, inv.yes_shares - self._tox.last_yes_inv)
        dn = max(0.0, inv.no_shares - self._tox.last_no_inv)
        self._tox.yes_fills.append(dy)
        self._tox.no_fills.append(dn)
        self._tox.last_yes_inv = inv.yes_shares
        self._tox.last_no_inv = inv.no_shares

    def _record_pm_mid(self, pm_yes: float | None) -> None:
        if pm_yes is not None:
            self._tox.pm_mids.append(pm_yes)

    def assess_toxicity(self) -> ToxicityAssessment:
        """根據 fill 不對稱 / PM velocity / LM best ask drop 評估逆選擇強度。"""
        a = ToxicityAssessment()
        W = self.cfg.toxicity_window

        # 1) Fill 不對稱
        yes_total = sum(list(self._tox.yes_fills)[-W:])
        no_total = sum(list(self._tox.no_fills)[-W:])
        denom = yes_total + no_total + 1e-6
        if (yes_total + no_total) > 1e-3:
            imbalance = abs(yes_total - no_total) / denom
            if imbalance > self.cfg.toxicity_imbalance_threshold:
                # 哪邊被吃多 → 那邊就是被資訊套利的方向 → 加寬該側
                if yes_total > no_total:
                    a.yes_spread_mult = self.cfg.toxicity_widen_multiplier
                    a.reasons.append(f"YES fill {yes_total:.1f} >> NO {no_total:.1f} (imbalance {imbalance:.2f})")
                else:
                    a.no_spread_mult = self.cfg.toxicity_widen_multiplier
                    a.reasons.append(f"NO fill {no_total:.1f} >> YES {yes_total:.1f} (imbalance {imbalance:.2f})")
                a.score += imbalance

        # 2) PM velocity（若有 oracle）
        pm_hist = list(self._tox.pm_mids)[-W:]
        if len(pm_hist) >= 2:
            velocity = abs(pm_hist[-1] - pm_hist[0])
            if velocity > self.cfg.toxicity_pm_velocity_threshold:
                if self.cfg.toxicity_pull_on_pm_jump:
                    a.pull_yes = True
                    a.pull_no = True
                    a.reasons.append(f"PM 漂移 {velocity:.3f} > {self.cfg.toxicity_pm_velocity_threshold:.3f}，撤所有單")
                else:
                    a.yes_spread_mult = max(a.yes_spread_mult, self.cfg.toxicity_widen_multiplier)
                    a.no_spread_mult = max(a.no_spread_mult, self.cfg.toxicity_widen_multiplier)
                    a.reasons.append(f"PM 漂移 {velocity:.3f}，加寬兩側")
                a.score += velocity * 10

        # 3) LM best ask drop（YES side）
        ask_hist = list(self._tox.lm_best_asks)[-W:]
        if len(ask_hist) >= 2:
            drop = ask_hist[0] - ask_hist[-1]
            if drop > self.cfg.toxicity_ask_drop_threshold:
                # YES ask 在下殺 → 有人在掃 YES → 我們的 BUY NO 即將被 hit（NO 變便宜）
                a.pull_no = True
                a.reasons.append(f"YES best ask 下殺 {drop:.3f}，撤 NO bid")
                a.score += drop * 5

        return a

    # ---------- 報價計算 ----------

    def compute_target_prices(
        self,
        yes_mid: float,
        no_mid: float,
        inventory: Inventory | None = None,
        toxicity: ToxicityAssessment | None = None,
    ) -> tuple[float, float]:
        """根據 mid + 庫存 + toxicity 計算 BUY YES + BUY NO 的目標掛單價。"""
        base_offset = self.cfg.half_spread_offset_pct / 100
        yes_offset = base_offset * (toxicity.yes_spread_mult if toxicity else 1.0)
        no_offset = base_offset * (toxicity.no_spread_mult if toxicity else 1.0)

        raw_yes_bid = max(0.01, yes_mid - yes_offset)
        raw_no_bid = max(0.01, no_mid - no_offset)

        # Inventory skew
        if inventory is not None and self.cfg.max_inventory_shares > 0:
            skew_ratio = self.cfg.inventory_skew_pct / 100
            net = inventory.yes_shares - inventory.no_shares
            skew_units = net / (self.cfg.max_inventory_shares * 0.1)
            if skew_units > 0:
                raw_yes_bid = max(0.01, raw_yes_bid - skew_units * skew_ratio)
            else:
                raw_no_bid = max(0.01, raw_no_bid - abs(skew_units) * skew_ratio)

        # 校正讓總和 = 1 - target_profit_pct/100
        target_sum = max(0.01, 1.0 - self.cfg.target_profit_pct / 100)
        actual_sum = raw_yes_bid + raw_no_bid
        if actual_sum > target_sum:
            excess = actual_sum - target_sum
            raw_yes_bid -= excess / 2
            raw_no_bid -= excess / 2

        yes_bid = max(0.01, min(0.99, round(raw_yes_bid, 3)))
        no_bid = max(0.01, min(0.99, round(raw_no_bid, 3)))
        return yes_bid, no_bid

    def compute_unwind_prices(
        self,
        yes_mid: float,
        no_mid: float,
        inventory: Inventory,
    ) -> tuple[float | None, float | None]:
        """根據庫存決定是否掛 SELL，回傳 (yes_sell_price | None, no_sell_price | None)。

        - 庫存 YES 超過 max × unwind_inventory_pct → 掛 SELL YES @ yes_mid + premium
        - 庫存 NO  超過 max × unwind_inventory_pct → 掛 SELL NO  @ no_mid  + premium
        """
        threshold = self.cfg.max_inventory_shares * self.cfg.unwind_inventory_pct
        premium = self.cfg.unwind_premium_pct / 100

        yes_sell = None
        no_sell = None

        if inventory.yes_shares > threshold:
            p = yes_mid + premium
            yes_sell = max(0.01, min(0.99, round(p, 3)))

        if inventory.no_shares > threshold:
            p = no_mid + premium
            no_sell = max(0.01, min(0.99, round(p, 3)))

        return yes_sell, no_sell

    # ---------- 庫存 / 取消 ----------

    async def fetch_inventory(self) -> Inventory | None:
        """讀目前 YES / NO 持倉。若 portfolio API 失敗則回 None。"""
        try:
            from limitless_sdk.portfolio import PortfolioFetcher
            pf = PortfolioFetcher(self.tc._sdk_client.http)
            positions = await pf.get_positions()
        except Exception:
            return None

        yes = no = 0.0
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
        try:
            if not self.tc.safety.require_explicit_execute:
                await self.tc._order_client.cancel_all(self.cfg.slug)
        except Exception:
            pass

    # ---------- 緊急清倉 ----------

    async def emergency_unwind(self, inv: Inventory | None) -> dict[str, Any]:
        """結算窗口內：撤所有單 + 用 FAK aggressive 市價賣掉所有庫存。"""
        await self.cancel_all()
        result: dict[str, Any] = {"cancelled_all": True, "sells": []}

        if inv is None or (inv.yes_shares <= 0 and inv.no_shares <= 0):
            return result

        # 拿當下 orderbook 估市價（取對手 best bid 略低）
        ob = await self.lm.fetch_orderbook(self.cfg.slug)
        if ob is None:
            result["error"] = "無 orderbook，無法估市價清倉"
            return result

        async def _market_sell(token: str, label: str, size: float, best_bid_price: float) -> None:
            if size <= 0:
                return
            # 用 best bid - 1 tick 確保成交（aggressive taker）
            px = max(0.01, min(0.99, round(best_bid_price - 0.001, 3)))
            req = OrderRequest(
                market_slug=self.cfg.slug,
                token_id=token,
                side="SELL",
                price=px,
                size_shares=size,
                order_type="FAK",
            )
            res = await self.tc.place_order(req)
            result["sells"].append({
                "side": f"SELL {label}",
                "price": px,
                "size": size,
                "accepted": res.accepted,
                "dry_run": res.dry_run,
                "error": res.error,
            })

        if inv.yes_shares > 0 and ob.yes_best_bid:
            await _market_sell(self.yes_token, "YES", inv.yes_shares, ob.yes_best_bid.price)
        if inv.no_shares > 0 and ob.no_best_bid:
            await _market_sell(self.no_token, "NO", inv.no_shares, ob.no_best_bid.price)

        return result

    # ---------- 主 iterate ----------

    async def iterate(self) -> IterationResult:
        notes: list[str] = []

        # 0. 結算偵測（最優先）
        if self.is_emergency_window():
            hrs = self.hours_to_resolution()
            notes.append(f"⚠️ 距結算 {hrs:.1f}h < {self.cfg.emergency_close_hours}h，啟動緊急清倉")
            inv = await self.fetch_inventory()
            if inv is not None:
                notes.append(f"清倉前庫存：YES={inv.yes_shares:.1f}, NO={inv.no_shares:.1f}")
            unwind = await self.emergency_unwind(inv)
            for s in unwind.get("sells", []):
                tag = "dry" if s["dry_run"] else ("OK" if s["accepted"] else "FAIL")
                notes.append(f"  {s['side']} @${s['price']:.3f} × {s['size']:.1f} [{tag}] {s.get('error') or ''}")
            # Telegram 通知緊急清倉
            try:
                notify.emergency_close(
                    self.cfg.slug, hrs,
                    inv.yes_shares if inv else 0,
                    inv.no_shares if inv else 0,
                )
            except Exception:
                pass
            return IterationResult(
                yes_bid_price=0, no_bid_price=0,
                yes_order_accepted=False, no_order_accepted=False,
                emergency_close=True, notes=notes,
            )

        # 1. 算 mid（含 microprice + oracle）
        mids = await self.fetch_mid()
        if mids is None:
            notes.append("無法取得 mid（orderbook 空）")
            return IterationResult(0, 0, False, False, notes=notes)
        yes_mid, no_mid = mids
        microprice_tag = "microprice" if self.cfg.use_microprice else "mid"
        notes.append(f"YES {microprice_tag}=${yes_mid:.3f}, NO {microprice_tag}=${no_mid:.3f} (oracle={self.cfg.oracle_mode})")

        # 1b. 把 PM mid 餵進 toxicity 歷史（若 oracle 用了 PM）
        if self.cfg.oracle_mode != "lm":
            pm_yes = await self._try_fetch_pm_mirror_yes()
            self._record_pm_mid(pm_yes)

        # 2. 庫存(偵測 fill = 庫存增加,在 _record_fill 更新前抓 delta)
        inv = await self.fetch_inventory()
        if inv is not None:
            notes.append(f"庫存：YES={inv.yes_shares:.1f}, NO={inv.no_shares:.1f}")
            yes_delta = inv.yes_shares - self._tox.last_yes_inv
            no_delta = inv.no_shares - self._tox.last_no_inv
            if yes_delta > 0.01:
                notes.append(f"  📊 YES fill +{yes_delta:.1f} 股")
                try:
                    notify.fill_detected(self.cfg.slug, "YES", yes_delta, yes_mid)
                except Exception:
                    pass
            if no_delta > 0.01:
                notes.append(f"  📊 NO fill +{no_delta:.1f} 股")
                try:
                    notify.fill_detected(self.cfg.slug, "NO", no_delta, no_mid)
                except Exception:
                    pass
            self._record_fill(inv)
        else:
            self._record_fill(None)

        # 3. Toxicity 評估
        tox = self.assess_toxicity()
        if tox.reasons:
            notes.append(f"🛑 toxicity={tox.score:.2f}: " + " | ".join(tox.reasons))
            # Telegram 通知:toxicity 觸發是罕見大事
            try:
                notify.toxicity_detected(self.cfg.slug, tox.score, tox.reasons)
            except Exception:
                pass
        elif tox.score > 0:
            notes.append(f"toxicity={tox.score:.2f}")

        # 4. 庫存上限：BUY 那邊不下，但 SELL 仍可掛
        skip_buys = False
        if inv is not None and inv.max_side >= self.cfg.max_inventory_shares:
            notes.append(f"⚠️ 庫存達上限 {self.cfg.max_inventory_shares}，本輪不下 BUY（仍嘗試 SELL 清倉）")
            skip_buys = True

        # 5. 算目標 BUY 價
        yes_bid, no_bid = self.compute_target_prices(yes_mid, no_mid, inv, tox)
        notes.append(f"BUY YES @${yes_bid:.3f} + BUY NO @${no_bid:.3f}, 和=${yes_bid + no_bid:.3f}")

        # 6. 資本檢查
        notional_buys = (yes_bid + no_bid) * self.cfg.quote_size_shares
        capital_ok = (self._capital_used + notional_buys) <= self.cfg.capital_usdc
        if not capital_ok:
            notes.append(f"⚠️ 累計資本 ${self._capital_used:.2f} + 本輪 ${notional_buys:.2f} > ${self.cfg.capital_usdc}，跳過 BUY")
            skip_buys = True

        # 7. 取消舊單（一次性）
        await self.cancel_all()

        yes_bid_accepted = False
        no_bid_accepted = False

        # 8. 掛 BUY 單（套 toxicity pull）
        if not skip_buys:
            if not tox.pull_yes:
                yes_req = OrderRequest(
                    market_slug=self.cfg.slug, token_id=self.yes_token,
                    side="BUY", price=yes_bid, size_shares=self.cfg.quote_size_shares,
                    order_type="GTC", post_only=True,
                )
                yes_res = await self.tc.place_order(yes_req)
                yes_bid_accepted = yes_res.accepted
                notes.append(f"  YES BUY {'OK' if yes_res.accepted else 'REJ:' + (yes_res.error or '?')} ({'dry' if yes_res.dry_run else 'live'})")
            else:
                notes.append("  YES BUY 撤（toxicity）")

            if not tox.pull_no:
                no_req = OrderRequest(
                    market_slug=self.cfg.slug, token_id=self.no_token,
                    side="BUY", price=no_bid, size_shares=self.cfg.quote_size_shares,
                    order_type="GTC", post_only=True,
                )
                no_res = await self.tc.place_order(no_req)
                no_bid_accepted = no_res.accepted
                notes.append(f"  NO  BUY {'OK' if no_res.accepted else 'REJ:' + (no_res.error or '?')} ({'dry' if no_res.dry_run else 'live'})")
            else:
                notes.append("  NO  BUY 撤（toxicity）")

            if yes_bid_accepted and no_bid_accepted:
                self._capital_used += notional_buys

        # 9. 主動 SELL 清庫存
        yes_sell_price = no_sell_price = 0.0
        yes_sell_accepted = no_sell_accepted = False
        if inv is not None:
            yes_sell, no_sell = self.compute_unwind_prices(yes_mid, no_mid, inv)
            unwind_size = self.cfg.unwind_size_shares or self.cfg.quote_size_shares

            if yes_sell is not None and inv.yes_shares > 0:
                size = min(unwind_size, inv.yes_shares)
                req = OrderRequest(
                    market_slug=self.cfg.slug, token_id=self.yes_token,
                    side="SELL", price=yes_sell, size_shares=size,
                    order_type="GTC", post_only=True,
                )
                res = await self.tc.place_order(req)
                yes_sell_price = yes_sell
                yes_sell_accepted = res.accepted
                notes.append(f"  SELL YES @${yes_sell:.3f} × {size:.1f} {'OK' if res.accepted else 'REJ:' + (res.error or '?')} (清庫存)")

            if no_sell is not None and inv.no_shares > 0:
                size = min(unwind_size, inv.no_shares)
                req = OrderRequest(
                    market_slug=self.cfg.slug, token_id=self.no_token,
                    side="SELL", price=no_sell, size_shares=size,
                    order_type="GTC", post_only=True,
                )
                res = await self.tc.place_order(req)
                no_sell_price = no_sell
                no_sell_accepted = res.accepted
                notes.append(f"  SELL NO  @${no_sell:.3f} × {size:.1f} {'OK' if res.accepted else 'REJ:' + (res.error or '?')} (清庫存)")

        # 10. PnL 紀錄(v0.9)— 偵測 fills + 寫 iteration snapshot
        try:
            if inv is not None:
                fills = pnl_db.detect_and_record_fills(
                    self.cfg.slug, inv.yes_shares, inv.no_shares,
                )
                for f in fills:
                    notes.append(f"  📊 detected fill: {f['outcome']} {f['shares']:.1f} @ ${f['price']:.3f}")

            pnl_db.record_iteration(
                slug=self.cfg.slug,
                yes_bid=yes_bid, no_bid=no_bid,
                yes_sell=yes_sell_price, no_sell=no_sell_price,
                yes_inventory=(inv.yes_shares if inv else 0),
                no_inventory=(inv.no_shares if inv else 0),
                capital_used=self._capital_used,
                toxicity_score=tox.score,
                notes=" | ".join(notes[-3:]) if notes else None,
            )
        except Exception as e:
            notes.append(f"  ⚠️ PnL DB 寫入失敗: {e}")

        return IterationResult(
            yes_bid_price=yes_bid, no_bid_price=no_bid,
            yes_order_accepted=yes_bid_accepted, no_order_accepted=no_bid_accepted,
            yes_sell_price=yes_sell_price, no_sell_price=no_sell_price,
            yes_sell_accepted=yes_sell_accepted, no_sell_accepted=no_sell_accepted,
            toxicity_score=tox.score, notes=notes,
        )

    async def run(self, on_iteration=None) -> dict:
        await self.init_market()
        start = asyncio.get_event_loop().time()
        iter_count = 0
        last_yes_bid = last_no_bid = 0.0
        emergency_triggered = False

        while True:
            iter_count += 1
            result = await self.iterate()
            if on_iteration:
                on_iteration(iter_count, result)
            last_yes_bid, last_no_bid = result.yes_bid_price, result.no_bid_price

            if result.emergency_close:
                emergency_triggered = True
                break

            if self.cfg.duration_s > 0:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed >= self.cfg.duration_s:
                    break
            await asyncio.sleep(self.cfg.iteration_sleep_s)

        await self.cancel_all()
        return {
            "iterations": iter_count,
            "capital_used": self._capital_used,
            "last_yes_bid": last_yes_bid,
            "last_no_bid": last_no_bid,
            "emergency_close": emergency_triggered,
        }
