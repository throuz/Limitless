"""統一 CLI 入口。

子命令結構：
- limitless pm scan       — Polymarket 套利掃描（**台灣無法下單**，僅作分析）
- limitless pm closest    — Polymarket 最接近套利的市場
- limitless scan        — Limitless 套利掃描（**台灣可用**）
- limitless closest     — Limitless 最接近套利的市場
- limitless crossarb              — 跨平台 Polymarket↔Limitless 價差訊號
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

from .polymarket.clients import ClobClient, GammaClient
from .polymarket.scanner import scan as pm_scan
from .client import LimitlessClient
from .scanner import scan as lm_scan, closest as lm_closest
from .crossarb import find_cross_pairs


console = Console()


# ---------- Polymarket 子命令（保留原功能）----------

@click.group(name="pm")
def polymarket_group() -> None:
    """Polymarket 工具(讀取分析、oracle、訊號)。

    台灣 close-only,Polymarket 只當「觀察 + 輔助 oracle」用。
    主交易場在 [limitless](根目錄命令)。
    """


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


# ---------- Limitless 子命令(主力 — 同時也在 top level 註冊為直接命令)----------

@click.group(name="limitless")
def limitless_group() -> None:
    """Limitless Exchange 操作(主交易場)。

    所有命令也可以直接呼叫(不用 limitless 前綴),例如:
        limitless mm-loop    ≡ limitless mm-loop
        limitless mm-rank    ≡ limitless mm-rank
        limitless scan       ≡ limitless scan
    """


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
                "沒有發現符合閾值的機會。試試 `limitless closest` 看市場效率。",
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
@click.option("--label", default="limitless-bot", help="API token 標籤")
def lm_auth_derive_cmd(privy_token: str, label: str) -> None:
    """一次性：用 Privy token 換 HMAC 永久 token。

    流程：
      1. 用錢包登入 https://limitless.exchange
      2. 開 DevTools → Application → LocalStorage/Cookies 找 Privy `token`
      3. 執行：limitless auth-derive --privy-token <token>
      4. 把回傳的 token_id + secret 寫進 .env

    [bold red]Secret 只會顯示一次！[/bold red]
    """
    from .trading import derive_hmac_credentials

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
    from .trading import LimitlessTradingClient, OrderRequest

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


# ---------- 做市 ----------

@limitless_group.command(name="make-market")
@click.option("--slug", required=True, help="要做市的市場 slug")
@click.option("--capital", "capital_usdc", type=float, default=100.0,
              help="本次做市總資本上限（USDC）")
@click.option("--quote-size", type=float, default=20.0,
              help="每邊單筆股數（YES 與 NO 各掛這麼多）")
@click.option("--target-profit-pct", type=float, default=4.0,
              help="想吃的價差百分點（雙邊都成交時的 ROI），預設 4%")
@click.option("--half-spread-pct", type=float, default=1.0,
              help="報價偏離 LM mid 多少，預設 1pp（單邊）")
@click.option("--max-inventory", type=float, default=50.0,
              help="YES 或 NO 任一達此股數就停止下單（庫存上限）")
@click.option("--iter-sleep", type=int, default=30,
              help="每次重新報價間隔秒數，預設 30 秒")
@click.option("--duration", type=int, default=600,
              help="做市持續秒數（預設 10 分鐘）；設 0 = 無限直到 Ctrl-C")
@click.option("--oracle", type=click.Choice(["lm", "pm", "blend"]), default="lm",
              help="公平價來源：lm=LM 自己 mid（預設）/ pm=Polymarket 鏡像（更抗資訊套利）"
                   "/ blend=PM 60% + LM 40%")
@click.option("--inventory-skew-pct", type=float, default=0.5,
              help="(v0.5b) 庫存每超出 max 的 10%，把對應方向 bid 拉走多少 pp（預設 0.5）")
# v0.6 旗標
@click.option("--microprice/--no-microprice", "use_microprice", default=True,
              help="(v0.6) 用 microprice（對手側 size 加權）當公平價；預設開")
@click.option("--toxicity-window", type=int, default=5,
              help="(v0.6) toxicity 偵測滾動窗口輪數（預設 5）")
@click.option("--toxicity-imbalance", type=float, default=0.7,
              help="(v0.6) YES/NO fill 不對稱比 > 此值 → 加寬該側（預設 0.7）")
@click.option("--toxicity-pm-velocity", type=float, default=0.03,
              help="(v0.6) PM mid 窗口內漂移 > $ 此值 → 撤所有單（預設 0.03）")
@click.option("--toxicity-ask-drop", type=float, default=0.02,
              help="(v0.6) LM YES best ask 窗口內下殺 > $ 此值 → 撤 NO bid（預設 0.02）")
@click.option("--toxicity-widen-mult", type=float, default=2.0,
              help="(v0.6) 偵測到 toxicity 時 spread × 此倍數（預設 2.0）")
@click.option("--unwind-inventory-pct", type=float, default=0.6,
              help="(v0.6) 庫存超過 max × 此比例就主動掛 SELL（預設 0.6 = 60%）")
@click.option("--unwind-premium-pct", type=float, default=1.0,
              help="(v0.6) SELL 報價比 mid 高多少 pp（預設 1.0pp）")
@click.option("--emergency-close-hours", type=float, default=24.0,
              help="(v0.6) 距結算 < 此小時數 → 強制 cancel + 市價清倉（預設 24h）")
@click.option("--no-emergency-close", is_flag=True,
              help="(v0.6) 停用結算窗口強制清倉（不建議）")
@click.option("--execute", is_flag=True,
              help="真實下單；不加就只是 dry-run，不會送 API")
def lm_make_market_cmd(slug: str, capital_usdc: float, quote_size: float,
                       target_profit_pct: float, half_spread_pct: float,
                       max_inventory: float, iter_sleep: int, duration: int,
                       oracle: str, inventory_skew_pct: float,
                       use_microprice: bool,
                       toxicity_window: int, toxicity_imbalance: float,
                       toxicity_pm_velocity: float, toxicity_ask_drop: float,
                       toxicity_widen_mult: float,
                       unwind_inventory_pct: float, unwind_premium_pct: float,
                       emergency_close_hours: float, no_emergency_close: bool,
                       execute: bool) -> None:
    """在指定市場做雙 BID 做市（CTF: BUY YES + BUY NO）。

    策略：兩邊同時掛買單，理論上總和 < $1。若兩邊都被吃 → 持有 1 YES + 1 NO，
    結算保證 $1，賺差額。若只吃一邊 → 累積該方向庫存，等對手出現或結算。

    [yellow]預設 dry-run[/yellow]：只印「將要做什麼」、不真的送 API。
    """
    from .market_maker import MakerConfig, MarketMaker
    from .trading import LimitlessTradingClient

    cfg = MakerConfig(
        slug=slug,
        capital_usdc=capital_usdc,
        quote_size_shares=quote_size,
        target_profit_pct=target_profit_pct,
        half_spread_offset_pct=half_spread_pct,
        max_inventory_shares=max_inventory,
        iteration_sleep_s=float(iter_sleep),
        duration_s=duration,
        oracle_mode=oracle,
        inventory_skew_pct=inventory_skew_pct,
        use_microprice=use_microprice,
        toxicity_window=toxicity_window,
        toxicity_imbalance_threshold=toxicity_imbalance,
        toxicity_pm_velocity_threshold=toxicity_pm_velocity,
        toxicity_ask_drop_threshold=toxicity_ask_drop,
        toxicity_widen_multiplier=toxicity_widen_mult,
        unwind_inventory_pct=unwind_inventory_pct,
        unwind_premium_pct=unwind_premium_pct,
        emergency_close_hours=emergency_close_hours,
        emergency_close_enabled=not no_emergency_close,
    )

    if execute:
        os.environ["LIMITLESS_EXECUTE"] = "1"

    async def _run():
        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            console.print("[yellow]無認證無法做市；先跑 auth-derive 並設定 .env[/yellow]")
            sys.exit(1)

        async with LimitlessClient() as lc:
            mm = MarketMaker(cfg, tc, lc)
            try:
                await mm.init_market()
            except Exception as e:
                console.print(f"[red]無法載入市場 {slug}: {e}[/red]")
                sys.exit(1)

            hrs = mm.hours_to_resolution()
            hrs_txt = f"{hrs:.1f}h" if hrs is not None else "未知"
            console.print(Panel(
                f"[bold]做市啟動 v0.6 ({'真實' if execute else 'DRY-RUN'})[/bold]\n"
                f"市場：{mm.market.get('title', slug)}\n"
                f"資本上限：${capital_usdc:.0f}  "
                f"每邊股數：{quote_size:.0f}\n"
                f"目標 ROI：{target_profit_pct:.1f}%  "
                f"報價偏移：±{half_spread_pct:.1f}pp\n"
                f"庫存上限：{max_inventory:.0f} 股  "
                f"持續：{duration}s  "
                f"重評間隔：{iter_sleep}s\n"
                f"[dim]v0.6: microprice={'on' if use_microprice else 'off'}  "
                f"toxicity window={toxicity_window}  "
                f"unwind@={unwind_inventory_pct*100:.0f}%  "
                f"emergency< {emergency_close_hours:.0f}h  "
                f"距結算 {hrs_txt}[/dim]",
                border_style="green" if execute else "yellow",
            ))

            def on_iter(n, result):
                if result.emergency_close:
                    console.print(f"[bold red]#{n:02d}  🚨 緊急清倉[/bold red]")
                else:
                    tox = f"[red] tox={result.toxicity_score:.2f}[/red]" if result.toxicity_score > 0 else ""
                    sells = ""
                    if result.yes_sell_price > 0:
                        sells += f"  SELL YES@${result.yes_sell_price:.3f}"
                    if result.no_sell_price > 0:
                        sells += f"  SELL NO@${result.no_sell_price:.3f}"
                    line = (
                        f"[dim]#{n:02d}[/dim]  "
                        f"YES @${result.yes_bid_price:.3f} + NO @${result.no_bid_price:.3f}  "
                        f"(和 ${result.yes_bid_price + result.no_bid_price:.3f}){tox}{sells}"
                    )
                    console.print(line)
                for note in result.notes:
                    console.print(f"      [dim]{note}[/dim]")

            try:
                stats = await mm.run(on_iteration=on_iter)
            except KeyboardInterrupt:
                console.print("\n[yellow]使用者中止，正在取消訂單...[/yellow]")
                await mm.cancel_all()
                sys.exit(0)
            finally:
                await tc.close()

        emerg = stats.get("emergency_close")
        console.print(Panel(
            f"做市結束{' (緊急清倉觸發)' if emerg else ''}\n"
            f"執行 iterations: {stats['iterations']}\n"
            f"累計資本使用: ${stats['capital_used']:.2f}\n"
            f"最後一輪報價: YES ${stats['last_yes_bid']:.3f} / NO ${stats['last_no_bid']:.3f}",
            border_style="red" if emerg else "dim",
        ))

    asyncio.run(_run())


# ---------- 做市市場排序（v0.6 新增）----------

# 高風險關鍵字 → 接近事件時機波動大,做市風險高
_NEWS_RISK_KEYWORDS = [
    # 政治 / 法律
    "election", "court", "ruling", "verdict", "vote", "primary", "debate",
    "impeach", "indict", "supreme",
    # 央行 / 經濟
    "fed", "fomc", "cpi", "ppi", "gdp", "payroll", "nfp", "jobless",
    "rate hike", "rate cut", "powell", "ecb",
    # 公司事件
    "earnings", "ipo", "merger", "acquisition", "lawsuit",
    # 體育即時
    "tonight", "today", "match", "game",
    # 中文
    "選舉", "判決", "投票", "央行", "升息", "降息", "通膨", "財報",
]


def _news_risk_score(title: str) -> float:
    """簡單關鍵字 heuristic：命中越多分數越高（0 = 安全）。"""
    t = title.lower()
    return float(sum(1 for kw in _NEWS_RISK_KEYWORDS if kw in t))


def _parse_iso(date_str: str | None) -> float | None:
    """回傳距現在的天數；無法解析 → None。

    Limitless 用兩種格式：ISO（"2026-05-21T..."）和人類可讀（"May 21, 2026"）。
    """
    if not date_str:
        return None
    from datetime import datetime, timezone
    s = str(date_str)
    # 試 ISO
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        pass
    # 試 "May 21, 2026" / "May 21, 2026 12:00 PM" 等
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except ValueError:
            continue
    return None


@limitless_group.command(name="reward-farm")
@click.option("--coins", default="BTC,ETH",
              help="逗號分隔的幣種(對應 'X Up or Down' 市場),預設 BTC,ETH")
@click.option("--freq", type=click.Choice(["5 Min", "15 Min", "Hourly"]), default="15 Min",
              help="結算頻率(預設 15 Min,比 5 Min 好觀察)")
@click.option("--size", "size_shares", type=float, default=100.0,
              help="每筆掛單股數;需 ≥ minSize(100)才算合格流動性")
@click.option("--edge-frac", type=float, default=0.7,
              help="δ = edge_frac × maxSpread。越小越貼 mid(分數高但易被成交),預設 0.7(偏安全)")
@click.option("--poll", "poll_interval_s", type=float, default=20.0, help="每輪間隔秒數")
@click.option("--pull-before", "pull_before_settlement_s", type=float, default=45.0,
              help="距結算 < 此秒數就撤所有單(避開到期 snap)")
@click.option("--duration", type=int, default=0, help="跑多少秒;0 = 直到 Ctrl-C")
@click.option("--execute", is_flag=True, help="真實掛單;不加就只 dry-run")
def lm_reward_farm_cmd(coins: str, freq: str, size_shares: float, edge_frac: float,
                       poll_interval_s: float, pull_before_settlement_s: float,
                       duration: int, execute: bool) -> None:
    """賺 Limitless LP 流動性獎勵(不是賺 spread)。

    在 maxSpread 帶內掛雙邊 GTC post_only 單,按「在帶內的 size × 近 mid²」每分鐘計分,
    分數佔比 × 每日獎勵池 = USDC 獎勵(不需成交)。臨近結算自動撤單避開 snap。

    [yellow]預設 dry-run[/yellow];加 --execute 才真實掛單。
    """
    from datetime import datetime, timezone
    from .reward_farm import RewardFarmConfig, RewardFarmer
    from .trading import LimitlessTradingClient

    coin_list = [c.strip().upper() for c in coins.split(",") if c.strip()]
    cfg = RewardFarmConfig(
        coins=coin_list, freq=freq, size_shares=size_shares, edge_frac=edge_frac,
        poll_interval_s=poll_interval_s, pull_before_settlement_s=pull_before_settlement_s,
        duration_s=duration,
    )
    if execute:
        os.environ["LIMITLESS_EXECUTE"] = "1"

    async def _run():
        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        # 安全限額:每筆最高股數 × 1.0(最壞 BUY 價)+ buffer;session 設大(每輪會重設)
        tc.safety.max_notional_per_order = max(tc.safety.max_notional_per_order, size_shares * 1.0 + 5)
        tc.safety.max_notional_per_session = max(tc.safety.max_notional_per_session,
                                                 size_shares * 2 * (len(coin_list) + 1) + 50)
        async with LimitlessClient() as lm:
            farmer = RewardFarmer(cfg, tc, lm)
            console.print(Panel(
                f"[bold]Reward Farming ({'真實' if execute else 'DRY-RUN'})[/bold]\n"
                f"幣種:{', '.join(coin_list)}  頻率:{freq}\n"
                f"每筆:{size_shares:.0f} 股  δ={edge_frac:.0%}×maxSpread  "
                f"poll={poll_interval_s:.0f}s  結算前撤單:{pull_before_settlement_s:.0f}s\n"
                f"[dim]獎勵=分數佔比×每日池(不需成交);被成交=逆選擇暴露,留意 fills[/dim]",
                border_style="green" if execute else "yellow"))

            def on_round(n, results):
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                console.print(f"[dim]#{n:03d} {ts}[/dim]")
                for r in results:
                    if r.get("note"):
                        console.print(f"   [dim]{r['slug'][:30]}: {r['note']}[/dim]")
                        continue
                    fy, fn = r.get("fills", (0, 0))
                    fill_txt = f" [red]fills Y={fy} N={fn}[/red]" if (fy or fn) else ""
                    bids = r.get("bids")
                    bid_txt = f" YES@{bids[0]} NO@{bids[1]}" if bids else ""
                    err = f" [red]{r.get('err')}[/red]" if r.get("err") else ""
                    console.print(
                        f"   {r['slug'][:26]:26} {r.get('action',''):10} "
                        f"M={r.get('M','-')}{bid_txt} left={r.get('secs_left','-')}s{fill_txt}{err}")

            try:
                stats = await farmer.run(on_round=on_round)
            except KeyboardInterrupt:
                console.print("\n[yellow]中止,撤所有單...[/yellow]")
            finally:
                await tc.close()
            ib = stats.get("in_band", {})
            lines = []
            for s, (y, n) in stats["fills"].items():
                inb, tot = ib.get(s, (0, 0))
                frac = f"{100*inb/tot:.0f}%" if tot else "-"
                lines.append(f"  {s[:30]}: 帶內{frac}({inb}/{tot})  fills YES={y} NO={n}")
            console.print(Panel(
                f"[bold]結束[/bold]  輪數:{stats['rounds']}\n"
                f"[dim]帶內% = 計分時段佔比(稀釋 naive 估計的關鍵);fills = 逆選擇暴露[/dim]\n" +
                "\n".join(lines),
                border_style="cyan"))

    asyncio.run(_run())


@limitless_group.command(name="mm-rank")
@click.option("--max-markets", type=int, default=500,
              help="掃描多少個活躍市場（預設 500，越多越慢）")
@click.option("--min-volume-usd", type=float, default=200.0,
              help="忽略量低於此值的市場（預設 $200；太冷沒人吃單）")
@click.option("--min-days", type=float, default=2.0,
              help="距結算 < N 天的市場過濾掉（避免結算 risk）")
@click.option("--min-spread-bps", type=int, default=50,
              help="LM YES spread 小於此值（基點）就不夠寬，跳過。100 bps = 1pp")
@click.option("--top", type=int, default=15)
def lm_mm_rank_cmd(max_markets: int, min_volume_usd: float,
                   min_days: float, min_spread_bps: int, top: int) -> None:
    """v0.6：把 Limitless 上適合做市的市場排序輸出。

    評分公式：
      score = spread_pp × (days_to_res / 7) × poly_arb_bonus × (1 + log(1+volume/1000))
              / (1 + news_risk)

    - spread_pp：YES side bid-ask spread 百分點；越寬越好
    - days_to_res：距結算天數；越遠越多重掛機會（但 >30 不再加分）
    - poly_arb_bonus：有 PM 鏡像 +30%（可當 oracle）
    - volume：log 量級正向加成（避免零量市場）
    - news_risk：標題命中高風險關鍵字數，當分母懲罰
    """
    from .scanner import LimitlessClient  # 同模組
    import math

    async def _run():
        async with LimitlessClient() as lc:
            with console.status("[cyan]載入活躍市場..."):
                singles, groups = await lc.fetch_active_markets(max_markets=max_markets)

            # 只看 single CLOB 市場（group 排另一個排序）
            candidates = [m for m in singles
                          if m.is_tradeable
                          and m.volume_usd >= min_volume_usd]

            # 結算過濾
            with_days = []
            for m in candidates:
                d = _parse_iso(m.end_date)
                if d is None or d < min_days:
                    continue
                with_days.append((m, d))

            if not with_days:
                console.print("[yellow]沒有符合條件的市場[/yellow]")
                return

            # 批次撈 orderbook（fetch_orderbooks 已有 max_concurrency 限制）
            slugs = [m.slug for m, _ in with_days]
            with console.status(f"[cyan]撈 {len(slugs)} 個 orderbook..."):
                books = await lc.fetch_orderbooks(slugs)

            rows = []
            for m, days in with_days:
                ob = books.get(m.slug)
                if ob is None:
                    continue
                yb = ob.yes_best_bid
                ya = ob.yes_best_ask
                if not yb or not ya:
                    continue
                spread_pp = (ya.price - yb.price) * 100  # 百分點
                if spread_pp * 100 < min_spread_bps:     # 換算 bps
                    continue
                mid = (yb.price + ya.price) / 2
                if not (0.05 < mid < 0.95):
                    # 極端市場（已經接近 0 或 1）做市風險不對稱
                    continue

                # 評分
                days_factor = min(days, 30) / 7
                pa_bonus = 1.3 if m.is_poly_arbitrage else 1.0
                vol_factor = 1 + math.log(1 + m.volume_usd / 1000)
                news_risk = _news_risk_score(m.title)
                risk_penalty = 1 + news_risk

                score = spread_pp * days_factor * pa_bonus * vol_factor / risk_penalty
                rows.append({
                    "slug": m.slug,
                    "title": m.title,
                    "score": score,
                    "spread_pp": spread_pp,
                    "mid": mid,
                    "days": days,
                    "vol": m.volume_usd,
                    "pa": m.is_poly_arbitrage,
                    "news_risk": news_risk,
                })

            rows.sort(key=lambda r: -r["score"])

            if not rows:
                console.print("[yellow]沒有 orderbook 雙邊都有報價的市場[/yellow]")
                return

            t = Table(title=f"做市候選市場 (top {min(top, len(rows))} / 共 {len(rows)})",
                      show_lines=True)
            t.add_column("#", width=3)
            t.add_column("Score", justify="right")
            t.add_column("Spread", justify="right")
            t.add_column("Mid", justify="right")
            t.add_column("Days", justify="right")
            t.add_column("Vol", justify="right")
            t.add_column("PA")
            t.add_column("Risk")
            t.add_column("Slug", overflow="fold")
            t.add_column("標題", overflow="fold")
            for i, r in enumerate(rows[:top], 1):
                risk_label = "🛑" * int(r["news_risk"]) if r["news_risk"] > 0 else ""
                t.add_row(
                    str(i),
                    f"{r['score']:.2f}",
                    f"{r['spread_pp']:.2f}pp",
                    f"${r['mid']:.3f}",
                    f"{r['days']:.1f}d",
                    f"${r['vol']:,.0f}",
                    "🪞" if r["pa"] else "",
                    risk_label,
                    r["slug"][:30],
                    r["title"][:50],
                )
            console.print(t)
            console.print(Panel(
                "用 top 候選跑做市：\n"
                "  [bold]limitless make-market --slug <slug> --oracle pm[/bold]\n"
                "🪞 = 有 PM 鏡像（強烈建議搭 --oracle pm）；🛑 = 新聞風險關鍵字命中",
                border_style="dim",
            ))

    asyncio.run(_run())


# ---------- v0.7：24/7 自動調度 mm-loop ----------

@limitless_group.command(name="mm-loop")
@click.option("--total-capital", type=float, default=500.0,
              help="全部市場合計資本上限（USDC）")
@click.option("--max-positions", type=int, default=3,
              help="同時做幾個市場")
@click.option("--capital-per-market", type=float, default=100.0,
              help="單一市場最多多少資本")
@click.option("--quote-size", type=float, default=10.0)
@click.option("--target-profit-pct", type=float, default=4.0)
@click.option("--half-spread-pct", type=float, default=1.0)
@click.option("--max-inventory", type=float, default=30.0)
@click.option("--rank-refresh-s", type=int, default=3600,
              help="多久重新跑一次 mm-rank（預設 1 小時）")
@click.option("--rank-min-volume", type=float, default=200.0)
@click.option("--rank-min-days", type=float, default=2.0,
              help="距結算 < N 天就不挑（預設 2 天，給 emergency window 留空間）")
@click.option("--rank-min-spread-bps", type=int, default=100)
@click.option("--rank-max-news-risk", type=float, default=2.0)
@click.option("--iter-sleep-s", type=float, default=30.0)
@click.option("--oracle", type=click.Choice(["lm", "pm", "blend"]), default="pm")
@click.option("--microprice/--no-microprice", "use_microprice", default=True)
@click.option("--emergency-close-hours", type=float, default=24.0)
@click.option("--execute", is_flag=True,
              help="真實下單；不加就只是 dry-run")
@click.option("--from-env", is_flag=True,
              help="忽略以上 flag，全部從環境變數讀（給 AWS / 容器部署用）")
def lm_mm_loop_cmd(total_capital: float, max_positions: int, capital_per_market: float,
                   quote_size: float, target_profit_pct: float, half_spread_pct: float,
                   max_inventory: float, rank_refresh_s: int, rank_min_volume: float,
                   rank_min_days: float, rank_min_spread_bps: int,
                   rank_max_news_risk: float, iter_sleep_s: float,
                   oracle: str, use_microprice: bool,
                   emergency_close_hours: float, execute: bool,
                   from_env: bool) -> None:
    """v0.7：24/7 自動調度做市。

    自動跑 mm-rank、挑 top N 市場、各起一個 MarketMaker 並行做市、
    結算或觸發 emergency_close 後自動換下一個市場。

    [yellow]預設 dry-run[/yellow]。加 --execute 或設 LIMITLESS_EXECUTE=1 才真實下單。

    收到 SIGTERM / SIGINT → 全部 cancel + 收乾淨後退出（給容器 graceful shutdown 用）。
    """
    from .mm_loop import MMLoop, MMLoopConfig, install_signal_handlers
    from .trading import LimitlessTradingClient

    if execute:
        os.environ["LIMITLESS_EXECUTE"] = "1"

    if from_env:
        cfg = MMLoopConfig.from_env()
    else:
        cfg = MMLoopConfig(
            total_capital_usdc=total_capital,
            max_positions=max_positions,
            capital_per_market=capital_per_market,
            quote_size_shares=quote_size,
            target_profit_pct=target_profit_pct,
            half_spread_pct=half_spread_pct,
            max_inventory_shares=max_inventory,
            rank_refresh_seconds=rank_refresh_s,
            rank_min_volume_usd=rank_min_volume,
            rank_min_days=rank_min_days,
            rank_min_spread_bps=rank_min_spread_bps,
            rank_max_news_risk=rank_max_news_risk,
            iteration_sleep_s=iter_sleep_s,
            oracle_mode=oracle,
            use_microprice=use_microprice,
            emergency_close_hours=emergency_close_hours,
            execute=execute or os.environ.get("LIMITLESS_EXECUTE") == "1",
        )

    async def _run():
        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)

        async with LimitlessClient() as lc:
            loop = MMLoop(cfg, tc, lc, on_event=_event_printer)
            install_signal_handlers(loop)

            console.print(Panel(
                f"[bold]mm-loop v0.7 ({'真實' if cfg.execute else 'DRY-RUN'})[/bold]\n"
                f"全域資本：${cfg.total_capital_usdc:.0f}  並行市場：{cfg.max_positions}\n"
                f"每市場：${cfg.capital_per_market:.0f}  股數：{cfg.quote_size_shares:.0f}  ROI {cfg.target_profit_pct:.1f}%\n"
                f"oracle={cfg.oracle_mode}  microprice={cfg.use_microprice}  emergency<{cfg.emergency_close_hours:.0f}h\n"
                f"rank refresh: {cfg.rank_refresh_seconds}s  min vol ${cfg.rank_min_volume_usd:.0f}  min days {cfg.rank_min_days}\n"
                f"[dim]Ctrl-C / SIGTERM → graceful shutdown[/dim]",
                border_style="green" if cfg.execute else "yellow",
            ))

            try:
                stats = await loop.main_loop()
            finally:
                await tc.close()

        console.print(Panel(
            f"[bold]mm-loop 結束[/bold]\n"
            f"完成 sessions: {stats.sessions_completed}  "
            f"emergency: {stats.sessions_emergency}\n"
            f"總 iterations: {stats.total_iterations}",
            border_style="dim",
        ))

    asyncio.run(_run())


def _event_printer(kind: str, data: dict) -> None:
    """mm-loop 的事件 → 結構化文字。AWS 上靠 CloudWatch 抓 stdout。"""
    from datetime import datetime
    ts = datetime.utcnow().strftime("%H:%M:%S")
    if kind == "loop_start":
        console.print(f"[{ts}] [green]loop_start[/green] max={data['max_positions']} cap=${data['total_capital']:.0f} execute={data['execute']}")
    elif kind == "rank_picked":
        console.print(f"[{ts}] [cyan]rank_picked[/cyan] +{data['count']} {data['slugs']}")
    elif kind == "rank_empty":
        console.print(f"[{ts}] [yellow]rank_empty[/yellow] 沒有符合條件的市場")
    elif kind == "session_start":
        console.print(f"[{ts}] [green]session_start[/green] {data['slug'][:30]} cap=${data['capital']:.0f} :: {data['title'][:60]}")
    elif kind == "session_end":
        tag = "[red]emergency[/red]" if data.get("emergency") else "[dim]normal[/dim]"
        console.print(f"[{ts}] {tag} session_end {data['slug'][:30]} iters={data['iterations']} cap=${data['capital_used']:.2f}")
    elif kind == "session_error":
        console.print(f"[{ts}] [red]session_error[/red] {data['slug'][:30]} phase={data['phase']} {data['error']}")
    elif kind == "iteration":
        # 為了不洗版,只在 toxicity > 0 / emergency / 顯著事件時印
        if data.get("emergency"):
            console.print(f"[{ts}] [red]🚨 emergency[/red] {data['slug'][:30]} iter#{data['n']}")
        elif data.get("toxicity", 0) > 0:
            console.print(f"[{ts}] [yellow]tox[/yellow] {data['slug'][:30]} #{data['n']} tox={data['toxicity']:.2f}")
        # 其他正常 iteration 不印,留給 mm-rank 累計統計
    elif kind == "shutdown_start":
        console.print(f"[{ts}] [yellow]shutdown_start[/yellow] active={data['active']}")
    elif kind == "shutdown_done":
        console.print(f"[{ts}] [yellow]shutdown_done[/yellow] completed={data['completed']} emergency={data['emergency']}")
    elif kind == "skip_no_capital":
        console.print(f"[{ts}] [dim]skip_no_capital[/dim] {data['slug'][:30]} remaining=${data['remaining']:.2f}")


# ---------- 跨平台價差 ----------

@click.command(name="crossarb")
@click.option("--min-event-similarity", type=float, default=0.85,
              help="LM group 與 PM event 標題嚴格相似度門檻（預設 0.85）")
@click.option("--min-sub-match", type=float, default=0.95,
              help="子市場匹配分數門檻（預設 0.95，幾乎要求 group_item_title 精確匹配）")
@click.option("--min-diff-pct", type=float, default=1.0,
              help="最小 YES 價差百分點（預設 1.0pp = $0.01）")
@click.option("--min-pm-liquidity", type=float, default=2000.0,
              help="PM 市場最低流動性（USDC）；PM 自己不夠厚就不當 oracle（預設 $2000）")
@click.option("--poly-arb-flag-only", is_flag=True,
              help="只配對 LM 上 isPolyArbitrage=True 的市場（保守模式）；"
                   "預設關閉以擴大訊號池")
@click.option("--limitless-max", type=int, default=1000)
@click.option("--polymarket-max", type=int, default=300)
@click.option("--top", type=int, default=20)
def crossarb_cmd(min_event_similarity: float, min_sub_match: float,
                 min_diff_pct: float, min_pm_liquidity: float,
                 poly_arb_flag_only: bool,
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
                min_pm_liquidity=min_pm_liquidity,
                require_poly_arbitrage_flag=poly_arb_flag_only,
                limitless_max_markets=limitless_max,
                polymarket_max_events=polymarket_max,
            )
        console.rule(f"[bold]找到 {len(pairs)} 組跨平台價差[/bold]")
        if not pairs:
            console.print("沒找到符合條件的配對。試試降低 --min-pm-liquidity 或 --min-diff-pct")
            return

        t = Table(show_lines=True)
        t.add_column("來源")
        t.add_column("LM 標題 / PM 對應事件", overflow="fold")
        t.add_column("PM(YES)", justify="right")
        t.add_column("LM(YES)", justify="right")
        t.add_column("價差", justify="right")
        t.add_column("PM 流動性", justify="right")
        t.add_column("方向訊號", overflow="fold")
        for p in pairs[:top]:
            color = "green" if p.diff_pct < 0 else "red"
            pm_event = p.pm_event_title or p.polymarket.question
            label = f"[bold]{p.limitless.title[:45]}[/bold]\n[dim]PM: {pm_event[:55]}[/dim]"
            t.add_row(
                p.matched_via,
                label,
                f"${p.pm_yes_mid:.3f}",
                f"${p.lm_yes_mid:.3f}",
                Text(f"{p.diff_pct:+.2f}pp", style=color),
                f"${p.polymarket.liquidity:,.0f}",
                p.signal,
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
    """limitless — Limitless Exchange 做市工具(+ Polymarket 輔助 oracle)。

    主交易場:Limitless(Base 鏈,台灣可用)。
    輔助:Polymarket 作為公平價 oracle 和訊號源(台灣 close-only,無法新開倉)。

    常用命令:
        limitless mm-loop          # 24/7 自動做市(主力)
        limitless mm-rank          # 找適合做市的市場
        limitless make-market      # 單市場做市
        limitless place-order      # 手動單筆下單
        limitless scan / closest   # Limitless 套利掃描
        limitless pnl summary      # 看 PnL 紀錄
        limitless crossarb         # 跨平台價差訊號

        limitless pm scan          # Polymarket 掃描(觀察)
        limitless whales list      # Polymarket 鯨魚追蹤

    部署:cd infra && cdk deploy  (見 infra/README.md)
    """
    load_dotenv()


@click.command(name="crossarb-execute")
@click.option("--min-event-similarity", type=float, default=0.85)
@click.option("--min-pm-liquidity", type=float, default=2000.0,
              help="PM oracle 最低流動性（USDC）— 太薄的 PM 市場不採用")
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
def crossarb_execute_cmd(min_event_similarity: float, min_pm_liquidity: float,
                         min_diff_pct: float,
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
    from .trading import LimitlessTradingClient, OrderRequest

    async def _run():
        with console.status("[cyan]擷取跨平台價差..."):
            pairs = await find_cross_pairs(
                min_event_similarity=min_event_similarity,
                min_sub_match=0.95,
                min_diff_pct=min_diff_pct,
                min_pm_liquidity=min_pm_liquidity,
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
                "請先跑：[bold]limitless auth-derive[/bold] 取得 HMAC token，\n"
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


# ---------- 鯨魚跟單 ----------

@click.group(name="whales")
def whales_group() -> None:
    """Polymarket 鯨魚追蹤 + 跟單系統。"""


@whales_group.command(name="list")
@click.option("--trades-limit", type=int, default=3000,
              help="從近期多少 trades 中提取活躍錢包（預設 3000）")
@click.option("--min-trade", type=float, default=300.0,
              help="忽略 notional < 此值的小單（預設 $300）")
@click.option("--min-value", type=float, default=10000.0,
              help="錢包當下組合價值最低門檻（USDC）")
@click.option("--min-bought", type=float, default=20000.0,
              help="錢包累計交易量最低門檻（USDC，衡量資歷）")
@click.option("--top", type=int, default=20)
def whales_list_cmd(trades_limit: int, min_trade: float, min_value: float,
                    min_bought: float, top: int) -> None:
    """列出近期最有 alpha 的鯨魚錢包。

    Alpha = (已實現 ROI + 70% × 未實現 ROI) × log(累計交易量)
    """
    from .polymarket.whales import top_whales

    async def _run():
        with console.status("[cyan]掃描 Polymarket 活躍錢包..."):
            whales = await top_whales(
                trades_limit=trades_limit,
                min_trade_notional=min_trade,
                min_portfolio_value=min_value,
                min_total_bought=min_bought,
                top_n=top,
            )
        if not whales:
            console.print("[yellow]沒找到符合條件的鯨魚。試試降低 --min-value 或擴大 --trades-limit[/yellow]")
            return

        t = Table(title=f"Top {len(whales)} Polymarket whales", show_lines=True)
        t.add_column("#", width=3)
        t.add_column("Wallet")
        t.add_column("Alpha", justify="right")
        t.add_column("已實現 ROI", justify="right")
        t.add_column("總 ROI", justify="right")
        t.add_column("組合價值", justify="right")
        t.add_column("Realized $", justify="right")
        t.add_column("Bought $", justify="right")
        t.add_column("倉位數", justify="right")
        for i, w in enumerate(whales, 1):
            roi_style = "green" if w.realized_roi_pct > 0 else "yellow" if w.realized_roi_pct > -2 else "red"
            t.add_row(
                str(i),
                w.proxy_wallet[:10] + "...",
                f"{w.alpha_score:+.1f}",
                Text(f"{w.realized_roi_pct:+.2f}%", style=roi_style),
                f"{w.total_roi_pct:+.2f}%",
                f"${w.portfolio_value:,.0f}",
                f"${w.total_realized_pnl:+,.0f}",
                f"${w.total_bought:,.0f}",
                str(w.n_positions),
            )
        console.print(t)
        console.print(Panel(
            "把要追蹤的 wallet 寫進 .env（逗號分隔）：\n"
            "[bold]WHALE_WALLETS=0xc97b...,0x204f...[/bold]\n"
            "然後跑 [bold]limitless whales watch[/bold] 即時監控他們的新動作",
            border_style="dim",
        ))

    asyncio.run(_run())


@whales_group.command(name="watch")
@click.option("--wallets", default=None,
              help="逗號分隔的 wallet list；預設讀 WHALE_WALLETS 環境變數")
@click.option("--lookback-min", type=int, default=60,
              help="只看最近 N 分鐘內的鯨魚動作")
@click.option("--min-trade", type=float, default=500.0,
              help="跟單訊號最低 notional")
@click.option("--trades-limit", type=int, default=3000)
def whales_watch_cmd(wallets: str | None, lookback_min: int,
                     min_trade: float, trades_limit: int) -> None:
    """監控指定鯨魚的最新動作、產生跟單訊號。"""
    from .polymarket.whales import find_whale_signals, top_whales, PolymarketDataClient, score_wallet
    import time

    wallet_str = wallets or os.environ.get("WHALE_WALLETS", "").strip()
    if not wallet_str:
        console.print("[red]沒指定要追的 wallet。用 --wallets 或設 WHALE_WALLETS 環境變數[/red]")
        return
    wallet_list = [w.strip().lower() for w in wallet_str.split(",") if w.strip()]

    async def _run():
        # 對給定 wallets 算 score（用來顯示）
        with console.status("[cyan]載入鯨魚 metadata..."):
            async with PolymarketDataClient() as c:
                scores = await asyncio.gather(*(score_wallet(c, w) for w in wallet_list))
        scores = [s for s in scores if s is not None]
        if not scores:
            console.print("[red]無法取得任何 wallet 的資料[/red]")
            return

        since = int(time.time()) - lookback_min * 60
        with console.status(f"[cyan]掃近 {lookback_min} 分鐘的鯨魚動作..."):
            signals = await find_whale_signals(
                scores, since_timestamp=since,
                trades_limit=trades_limit,
                min_trade_notional=min_trade,
            )

        if not signals:
            console.print(f"[yellow]近 {lookback_min} 分鐘內沒有 ≥ ${min_trade:.0f} 的鯨魚動作[/yellow]")
            return

        t = Table(title=f"鯨魚動作 (近 {lookback_min} 分鐘)", show_lines=True)
        t.add_column("時間")
        t.add_column("鯨魚")
        t.add_column("Alpha")
        t.add_column("方向")
        t.add_column("市場", overflow="fold")
        t.add_column("價格", justify="right")
        t.add_column("Notional", justify="right")
        for s in sorted(signals, key=lambda x: -x.trade.timestamp):
            from datetime import datetime, timezone
            ts = datetime.fromtimestamp(s.trade.timestamp, tz=timezone.utc).strftime("%H:%M")
            t.add_row(
                ts,
                s.whale[:8] + "...",
                f"{s.whale_alpha_score:+.0f}",
                f"{s.trade.side} {s.trade.outcome}",
                s.trade.title[:55],
                f"${s.trade.price:.3f}",
                f"${s.trade.notional:,.0f}",
            )
        console.print(t)

    asyncio.run(_run())


@whales_group.command(name="follow")
@click.option("--wallets", default=None, help="逗號分隔的 wallet list；預設讀 WHALE_WALLETS 環境變數")
@click.option("--lookback-min", type=int, default=60)
@click.option("--min-trade", type=float, default=500.0)
@click.option("--max-positions", type=int, default=3, help="本次最多開幾個跟單部位")
@click.option("--notional-per-trade", type=float, default=10.0)
@click.option("--max-price", type=float, default=0.85,
              help="鯨魚以這個價以上下注 → 跳過（已 priced in 太多）")
@click.option("--execute", is_flag=True, help="真實下單；不加就只是 dry-run")
def whales_follow_cmd(wallets: str | None, lookback_min: int, min_trade: float,
                      max_positions: int, notional_per_trade: float,
                      max_price: float, execute: bool) -> None:
    """跟單：把鯨魚最新動作鏡像到 Limitless（若有對應市場）。

    流程：
      1. 跑 whales watch 拿訊號
      2. 對每個訊號嘗試找 LM 對應市場（嚴格 token 比對）
      3. 對找到對應的訊號在 LM 下單（FAK 立即吃 best ask 或 GTC 排隊）
      4. 預設 dry-run；加 --execute 才真實送出
    """
    from .polymarket.whales import find_whale_signals, attach_limitless_markets, PolymarketDataClient, score_wallet
    from .trading import LimitlessTradingClient, OrderRequest
    import time

    wallet_str = wallets or os.environ.get("WHALE_WALLETS", "").strip()
    if not wallet_str:
        console.print("[red]沒指定要追的 wallet。用 --wallets 或設 WHALE_WALLETS[/red]")
        return
    wallet_list = [w.strip().lower() for w in wallet_str.split(",") if w.strip()]

    async def _run():
        # 算 whale scores
        with console.status("[cyan]載入鯨魚 metadata..."):
            async with PolymarketDataClient() as c:
                scores = await asyncio.gather(*(score_wallet(c, w) for w in wallet_list))
        scores = [s for s in scores if s is not None]

        since = int(time.time()) - lookback_min * 60
        with console.status(f"[cyan]掃近 {lookback_min} 分鐘鯨魚動作..."):
            signals = await find_whale_signals(
                scores, since_timestamp=since,
                trades_limit=3000,
                min_trade_notional=min_trade,
            )

        if not signals:
            console.print("[yellow]無新動作[/yellow]")
            return

        with console.status("[cyan]比對 Limitless 市場..."):
            signals = await attach_limitless_markets(signals)

        actionable = [s for s in signals if s.is_actionable
                      and s.trade.price <= max_price]
        non_actionable = [s for s in signals if not s.is_actionable]

        # 顯示所有訊號
        t = Table(title=f"鯨魚跟單訊號 ({'真實' if execute else 'DRY-RUN'})", show_lines=True)
        t.add_column("鯨魚")
        t.add_column("方向")
        t.add_column("PM 市場", overflow="fold")
        t.add_column("Notional", justify="right")
        t.add_column("LM 對應")
        for s in actionable + non_actionable[:5]:
            t.add_row(
                s.whale[:8] + "...",
                f"{s.trade.side} {s.trade.outcome}",
                s.trade.title[:55],
                f"${s.trade.notional:,.0f}",
                Text(s.lm_slug[:30] if s.lm_slug else "[red]無對應[/red]", style="green" if s.is_actionable else "red"),
            )
        console.print(t)
        console.print(f"可下單訊號：{len(actionable)} / 總訊號：{len(signals)}")

        if not actionable:
            console.print("[yellow]沒有 LM 有對應市場的訊號，本次不下單[/yellow]")
            return

        # 取前 N 個下單
        if execute:
            os.environ["LIMITLESS_EXECUTE"] = "1"
        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError as e:
            console.print(f"[red]無認證 — 無法下單[/red]")
            console.print(f"  {e}")
            return

        results_t = Table(title="下單結果", show_lines=True)
        results_t.add_column("方向")
        results_t.add_column("市場", overflow="fold")
        results_t.add_column("價格")
        results_t.add_column("Notional")
        results_t.add_column("結果")

        for s in actionable[:max_positions]:
            side_buy, outcome_label = (s.suggested_lm_side or "BUY YES").split()
            token_id = s.lm_yes_token if outcome_label == "YES" else s.lm_no_token
            # 用鯨魚的成交價當參考；加 1pp safety margin
            target_price = min(0.99, max(0.01, round(s.trade.price + 0.01, 3)))
            size = max(1.0, notional_per_trade / target_price)

            req = OrderRequest(
                market_slug=s.lm_slug,
                token_id=token_id,
                side="BUY",
                price=target_price,
                size_shares=round(size, 2),
                order_type="FAK",  # 立即吃，鯨魚動作後愈早跟愈好
            )
            res = await tc.place_order(req)
            results_t.add_row(
                f"BUY {outcome_label}",
                s.trade.title[:50],
                f"${req.price:.3f}",
                f"${req.notional:.2f}",
                "[yellow]DRY[/yellow]" if res.dry_run else ("[green]OK[/green]" if res.accepted else f"[red]{res.error}[/red]"),
            )

        await tc.close()
        console.print(results_t)

    asyncio.run(_run())


# ---------- PnL 追蹤 (v0.9) ----------

@click.group(name="pnl")
def pnl_group() -> None:
    """PnL 追蹤 + 報表(SQLite 本機儲存)。"""


@pnl_group.command(name="init")
def pnl_init_cmd() -> None:
    """初始化 PnL 資料庫(自動建立 ~/.limitless/pnl.db)。"""
    from . import pnl
    pnl.init_db()
    console.print(f"[green]✓[/green] PnL DB 初始化:{pnl.DB_PATH}")


@pnl_group.command(name="reset")
@click.confirmation_option(prompt="確定要砍掉所有 PnL 紀錄?")
def pnl_reset_cmd() -> None:
    """砍掉所有 PnL 紀錄(慎用,不可復原)。"""
    from . import pnl
    pnl.reset_db()
    console.print(f"[yellow]⚠[/yellow] PnL DB 已重置:{pnl.DB_PATH}")


@pnl_group.command(name="snapshot")
def pnl_snapshot_cmd() -> None:
    """立即抓一筆 wallet snapshot(USDC + ETH + CTF estimate + open orders)。

    建議放進 cron 每天跑一次,或讓 mm-loop 自動做。
    """
    from . import pnl
    from .trading import LimitlessTradingClient
    from eth_account import Account

    priv = os.environ.get("BASE_PRIVATE_KEY")
    if not priv:
        console.print("[red]缺 BASE_PRIVATE_KEY,無法推算 wallet 地址[/red]")
        return
    addr = Account.from_key(priv).address

    async def _run():
        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError:
            tc = None
            console.print("[yellow]無 LM 認證,只抓鏈上 USDC/ETH(不抓 portfolio)[/yellow]")
        snap = await pnl.snapshot_wallet(addr, tc)
        if tc:
            await tc.close()
        console.print(Panel(
            f"[bold]Wallet Snapshot[/bold]\n"
            f"地址:        {addr}\n"
            f"USDC:        ${snap['usdc']:.2f}\n"
            f"ETH:         {snap['eth']:.6f}\n"
            f"CTF 估值:    ${snap['ctf_value']:.2f}\n"
            f"鎖在開單:    ${snap['open_orders_locked']:.2f}\n"
            f"Active 市場: {snap['active_markets']}\n"
            f"[bold]總 Equity:    ${snap['total_equity']:.2f}[/bold]",
            border_style="green",
        ))

    asyncio.run(_run())


@pnl_group.command(name="settlements")
def pnl_settlements_cmd() -> None:
    """偵測已結算市場(過去有部位但現在 portfolio 沒了),寫入 PnL DB。"""
    from . import pnl
    from .trading import LimitlessTradingClient

    async def _run():
        try:
            tc = LimitlessTradingClient.from_env()
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            return
        with console.status("[cyan]偵測結算..."):
            settled = await pnl.detect_settlements(tc)
        await tc.close()
        if not settled:
            console.print("[yellow]沒偵測到新結算[/yellow]")
            return
        t = Table(title=f"偵測到 {len(settled)} 個結算市場", show_lines=True)
        t.add_column("Slug", overflow="fold")
        t.add_column("YES", justify="right")
        t.add_column("NO", justify="right")
        t.add_column("Cost", justify="right")
        t.add_column("Payout(估)", justify="right")
        t.add_column("PnL(估)", justify="right")
        for s in settled:
            pnl_v = s["pnl_estimated"]
            t.add_row(
                s["slug"][:40],
                f"{s['yes_shares']:.1f}",
                f"{s['no_shares']:.1f}",
                f"${s['cost']:.2f}",
                f"${s['payout_estimated']:.2f}",
                Text(f"${pnl_v:+.2f}", style="green" if pnl_v > 0 else "red"),
            )
        console.print(t)
        console.print("[dim]註:payout 是估計值。實際結果要從 LM history API 或網頁查證。[/dim]")

    asyncio.run(_run())


@pnl_group.command(name="summary")
@click.option("--days", type=int, default=30, help="統計區間(天)")
def pnl_summary_cmd(days: int) -> None:
    """整體 PnL 摘要。"""
    from . import pnl
    s = pnl.summary_stats(days=days)

    fill_rate = (s.orders_accepted / s.orders_total * 100) if s.orders_total else 0
    body_lines = [
        f"[bold]期間:過去 {s.days} 天[/bold]",
        f"",
        f"📊 [cyan]活動量[/cyan]",
        f"  Iterations:    {s.iterations:,}",
        f"  Orders:        {s.orders_total:,} (accepted {s.orders_accepted}, rejected {s.orders_rejected})",
        f"  Order 接受率:  {fill_rate:.1f}%",
        f"",
        f"🎯 [cyan]做市表現[/cyan]",
        f"  Fills:               {s.fills_count:,}",
        f"  Fills 總 notional:   ${s.fills_total_notional:,.2f}",
        f"  **配對率(估)**:     {s.pair_completion_rate_pct:.1f}%",
        f"  平均 toxicity:       {s.avg_toxicity:.2f}",
        f"",
        f"💰 [cyan]損益[/cyan]",
        f"  結算次數:           {s.settlements_count}",
        f"  已實現 PnL(估):     ${s.realized_pnl:+.2f}",
    ]
    if s.first_wallet_equity is not None and s.last_wallet_equity is not None:
        sign = "green" if (s.period_pnl or 0) >= 0 else "red"
        body_lines.extend([
            f"",
            f"📈 [cyan]Equity 變動[/cyan]",
            f"  期初:  ${s.first_wallet_equity:.2f}",
            f"  期末:  ${s.last_wallet_equity:.2f}",
            f"  [bold]變動:  [{sign}]${s.period_pnl:+.2f} ({s.period_pnl_pct:+.2f}%)[/{sign}][/bold]",
        ])
    else:
        body_lines.extend([
            f"",
            f"[dim](沒有 wallet snapshot 紀錄;跑 `pnl snapshot` 開始追蹤)[/dim]",
        ])

    console.print(Panel("\n".join(body_lines), title="PnL Summary", border_style="cyan"))


@pnl_group.command(name="daily")
@click.option("--days", type=int, default=14, help="顯示幾天")
def pnl_daily_cmd(days: int) -> None:
    """每日 PnL 分解。"""
    from . import pnl
    rows = pnl.daily_breakdown(days=days)
    if not rows:
        console.print("[yellow]沒有資料(可能還沒開始跑 bot,或還沒有 fills)[/yellow]")
        return

    t = Table(title=f"每日明細(過去 {days} 天)", show_lines=True)
    t.add_column("日期")
    t.add_column("市場數", justify="right")
    t.add_column("Fills", justify="right")
    t.add_column("Fill notional", justify="right")
    t.add_column("結算", justify="right")
    t.add_column("Realized PnL", justify="right")
    t.add_column("Equity", justify="right")
    for r in rows:
        pnl_v = r["realized_pnl"]
        pnl_style = "green" if pnl_v > 0 else ("red" if pnl_v < 0 else "dim")
        t.add_row(
            r["date"],
            str(r["markets"]),
            str(r["fills"]),
            f"${r['fills_notional']:.2f}",
            str(r["settlements"]),
            Text(f"${pnl_v:+.2f}", style=pnl_style),
            f"${r['equity']:.2f}" if r["equity"] is not None else "-",
        )
    console.print(t)


@pnl_group.command(name="markets")
@click.option("--days", type=int, default=30)
def pnl_markets_cmd(days: int) -> None:
    """按市場分解 PnL。"""
    from . import pnl
    rows = pnl.per_market_breakdown(days=days)
    if not rows:
        console.print("[yellow]沒資料[/yellow]")
        return

    t = Table(title=f"按市場分解(過去 {days} 天)", show_lines=True)
    t.add_column("Slug", overflow="fold")
    t.add_column("Iters", justify="right")
    t.add_column("YES fills", justify="right")
    t.add_column("NO fills", justify="right")
    t.add_column("Peak YES", justify="right")
    t.add_column("Peak NO", justify="right")
    t.add_column("Peak cap", justify="right")
    t.add_column("Realized PnL", justify="right")
    for r in rows:
        pnl_v = r["realized_pnl"]
        pnl_style = "green" if pnl_v > 0 else ("red" if pnl_v < 0 else "dim")
        t.add_row(
            r["slug"][:35],
            str(r["iterations"]),
            str(r["yes_fills"]),
            str(r["no_fills"]),
            f"{r['peak_yes_inv']:.1f}",
            f"{r['peak_no_inv']:.1f}",
            f"${r['peak_capital']:.2f}",
            Text(f"${pnl_v:+.2f}", style=pnl_style),
        )
    console.print(t)


cli.add_command(polymarket_group)        # `limitless pm ...`
cli.add_command(limitless_group)         # `limitless ...`(完整命名空間)
cli.add_command(whales_group)            # `limitless whales ...`
cli.add_command(crossarb_cmd)            # `limitless crossarb`
cli.add_command(crossarb_execute_cmd)    # `limitless crossarb-execute`
cli.add_command(pnl_group)               # `limitless pnl ...`


# ---------- Limitless 命令的 top-level 捷徑(`limitless mm-loop` ≡ `limitless mm-loop`)----------

for _name in ("scan", "closest", "auth-derive", "place-order",
              "make-market", "reward-farm", "mm-rank", "mm-loop"):
    _cmd = limitless_group.commands.get(_name)
    if _cmd is not None:
        cli.add_command(_cmd, name=_name)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
