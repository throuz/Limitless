"""統一 CLI 入口。

子命令結構：
- polymkt polymarket scan       — Polymarket 套利掃描（**台灣無法下單**，僅作分析）
- polymkt polymarket closest    — Polymarket 最接近套利的市場
- polymkt limitless scan        — Limitless 套利掃描（**台灣可用**）
- polymkt limitless closest     — Limitless 最接近套利的市場
- polymkt crossarb              — 跨平台 Polymarket↔Limitless 價差訊號
"""

from __future__ import annotations

import asyncio
import os
import sys

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .clients import ClobClient, GammaClient
from .scanner import scan as pm_scan
from .limitless.client import LimitlessClient
from .limitless.scanner import scan as lm_scan, closest as lm_closest
from .crossarb import find_cross_pairs


console = Console()


# ---------- Polymarket 子命令（保留原功能）----------

@click.group(name="polymarket")
def polymarket_group() -> None:
    """Polymarket 操作（台灣只能讀，無法下新單）。"""


@polymarket_group.command(name="scan")
@click.option("--max-events", type=int, default=200)
@click.option("--min-liquidity", type=float, default=1000.0)
@click.option("--min-edge-bps", type=int, default=50)
@click.option("--probe-shares", type=float, default=100.0)
@click.option("--top", type=int, default=10)
def pm_scan_cmd(max_events: int, min_liquidity: float, min_edge_bps: int,
                probe_shares: float, top: int) -> None:
    """掃描 Polymarket 套利機會（讀取分析用）。"""
    async def _run():
        with console.status("[cyan]Polymarket 掃描中..."):
            opps = await pm_scan(
                max_events=max_events,
                min_liquidity=min_liquidity,
                min_edge_per_set=min_edge_bps / 10_000,
                probe_shares=probe_shares,
            )
        console.print(Panel(
            f"找到 {len(opps)} 個機會。⚠️ 台灣為 Polymarket close-only，無法新開倉。\n"
            "這個結果僅供：(1) 觀察市場效率 (2) 作為 crossarb 訊號參考。",
            border_style="yellow",
        ))
        if opps:
            t = Table(show_lines=True)
            t.add_column("#", width=3)
            t.add_column("型態")
            t.add_column("標的", overflow="fold")
            t.add_column("成本/組", justify="right")
            t.add_column("Edge%", justify="right")
            t.add_column("可下組數", justify="right")
            t.add_column("總利潤", justify="right", style="green")
            for i, o in enumerate(opps[:top], 1):
                title = (o.event_title or o.legs[0].market_question)[:50]
                t.add_row(
                    str(i),
                    "同市場" if o.type == "same_market" else "互斥群組",
                    title,
                    f"${o.cost_per_set:.4f}",
                    f"{o.edge_pct * 100:+.2f}%",
                    f"{o.max_sets:,.1f}",
                    f"${o.total_edge:,.2f}",
                )
            console.print(t)
    asyncio.run(_run())


@polymarket_group.command(name="closest")
@click.option("--max-events", type=int, default=300)
@click.option("--min-liquidity", type=float, default=500.0)
@click.option("--top", type=int, default=15)
def pm_closest_cmd(max_events: int, min_liquidity: float, top: int) -> None:
    """Polymarket 最接近套利的市場。"""
    async def _run():
        async with GammaClient() as g, ClobClient() as c:
            evs = await g.fetch_active_events(max_events=max_events)
            sm = [m for ev in evs for m in ev.markets
                  if m.is_tradeable and m.liquidity >= min_liquidity]
            mutex = [ev for ev in evs if ev.neg_risk and len(ev.markets) >= 2
                     and all(m.is_tradeable for m in ev.markets)]
            tokens = set()
            for m in sm:
                tokens.add(m.yes_token); tokens.add(m.no_token)
            for ev in mutex:
                for m in ev.markets:
                    tokens.add(m.yes_token)
            books = await c.fetch_books(tokens)

        rows = []
        for m in sm:
            yb, nb = books.get(m.yes_token), books.get(m.no_token)
            if not yb or not nb or not yb.best_ask or not nb.best_ask:
                continue
            total = yb.best_ask.price + nb.best_ask.price
            rows.append((total, m))
        rows.sort(key=lambda x: x[0])
        t = Table(title="Polymarket 同市場 ΣAsk(YES+NO)")
        t.add_column("ΣAsk", justify="right")
        t.add_column("Edge", justify="right")
        t.add_column("流動性", justify="right")
        t.add_column("市場")
        for total, m in rows[:top]:
            edge = (1 - total) * 100
            t.add_row(f"${total:.4f}",
                      Text(f"{edge:+.2f}%", style="green" if edge > 0 else "yellow"),
                      f"${m.liquidity:,.0f}",
                      m.question[:70])
        console.print(t)
    asyncio.run(_run())


