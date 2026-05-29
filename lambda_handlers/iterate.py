"""Lambda iterate handler — 每 N 秒由 EventBridge 觸發一次。

執行流程:
1. 從 DDB 載入 global state + active markets list
2. 對每個 active slug:
   - 載入該市場的 state(ToxicityState、capital_used、PM cache)
   - 建 MarketMaker、注入 state、跑一次 iterate()
   - 把更新後的 state 寫回 DDB
   - 若觸發 emergency_close → 從 active 移除
3. 寫回 global state

reserved_concurrent_executions=1(在 CDK 設) → 確保不會兩個 iterate 同時跑。
"""

from __future__ import annotations

import asyncio
import os
import time
import traceback

from limitless.client import LimitlessClient
from limitless.market_maker import MakerConfig, MarketMaker
from limitless.trading import LimitlessTradingClient
from limitless.serverless import (
    ServerlessCfg,
    bootstrap_secrets,
    deserialize_tox,
    serialize_tox,
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


def handler(event: dict, context):
    """Lambda 同步進入點。"""
    bootstrap_secrets()   # 冷啟動把 SSM secrets 注入 os.environ
    try:
        return asyncio.run(_run(event, context))
    except Exception as e:
        log("handler_error", error=str(e), traceback=traceback.format_exc())
        try:
            notify.lambda_error("limitless-mm-iterate", "handler", str(e))
        except Exception:
            pass
        raise


async def _run(event: dict, context) -> dict:
    cfg = ServerlessCfg.from_env()
    table = get_table()

    g = load_global(table)
    active = load_active(table)

    if not active.slugs:
        log("iterate_skip", reason="no_active_markets")
        return {"status": "no_active", "processed": 0}

    # 全域資本檢查
    remaining_global = max(0.0, cfg.total_capital_usdc - g.total_capital_used)
    if remaining_global < 5.0:
        log("iterate_skip", reason="global_capital_exhausted",
            used=g.total_capital_used, cap=cfg.total_capital_usdc)
        return {"status": "global_exhausted", "processed": 0}

    # 初始化交易 + 讀取 client(共用整個 invocation)
    try:
        tc = LimitlessTradingClient.from_env()
    except RuntimeError as e:
        log("iterate_error", phase="trading_init", error=str(e))
        return {"status": "no_creds", "processed": 0}

    processed = 0
    errors = 0
    emergencies = 0

    async with LimitlessClient() as lm:
        for slug in list(active.slugs):
            try:
                res = await _iterate_one_market(slug, table, tc, lm, cfg, g, remaining_global)
                processed += 1
                if res.get("emergency"):
                    emergencies += 1
                    # 從 active 移除
                    if slug in active.slugs:
                        active.slugs.remove(slug)
                if res.get("exhausted"):
                    if slug in active.slugs:
                        active.slugs.remove(slug)
                remaining_global = max(0.0, cfg.total_capital_usdc - g.total_capital_used)
                if remaining_global < 5.0:
                    log("iterate_break", reason="global_capital_exhausted_mid_loop")
                    break
            except Exception as e:
                errors += 1
                log("iterate_market_error", slug=slug, error=str(e),
                    traceback=traceback.format_exc()[:1000])

    save_active(table, active)
    save_global(table, g)
    await tc.close()

    log("iterate_done", processed=processed, emergencies=emergencies,
        errors=errors, active=len(active.slugs),
        global_used=g.total_capital_used, global_cap=cfg.total_capital_usdc)
    return {
        "status": "ok",
        "processed": processed,
        "emergencies": emergencies,
        "errors": errors,
        "active": len(active.slugs),
    }


async def _iterate_one_market(slug: str, table, tc, lm, cfg: ServerlessCfg, g, remaining_global: float) -> dict:
    state = load_market(table, slug)
    if state is None:
        log("market_missing", slug=slug, msg="active 有此 slug 但 DDB 沒 state,建空白")
        state = MarketState(slug=slug, created_at=int(time.time()))

    # 判斷有無庫存(從 tox 上次見到的 last_*_inv,雖然可能 stale 但夠用)
    inv_hint = float((state.tox or {}).get("last_yes_inv", 0)) \
             + float((state.tox or {}).get("last_no_inv", 0))

    if state.exhausted and inv_hint < 0.01:
        # 真的沒倉位 → 確定可以撤
        try:
            await tc._order_client.cancel_all(slug)
        except Exception:
            pass
        return {"exhausted": True}

    # 計算這個市場還能用多少資本
    per_market_remaining = max(0.0, cfg.capital_per_market - state.capital_used)
    cap_this_market = min(per_market_remaining, remaining_global)
    winddown_mode = False
    if cap_this_market < 5.0:
        # 資本耗盡 — 還有倉位的話繼續跑(讓 mm.iterate() 的 SELL unwind 邏輯處理)
        if inv_hint > 0.01:
            log("market_capital_exhausted_winddown", slug=slug,
                used=state.capital_used, cap=cfg.capital_per_market,
                inv_hint=inv_hint)
            winddown_mode = True
            cap_this_market = 0.0   # 強制 mm.cfg.capital_usdc == state.capital_used → threshold=0
        else:
            log("market_capital_exhausted_no_inventory", slug=slug,
                used=state.capital_used, cap=cfg.capital_per_market)
            try:
                await tc._order_client.cancel_all(slug)
                log("market_orders_cancelled_on_exhaust", slug=slug)
            except Exception as e:
                log("market_cancel_error", slug=slug, error=str(e))
            state.exhausted = True
            save_market(table, state)
            return {"exhausted": True}

    mc = MakerConfig(
        slug=slug,
        capital_usdc=cap_this_market + state.capital_used,  # 因為 MM._capital_used 從 state 載入
        quote_size_shares=cfg.quote_size_shares,
        target_profit_pct=cfg.target_profit_pct,
        half_spread_offset_pct=cfg.half_spread_pct,
        max_inventory_shares=cfg.max_inventory_shares,
        iteration_sleep_s=cfg.iteration_sleep_s,
        duration_s=0,                                       # 我們自己管時間
        oracle_mode=cfg.oracle_mode,
        use_microprice=cfg.use_microprice,
        emergency_close_hours=cfg.emergency_close_hours,
        winddown_mode=winddown_mode,
    )

    mm = MarketMaker(mc, tc, lm)

    # 注入 state(關鍵:讓 toxicity window / 資本記得跨 invocation)
    mm._capital_used = state.capital_used
    mm._tox = deserialize_tox(state.tox, window=10)
    if state.pm_match_computed:
        mm._pm_match_cache = state.pm_match_cache   # 可能是 None(沒鏡像),但已嘗試過

    # 載入或補抓 market metadata
    if state.yes_token and state.no_token:
        mm.market = {
            "tokens": {"yes": state.yes_token, "no": state.no_token},
            "title": state.title,
            "expirationDate": state.expiration_date,
            "slug": slug,
        }
    else:
        await mm.init_market()
        if mm.market:
            state.yes_token = mm.market["tokens"]["yes"]
            state.no_token = mm.market["tokens"]["no"]
            state.title = mm.market.get("title", "")
            state.expiration_date = mm.market.get("expirationDate") or ""

    capital_before = mm._capital_used

    # 跑一次 iterate
    result = await mm.iterate()

    capital_delta = mm._capital_used - capital_before

    # 寫回 state
    state.capital_used = mm._capital_used
    state.tox = serialize_tox(mm._tox)
    state.iteration_count += 1
    state.last_iter_at = int(time.time())
    if hasattr(mm, "_pm_match_cache"):
        state.pm_match_cache = mm._pm_match_cache
        state.pm_match_computed = True

    # 全域累計
    g.total_capital_used += capital_delta

    save_market(table, state)

    log("iter",
        slug=slug, n=state.iteration_count,
        yes_bid=result.yes_bid_price, no_bid=result.no_bid_price,
        yes_sell=result.yes_sell_price, no_sell=result.no_sell_price,
        tox=result.toxicity_score, emergency=result.emergency_close,
        cap_used=state.capital_used, notes=result.notes)

    return {"emergency": result.emergency_close}
