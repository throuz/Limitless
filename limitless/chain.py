"""Base 鏈上 USDC 餘額讀取(純 httpx,不依賴 web3.py)。"""

from __future__ import annotations

import os
import httpx

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_RPC = "https://mainnet.base.org"


def wallet_address_from_priv() -> str | None:
    """從 BASE_PRIVATE_KEY 推 wallet address。"""
    priv = os.environ.get("BASE_PRIVATE_KEY")
    if not priv:
        return None
    try:
        from eth_account import Account
        return Account.from_key(priv).address
    except Exception:
        return None


async def get_usdc_balance(wallet: str, rpc_url: str = DEFAULT_RPC) -> float | None:
    """讀指定 wallet 的鏈上 USDC 餘額。失敗回 None。"""
    selector = "0x70a08231"
    addr_padded = wallet.lower().replace("0x", "").rjust(64, "0")
    data = selector + addr_padded
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": USDC_BASE, "data": data}, "latest"],
        "id": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.post(rpc_url, json=payload)
            r.raise_for_status()
            result = r.json().get("result")
            if not result:
                return None
            raw = int(result, 16)
            return raw / 1_000_000  # USDC decimals=6
    except Exception:
        return None