# ---------- Limitless 子命令（主力）----------

@click.group(name="limitless")
def limitless_group() -> None:
    """Limitless Exchange 操作（台灣可用）。"""


@limitless_group.command(name="scan")
@click.option("--max-markets", type=int, default=1000)
@click.option("--min-edge-bps", type=int, default=30,
              help="最小 edge 基點（30 = 0.3%，Limitless tick 比較粗）")
@click.option("--probe-shares", type=float, default=100.0)
@click.option("--min-volume-usd", type=float, default=100.0)
@click.option("--top", type=int, default=10)
@click.option("--watch", is_flag=True)
@click.option("--interval", type=int, default=30)
def lm_scan_cmd(max_markets: int, min_edge_bps: int, probe_shares: float,
                min_volume_usd: float, top: int, watch: bool, interval: int) -> None:
    """掃描 Limitless 套利機會（讀取）。"""

    async def _once():
        with console.status("[cyan]Limitless 掃描中..."):
            opps = await lm_scan(
                max_markets=max_markets,
                min_edge=min_edge_bps / 10_000,
                probe_shares=probe_shares,
                min_volume_usd=min_volume_usd,
            )
        console.rule(f"[bold]Limitless：找到 {len(opps)} 個機會[/bold]")
        if not opps:
            console.print(Panel(
                "沒有發現符合閾值的機會。試試 `polymkt limitless closest` 看市場效率。",
                border_style="yellow"))
            return
        t = Table(show_lines=True)
        t.add_column("#", width=3)
        t.add_column("型態")
        t.add_column("標的", overflow="fold")
        t.add_column("成本/組", justify="right")
        t.add_column("Edge%", justify="right")
        t.add_column("可下組數", justify="right")
        t.add_column("總利潤", justify="right", style="green")
        t.add_column("PolyArb")
        for i, o in enumerate(opps[:top], 1):
            t.add_row(
                str(i),
                "同市場" if o.type == "same_market" else "互斥群組",
                o.title[:50],
                f"${o.cost_per_set:.4f}",
                f"{o.edge_pct * 100:+.2f}%",
                f"{o.max_sets:,.1f}",
                f"${o.total_edge:,.2f}",
                "🪞" if o.is_poly_arbitrage else "",
            )
        console.print(t)

        # 詳細
        best = opps[0]
        body = (
            f"[bold]{best.title}[/bold]\n"
            f"型態：{'同市場' if best.type == 'same_market' else '互斥群組'}\n"
            f"鏡像 Polymarket：{'是' if best.is_poly_arbitrage else '否'}\n\n"
            f"組成：\n" +
            "\n".join(f"  • {l.label} @ ${l.price:.4f} × {l.shares:,.1f}股 = ${l.notional:.2f}"
                     for l in best.legs[:6]) +
            (f"\n  • ... +{len(best.legs)-6} 條" if len(best.legs) > 6 else "") +
            f"\n\n每組成本：${best.cost_per_set:.4f}\n"
            f"預估費用：${best.fee_drag:.4f}（{best.fee_rate * 100:.2f}%）\n"
            f"每組淨利：${best.edge_per_set:.4f}（{best.edge_pct * 100:+.2f}%）\n"
            f"最大組數：{best.max_sets:,.1f}\n"
            f"[bold green]預期總利潤：${best.total_edge:,.2f}[/bold green]\n"
            f"需要資金：${best.required_capital:,.2f}"
        )
        console.print(Panel(body, title="最佳機會", border_style="green"))

    async def _loop():
        while True:
            try:
                await _once()
            except Exception as e:
                console.print(f"[red]錯誤：{e}[/red]")
            console.print(f"[dim]{interval}s 後再掃...[/dim]")
            await asyncio.sleep(interval)

    try:
        asyncio.run(_loop() if watch else _once())
    except KeyboardInterrupt:
        sys.exit(0)


