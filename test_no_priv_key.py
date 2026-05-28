"""實證:只有 HMAC token 沒私鑰,能不能下單?

三組測試:
1. 用 SDK 但不給私鑰 → 看 OrderClient 初始化
2. 直接 raw POST /orders 不附簽名 → 看 API 拒絕訊息
3. 給 SDK 一個假私鑰 → 看簽名能不能造、API 認不認

全部用「絕對不會成交」的價格,即使意外成功也不花錢。
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
        print("缺 HMAC 環境變數")
        return

    client = Client(
        base_url="https://api.limitless.exchange",
        hmac_credentials=HMACCredentials(token_id=token_id, secret=secret),
    )

    print("=" * 60)
    print("Test 1:OrderClient 不給私鑰會怎樣?")
    print("=" * 60)
    try:
        oc = client.new_order_client(None)
        print(f"  竟然成功? oc = {oc}")
    except Exception as e:
        print(f"  失敗: {type(e).__name__}: {e}")

    print()
    print("=" * 60)
    print("Test 2:直接 POST /orders、payload 不附 signature")
    print("=" * 60)
    try:
        r = await client.http.post("/orders", {
            "marketSlug": "mexico-1775496699325",
            "side": "BUY",
            "outcome": "YES",
            "price": 0.001,    # 極低,永遠不會 match
            "size": 1,
        })
        print(f"  竟然成功(危險!): {r}")
    except Exception as e:
        print(f"  拒絕(預期): {type(e).__name__}: {e}")

    print()
    print("=" * 60)
    print("Test 3:給 SDK 一個假私鑰(不是你 wallet 的),試下單")
    print("=" * 60)
    print("  假私鑰對應到的地址不是你 Limitless 的地址")
    print("  → 簽名能造,但 API 會發現「簽名者 != 訂單擁有者」")
    try:
        DUMMY_KEY = "0x" + "1" * 64
        oc = client.new_order_client(DUMMY_KEY)
        print(f"  SDK 接受了假私鑰 (因為它只是字串)")

        from limitless_sdk.types.orders import OrderType, Side
        result = await oc.create_order(
            token_id="81409102699692320638771882504118380574511628476721868795773644019298238431360",
            side=Side.BUY,
            order_type=OrderType.GTC,
            market_slug="mexico-1775496699325",
            price=0.001,
            size=1,
        )
        print(f"  竟然成功(危險!): {result}")
    except Exception as e:
        print(f"  拒絕(預期): {type(e).__name__}: {str(e)[:300]}")

    try:
        await client.close()
    except Exception:
        pass


asyncio.run(main())
