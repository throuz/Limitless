# polymkt — 跨平台預測市場分析工具（台灣可用版）

> ⚠️ **免責聲明**：這套工具**不保證獲利**。預測市場有真實滑價、流動性、結算爭議等風險。
> 工具只是降低「發現機會」的難度。

## 為什麼是「跨平台」工具

**台灣使用者在 Polymarket 為 close-only**（無法開新倉，[官方 geoblock 文件](https://docs.polymarket.com/api-reference/geoblock.md)）。所以本工具的設計：

- **Polymarket**：讀取分析資料源（市場效率、價格訊號）
- **Limitless Exchange**（Base 鏈，台灣可用）：實際下單目標
- **Cross-arb 模組**：以 Polymarket 為 oracle，找 Limitless 上偏離的機會

## 安裝

```bash
cd /Users/mac/Projects/polymarket
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e '.[trade]'   # 含 Limitless SDK，可下單
# 只要分析、不下單可改用：.venv/bin/pip install -e .
```

## 啟用自動下單（一次性設定）

> ⚠️ 進入這節之前先讀完「[風險提醒](#風險提醒)」與「[安全機制](#安全機制)」。

### 1. 準備獨立 wallet 與 Base 上的 USDC

- **不要**用主錢包。建議：MetaMask 或 Rabby 開新 wallet，只放本次下單需要的 USDC（如 $100-200 練手）。
- **把 USDC 從 Polygon bridge 到 Base**。最便宜選擇：[Stargate](https://stargate.finance) 或 [Across](https://app.across.to)，gas 大約 $0.5。
- Limitless 在 Base 鏈，地址與 Polygon 共用（同個 EOA）。
- 在 https://limitless.exchange 用這個錢包做一次連線（會建立 Privy 帳號）。

### 2. 取得 HMAC API token

```bash
# 一次性：用瀏覽器登入 limitless.exchange 後從 DevTools 拿 Privy token
.venv/bin/polymkt limitless auth-derive --privy-token <貼上 Privy token>
```

把回傳的 token_id + secret **立即**填入 `.env`（secret 只顯示一次）：

```bash
cp .env.example .env
# 編輯 .env：
LIMITLESS_API_TOKEN_ID=...
LIMITLESS_API_SECRET=...
POLYGON_PRIVATE_KEY=0x...   # 獨立 wallet 的私鑰（也適用於 Base）
```

### 3. 跑 dry-run 看看會做什麼

```bash
.venv/bin/polymkt crossarb-execute --min-diff-pct 5 --max-positions 3 --notional-per-trade 10
# 仍是 DRY-RUN，列印「將要做什麼」但不真的下單
```

### 4. 真的下單

```bash
# 加 --execute 並設 LIMITLESS_EXECUTE=1
LIMITLESS_EXECUTE=1 .venv/bin/polymkt crossarb-execute \
  --min-diff-pct 5 --max-positions 3 --notional-per-trade 10 --execute
```

或單筆手動下單：

```bash
LIMITLESS_EXECUTE=1 .venv/bin/polymkt limitless place-order \
  --slug <market-slug> --side BUY --outcome YES \
  --price 0.30 --size 30 --order-type GTC --execute
```

## 安全機制

| 機制 | 設定 | 行為 |
|------|------|------|
| 預設 dry-run | `LIMITLESS_EXECUTE != 1` 或缺 `--execute` | 永不真實下單 |
| 單筆上限 | `LIMITLESS_MAX_PER_ORDER`（預設 $50） | 超過直接拒絕 |
| 會話累計上限 | `LIMITLESS_MAX_PER_SESSION`（預設 $500） | 累計超過後續訂單全拒 |
| 價格範圍檢查 | 內建 | `price` 必須在 (0, 1) |
| 私鑰存取 | 只從環境變數讀 | 從不寫入日誌或 stdout |
| Secret 顯示 | `auth-derive` 後只顯示一次 | 強制使用者立即保存 |

## CLI 完整指令

## 三個主指令（分析/掃描）

### 1. `polymkt limitless closest` — 看市場效率

```bash
.venv/bin/polymkt limitless closest --top 15
```

列出 Limitless 上同市場 `ΣAsk(YES+NO)` 與互斥群組 `ΣYES` 最接近 $1 的市場。
觀察：Limitless spread 普遍很寬（同市場 ΣAsk 可達 $1.98+），代表做市機會大。

### 2. `polymkt limitless scan` — 找純 Limitless 套利

```bash
.venv/bin/polymkt limitless scan --min-edge-bps 30 --probe-shares 100 --top 10
```

掃描兩種真套利：
- 同市場 YES+NO < $1
- 互斥群組 ΣYES < $1（marketType=group + 全部子市場未結算）

實測：通常 0 機會（市場有效率），這跟 Polymarket 一樣。

### 3. `polymkt crossarb` — Polymarket↔Limitless 訊號交易

```bash
.venv/bin/polymkt crossarb \
  --min-event-similarity 0.8 \
  --min-diff-pct 2.0 \
  --top 20
```

找 Limitless 上 `isPolyArbitrage=True` 的鏡像 Polymarket 市場，並比較 YES 價差：
- LM YES 比 PM YES **便宜** → 在 LM 買 YES（等價格收斂）
- LM YES 比 PM YES **貴** → 在 LM 賣 YES / 買 NO

**這不是純套利**（你在 PM 鎖死，無法雙邊對沖）。是「PM 較有效率 → LM 會收斂」的統計訊號。

## 三個下單指令

### `polymkt limitless auth-derive --privy-token <token>`

一次性：把 Privy token 換成 HMAC scoped token（寫入 `.env`）。

### `polymkt limitless place-order ...`

手動單筆下單。預設 dry-run，加 `--execute` 才真實送出。所有安全限額仍適用。

### `polymkt crossarb-execute ...`

**主力**：自動化跨平台訊號交易。
- 找 PM↔LM 價差 ≥ `--min-diff-pct` 的訊號
- 用 GTC（限價掛 maker）或 FAK（IOC 立即成交）方式下單
- 預設 GTC + maker-offset 0.5pp（取 PM 中價 + 安全 margin）

## 額外指令

| 指令 | 用途 |
|------|------|
| `polymkt polymarket scan` | Polymarket 套利掃描（僅供觀察，台灣下不了單） |
| `polymkt polymarket closest` | Polymarket 最接近套利的市場 |

## 實測結論（2026-05-20 跑 1000+ 市場）

1. **純市價單套利在 Polymarket 與 Limitless 都不存在**（最小 tick 一定吃掉空間）
2. **Limitless spread 很寬**（有些題目 ΣAsk 達 $1.98） → 做市機會多，但 inventory 風險也大
3. **跨平台價差實際存在**：Mallorca/Tottenham 等 LALIGA/EPL 降級題目，Limitless 報價 stale 在 $0.495，Polymarket 已成交到實際機率（15-30%）
4. **價差通常不可立即吃下**：LM 流動性差是價差的成因，下市價單可能不成交。建議用 **maker limit 單**

## 重要設計決策

### 跨平台匹配演算法

對齊 LM 鏡像市場與 PM 市場分兩層：
1. **Event-level**（group 對 event）：要求 normalized content tokens **完全相等**（避免「FIFA World Cup Group A Winner」被誤配「2026 FIFA World Cup Winner」）
2. **Sub-market level**（子市場對子市場）：優先用 PM `groupItemTitle` 精確匹配，否則用子字串包含關係

### Limitless 訂單簿單側返回

Limitless `/markets/{slug}/orderbook` 只返回 YES 側。NO 側透過對稱推算：
- 買 NO @ p = 賣 YES @ (1-p)
- NO best ask = 1 - YES best bid
- NO best bid = 1 - YES best ask

詳見 [polymkt/limitless/models.py](polymkt/limitless/models.py) 的 `LimitlessOrderBook`。

### Rate Limit 處理

Limitless API 對未認證請求的 rate limit 較嚴（會回 429）。Client 內建：
- Exponential backoff 重試
- `max_concurrency=4` 限制並發
- 分頁之間 300ms 延遲

## 程式碼結構

```
polymkt/
├── __init__.py
├── models.py              # Polymarket 模型
├── clients.py             # Polymarket Gamma + CLOB client
├── scanner.py             # Polymarket 套利掃描
├── crossarb.py            # 跨平台價差比對
├── limitless/
│   ├── models.py          # Limitless 模型（含 group + orderbook）
│   ├── client.py          # Limitless API client（含 retry/backoff）
│   └── scanner.py         # Limitless 套利掃描
└── cli.py                 # 統一 CLI 入口
```

## 路線圖

- [x] v0.1：Polymarket 掃描器（讀取分析）
- [x] v0.2：Limitless 掃描器（讀取，主要交易場域）
- [x] v0.3：Cross-arb 訊號（PM↔LM 價差）
- [x] v0.4：**Limitless 自動下單**（dry-run + 安全限額 + EIP-712 簽名透過官方 SDK）
- [ ] v0.5：Maker 策略（在 Limitless 寬 spread 上掛雙邊限價單，做市賺價差）
- [ ] v0.6：鯨魚追蹤（追蹤 Polymarket 上歷史高 ROI 錢包）
- [ ] v0.7：持倉管理（追蹤已開倉位、自動平倉、停損規則）

## 風險提醒

1. **訊號交易不是套利**：crossarb 的價差訊號可能因為 LM 真的有特殊資訊而存在，不一定會收斂
2. **流動性陷阱**：crossarb 找出的價差通常存在於 LM 流動性差的市場，下單可能滑價或不成交
3. **私鑰安全**：自動下單需要私鑰，**永不要 commit 真實私鑰**，建議用獨立 wallet 只放下單需要金額
4. **法規風險**：雖然 Limitless ToS 未禁止台灣，但區塊鏈預測市場在台灣法律灰色地帶