@limitless_group.command(name="closest")
@click.option("--max-markets", type=int, default=1000)
@click.option("--min-volume-usd", type=float, default=100.0)
@click.option("--top", type=int, default=15)
def lm_closest_cmd(max_markets: int, min_volume_usd: float, top: int) -> None:
    """Limitless 最接近套利的市場（含反向 edge）。"""
    async def _run():
        with console.status("[cyan]Limitless 計算中..."):
            sm_rows, mg_rows = await lm_closest(
                max_markets=max_markets,
                min_volume_usd=min_volume_usd,
            )

        # 同市場
        t1 = Table(title=f"Limitless 同市場 ΣAsk(YES+NO) — top {top}")
        t1.add_column("ΣAsk", justify="right")
        t1.add_column("Edge%", justify="right")
        t1.add_column("PolyArb")
        t1.add_column("市場")
        for r in sm_rows[:top]:
            style = "green" if r.edge_pct > 0 else "yellow"
            t1.add_row(
                f"${r.sigma_or_total:.4f}",
                Text(f"{r.edge_pct:+.2f}%", style=style),
                "🪞" if r.is_poly_arbitrage else "",
                r.title[:70],
            )
        console.print(t1)

        # 互斥群組
        t2 = Table(title=f"Limitless 互斥群組 ΣYES — top {top}")
        t2.add_column("ΣYES", justify="right")
        t2.add_column("Edge%", justify="right")
        t2.add_column("N")
        t2.add_column("PolyArb")
        t2.add_column("事件")
        for r in mg_rows[:top]:
            style = "green" if r.edge_pct > 0 else "yellow"
            t2.add_row(
                f"${r.sigma_or_total:.4f}",
                Text(f"{r.edge_pct:+.2f}%", style=style),
                str(r.n_legs),
                "🪞" if r.is_poly_arbitrage else "",
                r.title[:70],
            )
        console.print(t2)

    asyncio.run(_run())


# ---------- Limitless 交易子命令 ----------

@limitless_group.command(name="auth-derive")
@click.option("--privy-token", required=True, envvar="PRIVY_TOKEN",
              help="從瀏覽器 DevTools 拿到的 Privy `token`（不是 privy_access_token）")
@click.option("--label", default="polymkt-bot", help="API token 標籤")
def lm_auth_derive_cmd(privy_token: str, label: str) -> None:
    """一次性：用 Privy token 換 HMAC 永久 token。

    流程：
      1. 用錢包登入 https://limitless.exchange
      2. 開 DevTools → Application → LocalStorage/Cookies 找 Privy `token`
      3. 執行：polymkt limitless auth-derive --privy-token <token>
      4. 把回傳的 token_id + secret 寫進 .env

    [bold red]Secret 只會顯示一次！[/bold red]
    """
    from .limitless.trading import derive_hmac_credentials

    async def _run():
        try:
            token_id, secret = await derive_hmac_credentials(
                privy_identity_token=privy_token,
                label=label,
            )
        except Exception as e:
            console.print(f"[red]無法 derive token：{e}[/red]")
            sys.exit(1)
        console.print(Panel(
            f"[bold green]成功！把下面兩行加進 .env：[/bold green]\n\n"
            f"LIMITLESS_API_TOKEN_ID={token_id}\n"
            f"LIMITLESS_API_SECRET={secret}\n\n"
            f"[bold red]Secret 只會顯示這一次。[/bold red]\n"
            f"[yellow]絕對不要把 .env commit 進 git。[/yellow]",
            border_style="green",
        ))

    asyncio.run(_run())


@limitless_group.command(name="place-order")
@click.option("--slug", required=True, help="Limitless market slug")
@click.option("--side", type=click.Choice(["BUY", "SELL"]), required=True)
@click.option("--outcome", type=click.Choice(["YES", "NO"]), required=True,
              help="YES 或 NO outcome")
@click.option("--price", type=float, required=True, help="0 < price < 1")
@click.option("--size", type=float, required=True, help="股數")
@click.option("--order-type", type=click.Choice(["FAK", "GTC", "FOK"]), default="FAK",
              help="FAK = IOC（預設）, GTC = 限價排隊, FOK = 全成或全消")
