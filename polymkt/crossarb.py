"""跨平台價格比對：Polymarket（讀） ↔ Limitless（讀+可下單）。

策略邏輯：
- Polymarket 流動性高、價格發現快 → 把 Polymarket 價當作「fair value 訊號」
- Limitless 有 isPolyArbitrage 標記市場（鏡像 Polymarket）
- 若 Limitless YES 比 Polymarket YES 便宜很多 → 在 Limitless 買 YES，等價格收斂
- 反之亦然

⚠️ 這**不是純套利**（純套利要兩邊都能下單，但台灣 Polymarket 鎖死）。
這是「統計訊號」交易：假設 Polymarket 較有效率，Limitless 偏離後會收斂。

**匹配層級**：
1. 先對齊 event/group（題目長、語意豐富，相似度比對較可靠）
2. 對齊後再對齊子市場（用 normalized 標題或完全字串匹配）
3. 單一 single market 才退化到「市場 vs 市場」標題比對
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .clients import GammaClient
from .limitless.client import LimitlessClient
from .limitless.models import LimitlessMarket, LimitlessGroup
from .models import Event as PMEvent, Market as PMMarket


@dataclass
class MatchPair:
    polymarket: PMMarket
    limitless: LimitlessMarket
    title_similarity: float
    # 價差：以 YES 為基準（NO 由 1-YES 對稱）
    pm_yes_mid: float  # Polymarket midpoint(YES)
    lm_yes_mid: float  # Limitless midpoint(YES)
    diff_pct: float    # (lm - pm) * 100；正值表示 Limitless YES 比較貴

    @property
    def signal(self) -> str:
        """給人類看的方向建議。"""
        if self.diff_pct > 0:
            return f"Limitless YES 貴 {self.diff_pct:+.2f}pp → 在 Limitless 賣 YES / 買 NO"
        else:
            return f"Limitless YES 便宜 {self.diff_pct:+.2f}pp → 在 Limitless 買 YES"


# 對齊比對時要忽略的高頻常見字（避免 "2026 FIFA World Cup" 共有字撐高相似度）
# 注意：不要把單字母（a/b/c...）加進來 — "Group A"、"Group B" 是關鍵語意。
_STOP_TOKENS = {"the", "an", "in", "of", "on", "to", "for", "will",
                "be", "is", "are", "and", "or", "by", "before", "after",
                "2024", "2025", "2026", "2027", "2028"}


def _normalize(s: str) -> str:
    """標題正規化：小寫、去標點、去 Unicode 變體、合併空格。"""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _content_tokens(s: str) -> set[str]:
    """去除 stopword 後剩下的具區辨力的 token。

    **重要**：不要因為 token 短就忽略 — "B"、"H"、"J" 等在「Group B」「Q4」這種
    結構中是關鍵的區辨字。
    """
    return {t for t in _normalize(s).split() if t and t not in _STOP_TOKENS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _event_similarity(a: str, b: str) -> float:
    """事件層級對齊：要求兩個事件標題的「內容詞」幾乎完全相等。

    範例：
    - "FIFA World Cup Group B Winner" vs "2026 FIFA World Cup Winner"
       對稱差 = {group, b} → 拒絕（不同題目層級）
    - "2026 NBA Champion" vs "2026 NBA Champion" → 接受
    - "Will Trump win 2028 Presidential?" vs "Trump wins 2028 Presidential" → 接受

    回傳值：
    - 1.0 = 對稱差集為空（content tokens 完全相等）
    - 0.85 = 對稱差正好 1 個 token（容許小幅標題變化，如多一個修飾詞）
    - 0.0 否則（不視為匹配）
    """
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    diff = (ta ^ tb)
    if len(diff) == 1 and min(len(ta), len(tb)) >= 3:
        return 0.85
    return 0.0


def _polymarket_yes_mid(m: PMMarket) -> float | None:
    """從 Polymarket Market.outcome_prices 取 YES 中價。"""
    if not m.outcome_prices or len(m.outcome_prices) < 1:
        return None
    yes_price = m.outcome_prices[0]
    if yes_price <= 0 or yes_price >= 1:
        return None
    return yes_price


def _limitless_yes_mid(m: LimitlessMarket) -> float | None:
    """從 Limitless Market.prices[0] 或 lastTradePrice 取 YES 中價。"""
    if m.prices and 0 < m.prices[0] < 1:
        return m.prices[0]
    if m.last_trade_price and 0 < m.last_trade_price < 1:
        return m.last_trade_price
    return None


def _match_in_event(lm_sub_title: str, pm_event: PMEvent) -> tuple[PMMarket | None, float]:
    """在 PM event 內找與 LM 子市場標題對應的 PM 市場。

    PM 子市場通常標題像 "Will Spain win the 2026 FIFA World Cup?"，而 LM 子市場
    通常是 "Spain"。所以策略：找 PM 子市場 question 含 LM 子市場 title 為子字串
    （正規化後），或用 PM 的 `group_item_title` 直接 match（PM 在多選一事件下
    通常會把選項名放在這個欄位）。
    """
    lm_norm = _normalize(lm_sub_title)
    if not lm_norm:
        return None, 0.0

    best: tuple[PMMarket | None, float] = (None, 0.0)
    for pm in pm_event.markets:
        # 優先：PM 的 group_item_title 精確匹配（最可靠）
        if pm.group_item_title:
            git_norm = _normalize(pm.group_item_title)
            if git_norm == lm_norm:
                return pm, 1.0
            if git_norm and (git_norm in lm_norm or lm_norm in git_norm):
                if best[1] < 0.95:
                    best = (pm, 0.95)
                continue

        # 次：把 LM 標題視為子字串看是否出現在 PM question 內
        pm_norm = _normalize(pm.question)
        if lm_norm in pm_norm:
            score = len(lm_norm) / max(len(pm_norm), 1)
            if score > best[1]:
                best = (pm, max(score, 0.7))
            continue

        # 最後退化：SequenceMatcher，但門檻較高
        sim = SequenceMatcher(None, lm_norm, pm_norm).ratio()
        if sim > best[1]:
            best = (pm, sim)

    return best


async def find_cross_pairs(
    *,
    min_event_similarity: float = 0.6,
    min_sub_match: float = 0.7,
    min_diff_pct: float = 1.0,
    limitless_max_markets: int = 1000,
    polymarket_max_events: int = 300,
) -> list[MatchPair]:
    """找出 Limitless 與 Polymarket 對應市場 + 報出價差。

    匹配演算法：
    1. 對每個 LM group（isPolyArbitrage），在 PM events 找標題最相似的 event
       （相似度 >= `min_event_similarity`）。
    2. 在配對的 event 內，對每個 LM 子市場找對應的 PM 子市場
       （優先用 PM `group_item_title` 精確 match，相似度 >= `min_sub_match`）。
    3. 純 single LM 市場用整題標題對 PM 市場直接比對。
    """

    async with LimitlessClient() as lm_c, GammaClient() as pm_c:
        lm_singles, lm_groups = await lm_c.fetch_active_markets(max_markets=limitless_max_markets)
        pm_events = await pm_c.fetch_active_events(max_events=polymarket_max_events)

    pairs: list[MatchPair] = []

    # 1) LM group → PM event 對齊（用 token jaccard + SequenceMatcher 的較嚴值）
    for lm_g in lm_groups:
        if not lm_g.is_poly_arbitrage:
            continue
        if not lm_g.markets:
            continue

        best_ev_sim, best_ev = 0.0, None
        for pm_ev in pm_events:
            sim = _event_similarity(lm_g.title, pm_ev.title)
            if sim > best_ev_sim:
                best_ev_sim, best_ev = sim, pm_ev
        if best_ev is None or best_ev_sim < min_event_similarity:
            continue

        # 在配對的 event 內對齊子市場
        for lm_sub in lm_g.markets:
            if not lm_sub.is_tradeable:
                continue
            lm_yes = _limitless_yes_mid(lm_sub)
            if lm_yes is None:
                continue
            pm_sub, sub_score = _match_in_event(lm_sub.title, best_ev)
            if pm_sub is None or sub_score < min_sub_match:
                continue
            pm_yes = _polymarket_yes_mid(pm_sub)
            if pm_yes is None:
                continue
            diff_pct = (lm_yes - pm_yes) * 100
            if abs(diff_pct) < min_diff_pct:
                continue
            pairs.append(MatchPair(
                polymarket=pm_sub,
                limitless=lm_sub,
                title_similarity=best_ev_sim * sub_score,
                pm_yes_mid=pm_yes,
                lm_yes_mid=lm_yes,
                diff_pct=diff_pct,
            ))

    # 2) LM single isPolyArbitrage 市場直接整題對 PM 市場
    pm_market_norm = [(pm, _normalize(pm.question))
                      for ev in pm_events for pm in ev.markets if pm.is_tradeable]
    for lm in lm_singles:
        if not (lm.is_poly_arbitrage and lm.is_tradeable):
            continue
        lm_yes = _limitless_yes_mid(lm)
        if lm_yes is None:
            continue
        lm_norm = _normalize(lm.title)
        best_sim, best_pm = 0.0, None
        for pm, pm_norm in pm_market_norm:
            sim = SequenceMatcher(None, lm_norm, pm_norm).ratio()
            if sim > best_sim:
                best_sim, best_pm = sim, pm
        if best_pm is None or best_sim < 0.75:  # single market 要求更高相似度
            continue
        pm_yes = _polymarket_yes_mid(best_pm)
        if pm_yes is None:
            continue
        diff_pct = (lm_yes - pm_yes) * 100
        if abs(diff_pct) < min_diff_pct:
            continue
        pairs.append(MatchPair(
            polymarket=best_pm,
            limitless=lm,
            title_similarity=best_sim,
            pm_yes_mid=pm_yes,
            lm_yes_mid=lm_yes,
            diff_pct=diff_pct,
        ))

    pairs.sort(key=lambda p: abs(p.diff_pct), reverse=True)
    return pairs
