"""PnL 追蹤與分析模組 (v0.9)。

把 mm-loop / make-market 跑過程的狀態快照到 SQLite,提供「我到底有沒有賺錢」的答案。

設計:
- SQLite 單檔(~/.limitless/pnl.db),零依賴、本機跑
- 每輪 iterate() 記一筆 snapshot(庫存、quote、capital)
- 每筆 order placement 記 attempt(成/敗/拒絕)
- 從庫存 delta 推算 fills(LM 沒主動回 fill webhook)
- 每日 USDC + CTF 估值 snapshot,算累計 PnL

存取:
    from limitless import pnl
    pnl.record_iteration(slug=..., yes_bid=..., ...)
    pnl.record_order(slug=..., side=..., ...)
    stats = pnl.summary_stats(days=30)
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DB_PATH = Path(
    os.environ.get("LIMITLESS_MM_PNL_DB")
    or str(Path.home() / ".limitless" / "pnl.db")
)

# Lambda 檔案系統唯讀 → 全面 skip 寫入(state 已存 DynamoDB,Lambda 不用 SQLite)
_IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS iterations (
    ts INTEGER NOT NULL,
    slug TEXT NOT NULL,
    yes_bid REAL,
    no_bid REAL,
    yes_sell REAL,
    no_sell REAL,
    yes_inventory REAL,
    no_inventory REAL,
    capital_used REAL,
    toxicity_score REAL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_iter_slug_ts ON iterations(slug, ts);
CREATE INDEX IF NOT EXISTS idx_iter_ts ON iterations(ts);

CREATE TABLE IF NOT EXISTS orders (
    ts INTEGER NOT NULL,
    slug TEXT NOT NULL,
    side TEXT NOT NULL,          -- BUY/SELL
    outcome TEXT NOT NULL,       -- YES/NO
    price REAL NOT NULL,
    size REAL NOT NULL,
    notional REAL NOT NULL,
    order_type TEXT NOT NULL,    -- GTC/FAK/FOK
    accepted INTEGER NOT NULL,
    dry_run INTEGER NOT NULL,
    order_id TEXT,
    matched_shares REAL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_slug_ts ON orders(slug, ts);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS fills (
    ts INTEGER NOT NULL,
    slug TEXT NOT NULL,
    outcome TEXT NOT NULL,
    shares REAL NOT NULL,
    avg_price REAL,
    notional REAL,
    source TEXT NOT NULL         -- detected_delta / matched_response
);
CREATE INDEX IF NOT EXISTS idx_fills_slug_ts ON fills(slug, ts);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);

CREATE TABLE IF NOT EXISTS settlements (
    ts INTEGER NOT NULL,
    slug TEXT NOT NULL,
    title TEXT,
    yes_shares REAL,
    no_shares REAL,
    cost_basis REAL,
    payout REAL,
    realized_pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_settle_ts ON settlements(ts);

CREATE TABLE IF NOT EXISTS wallet_snapshots (
    ts INTEGER NOT NULL,
    date TEXT NOT NULL,
    usdc_balance REAL,
    eth_balance REAL,
    ctf_value_estimated REAL,
    open_orders_locked REAL,
    total_equity REAL,
    active_markets INTEGER,
    PRIMARY KEY (date)
);
"""


# ---------- DB connection ----------

@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    if _IN_LAMBDA:
        # Lambda 上沒 SQLite,直接拋讓 caller skip
        raise RuntimeError("Lambda 環境不寫 SQLite PnL")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def reset_db() -> None:
    """砍掉所有資料(慎用)。"""
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


# ---------- Recording API(MarketMaker 呼叫)----------

