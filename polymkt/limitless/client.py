"""Limitless Exchange API client（讀取部分）。

API 限制：
- markets list 端點 `limit` 最大值 = 25，要分頁
- orderbook 端點：`/markets/{slug}/orderbook`（只回 YES side，NO 透過對稱關係）
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Iterable

import httpx

from .models import LimitlessGroup, LimitlessMarket, LimitlessOrderBook


LIMITLESS_HOST = os.environ.get("LIMITLESS_HOST", "https://api.limitless.exchange")
MAX_PAGE_SIZE = 25
MAX_RETRIES = 5
BASE_BACKOFF = 1.5
PAGE_DELAY_S = 0.3        # 分頁之間的延遲，降低 429 機率


class LimitlessClient:
    """讀取用 client。下單需要簽名與 auth，之後再做。"""

    def __init__(
        self,
        host: str = LIMITLESS_HOST,
        timeout: float = 30.0,
        max_concurrency: int = 4,   # 保守，Limitless rate limit 嚴
    ):
        self._client = httpx.AsyncClient(
            base_url=host,
            timeout=timeout,
            headers={
                "User-Agent": "polymkt/0.1",
                "Accept": "application/json",
            },
        )
        self._sem = asyncio.Semaphore(max_concurrency)

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET with exponential backoff on 429/503."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = await self._client.get(path, **kwargs)
                if r.status_code in (429, 503):
                    # respect Retry-After if present
                    retry_after = r.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait = float(retry_after)
                    else:
                        wait = BASE_BACKOFF ** attempt + random.uniform(0, 0.5)
                    await asyncio.sleep(wait)
                    continue
                return r
            except httpx.TransportError as e:
                last_exc = e
                await asyncio.sleep(BASE_BACKOFF ** attempt + random.uniform(0, 0.5))
        if last_exc:
            raise last_exc
        raise httpx.HTTPError(f"Limitless API: {path} failed after {MAX_RETRIES} retries")

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LimitlessClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def fetch_active_markets(
        self,
        max_markets: int = 1000,
    ) -> tuple[list[LimitlessMarket], list[LimitlessGroup]]:
        """拉所有活躍市場 + groups。回傳 (singles, groups)。"""
        singles: list[LimitlessMarket] = []
        groups: list[LimitlessGroup] = []
        page = 1
        while len(singles) + sum(len(g.markets) for g in groups) < max_markets:
            r = await self._get(
                "/markets/active",
                params={"page": page, "limit": MAX_PAGE_SIZE},
            )
            r.raise_for_status()
            data = r.json()
            batch = data.get("data", [])
            if not batch:
                break

            for raw in batch:
                mtype = raw.get("marketType", "single")
                if mtype == "group":
                    g = LimitlessGroup.from_api(raw)
                    if g.markets:
                        groups.append(g)
                else:
                    m = LimitlessMarket.from_api(raw)
                    if m is not None:
                        singles.append(m)

            if len(batch) < MAX_PAGE_SIZE:
                break
            page += 1
            await asyncio.sleep(PAGE_DELAY_S)

        return singles, groups

    async def fetch_orderbook(self, slug: str) -> LimitlessOrderBook | None:
        """單一市場的訂單簿（用 slug 索引）。"""
        async with self._sem:
            try:
                r = await self._get(f"/markets/{slug}/orderbook")
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return LimitlessOrderBook.from_api(r.json())
            except httpx.HTTPError:
                return None

    async def fetch_orderbooks(
        self, slugs: Iterable[str]
    ) -> dict[str, LimitlessOrderBook]:
        slug_list = list(slugs)
        results = await asyncio.gather(
            *(self.fetch_orderbook(s) for s in slug_list),
            return_exceptions=True,
        )
        out: dict[str, LimitlessOrderBook] = {}
        for s, res in zip(slug_list, results):
            if isinstance(res, LimitlessOrderBook):
                out[s] = res
        return out
