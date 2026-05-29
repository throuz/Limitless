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
    load_market,
    save_market,
    delete_market,
    log,
    MarketState,
)


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
        raise


async def _run(event: dict, context) -> dict:
    cfg = ServerlessCfg.from_env()
    table = get_table()

    active = load_active(table)
    removed_settled = []
    removed_exhausted = []

    # 1. 清理已結算 / 已 exhausted 的市場
    keep_slugs = []
    for slug in active.slugs:
        st = load_market(table, slug)
        if st is None:
            log("rerank_cleanup_missing", slug=slug)
            continue
        if st.exhausted:
            removed_exhausted.append(slug)
            delete_market(table, slug)
            continue
        if st.expiration_date:
            d = _parse_days(st.expiration_date)
            # < 0 = 已過期;< emergency_hours/24 = 太接近結算,讓 iterate 自己 emergency_close
            if d is not None and d < 0:
                removed_settled.append(slug)
                delete_market(table, slug)
                continue
        keep_slugs.append(slug)

    active.slugs = keep_slugs

    if removed_settled or removed_exhausted:
        log("rerank_cleanup",
            settled=removed_settled, exhausted=removed_exhausted,
            remaining=len(active.slugs))

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
    return {
        "status": "ok",
        "added": len(new_slugs),
        "active_total": len(active.slugs),
        "removed_settled": len(removed_settled),
        "removed_exhausted": len(removed_exhausted),
    }
