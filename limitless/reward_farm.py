"""Reward farming 模式 — 賺 Limitless LP 流動性獎勵,而非賺 spread。

與 market_maker.py 完全獨立(不碰那套已驗證的雙 BID 做市邏輯)。

機制(= Polymarket LP rewards 模型,Limitless 照抄,連 c=3 都一樣):
- orderbook 端點直接回傳 `adjustedMidpoint`(M)、`maxSpread`(v)、`minSize`。
- 在距 M ≤ v 內掛「雙邊」GTC post_only 單(BUY YES @ M−δ + BUY NO @ (1−M)−δ),
  按「在帶內的 size × 近 mid 程度²」每分鐘抽樣計分,分數佔比 × 每日獎勵池 = USDC 獎勵。
  **不需要成交。**
- M ∈ (0.90,1.0] 或 [0,0.10) 時公式規定「只有雙邊計分」→ 本模式一律雙邊掛。

風險:貼 mid 會被成交 → 吃逆選擇。但 5/15-min 市場結算快,庫存風險鎖在單一窗口;
本模式在距結算 `pull_before_settlement_s` 內撤掉所有單,避開到期 snap。

實驗目標:量「實領 USDC 獎勵 vs 被成交的逆選擇虧損」。正的才放大 size / 市場數。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import LimitlessClient
from .trading import LimitlessTradingClient, OrderRequest

PRICE_TICK = 0.001


@dataclass
class RewardFarmConfig:
    coins: list[str]                      # 例 ["BTC", "ETH"]
    freq: str = "15 Min"                  # "5 Min" / "15 Min" / "Hourly"
    size_shares: float = 100.0            # 每筆掛單股數(需 ≥ minSize 才合格)
    edge_frac: float = 0.7                # δ = edge_frac × maxSpread(越小越貼 mid:分數高但易被吃)
    poll_interval_s: float = 20.0         # 每輪間隔
    pull_before_settlement_s: float = 45.0  # 距結算 < 此秒數 → 撤所有單
    duration_s: int = 0                   # 0 = 跑到 Ctrl-C
    recenter_eps: float = 0.004           # 目標價移動 > 此值才取消重掛(否則維持掛單)


@dataclass
class MarketRuntime:
    slug: str
    yes_token: str
    no_token: str
    expiration_ts: float                  # epoch 秒
    last_yes_bid: float = 0.0
    last_no_bid: float = 0.0
    last_yes_inv: float = 0.0
    last_no_inv: float = 0.0
    fills_yes: float = 0.0                 # 累計被成交股數(逆選擇暴露)
    fills_no: float = 0.0
    pulled: bool = False
    polls: int = 0                         # 處理過幾輪
    in_band_polls: int = 0                 # 其中「持有帶內合格雙邊單」的輪數(= 計分時段)


class RewardFarmer:
    def __init__(self, cfg: RewardFarmConfig, tc: LimitlessTradingClient, lm: LimitlessClient):
        self.cfg = cfg
        self.tc = tc
        self.lm = lm
        self._rt: dict[str, MarketRuntime] = {}      # slug -> runtime
        self._meta_cache: dict[str, dict] = {}        # slug -> market metadata

    # ---------- 市場發現(auto-roll:舊的到期就換新的) ----------

    async def _discover_slugs(self) -> list[str]:
        """每輪重新找符合 coin + freq 的當前活躍市場 slug(到期會換新)。"""
        all_m: list[dict] = []
        page = 1
        while page <= 12:
            r = await self.lm._get("/markets/active", params={"page": page, "limit": 25})
            d = r.json().get("data", [])
            if not d:
                break
            all_m += d
            page += 1
        wanted = []
        for coin in self.cfg.coins:
            pat = re.compile(rf"^{re.escape(coin)} Up or Down - {re.escape(self.cfg.freq)}$", re.I)
            match = next((m for m in all_m if pat.match(m.get("title", ""))), None)
            if match:
                wanted.append(match)
                self._meta_cache[match["slug"]] = match
        return [m["slug"] for m in wanted]

    def _runtime(self, slug: str) -> MarketRuntime | None:
        if slug in self._rt:
            return self._rt[slug]
        m = self._meta_cache.get(slug)
        if not m:
            return None
        tokens = m.get("tokens") or {}
        yes_t, no_t = tokens.get("yes"), tokens.get("no")
        if not (yes_t and no_t):
            return None
        exp = float(m.get("expirationTimestamp") or 0) / 1000.0
        if exp <= 0:
            return None
        rt = MarketRuntime(slug=slug, yes_token=str(yes_t), no_token=str(no_t), expiration_ts=exp)
        self._rt[slug] = rt
        return rt

    # ---------- 庫存(偵測成交 = 逆選擇暴露) ----------

    async def _inventory(self, slug: str) -> tuple[float, float]:
        try:
            from limitless_sdk.portfolio import PortfolioFetcher
            pf = PortfolioFetcher(self.tc._sdk_client.http)
            positions = await pf.get_positions()
        except Exception:
            return (0.0, 0.0)
        yes = no = 0.0
        for pos in (positions.get("clob") or []):
            mk = pos.get("market") or {}
            if mk.get("slug") != slug:
                continue
            bal = pos.get("tokensBalance") or {}
            try:
                yes += float(bal.get("yes") or 0) / 1_000_000
                no += float(bal.get("no") or 0) / 1_000_000
            except (TypeError, ValueError):
                pass
        return (yes, no)

    # ---------- 單一市場一輪 ----------

    async def _farm_market(self, slug: str, now: float) -> dict[str, Any]:
        rt = self._runtime(slug)
        if rt is None:
            return {"slug": slug, "note": "no-runtime(metadata 不足)"}

        secs_left = rt.expiration_ts - now
        rt.polls += 1

        # 偵測成交:庫存變化 = 被吃(逆選擇)
        yes_inv, no_inv = await self._inventory(slug)
        d_yes = max(0.0, yes_inv - rt.last_yes_inv)
        d_no = max(0.0, no_inv - rt.last_no_inv)
        rt.fills_yes += d_yes
        rt.fills_no += d_no
        rt.last_yes_inv, rt.last_no_inv = yes_inv, no_inv

        # 結算窗口:撤所有單,避開 snap
        if secs_left <= self.cfg.pull_before_settlement_s:
            if not rt.pulled:
                await self.tc.cancel_all(slug)
                rt.pulled = True
                rt.last_yes_bid = rt.last_no_bid = 0.0
            return {"slug": slug, "secs_left": round(secs_left), "action": "pulled(近結算)",
                    "inv": (round(yes_inv, 1), round(no_inv, 1)),
                    "fills": (round(rt.fills_yes, 1), round(rt.fills_no, 1))}
        rt.pulled = False

        # 讀獎勵基準
        try:
            r = await self.lm._get(f"/markets/{slug}/orderbook")
            ob = r.json()
        except Exception as e:
            return {"slug": slug, "note": f"orderbook ERR {e}"}
        M = ob.get("adjustedMidpoint")
        v = float(ob.get("maxSpread") or 0.035)
        if M is None:
            return {"slug": slug, "note": "無 adjustedMidpoint"}
        M = float(M)

        delta = min(self.cfg.edge_frac * v, v - PRICE_TICK)  # 留在帶內
        # M 太極端 → 某一側的 in-band 價會 < 0,雙邊無法都進帶內。
        # 而 M∈(0.90,1] / [0,0.10) 的 regime 規定「只有雙邊才計分」→ 掛了也領不到,
        # 還白白扛成交風險。撤掉舊單並跳過(等下一個窗口 M 回到中段再掛)。
        if (M - delta) < PRICE_TICK or ((1.0 - M) - delta) < PRICE_TICK:
            if rt.last_yes_bid > 0:
                await self.tc.cancel_all(slug)
                rt.last_yes_bid = rt.last_no_bid = 0.0
            return {"slug": slug, "secs_left": round(secs_left), "action": "skip(M極端)",
                    "M": M, "inv": (round(yes_inv, 1), round(no_inv, 1)),
                    "fills": (round(rt.fills_yes, 1), round(rt.fills_no, 1))}
        yes_bid = _clamp_tick(M - delta)
        no_bid = _clamp_tick((1.0 - M) - delta)
        if yes_bid <= 0 or no_bid <= 0 or yes_bid + no_bid >= 1.0:
            return {"slug": slug, "note": f"價格退化 M={M} → skip"}

        # 維持掛單:目標沒動 + 沒被吃 → 不動(省 API、不必要)
        moved = (abs(yes_bid - rt.last_yes_bid) >= self.cfg.recenter_eps
                 or abs(no_bid - rt.last_no_bid) >= self.cfg.recenter_eps)
        if rt.last_yes_bid > 0 and not moved and d_yes == 0 and d_no == 0:
            rt.in_band_polls += 1
            return {"slug": slug, "secs_left": round(secs_left), "action": "維持",
                    "M": M, "bids": (yes_bid, no_bid),
                    "inv": (round(yes_inv, 1), round(no_inv, 1)),
                    "fills": (round(rt.fills_yes, 1), round(rt.fills_no, 1))}

        # 重新置中:先撤再掛(避免新舊單雙倍暴露)
        await self.tc.cancel_all(slug)
        res_y = await self.tc.place_order(OrderRequest(
            market_slug=slug, token_id=rt.yes_token, side="BUY",
            price=yes_bid, size_shares=self.cfg.size_shares,
            order_type="GTC", post_only=True))
        res_n = await self.tc.place_order(OrderRequest(
            market_slug=slug, token_id=rt.no_token, side="BUY",
            price=no_bid, size_shares=self.cfg.size_shares,
            order_type="GTC", post_only=True))
        ok = res_y.accepted and res_n.accepted
        if ok:
            rt.last_yes_bid, rt.last_no_bid = yes_bid, no_bid
            rt.in_band_polls += 1
        return {"slug": slug, "secs_left": round(secs_left),
                "action": "重掛" + ("(dry)" if res_y.dry_run else ""),
                "M": M, "bids": (yes_bid, no_bid), "ok": ok,
                "err": (res_y.error or res_n.error),
                "inv": (round(yes_inv, 1), round(no_inv, 1)),
                "fills": (round(rt.fills_yes, 1), round(rt.fills_no, 1))}

    # ---------- 主迴圈 ----------

    async def run(self, on_round=None) -> dict[str, Any]:
        start = time.monotonic()
        round_n = 0
        try:
            while True:
                round_n += 1
                now = time.time()
                # 每輪重設 session notional:guardrail 改成「每輪」而非「累計」
                # (post_only 掛單不鎖資本;真實暴露 = 同時 resting 的單,有界)
                self.tc._session_notional = 0.0
                slugs = await self._discover_slugs()
                results = []
                for slug in slugs:
                    try:
                        results.append(await self._farm_market(slug, now))
                    except Exception as e:
                        results.append({"slug": slug, "note": f"ERR {type(e).__name__}: {e}"})
                if on_round:
                    on_round(round_n, results)
                if self.cfg.duration_s > 0 and (time.monotonic() - start) >= self.cfg.duration_s:
                    break
                await asyncio.sleep(self.cfg.poll_interval_s)
        finally:
            # 收尾:撤掉所有市場的單
            for slug in list(self._rt.keys()):
                try:
                    await self.tc.cancel_all(slug)
                except Exception:
                    pass
        return {
            "rounds": round_n,
            "fills": {s: (round(rt.fills_yes, 2), round(rt.fills_no, 2)) for s, rt in self._rt.items()},
            "in_band": {s: (rt.in_band_polls, rt.polls) for s, rt in self._rt.items()},
        }


def _clamp_tick(p: float) -> float:
    p = round(p / PRICE_TICK) * PRICE_TICK
    return round(max(PRICE_TICK, min(1.0 - PRICE_TICK, p)), 3)
