"""檢查你目前 Limitless 帳號的 partner capabilities + token scopes。"""

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
    print("1. List your active API tokens(看 scope!)")
    print("=" * 60)
    try:
        r = await client.http.get("/auth/api-tokens")
        print(r)
    except Exception as e:
        print(f"  錯誤: {e}")

    print()
    print("=" * 60)
    print("2. List partner accounts(看你是不是 partner)")
    print("=" * 60)
    try:
        r = await client.http.get("/profiles/partner-accounts")
        print(r)
    except Exception as e:
        print(f"  錯誤: {e}")

    print()
    print("=" * 60)
    print("3. 試試 delegated 訂單(會失敗但錯誤訊息會告訴我們缺什麼)")
    print("=" * 60)
    try:
        from limitless_sdk.delegated_orders import DelegatedOrderService
        from limitless_sdk.types.orders import OrderType, Side

        d = DelegatedOrderService(client.http)
        r = await d.create_order(
            token_id="0",
            side=Side.BUY,
            order_type=OrderType.GTC,
            market_slug="invalid-test-slug",
            on_behalf_of=1,
            price=0.5,
            size=10,
        )
        print(r)
    except Exception as e:
        print(f"  錯誤(預期): {type(e).__name__}: {e}")

    try:
        await client.close()
    except Exception:
        pass


asyncio.run(main())
