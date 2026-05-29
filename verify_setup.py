"""一鍵驗證:私鑰 / HMAC token / wallet 餘額 全部對得上才能跑 bot。

跑法:
  .venv/bin/python verify_setup.py
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg):
    print(f"  {RED}✗{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠{RESET} {msg}")


async def main():
    print("=" * 60)
    print("Step 1:env 變數齊全嗎?")
    print("=" * 60)

    required = ["LIMITLESS_API_TOKEN_ID", "LIMITLESS_API_SECRET", "BASE_PRIVATE_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        fail(f"缺少: {missing}")
        sys.exit(1)
    for k in required:
        v = os.environ[k]
        ok(f"{k} 有值 (前 8 字: {v[:8]}...)")

    print()
    print("=" * 60)
    print("Step 2:私鑰格式正確嗎?能推出地址嗎?")
    print("=" * 60)
    key = os.environ["BASE_PRIVATE_KEY"]
    if not key.startswith("0x"):
        warn(f"私鑰沒 0x 前綴 (SDK 仍可用,但建議加上)")
    expected_len = 66 if key.startswith("0x") else 64
    if len(key) != expected_len:
        fail(f"私鑰長度 = {len(key)}, 應該是 66(含 0x) 或 64(不含)")
        sys.exit(1)
    ok(f"格式正確(長度 {len(key)})")

    from eth_account import Account
    try:
        acc = Account.from_key(key)
        wallet_addr = acc.address
        ok(f"私鑰推算出地址: {wallet_addr}")
    except Exception as e:
        fail(f"無法從私鑰解出地址: {e}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("Step 3:HMAC token 能用嗎?綁的是哪個地址?")
    print("=" * 60)
    try:
        from limitless_sdk import Client, HMACCredentials
        from limitless_sdk.portfolio import PortfolioFetcher

        client = Client(
            base_url="https://api.limitless.exchange",
            hmac_credentials=HMACCredentials(
                token_id=os.environ["LIMITLESS_API_TOKEN_ID"],
                secret=os.environ["LIMITLESS_API_SECRET"],
            ),
        )
        pf = PortfolioFetcher(client.http)
        positions = await pf.get_positions()
        ok(f"HMAC token 有效,API 可呼叫")

        # 找 maker address
        maker_addrs = set()
        for pos in (positions.get("clob") or []):
            a = pos.get("makerAddress")
            if a:
                maker_addrs.add(a)

        if maker_addrs:
            for a in maker_addrs:
                if a.lower() == wallet_addr.lower():
                    ok(f"HMAC 綁定地址 = 私鑰推算地址 = {a}")
                else:
                    fail(f"地址不匹配!")
                    fail(f"  私鑰推算地址:    {wallet_addr}")
                    fail(f"  HMAC 綁定地址:   {a}")
                    fail(f"  → bot 下單會被 API 拒絕 (HTTP 403)")
                    fail(f"  → 需要用 MetaMask 重新 auth-derive,或重新匯出對的私鑰")
        else:
            warn(f"沒有持倉,從別處查 HMAC 綁定地址")
            # 用 /me 或 /profile 試
            try:
                r = await client.http.get(f"/profiles/{wallet_addr}")
                ok(f"用私鑰地址查 profile 成功 → HMAC 跟私鑰是同一個 wallet")
            except Exception as e:
                if "403" in str(e):
                    fail(f"HMAC token 不認識這個地址 → 不匹配")
                else:
                    warn(f"查 profile 失敗: {e}")

        # 查 token scope
        try:
            tokens = await client.http.get("/auth/api-tokens")
            for t in tokens:
                scopes = t.get("scopes", [])
                if "trading" in scopes:
                    ok(f"Token scope 含 'trading' (可下單)")
                else:
                    fail(f"Token scope = {scopes},缺 'trading'")
        except Exception as e:
            warn(f"查 token scope 失敗: {e}")

        await client.close()
    except Exception as e:
        fail(f"HMAC 測試失敗: {e}")
        sys.exit(1)

    print()
    print("=" * 60)
    print(f"Step 4:wallet 鏈上餘額(地址 {wallet_addr})")
    print("=" * 60)

    import httpx
    BASE_RPC = "https://mainnet.base.org"

    # ETH balance
    try:
        r = httpx.post(BASE_RPC, json={
            "jsonrpc": "2.0", "method": "eth_getBalance",
            "params": [wallet_addr, "latest"], "id": 1,
        }, timeout=10).json()
        eth = int(r["result"], 16) / 1e18
        if eth >= 0.0001:
            ok(f"ETH 餘額: {eth:.6f} ETH (有 gas)")
        elif eth > 0:
            warn(f"ETH 餘額: {eth:.6f} ETH (有但很少,可能不夠未來的 approve/withdraw)")
        else:
            warn(f"ETH 餘額: 0 (下單不需要,但 approve / 提錢時要)")
    except Exception as e:
        fail(f"查 ETH 失敗: {e}")

    # USDC balance
    try:
        USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        data = "0x70a08231" + wallet_addr[2:].zfill(64)
        r = httpx.post(BASE_RPC, json={
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": data}, "latest"], "id": 1,
        }, timeout=10).json()
        usdc_raw = int(r["result"], 16)
        usdc = usdc_raw / 1e6
        if usdc >= 5:
            ok(f"USDC 餘額: ${usdc:.2f} (足夠 bot 起步)")
        elif usdc > 0:
            warn(f"USDC 餘額: ${usdc:.2f} (有但太少,建議充 $50-100 才有意義)")
        else:
            fail(f"USDC 餘額: 0 (要先把錢搬過來才能下單)")
    except Exception as e:
        fail(f"查 USDC 失敗: {e}")

    print()
    print("=" * 60)
    print("總結")
    print("=" * 60)
    print(f"  wallet 地址:      {wallet_addr}")
    print()
    print("  如果上面全部 ✓ → bot 可以跑 dry-run 了:")
    print("    .venv/bin/python -m limitless.cli limitless mm-loop \\")
    print("      --total-capital 80 --max-positions 3 --capital-per-market 25 \\")
    print("      --quote-size 5 --oracle pm")
    print()
    print("  有 ✗ → 看上面錯誤訊息,通常是 USDC 沒進來 或 HMAC/私鑰不匹配")


asyncio.run(main())
