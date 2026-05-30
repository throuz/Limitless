"""Serverless 做市的狀態序列化 + DynamoDB I/O。

把 MarketMaker 的 in-memory state(ToxicityState、_capital_used、PM cache)
搬到 DynamoDB,讓 Lambda 每次 invocation 都可以無狀態跑。

DynamoDB 單表設計(只用 PK):
- pk="global"          → {total_capital_used, last_rerank_at}
- pk="active"          → {slugs: [...], last_rerank: ts}
- pk="market#<slug>"   → 單一市場完整 state

寫入 DynamoDB 時 float 會自動變 Decimal,讀出來要把 Decimal 再 cast 回來。
這個模組統一處理這層 boilerplate,讓 handler 程式碼乾淨。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any

from .market_maker import ToxicityState


# ---------- Decimal 轉換(DynamoDB ←→ Python float)----------

def _to_decimal(obj: Any) -> Any:
    """遞迴把 float → Decimal,讓 boto3 寫 DynamoDB 不報錯。"""
    if isinstance(obj, float):
        # DynamoDB 不接受 NaN / Infinity
        if obj != obj or obj in (float("inf"), float("-inf")):
            return Decimal("0")
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_decimal(v) for v in obj]
    return obj


def _from_decimal(obj: Any) -> Any:
    """讀回來時把 Decimal → float(我們的算法都用 float)。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_decimal(v) for v in obj]
    return obj


# ---------- ToxicityState 序列化 ----------

def serialize_tox(t: ToxicityState) -> dict:
    return {
        "yes_fills": list(t.yes_fills),
        "no_fills": list(t.no_fills),
        "pm_mids": list(t.pm_mids),
        "lm_best_asks": list(t.lm_best_asks),
        "last_yes_inv": t.last_yes_inv,
        "last_no_inv": t.last_no_inv,
    }


def deserialize_tox(d: dict | None, window: int = 10) -> ToxicityState:
    d = d or {}
    t = ToxicityState()
    t.yes_fills = deque(d.get("yes_fills", []), maxlen=window)
    t.no_fills = deque(d.get("no_fills", []), maxlen=window)
    t.pm_mids = deque(d.get("pm_mids", []), maxlen=window)
    t.lm_best_asks = deque(d.get("lm_best_asks", []), maxlen=window)
    t.last_yes_inv = float(d.get("last_yes_inv", 0.0))
    t.last_no_inv = float(d.get("last_no_inv", 0.0))
    return t


# ---------- 表項目 dataclass ----------

@dataclass
class GlobalState:
    total_capital_used: float = 0.0
    last_rerank_at: int = 0
    sessions_completed: int = 0
    sessions_emergency: int = 0
    dynamic_total_cap: float = 0.0       # 鏈上 USDC × safety_buffer,rerank 更新
    dynamic_cap_updated_at: int = 0      # 最後一次更新時間戳
    last_fill_at: int = 0                # 最後一次偵測到成交的時間戳(watchdog 用)
    last_no_fill_alert_at: int = 0       # 最後一次發「24h 無成交」警報
    last_allowance_alert_at: int = 0     # 最後一次發「allowance 不足」警報


@dataclass
class ActiveList:
    slugs: list[str] = field(default_factory=list)
    last_rerank: int = 0
    last_picked_meta: list[dict] = field(default_factory=list)  # debug,看上一輪挑了什麼


@dataclass
class MarketState:
    """單一市場的完整持久化狀態。"""
    slug: str = ""
    title: str = ""
    yes_token: str = ""
    no_token: str = ""
    expiration_date: str = ""    # 從 LM market metadata 抓
    capital_used: float = 0.0
    tox: dict = field(default_factory=dict)
    iteration_count: int = 0
    pm_match_cache: str | None = None  # PM market id,None 表示沒鏡像
    pm_match_computed: bool = False    # 已經嘗試過配對(避免每次都重算)
    exhausted: bool = False             # 資本用盡標記
    created_at: int = 0
    last_iter_at: int = 0
    # 「上次成功掛單時的價」— 用來判斷是否該重新掛
    last_quoted_yes_bid: float = 0.0
    last_quoted_no_bid: float = 0.0
    last_quoted_yes_sell: float = 0.0
    last_quoted_no_sell: float = 0.0


