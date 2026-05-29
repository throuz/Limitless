"""Telegram 通知模組。

設計:
- 完全可選:沒設 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 時所有 notify_* 函式變 no-op
- 失敗不擋主程式:網路掛掉、Telegram 掛掉,都只 print 警告
- 同步呼叫(用 httpx.post),避免到處插 await

設定:
  export TELEGRAM_BOT_TOKEN=<從 @BotFather 拿>
  export TELEGRAM_CHAT_ID=<你跟 bot 對話的 chat id>

Lambda 自動從 SSM 拉(同 LIMITLESS_API_TOKEN_ID 機制)。
"""

from __future__ import annotations

import os
from typing import Any


def _enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(message: str, parse_mode: str = "HTML") -> bool:
    """同步發訊息。沒設定或失敗都回 False 但不 raise。"""
    if not _enabled():
        return False

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    try:
        import httpx
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[notify] telegram HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[notify] telegram 失敗: {e}")
        return False


# ---------- 事件範本(各高層事件對應一個發送函式)----------

def emergency_close(slug: str, hours_remaining: float | None,
                    yes_shares: float, no_shares: float) -> None:
    """部位被結算前強制清倉。"""
    hrs = f"{hours_remaining:.1f}h" if hours_remaining is not None else "未知"
    send(
        f"🚨 <b>緊急清倉</b>\n"
        f"市場:<code>{slug[:60]}</code>\n"
        f"距結算:{hrs}\n"
        f"庫存:YES={yes_shares:.1f}, NO={no_shares:.1f}\n"
        f"動作:已撤所有單 + 市價清倉"
    )


def lambda_error(function_name: str, phase: str, error: str) -> None:
    """Lambda 拋例外或拒絕。"""
    send(
        f"❌ <b>Lambda 錯誤</b>\n"
        f"Function:<code>{function_name}</code>\n"
        f"Phase:{phase}\n"
        f"Error:<code>{error[:300]}</code>"
    )


def trading_rejected(slug: str, reason: str) -> None:
    """API 拒絕訂單(例如 insufficient allowance)。"""
    send(
        f"⚠️ <b>訂單被拒</b>\n"
        f"市場:<code>{slug[:60]}</code>\n"
        f"原因:<code>{reason[:200]}</code>"
    )


def capital_exhausted(total_used: float, total_cap: float) -> None:
    """全域 capital cap 達上限,bot 停掛新單。"""
    send(
        f"🛑 <b>全域資本耗盡</b>\n"
        f"累計使用:${total_used:.2f}\n"
        f"上限:${total_cap:.2f}\n"
        f"動作:停掛新單(現有部位繼續等結算)\n"
        f"處理:加錢或調整 MM_LOOP_TOTAL_CAPITAL"
    )


def first_pair_completion(slug: str, profit_estimate: float) -> None:
    """第一次完整配對成功(確認 bot 真的在做事)。"""
    send(
        f"🎉 <b>第一次配對成功</b>\n"
        f"市場:<code>{slug[:60]}</code>\n"
        f"估利潤:+${profit_estimate:.2f}\n"
        f"(YES + NO 都已成交,等結算)"
    )


def daily_summary(stats: dict[str, Any]) -> None:
    """每日摘要。stats 從 pnl.summary_stats() 來。"""
    pnl = stats.get("realized_pnl", 0.0)
    equity = stats.get("last_wallet_equity")
    period_pnl = stats.get("period_pnl")
    arrow = "📈" if (pnl or 0) >= 0 else "📉"
    body = (
        f"{arrow} <b>24h 摘要</b>\n"
        f"Iterations:{stats.get('iterations', 0)}\n"
        f"Fills:{stats.get('fills_count', 0)} "
        f"(配對率 ~{stats.get('pair_completion_rate_pct', 0):.0f}%)\n"
        f"結算:{stats.get('settlements_count', 0)}\n"
        f"已實現 PnL:${pnl:+.2f}\n"
    )
    if equity is not None:
        body += f"Equity:${equity:.2f}"
        if period_pnl is not None:
            body += f" ({period_pnl:+.2f})"
    send(body)


def boot(execute: bool, capital: float, max_positions: int) -> None:
    """系統啟動通知(本機 mm-loop 或 Lambda 首次冷啟動)。"""
    mode = "<b>REAL</b>" if execute else "DRY-RUN"
    send(
        f"🟢 <b>Bot 啟動</b>\n"
        f"模式:{mode}\n"
        f"全域資本:${capital:.0f}\n"
        f"並行市場:{max_positions}"
    )