def record_iteration(
    slug: str,
    yes_bid: float = 0,
    no_bid: float = 0,
    yes_sell: float = 0,
    no_sell: float = 0,
    yes_inventory: float = 0,
    no_inventory: float = 0,
    capital_used: float = 0,
    toxicity_score: float = 0,
    notes: str | None = None,
    ts: int | None = None,
) -> None:
    if _IN_LAMBDA:
        return  # Lambda 用 DDB 做 state,不用 SQLite
    ts = ts or int(time.time())
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO iterations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, slug, yes_bid, no_bid, yes_sell, no_sell,
             yes_inventory, no_inventory, capital_used, toxicity_score, notes),
        )


def record_order(
    slug: str,
    side: str,
    outcome: str,
    price: float,
    size: float,
    order_type: str,
    accepted: bool,
    dry_run: bool,
    order_id: str | None = None,
    matched_shares: float = 0,
    error: str | None = None,
    ts: int | None = None,
) -> None:
    if _IN_LAMBDA:
        return
    ts = ts or int(time.time())
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, slug, side, outcome, price, size, price * size,
             order_type, 1 if accepted else 0, 1 if dry_run else 0,
             order_id, matched_shares, error),
        )


def detect_and_record_fills(slug: str, yes_inv_now: float, no_inv_now: float) -> list[dict]:
    if _IN_LAMBDA:
        return []
    """比對上一輪 iteration 的庫存 vs 現在,推算 fill。

    限制:
    - 只看「庫存增加」(我們是 maker BUY,沒主動賣)
    - 不能區分「我們的單成交」vs「外部來源(空投等)」
    - 我們的下單流程沒掛 SELL 時,假設增量全是 BUY fill

    回傳:這次偵測到的 fills list,給 caller 印 log 用。
    """
    init_db()
    new_fills = []
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT yes_inventory, no_inventory, yes_bid, no_bid "
            "FROM iterations WHERE slug=? ORDER BY ts DESC LIMIT 1",
            (slug,),
        )
        prev = cur.fetchone()
        if not prev:
            return []   # 第一筆 iter,沒得比

        ts = int(time.time())
        yes_delta = yes_inv_now - (prev["yes_inventory"] or 0)
        no_delta = no_inv_now - (prev["no_inventory"] or 0)

        if yes_delta > 0.01:
            px = prev["yes_bid"] or 0
            conn.execute(
                "INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                (ts, slug, "YES", yes_delta, px, yes_delta * px, "detected_delta"),
            )
            new_fills.append({"outcome": "YES", "shares": yes_delta, "price": px})
        if no_delta > 0.01:
            px = prev["no_bid"] or 0
            conn.execute(
                "INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                (ts, slug, "NO", no_delta, px, no_delta * px, "detected_delta"),
            )
            new_fills.append({"outcome": "NO", "shares": no_delta, "price": px})
    return new_fills


def record_settlement(
    slug: str,
    title: str | None,
    yes_shares: float,
    no_shares: float,
    cost_basis: float,
    payout: float,
    ts: int | None = None,
) -> None:
    if _IN_LAMBDA:
        return
    ts = ts or int(time.time())
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?)",
            (ts, slug, title, yes_shares, no_shares,
             cost_basis, payout, payout - cost_basis),
        )


def record_wallet_snapshot(
    usdc_balance: float,
    eth_balance: float = 0,
    ctf_value_estimated: float = 0,
    open_orders_locked: float = 0,
    active_markets: int = 0,
    date: str | None = None,
    ts: int | None = None,
) -> None:
    if _IN_LAMBDA:
        return
    ts = ts or int(time.time())
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_equity = usdc_balance + ctf_value_estimated + open_orders_locked
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO wallet_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (ts, date, usdc_balance, eth_balance, ctf_value_estimated,
             open_orders_locked, total_equity, active_markets),
        )


# ---------- 查詢 / 分析 API(CLI 呼叫)----------

@dataclass
class PnLSummary:
    days: int
    iterations: int
    orders_total: int
    orders_accepted: int
    orders_rejected: int
    fills_count: int
    fills_total_notional: float
    settlements_count: int
    realized_pnl: float
    pair_completion_rate_pct: float
    avg_toxicity: float
    last_wallet_equity: float | None
    first_wallet_equity: float | None
    period_pnl: float | None
    period_pnl_pct: float | None


