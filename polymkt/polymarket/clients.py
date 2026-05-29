"""Polymarket API clients。

- GammaClient：拉市場/事件元資料（高層次）
- ClobClient：拉訂單簿（精準價格）

兩者都是 async；同時打很多 orderbook 才有用。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Iterable

import httpx

from .models import Event, Market, OrderBook


GAMMA_HOST = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_HOST = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")


class GammaClient:
    """Polymarket Gamma API：市場/事件清單與基本資料。"""

    def __init__(self, host: str = GAMMA_HOST, timeout: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=host,
            timeout=timeout,
            headers={"User-Agent": "polymkt-scanner/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GammaClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def fetch_active_events(
        self,
        max_events: int = 500,
        page_size: int = 100,
    ) -> list[Event]:
        """拉所有正在進行中的事件（含其下市場）。"""
        events: list[Event] = []
        offset = 0
        while len(events) < max_events:
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": min(page_size, max_events - len(events)),
                "offset": offset,
            }
            r = await self._client.get("/events", params=params)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for raw in batch:
                ev = Event.from_gamma(raw)
                if ev.markets:
                    events.append(ev)
            if len(batch) < params["limit"]:
                break
            offset += params["limit"]
        return events

    async def fetch_active_markets(
        self,
        max_markets: int = 500,
        page_size: int = 100,
    ) -> list[Market]:
        """直接拉所有市場（不分組）。比較快，但失去 event 結構。"""
        markets: list[Market] = []
        offset = 0
        while len(markets) < max_markets:
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": min(page_size, max_markets - len(markets)),
                "offset": offset,
            }
            r = await self._client.get("/markets", params=params)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for raw in batch:
                m = Market.from_gamma(raw)
                if m is not None and m.is_tradeable:
                    markets.append(m)
            if len(batch) < params["limit"]:
                break
            offset += params["limit"]
        return markets


class ClobClient:
    """Polymarket CLOB API：訂單簿與下單。

    這層只做讀取（公開端點）。下單需要簽名，會走 `py-clob-client`。
    """

    def __init__(
        self,
        host: str = CLOB_HOST,
        timeout: float = 30.0,
        max_concurrency: int = 20,
    ):
        self._client = httpx.AsyncClient(
            base_url=host,
            timeout=timeout,
            headers={"User-Agent": "polymkt-scanner/0.1"},
        )
        self._sem = asyncio.Semaphore(max_concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ClobClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def fetch_book(self, token_id: str) -> OrderBook | None:
        async with self._sem:
            try:
                r = await self._client.get("/book", params={"token_id": token_id})
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return OrderBook.from_clob(r.json())
            except httpx.HTTPError:
                return None

    async def fetch_books(
        self, token_ids: Iterable[str]
    ) -> dict[str, OrderBook]:
        """同時拉多本訂單簿。"""
        ids = list(token_ids)
        results = await asyncio.gather(
            *(self.fetch_book(tid) for tid in ids),
            return_exceptions=True,
        )
        out: dict[str, OrderBook] = {}
        for tid, res in zip(ids, results):
            if isinstance(res, OrderBook):
                out[tid] = res
        return out
