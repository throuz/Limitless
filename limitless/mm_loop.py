"""24/7 自動做市調度器（v0.7）。

職責：
- 定期跑 mm-rank,挑出 top N 個市場
- 對每個市場開一個 MarketMaker 並行跑(asyncio task)
- 任一市場結算 / 觸發 emergency_close → 自動移除,挑下一個
- SIGTERM / SIGINT 收到 → 全部 cancel + 收乾淨
- 全部行為都尊重 LimitlessTradingClient 的 session_notional 上限,當作硬性 cap

設計選擇：
- 共用同一個 LimitlessTradingClient(因此 max_per_session 是全域 cap,正合需求)
- 共用同一個 LimitlessClient(讀取 orderbook,內建並發限制)
- 每個 slug 一個 MarketMaker + 一個 asyncio.Task
- 不重複跑同 slug(若 mm-rank 又選到同一個就略過)
"""

from __future__ import annotations

import asyncio
import math
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .client import LimitlessClient
from .market_maker import MakerConfig, MarketMaker, IterationResult
from .trading import LimitlessTradingClient


# 與 cli._news_risk_score 同一份關鍵字。重複定義避免 cli 對 mm_loop 的反向依賴。
_NEWS_RISK_KEYWORDS = [
    "election", "court", "ruling", "verdict", "vote", "primary", "debate",
    "impeach", "indict", "supreme",
    "fed", "fomc", "cpi", "ppi", "gdp", "payroll", "nfp", "jobless",
    "rate hike", "rate cut", "powell", "ecb",
    "earnings", "ipo", "merger", "acquisition", "lawsuit",
    "tonight", "today", "match", "game",
    "選舉", "判決", "投票", "央行", "升息", "降息", "通膨", "財報",
]


def _news_risk_score(title: str) -> float:
    t = title.lower()
    return float(sum(1 for kw in _NEWS_RISK_KEYWORDS if kw in t))


def _parse_end_date_days(date_str: str | None) -> float | None:
    if not date_str:
        return None
    s = str(date_str)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except ValueError:
            continue
    return None


@dataclass
class MMLoopConfig:
    """24/7 調度器設定。"""

    # 全域上限
    total_capital_usdc: float = 500.0      # 全部市場合計總資本上限
    max_positions: int = 3                 # 同時做幾個市場
    capital_per_market: float = 100.0      # 單一市場最多多少資本(會傳給 MakerConfig.capital_usdc)
    quote_size_shares: float = 10.0
    target_profit_pct: float = 4.0
    half_spread_pct: float = 1.0
    max_inventory_shares: float = 30.0

    # mm-rank 篩選
    rank_max_markets: int = 500
    rank_min_volume_usd: float = 200.0
    rank_min_days: float = 2.0             # 距結算 < N 天 → 跳過(避免結算 risk + 預留 emergency window)
    rank_min_spread_bps: int = 100
    rank_max_news_risk: float = 2.0        # title 命中關鍵字 > 此數 → 跳過

    # 行為
    rank_refresh_seconds: int = 3600       # 多久重新跑一次 mm-rank
    iteration_sleep_s: float = 30.0        # 每個 MM 的重評間隔
    duration_per_market_s: int = 0          # 0 = 無上限,跑到結算自動換
    oracle_mode: str = "pm"                # 預設用 PM oracle(更抗資訊套利)
    use_microprice: bool = True
    emergency_close_hours: float = 24.0

    # 是否真實下單
    execute: bool = False

    @classmethod
    def from_env(cls) -> "MMLoopConfig":
        b = lambda k, d: os.environ.get(k, "1" if d else "0") == "1"
        f = lambda k, d: float(os.environ.get(k, str(d)))
        i = lambda k, d: int(os.environ.get(k, str(d)))
        s = lambda k, d: os.environ.get(k, d)
        return cls(
            total_capital_usdc=f("MM_LOOP_TOTAL_CAPITAL", 500),
            max_positions=i("MM_LOOP_MAX_POSITIONS", 3),
            capital_per_market=f("MM_LOOP_CAPITAL_PER_MARKET", 100),
            quote_size_shares=f("MM_LOOP_QUOTE_SIZE", 10),
            target_profit_pct=f("MM_LOOP_TARGET_PROFIT_PCT", 4),
            half_spread_pct=f("MM_LOOP_HALF_SPREAD_PCT", 1),
            max_inventory_shares=f("MM_LOOP_MAX_INVENTORY", 30),
            rank_max_markets=i("MM_LOOP_RANK_MAX_MARKETS", 500),
            rank_min_volume_usd=f("MM_LOOP_RANK_MIN_VOLUME", 200),
            rank_min_days=f("MM_LOOP_RANK_MIN_DAYS", 2),
            rank_min_spread_bps=i("MM_LOOP_RANK_MIN_SPREAD_BPS", 100),
            rank_max_news_risk=f("MM_LOOP_RANK_MAX_NEWS_RISK", 2),
            rank_refresh_seconds=i("MM_LOOP_RANK_REFRESH_S", 3600),
            iteration_sleep_s=f("MM_LOOP_ITER_SLEEP_S", 30),
            duration_per_market_s=i("MM_LOOP_DURATION_PER_MARKET_S", 0),
            oracle_mode=s("MM_LOOP_ORACLE", "pm"),
            use_microprice=b("MM_LOOP_USE_MICROPRICE", True),
            emergency_close_hours=f("MM_LOOP_EMERGENCY_HOURS", 24),
            execute=os.environ.get("LIMITLESS_EXECUTE", "0") == "1",
        )