@click.option("--post-only", is_flag=True, help="(僅 GTC 有效) 只當 maker，不吃對手單")
@click.option("--execute", is_flag=True,
              help="真實下單；不加此旗標就只是 dry-run")
def lm_place_order_cmd(slug: str, side: str, outcome: str, price: float,
                       size: float, order_type: str, post_only: bool,
                       execute: bool) -> None:
    """手動下一筆訂單。預設 dry-run，加 --execute 才會真實送出。"""
    from .limitless.trading import LimitlessTradingClient, OrderRequest

    async def _run():
        # 先讀市場拿 token_id（YES 或 NO）
        async with LimitlessClient() as lc:
            ob = await lc.fetch_orderbook(slug)
        if ob is None:
            console.print(f"[red]找不到市場 {slug!r} 的 orderbook[/red]")
            sys.exit(1)

        # YES token 就是 orderbook 的 tokenId；NO 是 1-price 對稱，但 LM 訂單需要實際 NO token id
        # 必須再撈一次市場 metadata（包含 tokens.yes / tokens.no）
        # 從 LM client 內部 _client 直接 GET（或加方法到 client）
        # 簡化：用 GET /markets/{slug}
        import httpx
        r = httpx.get(f"https://api.limitless.exchange/markets/{slug}", timeout=10)
        r.raise_for_status()
        m = r.json()
        yes_token = m["tokens"]["yes"]
        no_token = m["tokens"]["no"]
        token_id = yes_token if outcome == "YES" else no_token

        # 暫時把 EXECUTE flag 注入環境變數，再讀 client
        if execute:
            os.environ["LIMITLESS_EXECUTE"] = "1"

        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)

        req = OrderRequest(
            market_slug=slug,
            token_id=token_id,
            side=side,
            price=price,
            size_shares=size,
            order_type=order_type,
            post_only=post_only,
        )
        console.print(Panel(
            f"[bold]即將{'真實' if execute else 'DRY-RUN'}下單[/bold]\n"
            f"市場：{m.get('title', slug)} ({slug})\n"
            f"方向：{side} {outcome}（token={token_id[:16]}...）\n"
            f"價格：${price:.4f} × {size:,.2f} 股 = ${req.notional:.2f}\n"
            f"訂單類型：{order_type}{'  post-only' if post_only else ''}",
            border_style="green" if execute else "yellow",
            title="下單預覽",
        ))

        result = await tc.place_order(req)
        await tc.close()

        if not result.accepted:
            console.print(f"[red]拒絕：{result.error}[/red]")
            sys.exit(1)

        if result.dry_run:
            console.print("[yellow]DRY-RUN 通過所有安全檢查。要實際送出請加 --execute[/yellow]")
        else:
            console.print(Panel(
                f"[bold green]訂單已送出[/bold green]\n"
                f"order_id: {result.order_id}\n"
                f"已成交：{result.matched_shares:,.2f} 股 "
                f"(${result.matched_notional:.2f})",
                border_style="green",
            ))

    asyncio.run(_run())


# ---------- 跨平台價差 ----------

@click.command(name="crossarb")
@click.option("--min-event-similarity", type=float, default=0.6,
              help="LM group 與 PM event 標題相似度門檻（預設 0.6）")
@click.option("--min-sub-match", type=float, default=0.7,
              help="子市場匹配分數門檻（預設 0.7）")
@click.option("--min-diff-pct", type=float, default=1.0,
              help="最小 YES 價差百分點（預設 1.0pp = $0.01）")