# ---------- DynamoDB 操作 ----------

def get_table():
    """Lazy import boto3 — local dev 不裝 boto3 也能 import 這個 module。"""
    import boto3
    name = os.environ.get("DDB_TABLE_NAME")
    if not name:
        raise RuntimeError("缺少環境變數 DDB_TABLE_NAME")
    return boto3.resource("dynamodb").Table(name)


def load_global(table) -> GlobalState:
    r = table.get_item(Key={"pk": "global"}).get("Item")
    if not r:
        return GlobalState()
    r = _from_decimal(r)
    return GlobalState(
        total_capital_used=r.get("total_capital_used", 0.0),
        last_rerank_at=int(r.get("last_rerank_at", 0)),
        sessions_completed=int(r.get("sessions_completed", 0)),
        sessions_emergency=int(r.get("sessions_emergency", 0)),
        dynamic_total_cap=float(r.get("dynamic_total_cap", 0.0)),
        dynamic_cap_updated_at=int(r.get("dynamic_cap_updated_at", 0)),
        last_fill_at=int(r.get("last_fill_at", 0)),
        last_no_fill_alert_at=int(r.get("last_no_fill_alert_at", 0)),
        last_allowance_alert_at=int(r.get("last_allowance_alert_at", 0)),
    )


def save_global(table, g: GlobalState) -> None:
    table.put_item(Item=_to_decimal({"pk": "global", **asdict(g)}))


def load_global_consistent(table) -> GlobalState:
    """Strongly consistent read,iterate 用以避免讀到 stale dynamic_total_cap。"""
    r = table.get_item(Key={"pk": "global"}, ConsistentRead=True).get("Item")
    if not r:
        return GlobalState()
    r = _from_decimal(r)
    return GlobalState(
        total_capital_used=r.get("total_capital_used", 0.0),
        last_rerank_at=int(r.get("last_rerank_at", 0)),
        sessions_completed=int(r.get("sessions_completed", 0)),
        sessions_emergency=int(r.get("sessions_emergency", 0)),
        dynamic_total_cap=float(r.get("dynamic_total_cap", 0.0)),
        dynamic_cap_updated_at=int(r.get("dynamic_cap_updated_at", 0)),
        last_fill_at=int(r.get("last_fill_at", 0)),
        last_no_fill_alert_at=int(r.get("last_no_fill_alert_at", 0)),
        last_allowance_alert_at=int(r.get("last_allowance_alert_at", 0)),
    )


def load_active(table) -> ActiveList:
    r = table.get_item(Key={"pk": "active"}).get("Item")
    if not r:
        return ActiveList()
    r = _from_decimal(r)
    return ActiveList(
        slugs=list(r.get("slugs", [])),
        last_rerank=int(r.get("last_rerank", 0)),
        last_picked_meta=list(r.get("last_picked_meta", [])),
    )


def save_active(table, a: ActiveList) -> None:
    table.put_item(Item=_to_decimal({"pk": "active", **asdict(a)}))


def _market_pk(slug: str) -> str:
    return f"market#{slug}"


def load_market(table, slug: str) -> MarketState | None:
    r = table.get_item(Key={"pk": _market_pk(slug)}).get("Item")
    if not r:
        return None
    r = _from_decimal(r)
    return MarketState(
        slug=r.get("slug", slug),
        title=r.get("title", ""),
        yes_token=r.get("yes_token", ""),
        no_token=r.get("no_token", ""),
        expiration_date=r.get("expiration_date", ""),
        capital_used=r.get("capital_used", 0.0),
        tox=r.get("tox") or {},
        iteration_count=int(r.get("iteration_count", 0)),
        pm_match_cache=r.get("pm_match_cache") or None,
        pm_match_computed=bool(r.get("pm_match_computed", False)),
        exhausted=bool(r.get("exhausted", False)),
        created_at=int(r.get("created_at", 0)),
        last_iter_at=int(r.get("last_iter_at", 0)),
        last_quoted_yes_bid=float(r.get("last_quoted_yes_bid", 0.0)),
        last_quoted_no_bid=float(r.get("last_quoted_no_bid", 0.0)),
        last_quoted_yes_sell=float(r.get("last_quoted_yes_sell", 0.0)),
        last_quoted_no_sell=float(r.get("last_quoted_no_sell", 0.0)),
    )


