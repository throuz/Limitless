"""Limitless 做市 bot「報價延遲」偵測器。

假說:Limitless 成交量極低(72% 市場零成交),沒人盯的足球 prop 做市 bot 可能
「反應慢」——比分已經決定結果(例:已進 3 球 → O3 必為 YES),但 bot 還掛著舊價。
若是,就能用接近確定的價狙擊它的 stale 報價。

用法:
  python lag_monitor.py            # 跑一輪
  python lag_monitor.py --loop     # 每 LOOP_SEC 秒採樣一次,發現機會記到 lag_hits.log

資料源:
  ESPN 免費隱藏 API(fifa.friendly scoreboard,含即時比分/狀態)當 ground truth。
  Limitless client 抓 bot 的 O3 / BTTS 報價。
偵測:比分已定 truth(O3: 總進球>=3→1.0, 完賽且<3→0.0;BTTS 類推),
      若 LM 報價還偏離 truth 夠多 → 印出可狙擊的價差。
"""
from __future__ import annotations
import asyncio, re, sys, time, datetime
import httpx
from limitless.client import LimitlessClient

LOOP_SEC = 180
LOG = "lag_hits.log"

def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\b(rep\.? of|republic of)\b", "", s)
    for a, b in {"türkiye": "turkey", "united states": "usa", "congo dr": "congo"}.items():
        s = s.replace(a, b)
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _last(s: str) -> str:
    p = _norm(s).split()
    return p[-1] if p else ""

def _key(a: str, b: str) -> frozenset:
    return frozenset([_last(a), _last(b)])

def espn_live() -> dict:
    """回傳 {teams_key: {h,a,hg,ag,state,done,clock,total,btts}}。state: pre/in/post。"""
    base = "https://site.api.espn.com/apis/site/v2/sports/soccer"
    out: dict = {}
    today = datetime.date.today()
    dates = [(today + datetime.timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1, 2)]
    with httpx.Client(timeout=20, headers={"User-Agent": "lag/1.0"}) as c:
        for lg in ("fifa.friendly", "fifa.friendly.w"):
            for d in dates:
                try:
                    r = c.get(f"{base}/{lg}/scoreboard", params={"dates": d})
                    if r.status_code != 200:
                        continue
                    for e in r.json().get("events", []):
                        comp = e.get("competitions", [{}])[0]
                        st = comp.get("status", {}).get("type", {})
                        cs = comp.get("competitors", [])
                        if len(cs) < 2:
                            continue
                        sc = {cp.get("homeAway"): (cp.get("team", {}).get("displayName", ""),
                                                   int(cp.get("score") or 0)) for cp in cs}
                        if "home" not in sc or "away" not in sc:
                            continue
                        hn, hg = sc["home"]; an, ag = sc["away"]
                        out[_key(hn, an)] = dict(h=hn, a=an, hg=hg, ag=ag,
                                                 state=st.get("state"), done=st.get("completed"),
                                                 clock=comp.get("status", {}).get("displayClock"),
                                                 total=hg + ag, btts=(hg > 0 and ag > 0))
                except Exception:
                    pass
    return out

async def one_pass() -> list[str]:
    live = espn_live()
    relevant = {k: v for k, v in live.items() if v["state"] in ("in", "post")}
    async with LimitlessClient() as c:
        singles, _ = await c.fetch_active_markets(max_markets=1000)
        foot = [m for m in singles if m.is_tradeable and
                ("total goals" in m.title.lower() or "both to score" in m.title.lower())]
        obs = await c.fetch_orderbooks([m.slug for m in foot])
    hits: list[str] = []
    for m in foot:
        mm = re.match(r"(.+?) and (.+?) (both to score|have \d+ or more total goals)", m.title)
        if not mm:
            continue
        k = _key(mm.group(1), mm.group(2))
        if k not in relevant:
            continue
        v = relevant[k]
        ob = obs.get(m.slug)
        if not ob or not ob.yes_best_bid or not ob.yes_best_ask:
            continue
        bid, ask = ob.yes_best_bid.price, ob.yes_best_ask.price
        if ask >= 1.0 and bid <= 0:
            continue
        kind = "BTTS" if "both" in mm.group(3) else "O3"
        if kind == "O3":
            truth = 1.0 if v["total"] >= 3 else (0.0 if v["done"] else None)
        else:
            truth = 1.0 if v["btts"] else (0.0 if v["done"] else None)
        if truth is None:
            continue
        edge = None
        if truth == 1.0 and ask < 0.97:
            edge = f"買YES@{ask:.2f}→1.00 賺{(1-ask)*100:.0f}¢ (depth {ob.yes_best_ask.shares:.0f})"
        elif truth == 0.0 and bid > 0.03:
            edge = f"賣YES@{bid:.2f}→0 賺{bid*100:.0f}¢ (depth {ob.yes_best_bid.shares:.0f})"
        if edge:
            ts = datetime.datetime.now().strftime("%m-%d %H:%M")
            hits.append(f"[{ts}] {v['h']} {v['hg']}-{v['ag']} {v['a']} "
                        f"[{v['state']} {v.get('clock')}] {kind} LM {bid:.2f}/{ask:.2f} truth={truth} ★{edge}")
    return hits

async def main():
    loop = "--loop" in sys.argv
    while True:
        try:
            hits = await one_pass()
        except Exception as e:
            print(f"pass error: {e}")
            hits = []
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if hits:
            print(f"[{ts}] {len(hits)} 個 stale-quote 機會:")
            for h in hits:
                print("  " + h)
            with open(LOG, "a") as f:
                for h in hits:
                    f.write(h + "\n")
        else:
            print(f"[{ts}] 無 stale 機會 (沒有 in-play/剛完賽且 bot 報價偏離的場)")
        if not loop:
            break
        await asyncio.sleep(LOOP_SEC)

if __name__ == "__main__":
    asyncio.run(main())