def summary_stats(days: int = 30) -> PnLSummary:
    init_db()
    cutoff = int(time.time()) - days * 86400

    with get_conn() as conn:
        # Iterations
        iters = conn.execute(
            "SELECT COUNT(*) AS n, AVG(toxicity_score) AS tox FROM iterations WHERE ts >= ?",
            (cutoff,),
        ).fetchone()

        # Orders
        orders = conn.execute(
            "SELECT COUNT(*) AS n, SUM(accepted) AS acc FROM orders WHERE ts >= ?",
            (cutoff,),
        ).fetchone()

        # Fills(配對率計算)
        fills_total = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(notional), 0) AS notional FROM fills WHERE ts >= ?",
            (cutoff,),
        ).fetchone()

        # 配對率:每個 slug 在每個小時內,YES fill 跟 NO fill 都有 = 算一次配對成功
        # 簡化版:整體 YES fill 數 與 NO fill 數,min 視作配對成功
        sides = conn.execute(
            "SELECT outcome, COUNT(*) AS c FROM fills WHERE ts >= ? GROUP BY outcome",
            (cutoff,),
        ).fetchall()
        yes_count = next((s["c"] for s in sides if s["outcome"] == "YES"), 0)
        no_count = next((s["c"] for s in sides if s["outcome"] == "NO"), 0)
        pair_estimate = min(yes_count, no_count)
        total_fills = yes_count + no_count
        pair_rate = (2 * pair_estimate / total_fills * 100) if total_fills else 0.0

        # Settlements
        settle = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(realized_pnl), 0) AS pnl FROM settlements WHERE ts >= ?",
            (cutoff,),
        ).fetchone()

        # Wallet equity 變化
        first_equity = conn.execute(
            "SELECT total_equity FROM wallet_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
            (cutoff,),
        ).fetchone()
        last_equity = conn.execute(
            "SELECT total_equity FROM wallet_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()

        first_eq = first_equity["total_equity"] if first_equity else None
        last_eq = last_equity["total_equity"] if last_equity else None
        period_pnl = (last_eq - first_eq) if (first_eq is not None and last_eq is not None) else None
        period_pnl_pct = (period_pnl / first_eq * 100) if (period_pnl is not None and first_eq) else None

    return PnLSummary(
        days=days,
        iterations=iters["n"] or 0,
        orders_total=orders["n"] or 0,
        orders_accepted=orders["acc"] or 0,
        orders_rejected=(orders["n"] or 0) - (orders["acc"] or 0),
        fills_count=fills_total["n"] or 0,
        fills_total_notional=fills_total["notional"] or 0.0,
        settlements_count=settle["n"] or 0,
        realized_pnl=settle["pnl"] or 0.0,
        pair_completion_rate_pct=pair_rate,
        avg_toxicity=iters["tox"] or 0.0,
        first_wallet_equity=first_eq,
        last_wallet_equity=last_eq,
        period_pnl=period_pnl,
        period_pnl_pct=period_pnl_pct,
    )


def daily_breakdown(days: int = 30) -> list[dict]:
    """每天的 fill / order / 結算 PnL。"""
    init_db()
    cutoff = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT date(ts, 'unixepoch') AS d,
                   COUNT(DISTINCT slug) AS markets,
                   COUNT(*) AS fills_n,
                   COALESCE(SUM(notional), 0) AS fills_notional
            FROM fills WHERE ts >= ?
            GROUP BY d ORDER BY d
            """,
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            d = r["d"]
            # 對應日結算 PnL
            sp = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl, COUNT(*) AS n "
                "FROM settlements WHERE date(ts, 'unixepoch') = ?", (d,)
            ).fetchone()
            # 對應日 wallet snapshot
            eq = conn.execute(
                "SELECT total_equity FROM wallet_snapshots WHERE date = ?", (d,)
            ).fetchone()
            out.append({
                "date": d,
                "markets": r["markets"],
                "fills": r["fills_n"],
                "fills_notional": r["fills_notional"],
                "settlements": sp["n"],
                "realized_pnl": sp["pnl"],
                "equity": eq["total_equity"] if eq else None,
            })
        return out


def per_market_breakdown(days: int = 30) -> list[dict]:
    init_db()
    cutoff = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT slug,
                   COUNT(*) AS iter_n,
                   AVG(yes_bid) AS avg_yes,
                   AVG(no_bid) AS avg_no,
                   MAX(yes_inventory) AS peak_yes_inv,
                   MAX(no_inventory) AS peak_no_inv,
                   MAX(capital_used) AS peak_capital
            FROM iterations WHERE ts >= ?
            GROUP BY slug ORDER BY iter_n DESC
            """,
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            slug = r["slug"]
            fills = conn.execute(
                "SELECT outcome, COUNT(*) AS n, COALESCE(SUM(notional), 0) AS notional "
                "FROM fills WHERE slug = ? AND ts >= ? GROUP BY outcome",
                (slug, cutoff),
            ).fetchall()
            yes_fills = next((f["n"] for f in fills if f["outcome"] == "YES"), 0)
            no_fills = next((f["n"] for f in fills if f["outcome"] == "NO"), 0)

            settle = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM settlements WHERE slug = ?",
                (slug,),
            ).fetchone()

            out.append({
                "slug": slug,
                "iterations": r["iter_n"],
                "yes_fills": yes_fills,
                "no_fills": no_fills,
                "peak_yes_inv": r["peak_yes_inv"] or 0,
                "peak_no_inv": r["peak_no_inv"] or 0,
                "peak_capital": r["peak_capital"] or 0,
                "realized_pnl": settle["pnl"] or 0,
            })
        return out


