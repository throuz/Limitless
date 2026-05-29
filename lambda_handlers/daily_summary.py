"""每日摘要 Lambda。

每天 UTC 22:00(台灣早上 06:00)EventBridge 觸發一次:
1. 從 DDB 撈過去 24h 的 active markets / 累計 capital
2. 從 Base 鏈查 wallet 餘額(USDC + ETH)
3. 估 CTF 持倉價值
4. 透過 Telegram 發送摘要

注意:Lambda 沒裝 SQLite-backed PnL DB(SQLite 是本機的設計),
所以這裡的摘要是「**從 DDB + 鏈上即時讀**」,不是從歷史紀錄算。
"""

from __future__ import annotations

import asyncio
import os
import time
import traceback
from datetime import datetime, timezone

from limitless.serverless import (
    bootstrap_secrets,
    get_table,
    load_global,
    load_active,
    load_market,
    log,
)
from limitless import notify


def handler(event: dict, context):
    bootstrap_secrets()
    try:
        return asyncio.run(_run(event, context))
    except Exception as e:
        log("daily_summary_error", error=str(e), traceback=traceback.format_exc())
        try:
            notify.lambda_error("limitless-mm-daily-summary", "handler", str(e))
        except Exception:
            pass
        raise


async def _run(event: dict, context) -> dict:
    table = get_table()
    g = load_global(table)
    active = load_active(table)

    # 累計 iterations / capital_used
    total_iters = 0
    total_market_capital = 0.0
    for slug in active.slugs:
        st = load_market(table, slug)
        if st is None:
            continue
        total_iters += st.iteration_count
        total_market_capital += st.capital_used

    # 查鏈上 wallet 餘額
    usdc = eth = ctf_value = 0.0
    try:
        import httpx
        from eth_account import Account

        priv = os.environ.get("BASE_PRIVATE_KEY")
        if priv:
            addr = Account.from_key(priv).address
            BASE_RPC = "https://mainnet.base.org"
            USDC_CT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

            # USDC
            data = "0x70a08231" + addr[2:].zfill(64)
            r = httpx.post(BASE_RPC, json={
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": USDC_CT, "data": data}, "latest"], "id": 1,
            }, timeout=10).json()
            usdc = int(r["result"], 16) / 1e6

            # ETH
            r = httpx.post(BASE_RPC, json={
                "jsonrpc": "2.0", "method": "eth_getBalance",
                "params": [addr, "latest"], "id": 1,
            }, timeout=10).json()
            eth = int(r["result"], 16) / 1e18

            # CTF 估值(若有 trading creds)
            try:
                from limitless.trading import LimitlessTradingClient
                from limitless_sdk.portfolio import PortfolioFetcher
                tc = LimitlessTradingClient.from_env()
                pf = PortfolioFetcher(tc._sdk_client.http)
                positions = await pf.get_positions()
                for pos in (positions.get("clob") or []):
                    yb = float(pos.get("tokensBalance", {}).get("yes") or 0) / 1e6
                    nb = float(pos.get("tokensBalance", {}).get("no") or 0) / 1e6
                    lt = pos.get("latestTrade") or {}
                    yp = float(lt.get("latestYesPrice") or 0.5)
                    np_ = float(lt.get("latestNoPrice") or 0.5)
                    ctf_value += yb * yp + nb * np_
                await tc.close()
            except Exception as e:
                log("daily_summary_ctf_skip", error=str(e))
    except Exception as e:
        log("daily_summary_balance_error", error=str(e))

    total_equity = usdc + ctf_value

    # 透過 notify 發 Telegram
    arrow = "📈" if total_equity >= 100 else "📉"  # 沒歷史可比,簡單看現值
    body = (
        f"{arrow} <b>24h 摘要</b>\n"
        f"Active 市場:{len(active.slugs)}\n"
        f"累計 iterations:{total_iters}\n"
        f"市場合計 capital_used:${total_market_capital:.2f}\n"
        f"全域 capital_used:${g.total_capital_used:.2f}\n"
        f"完成 sessions:{g.sessions_completed}\n"
        f"emergency:{g.sessions_emergency}\n"
        f"\n"
        f"💰 <b>鏈上餘額</b>\n"
        f"USDC:${usdc:.2f}\n"
        f"ETH:{eth:.6f}\n"
        f"CTF 持倉估值:${ctf_value:.2f}\n"
        f"<b>總 Equity:${total_equity:.2f}</b>"
    )
    notify.send(body)
    log("daily_summary_done", equity=total_equity, active=len(active.slugs))
    return {
        "status": "ok",
        "active_markets": len(active.slugs),
        "iterations": total_iters,
        "equity": total_equity,
    }