def save_market(table, m: MarketState) -> None:
    item = _to_decimal({"pk": _market_pk(m.slug), **asdict(m)})
    table.put_item(Item=item)


def delete_market(table, slug: str) -> None:
    table.delete_item(Key={"pk": _market_pk(slug)})


# ---------- Lambda 統一設定讀取 ----------

@dataclass
class ServerlessCfg:
    """Lambda 端的 config,從 env 讀,跟 MMLoopConfig 對齊。"""
    total_capital_usdc: float = 500.0
    max_positions: int = 3
    capital_per_market: float = 100.0
    quote_size_shares: float = 10.0
    target_profit_pct: float = 4.0
    half_spread_pct: float = 1.0
    max_inventory_shares: float = 30.0
    iteration_sleep_s: float = 120.0   # 對應 EventBridge schedule
    rank_max_markets: int = 500
    rank_min_volume_usd: float = 200.0
    rank_min_days: float = 2.0
    rank_min_spread_bps: int = 100
    rank_max_news_risk: float = 2.0
    # v0.7：選市場過濾趨勢盤 — 只做接近 0.5、雙邊都有真實深度的市場。
    rank_min_mid: float = 0.30           # mid < 此值 = 一面倒的輸方,跳過
    rank_max_mid: float = 0.70           # mid > 此值 = 一面倒的贏方,跳過
    rank_min_depth_shares: float = 50.0  # 每側 orderbook 深度門檻(雙邊做市才有意義)
    rank_spread_cap_pp: float = 10.0     # spread 獎勵上限(避免偏好流動性差的超寬盤)
    oracle_mode: str = "pm"
    use_microprice: bool = True
    emergency_close_hours: float = 24.0
    # Toxicity 細項
    toxicity_window: int = 5
    toxicity_imbalance_threshold: float = 0.7
    toxicity_pm_velocity_threshold: float = 0.03
    toxicity_ask_drop_threshold: float = 0.02
    toxicity_widen_multiplier: float = 2.0
    # Inventory / unwind
    inventory_skew_pct: float = 0.5
    unwind_inventory_pct: float = 0.6
    unwind_premium_pct: float = 1.0
    # v0.7：庫存失衡時主動拉缺口側 bid 補腿(完成配對降風險)
    inventory_rebalance_pct: float = 50.0
    inventory_rebalance_max_cross_pct: float = 0.0
    rebalance_when_capped: bool = True
    execute: bool = False

    @classmethod
    def from_env(cls) -> "ServerlessCfg":
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
            iteration_sleep_s=f("MM_LOOP_ITER_SLEEP_S", 120),
            rank_max_markets=i("MM_LOOP_RANK_MAX_MARKETS", 500),
            rank_min_volume_usd=f("MM_LOOP_RANK_MIN_VOLUME", 200),
            rank_min_days=f("MM_LOOP_RANK_MIN_DAYS", 2),
            rank_min_spread_bps=i("MM_LOOP_RANK_MIN_SPREAD_BPS", 100),
            rank_max_news_risk=f("MM_LOOP_RANK_MAX_NEWS_RISK", 2),
            rank_min_mid=f("MM_LOOP_RANK_MIN_MID", 0.30),
            rank_max_mid=f("MM_LOOP_RANK_MAX_MID", 0.70),
            rank_min_depth_shares=f("MM_LOOP_RANK_MIN_DEPTH_SHARES", 50),
            rank_spread_cap_pp=f("MM_LOOP_RANK_SPREAD_CAP_PP", 10),
            oracle_mode=s("MM_LOOP_ORACLE", "pm"),
            use_microprice=os.environ.get("MM_LOOP_USE_MICROPRICE", "1") == "1",
            emergency_close_hours=f("MM_LOOP_EMERGENCY_HOURS", 24),
            # Toxicity
            toxicity_window=i("MM_LOOP_TOXICITY_WINDOW", 5),
            toxicity_imbalance_threshold=f("MM_LOOP_TOXICITY_IMBALANCE", 0.7),
            toxicity_pm_velocity_threshold=f("MM_LOOP_TOXICITY_PM_VELOCITY", 0.03),
            toxicity_ask_drop_threshold=f("MM_LOOP_TOXICITY_ASK_DROP", 0.02),
            toxicity_widen_multiplier=f("MM_LOOP_TOXICITY_WIDEN_MULT", 2.0),
            # Inventory / unwind
            inventory_skew_pct=f("MM_LOOP_INVENTORY_SKEW_PCT", 0.5),
            unwind_inventory_pct=f("MM_LOOP_UNWIND_INVENTORY_PCT", 0.6),
            unwind_premium_pct=f("MM_LOOP_UNWIND_PREMIUM_PCT", 1.0),
            inventory_rebalance_pct=f("MM_LOOP_INVENTORY_REBALANCE_PCT", 50),
            inventory_rebalance_max_cross_pct=f("MM_LOOP_INVENTORY_REBALANCE_MAX_CROSS_PCT", 0),
            rebalance_when_capped=os.environ.get("MM_LOOP_REBALANCE_WHEN_CAPPED", "1") == "1",
            execute=os.environ.get("LIMITLESS_EXECUTE", "0") == "1",
        )