@dataclass
class SessionStats:
    """單一市場 session 的累計統計。"""
    slug: str
    title: str
    started_at: float
    iterations: int = 0
    emergency_closes: int = 0
    last_yes_bid: float = 0.0
    last_no_bid: float = 0.0
    last_toxicity: float = 0.0
    capital_used: float = 0.0


@dataclass
class LoopStats:
    """整個 loop 的統計。"""
    started_at: float = field(default_factory=lambda: 0.0)
    total_iterations: int = 0
    sessions_completed: int = 0
    sessions_emergency: int = 0
    active: dict[str, SessionStats] = field(default_factory=dict)
    completed: list[SessionStats] = field(default_factory=list)


class MMLoop:
    """調度器:挑市場、開 session、處理結算與停機。"""

    def __init__(
        self,
        cfg: MMLoopConfig,
        trading_client: LimitlessTradingClient,
        lm_client: LimitlessClient,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.cfg = cfg
        self.tc = trading_client
        self.lm = lm_client
        self.on_event = on_event or (lambda kind, data: None)
        self._tasks: dict[str, asyncio.Task] = {}
        self._makers: dict[str, MarketMaker] = {}
        self._stats = LoopStats()
        self._shutdown = asyncio.Event()

    # ---------- 市場篩選 ----------

    async def rank_markets(self) -> list[tuple[str, str, float]]:
        """跑 mm-rank 邏輯，回傳 [(slug, title, score), ...] 由高到低。"""
        singles, _ = await self.lm.fetch_active_markets(max_markets=self.cfg.rank_max_markets)
        candidates = [m for m in singles
                      if m.is_tradeable and m.volume_usd >= self.cfg.rank_min_volume_usd]
        with_days = []
        for m in candidates:
            d = _parse_end_date_days(m.end_date)
            if d is None or d < self.cfg.rank_min_days:
                continue
            risk = _news_risk_score(m.title)
            if risk > self.cfg.rank_max_news_risk:
                continue
            with_days.append((m, d, risk))
        if not with_days:
            return []

        slugs = [m.slug for m, _, _ in with_days]
        books = await self.lm.fetch_orderbooks(slugs)

        rows: list[tuple[str, str, float]] = []
        for m, days, risk in with_days:
            ob = books.get(m.slug)
            if ob is None:
                continue
            yb, ya = ob.yes_best_bid, ob.yes_best_ask
            if not yb or not ya:
                continue
            spread_pp = (ya.price - yb.price) * 100
            if spread_pp * 100 < self.cfg.rank_min_spread_bps:
                continue
            mid = (yb.price + ya.price) / 2
            if not (0.05 < mid < 0.95):
                continue
            days_factor = min(days, 30) / 7
            pa_bonus = 1.3 if m.is_poly_arbitrage else 1.0
            vol_factor = 1 + math.log(1 + m.volume_usd / 1000)
            score = spread_pp * days_factor * pa_bonus * vol_factor / (1 + risk)
            rows.append((m.slug, m.title, score))
        rows.sort(key=lambda r: -r[2])
        return rows

    async def pick_next_slugs(self, n: int) -> list[tuple[str, str]]:
        """挑 n 個還沒在跑的 slug,回傳 [(slug, title)]。"""
        ranked = await self.rank_markets()
        out: list[tuple[str, str]] = []
        for slug, title, _ in ranked:
            if slug in self._makers:
                continue
            out.append((slug, title))
            if len(out) >= n:
                break
        return out

    # ---------- 全域資本檢查 ----------

    def remaining_global_capital(self) -> float:
        used = sum(mm._capital_used for mm in self._makers.values())
        return max(0.0, self.cfg.total_capital_usdc - used)

    # ---------- 啟動單一 session ----------

    async def start_session(self, slug: str, title: str) -> None:
        """開新 MarketMaker + asyncio task。"""
        # 為這個市場分配多少資本(若全域剩餘不夠,壓到剩餘)
        cap = min(self.cfg.capital_per_market, self.remaining_global_capital())
        if cap < 5:
            self.on_event("skip_no_capital", {"slug": slug, "remaining": cap})
            return

        mc = MakerConfig(
            slug=slug,
            capital_usdc=cap,
            quote_size_shares=self.cfg.quote_size_shares,
            target_profit_pct=self.cfg.target_profit_pct,
            half_spread_offset_pct=self.cfg.half_spread_pct,
            max_inventory_shares=self.cfg.max_inventory_shares,
            iteration_sleep_s=self.cfg.iteration_sleep_s,
            duration_s=self.cfg.duration_per_market_s,
            oracle_mode=self.cfg.oracle_mode,
            use_microprice=self.cfg.use_microprice,
            emergency_close_hours=self.cfg.emergency_close_hours,
        )
        mm = MarketMaker(mc, self.tc, self.lm)
        self._makers[slug] = mm
        stats = SessionStats(slug=slug, title=title,
                             started_at=asyncio.get_event_loop().time())
        self._stats.active[slug] = stats

        self.on_event("session_start", {"slug": slug, "title": title, "capital": cap})

        async def _run():
            try:
                await mm.init_market()
            except Exception as e:
                self.on_event("session_error", {"slug": slug, "phase": "init", "error": str(e)})
                return

            def _on_iter(n: int, result: IterationResult) -> None:
                stats.iterations = n
                stats.last_yes_bid = result.yes_bid_price
                stats.last_no_bid = result.no_bid_price
                stats.last_toxicity = result.toxicity_score
                stats.capital_used = mm._capital_used
                if result.emergency_close:
                    stats.emergency_closes += 1
                self._stats.total_iterations += 1
                self.on_event("iteration", {
                    "slug": slug, "n": n,
                    "yes_bid": result.yes_bid_price, "no_bid": result.no_bid_price,
                    "toxicity": result.toxicity_score,
                    "emergency": result.emergency_close,
                    "notes": result.notes,
                })

            try:
                run_stats = await mm.run(on_iteration=_on_iter)
                self.on_event("session_end", {
                    "slug": slug,
                    "iterations": run_stats.get("iterations", 0),
                    "capital_used": run_stats.get("capital_used", 0),
                    "emergency": run_stats.get("emergency_close", False),
                })
                if run_stats.get("emergency_close"):
                    self._stats.sessions_emergency += 1
            except asyncio.CancelledError:
                # shutdown
                await mm.cancel_all()
                raise
            except Exception as e:
                self.on_event("session_error", {"slug": slug, "phase": "run", "error": str(e)})
            finally:
                self._stats.sessions_completed += 1
                if slug in self._stats.active:
                    self._stats.completed.append(self._stats.active.pop(slug))
                self._makers.pop(slug, None)
                self._tasks.pop(slug, None)

        task = asyncio.create_task(_run(), name=f"mm:{slug}")
        self._tasks[slug] = task

    # ---------- 主迴圈 ----------

    async def main_loop(self) -> LoopStats:
        """直到 shutdown event 觸發。"""
        self._stats.started_at = asyncio.get_event_loop().time()
        self.on_event("loop_start", {
            "max_positions": self.cfg.max_positions,
            "total_capital": self.cfg.total_capital_usdc,
            "execute": self.cfg.execute,
        })

        try:
            while not self._shutdown.is_set():
                # 1. 看還缺幾個 session
                # 自動清理:已 done 的 task 會自己從 _tasks pop(在 _run finally)
                active_count = len(self._tasks)
                deficit = self.cfg.max_positions - active_count

                if deficit > 0 and self.remaining_global_capital() >= 5:
                    new_slugs = await self.pick_next_slugs(deficit)
                    if new_slugs:
                        self.on_event("rank_picked", {
                            "count": len(new_slugs),
                            "slugs": [s for s, _ in new_slugs],
                        })
                        for slug, title in new_slugs:
                            await self.start_session(slug, title)
                    else:
                        self.on_event("rank_empty", {})

                # 2. 等待:rank_refresh_seconds 或 shutdown
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self.cfg.rank_refresh_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.graceful_shutdown()

        return self._stats

    async def graceful_shutdown(self) -> None:
        """收到 shutdown 訊號:取消所有 task,讓每個 MM 的 finally 收乾淨。"""
        self.on_event("shutdown_start", {"active": list(self._tasks.keys())})

        for slug, task in list(self._tasks.items()):
            task.cancel()

        if self._tasks:
            # 等所有 task 結束(MM 的 cancel 內已含 cancel_all)
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        self.on_event("shutdown_done", {
            "completed": self._stats.sessions_completed,
            "emergency": self._stats.sessions_emergency,
        })

    def request_shutdown(self) -> None:
        """從訊號 handler 呼叫。"""
        self._shutdown.set()


# ---------- 訊號 handler 安裝 ----------

def install_signal_handlers(loop: MMLoop) -> None:
    """讓 SIGTERM / SIGINT 觸發 graceful shutdown(asyncio-safe)。"""
    aloop = asyncio.get_event_loop()

    def _handler():
        loop.request_shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            aloop.add_signal_handler(sig, _handler)
        except (NotImplementedError, RuntimeError):
            # Windows / 某些環境不支援 add_signal_handler;直接掛 signal
            signal.signal(sig, lambda *_: loop.request_shutdown())