@click.option("--limitless-max", type=int, default=1000)
@click.option("--polymarket-max", type=int, default=300)
@click.option("--top", type=int, default=20)
def crossarb_cmd(min_event_similarity: float, min_sub_match: float,
                 min_diff_pct: float,
                 limitless_max: int, polymarket_max: int, top: int) -> None:
    """跨平台 Polymarket↔Limitless 價差訊號。

    台灣使用者用法：把 Polymarket 當作「公平價格訊號」，當 Limitless 偏離超過
    閾值時，假設它會收斂回 Polymarket 價 → 在 Limitless 下對應方向的單。

    這**不是純套利**（純套利要兩邊都能下），但統計上有 edge。
    """
    async def _run():
        with console.status("[cyan]擷取兩邊資料 + 標題比對..."):
            pairs = await find_cross_pairs(
                min_event_similarity=min_event_similarity,
                min_sub_match=min_sub_match,
                min_diff_pct=min_diff_pct,
                limitless_max_markets=limitless_max,
                polymarket_max_events=polymarket_max,
            )
        console.rule(f"[bold]找到 {len(pairs)} 組跨平台價差[/bold]")
        if not pairs:
            console.print("沒找到符合條件的配對。試試 --min-event-similarity 0.5 或 --min-diff-pct 0.5")
            return

        t = Table(show_lines=True)
        t.add_column("相似度", justify="right")
        t.add_column("PM(YES)", justify="right")
        t.add_column("LM(YES)", justify="right")
        t.add_column("價差", justify="right")
        t.add_column("方向訊號", overflow="fold")
        t.add_column("標的", overflow="fold")
        for p in pairs[:top]:
            color = "green" if p.diff_pct < 0 else "red"
            t.add_row(
                f"{p.title_similarity:.2f}",
                f"${p.pm_yes_mid:.3f}",
                f"${p.lm_yes_mid:.3f}",
                Text(f"{p.diff_pct:+.2f}pp", style=color),
                p.signal,
                p.limitless.title[:50],
            )
        console.print(t)
        console.print(Panel(
            "[bold]如何使用[/bold]\n"
            "• 表中『方向訊號』告訴你在 Limitless 該買哪一邊\n"
            "• 假設：Polymarket 流動性大 → 價格較有效率 → Limitless 會回歸\n"
            "• [yellow]這不是無風險套利[/yellow]：若 Polymarket 自己定價錯了，訊號就錯\n"
            "• 建議只在價差 > 2pp + 該題目 Polymarket 流動性高 時下注\n"
            "• 控倉位：每筆不超過總資金 5%；同時開多筆分散事件風險",
            border_style="dim",
        ))

    asyncio.run(_run())


# ---------- 組裝 ----------

@click.group()
def cli() -> None:
    """polymkt — 跨平台預測市場分析與下單工具。"""
    load_dotenv()


@click.command(name="crossarb-execute")
@click.option("--min-event-similarity", type=float, default=0.8)
@click.option("--min-diff-pct", type=float, default=5.0,
              help="只交易價差 >= 此值（百分點）的訊號（預設 5pp，較保守）")
@click.option("--max-positions", type=int, default=5,
              help="本次最多開幾個倉位")
@click.option("--notional-per-trade", type=float, default=10.0,
              help="每筆 USDC 大小（預設 $10，安全起步）")
@click.option("--order-type", type=click.Choice(["FAK", "GTC"]), default="GTC",
              help="GTC = 限價單，立即可吃就吃、不可吃就掛單等；"
                   "FAK = IOC 立即成交（吃不到就放棄）")
@click.option("--safety-margin-pct", type=float, default=4.0,
              help="買價上限 = PM_fair × (1 - 此值%)，預設 4%（含 3% 最壞情況手續費 + 1% 安全 buffer）。"
                   "值越大越保守、越不易成交但越安全；值越小越激進。")
