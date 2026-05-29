"""polymkt.limitless — 主交易場(Limitless Exchange,Base 鏈)。

模組:
    client.py        — Limitless API client(read-only orderbook/markets)
    models.py        — Market / Group / OrderBook 資料模型
    scanner.py       — 套利機會掃描
    trading.py       — 下單 client(包 limitless-sdk,含 dry-run + 安全限額)
    market_maker.py  — v0.6 做市核心(microprice、toxicity、unwind、emergency)
    mm_loop.py       — v0.7 24/7 自動換市場調度器
    serverless.py    — Lambda 端狀態序列化 + DynamoDB I/O
    pnl.py           — v0.9 PnL 追蹤(SQLite)+ wallet snapshot + 結算偵測

CLI:見 polymkt.cli 的 `limitless` 子命令(或 top-level 捷徑)。
"""
