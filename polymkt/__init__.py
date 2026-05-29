"""polymkt — Limitless Exchange 做市工具(+ Polymarket 輔助 oracle)。

主要場域:Limitless(Base 鏈,台灣可用)。
輔助:Polymarket 當 oracle / 訊號源(台灣 close-only)。

子套件:
    polymkt.limitless  — 主交易場(orderbook、下單、做市、mm-loop、PnL、serverless)
    polymkt.polymarket — 輔助分析(Gamma/CLOB client、scanner、鯨魚追蹤)

模組:
    polymkt.crossarb  — 跨平台 PM↔LM 價差訊號(連接兩個子套件)
    polymkt.cli       — 統一 CLI 入口
"""

__version__ = "0.9.0"