@click.option("--limitless-max", type=int, default=1000)
@click.option("--polymarket-max", type=int, default=300)
@click.option("--execute", is_flag=True, help="真實下單；不加就只是 dry-run")
def crossarb_execute_cmd(min_event_similarity: float, min_diff_pct: float,
                         max_positions: int, notional_per_trade: float,
                         order_type: str, safety_margin_pct: float,
                         limitless_max: int, polymarket_max: int,
                         execute: bool) -> None:
    """自動化跨平台訊號交易。

    流程：
      1. 跑 crossarb 找跨平台價差
      2. 對 |diff| >= --min-diff-pct 的訊號，計算可接受的最高買價
      3. 在 Limitless 下單捕捉（GTC 或 FAK）
      4. 全程 dry-run 除非加 --execute

    定價邏輯（重要）：
      買價上限 = PM 端的「公平價」× (1 - safety_margin_pct%)
      只要 LM 上有比這便宜的對手單，就會成交、保證正 EV（假設 PM 是對的）。
      若 LM 沒人賣得這麼便宜，訂單就掛著（GTC）或放棄（FAK）。

    [bold]預設行為[/bold]：dry-run，列出將要做什麼，**不真的下單**。
    """
    from .limitless.trading import LimitlessTradingClient, OrderRequest

    async def _run():
        with console.status("[cyan]擷取跨平台價差..."):
            pairs = await find_cross_pairs(
                min_event_similarity=min_event_similarity,
                min_sub_match=0.7,
                min_diff_pct=min_diff_pct,
                limitless_max_markets=limitless_max,
                polymarket_max_events=polymarket_max,
            )

        # 取最佳 N 個 |diff|
        candidates = pairs[:max_positions]
        if not candidates:
            console.print("[yellow]沒有符合條件的訊號。[/yellow]")
            return

        if execute:
            os.environ["LIMITLESS_EXECUTE"] = "1"
        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            console.print(Panel(
                "尚未設定 Limitless 認證或私鑰。\n"
                "請先跑：[bold]polymkt limitless auth-derive[/bold] 取得 HMAC token，\n"
                "並把 token_id / secret / BASE_PRIVATE_KEY 寫入 .env。\n\n"
                "[yellow]這次跑出的價差結果如下（純分析、未下單）：[/yellow]",
                border_style="yellow",
            ))
            for p in candidates:
                console.print(
                    f"  {p.diff_pct:+.2f}pp | PM=${p.pm_yes_mid:.3f} LM=${p.lm_yes_mid:.3f} | "
                    f"{p.limitless.title[:60]}"
                )
            return

        t = Table(title=f"訊號交易 ({'真實送出' if execute else 'DRY-RUN'})", show_lines=True)
        t.add_column("方向")
        t.add_column("標的", overflow="fold")
        t.add_column("PM 公平價", justify="right")
        t.add_column("買價上限", justify="right")
        t.add_column("股數", justify="right")
        t.add_column("Notional", justify="right")
        t.add_column("結果")

        for p in candidates:
            lm = p.limitless
            # 訊號方向 + 公平價（用 PM 報價作為 fair value 估計）
            # - LM YES 便宜（diff<0）→ 買 YES，公平價 = PM_YES
            # - LM YES 貴（diff>0）→ 買 NO（等同賣 YES），公平價 = 1 - PM_YES
            if p.diff_pct < 0:
                outcome_token = lm.yes_token
                outcome_label = "YES"
                pm_fair = p.pm_yes_mid
            else:
                outcome_token = lm.no_token
                outcome_label = "NO"
                pm_fair = 1.0 - p.pm_yes_mid

            # 買價上限 = 公平價 × (1 - safety_margin%)
            # 保證即使付到這個價，扣 3% 最壞 taker fee 後仍有正 EV
            target_price = pm_fair * (1 - safety_margin_pct / 100)
            # 鉗在合理範圍（tick = 0.001）
            target_price = max(0.01, min(0.99, round(target_price, 3)))

            size = max(1.0, notional_per_trade / target_price)
            req = OrderRequest(
                market_slug=lm.slug,
                token_id=outcome_token,
                side="BUY",          # 訊號交易永遠是 BUY（買便宜的一邊）
                price=round(target_price, 3),  # tick = 0.001
                size_shares=round(size, 2),
                order_type=order_type,
            )

            # 預期 edge（假設 PM 對）：(pm_fair - target_price) × shares
            expected_edge_per_share = pm_fair - target_price
            expected_edge_total = expected_edge_per_share * size

            res = await tc.place_order(req)
            if not res.accepted:
                outcome_text = f"[red]拒絕：{res.error}[/red]"
            elif res.dry_run:
                outcome_text = (
                    f"[yellow]DRY-RUN OK[/yellow]\n"
                    f"預期 EV: ${expected_edge_total:+.2f}"
                )
            else:
                outcome_text = (
                    f"[green]ok order={res.order_id} matched={res.matched_shares:.1f}"
                    f"[/green]"
                )

            t.add_row(
                f"BUY {outcome_label}",
                lm.title[:50],
                f"${pm_fair:.3f}",
                f"${req.price:.3f}",
                f"{req.size_shares:,.1f}",
                f"${req.notional:.2f}",
                outcome_text,
            )

        await tc.close()
        console.print(t)
        console.print(Panel(
            f"本次累計 notional：${tc.session_notional_used:.2f}",
            border_style="dim",
        ))

    asyncio.run(_run())


cli.add_command(polymarket_group)
cli.add_command(limitless_group)
cli.add_command(crossarb_cmd)
cli.add_command(crossarb_execute_cmd)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
