"""檢查你目前 Limitless 帳號的 partner capabilities + token scopes。

目的:確認能不能跑 delegated signing(不用私鑰下單)。

跑法:
  .venv/bin/python check_capabilities.py
"""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()


async def main():
    from limitless_sdk import Client, HMACCredentials

    token_id = os.environ.get("LIMITLESS_API_TOKEN_ID")
    secret = os.environ.get("LIMITLESS_API_SECRET")
    if not (token_id and secret):
        print("缺 LIMITLESS_API_TOKEN_ID / LIMITLESS_API_SECRET")
        return

    client = Client(
        base_url="https://api.limitless.exchange",
        hmac_credentials=HMACCredentials(token_id=token_id, secret=secret),
    )

    print("=" * 60)
    print("1. Partner capabilities(看你帳號能拿到什麼 scope)")
    print("=" * 60)
    try:
        # 直接打 endpoint
        r = await client.http.get("/api-tokens/capabilities")
        print(f"Response: {r}")
    except Exception as e:
        print(f"  錯誤: {e}")

    print()
    print("=" * 60)
    print("2. List active tokens(看你目前 token 的 scope)")
    print("=" * 60)
    try:
        r = await client.http.get("/api-tokens")
        print(f"Response: {r}")
    except Exception as e:
        print(f"  錯誤: {e}")

    print()
    print("=" * 60)
    print("3. 你的 portfolio / 持倉(驗證 token 能用)")
    print("=" * 60)
    try:
        from limitless_sdk.portfolio import PortfolioFetcher
        pf = PortfolioFetcher(client.http)
        positions = await pf.get_positions()
        print(f"持倉: {positions}")
    except Exception as e:
        print(f"  錯誤: {e}")

    print()
    print("=" * 60)
    print("4. List partner accounts(看你有沒有 server wallet sub-account)")
    print("=" * 60)
    try:
        r = await client.http.get("/partner-accounts")
        print(f"Response: {r}")
    except Exception as e:
        print(f"  錯誤: {e}")

    try:
        await client.close()
    except Exception:
        pass


asyncio.run(main())
