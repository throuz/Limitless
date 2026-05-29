"""Lambda rerank handler — 每小時由 EventBridge 觸發一次。

職責:
1. 載入 active markets list
2. 移除已結算 / 已 exhausted 的市場
3. 若 active 數量 < max_positions,跑 mm-rank,挑新市場補上
4. 為新市場初始化 MarketState
"""

from __future__ import annotations

import asyncio
import math
import time
import traceback
from datetime import datetime, timezone

from limitless.client import LimitlessClient
from limitless.serverless import (
    ServerlessCfg,
    bootstrap_secrets,
    get_table,
    load_active,
    save_active,
    load_global,
    save_global,
    load_market,
    save_market,
    delete_market,
    log,
    MarketState,
)
from limitless import notify


# 與 cli._news_risk_score / mm_loop._news_risk_score 同一份
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


def _parse_days(date_str: str | None) -> float | None:
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


def handler(event: dict, context):
    bootstrap_secrets()
    try:
        return asyncio.run(_run(event, context))
    except Exception as e:
        log("rerank_handler_error", error=str(e), traceback=traceback.format_exc())
        try:
            notify.lambda_error("limitless-mm-rerank", "handler", str(e))
        except Exception:
            pass
        raise


async def _run(event: dict, context) -> dict:
    cfg = ServerlessCfg.from_env()
    table = get_table()

    active = load_active(table)
    g = load_global(table)

    # 鏈上 USDC 動態 cap(每次 rerank 更新一次)→ 取代 env 寫死
    try:
        from limitless.chain import get_usdc_balance, wallet_address_from_priv
        addr = wallet_address_from_priv()
        if addr:
            bal = await get_usdc_balance(addr)
            if bal is not None and bal > 0:
                # 保留 10% buffer 給 fee/slippage,避免 over-commit
                new_cap = round(bal * 0.9, 2)
                old_cap = g.dynamic_total_cap
                g.dynamic_total_cap = new_cap
                g.dynamic_cap_updated_at = int(time.time())
                log("rerank_dynamic_cap_updated",
                    wallet=addr, chain_usdc=bal,
                    new_cap=new_cap, old_cap=old_cap)
                # 立刻寫回 DDB,避免後續早返回路徑遺漏
                save_global(table, g)
    except Exception as e:
        log("rerank_dynamic_cap_error", error=str(e))

    removed_settled = []
    removed_exhausted = []
    capital_freed = 0.0   # 累計從移除市場「歸還」到 global cap 的額度

    # 準備 trading client 給 cancel_all 用(無認證時 cancel 步驟 silent skip)
    tc = None
    try:
        from limitless.trading import LimitlessTradingClient
        tc = LimitlessTradingClient.from_env()
    except Exception as e:
        log("rerank_no_trading_creds", error=str(e))

    async def _cancel_orders(slug: str, reason: str) -> None:
        """被移除市場上的殘餘訂單也要撤掉(否則仍會被吃)。"""
        if tc is None:
            return
        try:
            r = await tc._order_client.cancel_all(slug)
            log("rerank_cancelled_orders", slug=slug, reason=reason, result=str(r)[:200])
        except Exception as e:
            log("rerank_cancel_error", slug=slug, error=str(e))

    # 1. 清理已結算 / 已 exhausted 的市場
    keep_slugs = []
    for slug in active.slugs:
        st = load_market(table, slug)
        if st is None:
            log("rerank_cleanup_missing", slug=slug)
            await _cancel_orders(slug, "missing_state")
            continue
        if st.exhausted:
            # 有庫存的話保留(讓 iterate 用 SELL unwind 處理,避免 orphan 部位)
            inv_hint = float((st.tox or {}).get("last_yes_inv", 0)) \
                     + float((st.tox or {}).get("last_no_inv", 0))
            if inv_hint > 0.01:
                log("rerank_keep_exhausted_for_winddown", slug=slug, inv=inv_hint)
                keep_slugs.append(slug)
                continue
            removed_exhausted.append(slug)
            await _cancel_orders(slug, "exhausted")
            capital_freed += st.capital_used
            delete_market(table, slug)
            continue
        if st.expiration_date:
            d = _parse_days(st.expiration_date)
            if d is not None and d < 0:
                removed_settled.append(slug)
                await _cancel_orders(slug, "settled")
                capital_freed += st.capital_used
                delete_market(table, slug)
                continue
        keep_slugs.append(slug)

    # 從 global 扣回被移除市場的 capital_used(否則 global cap 永遠只增不減)
    if capital_freed > 0:
        old_used = g.total_capital_used
        g.total_capital_used = max(0.0, g.total_capital_used - capital_freed)
        log("rerank_capital_freed", freed=capital_freed,
            global_before=old_used, global_after=g.total_capital_used)
        save_global(table, g)

    active.slugs = keep_slugs

    if removed_settled or removed_exhausted:
        log("rerank_cleanup",
            settled=removed_settled, exhausted=removed_exhausted,
            remaining=len(active.slugs))
        try:
            notify.markets_removed(removed_settled, removed_exhausted)
        except Exception:
            pass

    # 2. 看缺幾個
    deficit = cfg.max_positions - len(active.slugs)
    if deficit <= 0:
        log("rerank_no_deficit", active=len(active.slugs), max=cfg.max_positions)
        active.last_rerank = int(time.time())
        save_active(table, active)
        return {"status": "no_deficit", "active": len(active.slugs)}

    # 3. 跑 mm-rank 邏輯,挑新市場
    new_slugs: list[tuple[str, str, str, str]] = []  # [(slug, title, yes_token, no_token, exp, score)]
    async with LimitlessClient() as lm:
        singles, _ = await lm.fetch_active_markets(max_markets=cfg.rank_max_markets)
        candidates = [m for m in singles
                      if m.is_tradeable and m.volume_usd >= cfg.rank_min_volume_usd]

        rated = []
        for m in candidates:
            d = _parse_days(m.end_date)
            if d is None or d < cfg.rank_min_days:
                continue
            risk = _news_risk_score(m.title)
            if risk > cfg.rank_max_news_risk:
                continue
            rated.append((m, d, risk))

        if not rated:
            log("rerank_empty", reason="no_candidates_pass_pre_filter",
                volume_min=cfg.rank_min_volume_usd, days_min=cfg.rank_min_days)
            active.last_rerank = int(time.time())
            save_active(table, active)
            return {"status": "no_candidates", "active": len(active.slugs)}

        slugs_to_fetch = [m.slug for m, _, _ in rated]
        books = await lm.fetch_orderbooks(slugs_to_fetch)

        scored: list[tuple] = []
        for m, days, risk in rated:
            if m.slug in active.slugs:
                continue
            ob = books.get(m.slug)
            if ob is None:
                continue
            yb, ya = ob.yes_best_bid, ob.yes_best_ask
            if not yb or not ya:
                continue
            spread_pp = (ya.price - yb.price) * 100
            if spread_pp * 100 < cfg.rank_min_spread_bps:
                continue
            mid = (yb.price + ya.price) / 2
            if not (0.05 < mid < 0.95):
                continue
            days_factor = min(days, 30) / 7
            pa_bonus = 1.3 if m.is_poly_arbitrage else 1.0
            vol_factor = 1 + math.log(1 + m.volume_usd / 1000)
            score = spread_pp * days_factor * pa_bonus * vol_factor / (1 + risk)
            scored.append((score, m, m.end_date or ""))

        scored.sort(key=lambda r: -r[0])
        for score, m, exp in scored[:deficit]:
            new_slugs.append((m.slug, m.title, m.yes_token, m.no_token, exp, score))

    # 4. 把新市場寫進 DDB
    now = int(time.time())
    picked_meta = []
    for slug, title, yes_token, no_token, exp, score in new_slugs:
        active.slugs.append(slug)
        st = MarketState(
            slug=slug, title=title,
            yes_token=yes_token, no_token=no_token,
            expiration_date=exp,
            capital_used=0.0, tox={}, iteration_count=0,
            pm_match_cache=None, pm_match_computed=False,
            exhausted=False,
            created_at=now, last_iter_at=0,
        )
        save_market(table, st)
        picked_meta.append({"slug": slug, "title": title[:60], "score": round(score, 2)})

    active.last_rerank = now
    active.last_picked_meta = picked_meta
    save_active(table, active)

    log("rerank_picked",
        added=len(new_slugs), active_total=len(active.slugs),
        picks=picked_meta)
    try:
        notify.markets_added(picked_meta)
    except Exception:
        pass

    # 關閉 trading client
    if tc is not None:
        try:
            await tc.close()
        except Exception:
            pass

    return {
        "status": "ok",
        "added": len(new_slugs),
        "active_total": len(active.slugs),
        "removed_settled": len(removed_settled),
        "removed_exhausted": len(removed_exhausted),
    }