# ---------- Lambda cold-start:從 SSM 拉 secret 注入 env ----------

_BOOTSTRAPPED = False


def bootstrap_secrets() -> None:
    """Lambda 冷啟動時呼叫一次:把 SSM SecureString 拉下來注入 os.environ。

    ECS 可直接從 Secrets Manager 注入 env,但 Lambda 沒有;
    所以 handler 自己負責 fetch。容器在同個 worker 復用時,
    這個 function 只執行一次(_BOOTSTRAPPED 旗標)。
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    # 必須有的 secrets(沒拉到 = bootstrap 失敗)
    required_mapping = {
        "SSM_TOKEN_ID_NAME": "LIMITLESS_API_TOKEN_ID",
        "SSM_API_SECRET_NAME": "LIMITLESS_API_SECRET",
        "SSM_PRIV_KEY_NAME": "BASE_PRIVATE_KEY",
    }
    # 可選 secrets(Telegram。沒設不影響主邏輯,只是無通知)
    optional_mapping = {
        "SSM_TELEGRAM_BOT_TOKEN_NAME": "TELEGRAM_BOT_TOKEN",
        "SSM_TELEGRAM_CHAT_ID_NAME": "TELEGRAM_CHAT_ID",
    }

    # 本機/EC2/ECS 已經有完整必要 env → 跳過(可選的就保持當下狀態)
    if all(os.environ.get(v) for v in required_mapping.values()):
        _BOOTSTRAPPED = True
        return

    import boto3
    ssm = boto3.client("ssm")
    for src_name_env, dest_env in required_mapping.items():
        if os.environ.get(dest_env):
            continue
        param_name = os.environ.get(src_name_env)
        if not param_name:
            log("bootstrap_skip", reason=f"{src_name_env} not set")
            continue
        try:
            r = ssm.get_parameter(Name=param_name, WithDecryption=True)
            os.environ[dest_env] = r["Parameter"]["Value"]
        except Exception as e:
            log("bootstrap_ssm_error", param=param_name, error=str(e))
            raise

    # 可選 secrets — 拉不到只 log 不 raise
    for src_name_env, dest_env in optional_mapping.items():
        if os.environ.get(dest_env):
            continue
        param_name = os.environ.get(src_name_env)
        if not param_name:
            continue
        try:
            r = ssm.get_parameter(Name=param_name, WithDecryption=True)
            val = r["Parameter"]["Value"]
            if val and val != "REPLACE_ME":
                os.environ[dest_env] = val
        except Exception as e:
            log("bootstrap_optional_ssm_skip", param=param_name, error=str(e))

    _BOOTSTRAPPED = True


# ---------- 結構化 log(去到 CloudWatch)----------

def log(kind: str, **kw) -> None:
    """印 JSON 一行到 stdout,CloudWatch 自動切欄。"""
    rec = {"kind": kind, "ts": int(time.time())}
    rec.update(kw)
    try:
        print(json.dumps(rec, default=str), flush=True)
    except Exception:
        print(f"[log-err] kind={kind} kw={kw}", flush=True)