def equity_curve(days: int = 30) -> list[tuple[str, float]]:
    init_db()
    cutoff = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, total_equity FROM wallet_snapshots WHERE ts >= ? ORDER BY date",
            (cutoff,),
        ).fetchall()
        return [(r["date"], r["total_equity"] or 0) for r in rows]


# ---------- Wallet snapshot + 結算偵測(需要 LM client + RPC)----------

async def snapshot_wallet(wallet_address: str, tc=None) -> dict:
    """寫一筆當下 wallet snapshot。需要:
    - wallet_address:你 EOA 地址(0x...)
    - tc:LimitlessTradingClient(已驗證過,選填,用來抓 portfolio 估 CTF value)

    回傳:寫入的 dict,給 caller 印 log 用。
    """
    import httpx

    BASE_RPC = "https://mainnet.base.org"
    USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    # USDC
    try:
        data = "0x70a08231" + wallet_address[2:].zfill(64)
        r = httpx.post(BASE_RPC, json={
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": data}, "latest"], "id": 1,
        }, timeout=10).json()
        usdc = int(r["result"], 16) / 1e6
    except Exception:
        usdc = 0.0

    # ETH
    try:
        r = httpx.post(BASE_RPC, json={
            "jsonrpc": "2.0", "method": "eth_getBalance",
            "params": [wallet_address, "latest"], "id": 1,
        }, timeout=10).json()
        eth = int(r["result"], 16) / 1e18
    except Exception:
        eth = 0.0

    # CTF estimated value + open orders(若有 tc)
    ctf_value = 0.0
    open_orders_locked = 0.0
    active_markets = 0
    if tc is not None:
        try:
            from limitless_sdk.portfolio import PortfolioFetcher
            pf = PortfolioFetcher(tc._sdk_client.http)
            positions = await pf.get_positions()
            for pos in (positions.get("clob") or []):
                # 估 YES/NO 部位的現值
                yes_bal = float(pos.get("tokensBalance", {}).get("yes") or 0) / 1e6
                no_bal = float(pos.get("tokensBalance", {}).get("no") or 0) / 1e6
                lt = pos.get("latestTrade") or {}
                yes_price = float(lt.get("latestYesPrice") or 0.5)
                no_price = float(lt.get("latestNoPrice") or 0.5)
                ctf_value += yes_bal * yes_price + no_bal * no_price

                # 開單鎖定資本
                orders = pos.get("orders") or {}
                locked_raw = float(orders.get("totalCollateralLocked") or 0)
                open_orders_locked += locked_raw / 1e6
                if pos.get("orders", {}).get("liveOrders"):
                    active_markets += 1
        except Exception:
            pass

    record_wallet_snapshot(
        usdc_balance=usdc,
        eth_balance=eth,
        ctf_value_estimated=ctf_value,
        open_orders_locked=open_orders_locked,
        active_markets=active_markets,
    )
    return {
        "usdc": usdc, "eth": eth, "ctf_value": ctf_value,
        "open_orders_locked": open_orders_locked,
        "total_equity": usdc + ctf_value + open_orders_locked,
        "active_markets": active_markets,
    }


