"""Limitless 下單客戶端。

把官方 `limitless-sdk` 包一層，加上：
- **Dry-run 模式**（預設開啟，未設 `LIMITLESS_EXECUTE=1` 環境變數絕不真實下單）
- **安全限額**（單筆最大 USDC、每次掃描總 USDC 上限）
- **不寫日誌**任何私鑰、HMAC secret

依賴：`pip install -e '.[trade]'` 安裝 limitless-sdk。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# 延遲匯入：trade 額外套件未安裝時，模組本身仍可被 import（提供清楚錯誤）
def _require_sdk() -> Any:
    try:
        import limitless_sdk  # noqa: F401
        return limitless_sdk
    except ImportError as e:
        raise RuntimeError(
            "limitless-sdk 未安裝。請執行：\n"
            "  .venv/bin/pip install -e '.[trade]'\n"
            f"原始錯誤：{e}"
        )


@dataclass
class SafetyLimits:
    """單一交易工作階段的硬性安全限制（USDC）。"""

    max_notional_per_order: float = 50.0   # 單筆訂單最大 USDC
    max_notional_per_session: float = 500.0  # 整個會話累計上限
    require_explicit_execute: bool = True  # 必須有 LIMITLESS_EXECUTE=1 才真實下單

    @classmethod
    def from_env(cls) -> "SafetyLimits":
        return cls(
            max_notional_per_order=float(os.environ.get("LIMITLESS_MAX_PER_ORDER", "50")),
            max_notional_per_session=float(os.environ.get("LIMITLESS_MAX_PER_SESSION", "500")),
            require_explicit_execute=os.environ.get("LIMITLESS_EXECUTE", "0") != "1",
        )


@dataclass
class OrderRequest:
    """簡化的下單請求（不含 EIP-712 細節）。"""
    market_slug: str
    token_id: str          # YES 或 NO token CTF asset ID
    side: str              # "BUY" 或 "SELL"
    price: float           # 0 < price < 1
    size_shares: float     # 想買/賣的股數
    order_type: str = "FAK"  # FAK = IOC, GTC = 限價, FOK = 全成或全消
    post_only: bool = False

    @property
    def notional(self) -> float:
        return self.price * self.size_shares


@dataclass
class OrderResult:
    """下單結果（dry-run 或實際）。"""
    accepted: bool
    dry_run: bool
    request: OrderRequest
    order_id: str | None = None
    matched_shares: float = 0.0
    matched_notional: float = 0.0
    error: str | None = None
    raw_response: dict | None = field(default=None, repr=False)


class LimitlessTradingClient:
    """包裝 limitless-sdk 的下單客戶端。

    使用範例：
        client = LimitlessTradingClient.from_env()
        result = await client.place_order(OrderRequest(
            market_slug="...", token_id="...", side="BUY",
            price=0.30, size_shares=100, order_type="FAK"
        ))
        if result.accepted:
            print(f"order {result.order_id} matched {result.matched_shares} shares")
    """

    def __init__(
        self,
        hmac_token_id: str,
        hmac_secret: str,
        private_key: str,
        safety: SafetyLimits | None = None,
        base_url: str = "https://api.limitless.exchange",
    ):
        sdk = _require_sdk()
        from limitless_sdk import Client, HMACCredentials  # type: ignore

        self._sdk_client = Client(
            base_url=base_url,
            hmac_credentials=HMACCredentials(token_id=hmac_token_id, secret=hmac_secret),
        )
        self._order_client = self._sdk_client.new_order_client(private_key)
        self.safety = safety or SafetyLimits()
        self._session_notional = 0.0

    @classmethod
    def from_env(cls) -> "LimitlessTradingClient":
        """從 .env 讀取所有設定，並套用 SafetyLimits.from_env。"""
        token_id = os.environ.get("LIMITLESS_API_TOKEN_ID")
        secret = os.environ.get("LIMITLESS_API_SECRET")
        priv = os.environ.get("BASE_PRIVATE_KEY")
        if not (token_id and secret and priv):
            raise RuntimeError(
                "缺少必要環境變數:LIMITLESS_API_TOKEN_ID, LIMITLESS_API_SECRET, "
                "BASE_PRIVATE_KEY。\n"
                "請參考 .env.example 設定。"
            )
        return cls(
            hmac_token_id=token_id,
            hmac_secret=secret,
            private_key=priv,
            safety=SafetyLimits.from_env(),
        )

    # ---------- 安全檢查 ----------

    def _check_safety(self, req: OrderRequest) -> str | None:
        """回傳錯誤訊息，或 None 表示通過。"""
        if not (0 < req.price < 1):
            return f"price {req.price} 必須在 (0, 1) 之間"
        if req.size_shares <= 0:
            return f"size_shares {req.size_shares} 必須 > 0"
        if req.notional > self.safety.max_notional_per_order:
            return (
                f"單筆 notional ${req.notional:.2f} 超過上限 "
                f"${self.safety.max_notional_per_order:.2f}（"
                f"LIMITLESS_MAX_PER_ORDER）"
            )
        if self._session_notional + req.notional > self.safety.max_notional_per_session:
            return (
                f"本會話累計 ${self._session_notional + req.notional:.2f} 超過上限 "
                f"${self.safety.max_notional_per_session:.2f}（LIMITLESS_MAX_PER_SESSION）"
            )
        if req.side not in ("BUY", "SELL"):
            return f"side 必須是 BUY 或 SELL，不是 {req.side!r}"
        if req.order_type not in ("FAK", "GTC", "FOK"):
            return f"order_type 必須是 FAK/GTC/FOK，不是 {req.order_type!r}"
        return None

    # ---------- 主下單 API ----------

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """送出一筆訂單；若 `safety.require_explicit_execute=True` 則只 dry-run。"""
        err = self._check_safety(req)
        if err is not None:
            return OrderResult(accepted=False, dry_run=True, request=req, error=err)

        # dry-run：不打 API、不簽名，但更新會話會計
        if self.safety.require_explicit_execute:
            self._session_notional += req.notional
            return OrderResult(
                accepted=True,
                dry_run=True,
                request=req,
                error=None,
                raw_response={"note": "dry-run；要真實下單請設 LIMITLESS_EXECUTE=1"},
            )

        # 真實下單
        from limitless_sdk.types.orders import OrderType, Side  # type: ignore
        side = Side.BUY if req.side == "BUY" else Side.SELL
        otype = {"FAK": OrderType.FAK, "GTC": OrderType.GTC, "FOK": OrderType.FOK}[
            req.order_type
        ]

        try:
            resp = await self._order_client.create_order(
                token_id=req.token_id,
                side=side,
                order_type=otype,
                market_slug=req.market_slug,
                price=req.price,
                size=req.size_shares,
                post_only=req.post_only if req.order_type == "GTC" else None,
            )
        except Exception as e:
            return OrderResult(
                accepted=False, dry_run=False, request=req, error=str(e)
            )

        # 解析回應
        order_id = None
        matched_shares = 0.0
        try:
            order_id = getattr(resp.order, "id", None) or getattr(resp.order, "order_id", None)
        except AttributeError:
            pass
        try:
            for mm in (resp.maker_matches or []):
                matched_shares += float(getattr(mm, "size", 0) or 0)
        except AttributeError:
            pass

        self._session_notional += req.notional
        return OrderResult(
            accepted=True,
            dry_run=False,
            request=req,
            order_id=order_id,
            matched_shares=matched_shares,
            matched_notional=matched_shares * req.price,
            raw_response=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    async def cancel(self, order_id: str) -> dict:
        if self.safety.require_explicit_execute:
            return {"dry_run": True, "would_cancel": order_id}
        return await self._order_client.cancel(order_id)

    async def cancel_all(self, market_slug: str) -> dict:
        if self.safety.require_explicit_execute:
            return {"dry_run": True, "would_cancel_all_in": market_slug}
        return await self._order_client.cancel_all(market_slug)

    @property
    def session_notional_used(self) -> float:
        return self._session_notional

    async def close(self) -> None:
        close = getattr(self._sdk_client, "close", None)
        if close is None:
            return
        try:
            res = close()
            # 若 close 是 async，等待它
            if hasattr(res, "__await__"):
                await res
        except Exception:
            pass


# ---------- Auth helper：Privy token → HMAC scoped token ----------

async def derive_hmac_credentials(
    privy_identity_token: str,
    label: str = "limitless-bot",
    scopes: list[str] | None = None,
    base_url: str = "https://api.limitless.exchange",
) -> tuple[str, str]:
    """從 Privy identity token 取得 HMAC token_id + secret。

    使用流程（一次性）：
    1. 用瀏覽器登入 limitless.exchange，打開 DevTools → Application → Cookies/LocalStorage
    2. 找 Privy `token`（不是 `privy_access_token`）並複製
    3. 呼叫此函式取得 (token_id, secret)
    4. 把這兩個值放進 .env：
       LIMITLESS_API_TOKEN_ID=...
       LIMITLESS_API_SECRET=...
    5. **secret 只顯示這一次**，遺失需重新申請。
    """
    _require_sdk()
    from limitless_sdk import Client, ApiTokenService, DeriveApiTokenInput  # type: ignore

    if scopes is None:
        scopes = ["trading"]

    client = Client(base_url=base_url)
    try:
        svc = ApiTokenService(client.from_http_client())
        resp = await svc.derive_token(
            identity_token=privy_identity_token,
            payload=DeriveApiTokenInput(label=label, scopes=scopes),
        )
        token_id = getattr(resp, "token_id", None) or getattr(resp, "tokenId", None)
        secret = getattr(resp, "secret", None)
        if not (token_id and secret):
            raise RuntimeError(f"無法從回應取出 token_id/secret: {resp!r}")
        return str(token_id), str(secret)
    finally:
        try:
            await client.close()
        except Exception:
            pass