async def detect_settlements(tc) -> list[dict]:
    """偵測「我過去有部位但現在 portfolio 沒了」的市場 = 已結算。

    需要 tc 已認證。回傳本次偵測到的結算 list。
    """
    settled = []
    init_db()
    if tc is None:
        return []

    try:
        from limitless_sdk.portfolio import PortfolioFetcher
        pf = PortfolioFetcher(tc._sdk_client.http)
        positions = await pf.get_positions()
    except Exception:
        return []

    current_slugs = {
        pos.get("market", {}).get("slug")
        for pos in (positions.get("clob") or [])
        if pos.get("market", {}).get("slug")
    }

    with get_conn() as conn:
        # 過去 30 天有 iteration 紀錄、且現在不在 portfolio 的 slug
        cutoff = int(time.time()) - 30 * 86400
        rows = conn.execute(
            """
            SELECT DISTINCT slug FROM iterations
            WHERE ts >= ?
              AND slug NOT IN (
                SELECT slug FROM settlements
              )
            """,
            (cutoff,),
        ).fetchall()
        for r in rows:
            slug = r["slug"]
            if slug in current_slugs:
                continue   # 還在 portfolio

            # 算 cost basis(fills 加總)
            cost_row = conn.execute(
                "SELECT COALESCE(SUM(notional), 0) AS cost FROM fills WHERE slug = ?",
                (slug,),
            ).fetchone()
            cost = cost_row["cost"] or 0.0

            # 算我們最後一筆 iteration 看到的庫存
            iter_row = conn.execute(
                "SELECT yes_inventory, no_inventory FROM iterations "
                "WHERE slug = ? ORDER BY ts DESC LIMIT 1",
                (slug,),
            ).fetchone()
            yes_sh = (iter_row["yes_inventory"] if iter_row else 0) or 0
            no_sh = (iter_row["no_inventory"] if iter_row else 0) or 0

            # payout 估計:結算後 1 YES + 1 NO 一對 = $1
            # 假設庫存配對的部分 = $1/對
            # 剩下單邊 = 50% 機率拿 $1, 50% 拿 $0 → 平均 = 0.5/股 (悲觀估)
            # 但已經結算所以實際結果是 0 或 1。沒有外部資料無法精確
            # 採保守估計:配對的部分 + 單邊 × 0.5
            paired = min(yes_sh, no_sh)
            unpaired_yes = yes_sh - paired
            unpaired_no = no_sh - paired
            payout = paired + 0.5 * (unpaired_yes + unpaired_no)
            # 註明這是估計,精確值要另外用 LM history API 抓

            record_settlement(
                slug=slug, title=None,
                yes_shares=yes_sh, no_shares=no_sh,
                cost_basis=cost, payout=payout,
            )
            settled.append({
                "slug": slug,
                "yes_shares": yes_sh, "no_shares": no_sh,
                "cost": cost, "payout_estimated": payout,
                "pnl_estimated": payout - cost,
            })

    return settled
