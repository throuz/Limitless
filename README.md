# limitless — Limitless Exchange 做市工具(台灣可用版)

> ⚠️ **免責聲明**:這套工具**不保證獲利**。預測市場有真實滑價、流動性、結算爭議等風險。
> 工具只是降低「發現機會 + 自動執行」的難度。

## 一句話定位

**Limitless Exchange 的 24/7 自動做市 + PnL 追蹤,以 Polymarket 當輔助 oracle**。

- **主交易場**:Limitless(Base 鏈,台灣可用)
- **輔助 oracle**:Polymarket(台灣 close-only,只讀,當公平價/訊號來源)
- **執行能力**:make-market(單市場做市)、mm-rank(找標的)、mm-loop(24/7 自動換市場)
- **部署**:本機 + tmux,或 AWS CDK(serverless / EC2 / Fargate 三選一)

## 為什麼設計這樣

| 元件 | 角色 | 為什麼 |
|---|---|---|
| **Limitless** | 主交易場 ✅ | Base 鏈 + 台灣可用 + spread 寬 |
| **Polymarket** | 輔助 oracle | 流動性 10-100× 大,mid 較準;台灣不能新開倉 |
| **Cross-arb** | 訊號 / 公平價 | LM 偏離 PM 太多 → 訊號可吃 / oracle 校正 |

## 安裝

```bash
cd /Users/mac/Projects/limitless
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e '.[trade]'   # 含 Limitless SDK，可下單
# 只要分析、不下單可改用：.venv/bin/pip install -e .
```

> ⚠️ **若 `.venv/bin/limitless` 拋 `bad interpreter` 錯誤**:venv 是在舊路徑建的(shebang 寫死)。
> 全域替代寫法:把下面所有 `.venv/bin/limitless` 換成 `.venv/bin/python -m limitless.cli`,功能完全相同。
> 永久解法:`rm -rf .venv` 然後重跑上面的 venv 建立指令。

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
.venv/bin/limitless auth-derive --privy-token <貼上 Privy token>
```

把回傳的 token_id + secret **立即**填入 `.env`（secret 只顯示一次）：

```bash
cp .env.example .env
# 編輯 .env：
LIMITLESS_API_TOKEN_ID=...
LIMITLESS_API_SECRET=...
BASE_PRIVATE_KEY=0x...   # 獨立 wallet 的私鑰(0x + 64 hex,共 66 字元)
```

### 3. 跑 dry-run 看看會做什麼

```bash
.venv/bin/limitless crossarb-execute --min-diff-pct 5 --max-positions 3 --notional-per-trade 10
# 仍是 DRY-RUN，列印「將要做什麼」但不真的下單
```

### 4. 真的下單

```bash
# 加 --execute 並設 LIMITLESS_EXECUTE=1
LIMITLESS_EXECUTE=1 .venv/bin/limitless crossarb-execute \
  --min-diff-pct 5 --max-positions 3 --notional-per-trade 10 --execute
```

或單筆手動下單：

```bash
LIMITLESS_EXECUTE=1 .venv/bin/limitless place-order \
  --slug <market-slug> --side BUY --outcome YES \
  --price 0.30 --size 30 --order-type GTC --execute
```

## 鯨魚跟單系統

從 Polymarket 公開 data API（`data-api.polymarket.com`）拿全平台交易，自動找高 ROI 錢包並追蹤。

### 三步驟流程

```bash
# 1. 列出近期最有 alpha 的鯨魚（dollar-weighted ROI × log 量級）
.venv/bin/limitless whales list --top 10

# 2. 挑你想追的 wallet，寫進 .env：
#    WHALE_WALLETS=0xc97b...,0x42c99f...
#    或直接 --wallets 帶入

# 3. 監控他們的新動作（看訊號，不下單）
.venv/bin/limitless whales watch --lookback-min 60 --min-trade 500

# 4. 自動跟單（dry-run）
.venv/bin/limitless whales follow --lookback-min 60 --min-trade 500 \
  --max-positions 3 --notional-per-trade 10
```

### Alpha 評分公式

```
alpha = (已實現 PnL + 0.7 × 未實現 PnL) / 累計買入金額 × 100 × log(1 + 累計買入金額/1000)
```

- 用 dollar-weighted（不被「玩 10 筆小單虧 -100%」拉低）
- log 量級加分（避免「賺一次大運氣」混進來）
- 70% 折現未實現（避免被市價短期波動過度影響）

### 已知限制

1. **API 偶爾不一致**：某些 wallet `/positions` 時而回空。我們已經做了 fallback，但偶爾鯨魚 score 抓不到
2. **跟單對應率低**：鯨魚追的題目多半 LM 沒對應（網球、商品、財報），實測 follow 命令對應率 < 20%
3. **過去 ROI ≠ 未來 ROI**：高 ROI 可能是運氣 + 倖存者偏差
4. **時間延遲**：鯨魚 PM 下單 → 我們 watch 抓到 → LM 下單，整段約 30-60 秒；對短期 alpha 不利

### 把 wallet 寫進 .env

```bash
# .env
WHALE_WALLETS=0xc97b0b2a2547bb3ed57167092ef8a6e816c347e5,0x42c99f38d2b951b0dc8e8bd5371fa80c9dd19623
```

之後 `watch` / `follow` 命令無需 `--wallets` 旗標。

## 做市 (v0.6)

### 原理:CTF 雙 BID

不需要先有庫存就能做市。掛兩個 BUY 限價單(YES + NO),總和 < $1:

```
YES mid = $0.30, NO mid = $0.70 → 兩邊各扣 1pp 偏移
掛 BUY YES @ $0.28 + BUY NO @ $0.68 (總和 $0.96)

情境 A:兩邊都被吃 → 持 1 YES + 1 NO → 結算保證 $1 → 賺 $0.04 (4.2% ROI)
情境 B:只 YES 被吃 → 累積 YES 庫存 → v0.6 主動掛 SELL YES 出場
情境 C:只 NO 被吃  → 累積 NO 庫存  → 同上鏡像
情境 D:都沒被吃    → 下輪重評,視 microprice 變化重掛
```

### v0.6 防禦模組(5 個)

| 模組 | 解決什麼問題 | 對應 flag |
|---|---|---|
| **Microprice 公平價** | 對手側 size 加權,book 不對稱時比 mid 準 | `--microprice/--no-microprice` |
| **Toxicity 偵測** | YES/NO fill 不對稱 / PM 漂移 / LM ask 突跌 → 加寬該側 spread 或撤單 | `--toxicity-window`、`--toxicity-imbalance` 等 |
| **主動清庫存** | 庫存超過 max × 60% → 加掛 SELL @ mid + premium,maker 清部位 | `--unwind-inventory-pct`、`--unwind-premium-pct` |
| **結算偵測** | 距結算 < 24h → 撤所有單 + FAK 市價清倉,避免結算炸彈 | `--emergency-close-hours` |
| **PM Oracle**(v0.5b) | 用 Polymarket 鏡像 mid 當公平價,抗資訊套利 | `--oracle pm/blend` |
| **Inventory Skew**(v0.5b) | 庫存偏一邊時拉走該側 bid,降低再買入機率 | `--inventory-skew-pct` |

### 啟動指令

```bash
# 先 dry-run 看設定是否合理
.venv/bin/python -m limitless.cli limitless make-market \
  --slug <some-market-slug> \
  --capital 100 \
  --quote-size 20 \
  --target-profit-pct 4 \
  --half-spread-pct 1 \
  --max-inventory 50 \
  --oracle pm \
  --emergency-close-hours 24 \
  --duration 600

# 真的下單(加 --execute)
LIMITLESS_EXECUTE=1 .venv/bin/python -m limitless.cli limitless make-market \
  --slug <some-market-slug> --capital 100 --quote-size 20 \
  --oracle pm --execute
```

### 參數含義

| 參數 | 預設 | 意義 |
|------|------|------|
| `--capital` | $100 | 本次做市總資本上限;累計達就停止下新單 |
| `--quote-size` | 20 | 每邊單筆股數 |
| `--target-profit-pct` | 4% | 兩邊都成交時的 ROI;同時也是 yes_bid + no_bid 的折扣 |
| `--half-spread-pct` | 1pp | 每邊報價偏離 LM mid 多少 |
| `--max-inventory` | 50 股 | YES 或 NO 任一達此股數就停止下 BUY |
| `--iter-sleep` | 30s | 重新報價間隔 |
| `--duration` | 600s | 持續時間;設 0 = 無限直到 Ctrl-C |
| `--oracle` | `lm` | `lm` / `pm` / `blend`:公平價來源 |
| `--microprice/--no-microprice` | on | 用 microprice 取代 mid |
| `--toxicity-window` | 5 | toxicity 偵測滾動窗口輪數 |
| `--toxicity-imbalance` | 0.7 | YES/NO fill 不對稱比 > 此值 → 加寬該側 |
| `--toxicity-pm-velocity` | 0.03 | PM mid 窗口內漂移 > $ 此值 → 撤所有單 |
| `--toxicity-widen-mult` | 2.0 | toxicity 偵測時 spread × 此倍數 |
| `--unwind-inventory-pct` | 0.6 | 庫存超過 max × 此比例就主動掛 SELL |
| `--unwind-premium-pct` | 1.0 | SELL 報價比 mid 高多少 pp |
| `--emergency-close-hours` | 24 | 距結算 < 此小時數 → 強制清倉退場 |
| `--inventory-skew-pct` | 0.5 | 庫存偏一邊時拉走該側 bid 多少 pp |

### 怎麼選市場

最直接的方法:跑 [`mm-rank`](#mm-rank--自動排序最適合做市的市場) 自動排序。

或手動判斷:
- ✅ **適合做市**:流動性低、spread 寬、距結算還久(>1 週)、PM 有相關信號可當公平價
- ❌ **避免做市**:高頻幣價題(其他 bot 已佔領)、即將結算(resolution risk)、單邊資訊明確(被資訊套利)、政治/Fed/財報等新聞密集題目

### 已知限制(v0.6)

- 單一市場時 capital 鎖死該市場(可用 [mm-loop](#mm-loop--247-自動換市場) 解)
- 結算事件只看 expirationDate;若 LM 提前結算我們察覺不到
- 主動 SELL 清倉是 maker(GTC + post-only),急著清要切 FAK
- Toxicity 模型靠 fill 數據統計,新市場樣本不足時敏感度低

### 風險

- **庫存單邊堆積**:只一邊被吃 → 累積該方向庫存 → 結算為相反方向時全賠;v0.6 用主動 SELL + skew 緩解,但無法完全消除
- **資訊套利**:知情交易者偷掃單;v0.6 toxicity 偵測能擋大部分但非全部
- **資本鎖死**:訂單未成交也鎖定資本(LM 內部 escrow)

## 24/7 AWS 雲端部署 (v0.8)

不想本機開機跑 mm-loop 的話,可以部署到 AWS。**三種 stack 任選**:

| Stack | 月成本 | 適合 |
|---|---|---|
| **serverless**(預設) ⭐ | **$0-0.50**(永久免費) | 永久免費、無 OS 維運;Lambda + DynamoDB + EventBridge |
| **free** | $0 → ~$8(12 月後) | 想用 OS 觀察 + EC2 free tier 還沒過期 |
| **managed** | ~$11(永久) | 不想管 OS,完全 AWS 託管;Fargate |

部署是 AWS CDK (Python),一行指令完成:

```bash
cd infra
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/cdk bootstrap aws://<acct>/ap-northeast-1   # 一次性
.venv/bin/cdk deploy                                   # serverless dry-run
STACK_EXECUTE=1 .venv/bin/cdk deploy                   # 真實下單
```

完整步驟、3 種 stack 對照、安全防護、暫停/恢復、故障排除等,**詳見 [infra/README.md](infra/README.md)**。

## 安全機制

| 機制 | 設定 | 行為 |
|------|------|------|
| 預設 dry-run | `LIMITLESS_EXECUTE != 1` 或缺 `--execute` | 永不真實下單 |
| 單筆上限 | `LIMITLESS_MAX_PER_ORDER`(預設 $50) | 超過直接拒絕 |
| 會話累計上限 | `LIMITLESS_MAX_PER_SESSION`(預設 $500) | 累計超過後續訂單全拒 |
| 全域資本上限(v0.7) | `MM_LOOP_TOTAL_CAPITAL`(預設 $500) | mm-loop 所有市場合計超過即停 |
| 結算窗口保護(v0.6) | `--emergency-close-hours`(預設 24h) | 距結算 < 此時數強制清倉 |
| 價格範圍檢查 | 內建 | `price` 必須在 (0, 1) |
| 私鑰存取 | 只從環境變數讀 | 從不寫入日誌或 stdout |
| Secret 顯示 | `auth-derive` 後只顯示一次 | 強制使用者立即保存 |
| AWS 部署:私鑰加密 | SSM Parameter Store SecureString / Secrets Manager | KMS 加密 at rest;只 task role 可讀 |
| AWS 部署:Lambda 並發限制 | `reserved_concurrent_executions=1` | 防止 iteration 重疊產生競態 |

## CLI 完整指令

## 三個主指令（分析/掃描）

### 1. `limitless limitless closest` — 看市場效率

```bash
.venv/bin/limitless closest --top 15
```

列出 Limitless 上同市場 `ΣAsk(YES+NO)` 與互斥群組 `ΣYES` 最接近 $1 的市場。
觀察：Limitless spread 普遍很寬（同市場 ΣAsk 可達 $1.98+），代表做市機會大。

### 2. `limitless limitless scan` — 找純 Limitless 套利

```bash
.venv/bin/limitless scan --min-edge-bps 30 --probe-shares 100 --top 10
```

掃描兩種真套利：
- 同市場 YES+NO < $1
- 互斥群組 ΣYES < $1（marketType=group + 全部子市場未結算）

實測：通常 0 機會（市場有效率），這跟 Polymarket 一樣。

### 3. `limitless crossarb` — Polymarket↔Limitless 訊號交易

```bash
.venv/bin/limitless crossarb \
  --min-event-similarity 0.8 \
  --min-diff-pct 2.0 \
  --top 20
```

找 Limitless 上 `isPolyArbitrage=True` 的鏡像 Polymarket 市場，並比較 YES 價差：
- LM YES 比 PM YES **便宜** → 在 LM 買 YES（等價格收斂）
- LM YES 比 PM YES **貴** → 在 LM 賣 YES / 買 NO

**這不是純套利**（你在 PM 鎖死，無法雙邊對沖）。是「PM 較有效率 → LM 會收斂」的統計訊號。

## 三個下單指令

### `limitless limitless auth-derive --privy-token <token>`

一次性：把 Privy token 換成 HMAC scoped token（寫入 `.env`）。

### `limitless limitless place-order ...`

手動單筆下單。預設 dry-run，加 `--execute` 才真實送出。所有安全限額仍適用。

### `limitless crossarb-execute ...`

跨平台訊號交易。找 PM↔LM 價差 ≥ `--min-diff-pct` 的訊號 → 在 LM 下單。
**已知限制**：訊號池小（4-6 個），多數對應 LM 流動性差的市場。

### `limitless limitless make-market --slug <slug>` (v0.6 做市)

**雙 BID 做市**:在指定市場同時掛 BUY YES + BUY NO,總和 < $1,等對手吃單。
- 雙邊都成交:保證 $1 payout,賺差額
- 單邊成交:累積該方向庫存 → v0.6 主動掛 SELL 清倉
- 預設 dry-run;目標 ROI、報價偏移、庫存上限、持續時間都可調
- **v0.6 新增**:microprice / toxicity 偵測 / 主動 SELL 清庫存 / 結算偵測 emergency close
- **v0.5b**:可選 `--oracle pm/blend` 用 Polymarket 鏡像當公平價,`--inventory-skew-pct` 庫存偏一邊時拉走報價
- 詳見[做市運作說明](#做市-v06)章節

### `limitless limitless mm-rank` — 自動排序最適合做市的市場

```bash
.venv/bin/python -m limitless.cli limitless mm-rank \
  --min-volume-usd 200 --min-days 3 --min-spread-bps 100 --top 10
```

評分公式:
```
score = spread_pp × (days_to_res / 7) × poly_arb_bonus × log(1 + volume/1000)
        / (1 + news_risk)
```

硬過濾:
- 距結算 < `--min-days` 天 → 跳過(避免結算 risk)
- volume < `--min-volume-usd` → 跳過
- spread < `--min-spread-bps` bps → 跳過(沒利潤可吃)
- mid < 0.05 或 > 0.95 → 跳過(風險不對稱)
- 標題命中新聞風險關鍵字(election/Fed/CPI/earnings/今晚比賽 等) → 懲罰

挑 score 高、有 🪞(PM 鏡像)、沒 🛑(新聞風險)的市場直接餵給 `make-market`。

### `limitless limitless mm-loop` — 24/7 自動換市場

```bash
# 本機跑(預設 dry-run)
.venv/bin/python -m limitless.cli limitless mm-loop \
  --total-capital 500 --max-positions 3 --capital-per-market 100 \
  --oracle pm

# 真實下單
LIMITLESS_EXECUTE=1 .venv/bin/python -m limitless.cli limitless mm-loop \
  --total-capital 500 --max-positions 3 --execute

# 從 env 讀全部(給容器 / Lambda 部署用)
.venv/bin/python -m limitless.cli limitless mm-loop --from-env
```

行為:
1. 開啟時跑 mm-rank,挑 top N 市場開 `MarketMaker` session
2. 每市場結算 / 觸發 emergency_close → 自動移除
3. 每小時(可調)重新 mm-rank,補新市場到 `--max-positions`
4. SIGTERM / SIGINT → 全部 cancel + 收乾淨退出
5. **全域資本上限**:`--total-capital` 是所有市場合計上限,做為硬性 cap

詳見[做市運作說明](#做市-v06)的所有 flag 也都套用到 mm-loop(用同名 env var)。

### `limitless whales list / watch / follow`

**鯨魚跟單系統**：追蹤 Polymarket 高 ROI 錢包、將其新動作鏡像到 Limitless。
- `whales list` — 列出近期最有 alpha 的鯨魚（dollar-weighted ROI × log 量級）
- `whales watch --wallets ...` — 監控指定鯨魚的最新動作
- `whales follow --wallets ...` — 自動配對 LM 對應市場 + 下跟單訂單（預設 dry-run）

⚠️ 已知限制：鯨魚最活躍的題目（網球、商品、財報）多半 LM 沒對應 → 跟單訊號**actionable 率約 10-20%**。詳見[鯨魚系統章節](#鯨魚跟單系統)。

## 額外指令

| 指令 | 用途 |
|------|------|
| `limitless pm scan` | Polymarket 套利掃描(僅供觀察,台灣下不了單) |
| `limitless pm closest` | Polymarket 最接近套利的市場 |

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

詳見 [limitless/models.py](limitless/models.py) 的 `LimitlessOrderBook`。

### Rate Limit 處理

Limitless API 對未認證請求的 rate limit 較嚴（會回 429）。Client 內建：
- Exponential backoff 重試
- `max_concurrency=4` 限制並發
- 分頁之間 300ms 延遲

## 程式碼結構

```
limitless/                                ← repo root
├── limitless/
│   ├── __init__.py                       # 套件 docstring
│   ├── crossarb.py                       # 跨平台 PM↔LM 價差訊號(連接兩邊)
│   ├── cli.py                            # 統一 CLI 入口
│   │
│   ├── limitless/                        # 🟢 主交易場
│   │   ├── client.py                     # LM API client(read,retry/backoff)
│   │   ├── models.py                     # Market / Group / OrderBook 模型
│   │   ├── scanner.py                    # 套利機會掃描
│   │   ├── trading.py                    # 下單 client(包 limitless-sdk + dry-run + 安全限額)
│   │   ├── market_maker.py               # v0.6 做市核心(microprice/toxicity/unwind/emergency)
│   │   ├── mm_loop.py                    # v0.7 24/7 自動換市場調度器
│   │   ├── serverless.py                 # Lambda 端狀態序列化(DynamoDB I/O)
│   │   └── pnl.py                        # v0.9 PnL 追蹤(SQLite)+ wallet snapshot
│   │
│   └── polymarket/                       # 🟡 輔助 oracle / 訊號源
│       ├── clients.py                    # Gamma + CLOB client(read-only)
│       ├── models.py                     # PM Event / Market / OrderBook
│       ├── scanner.py                    # PM 套利掃描(觀察用)
│       └── whales.py                     # 鯨魚追蹤(score / watch / follow)
│
├── lambda_handlers/                      # AWS Lambda 進入點
│   ├── iterate.py                        # 每 N 秒跑一輪所有 active 市場
│   └── rerank.py                         # 每小時重新挑市場
│
├── infra/                                # AWS CDK 基礎設施(Python)
│   ├── app.py                            # CDK entry(STACK_TIER=serverless/free/managed)
│   ├── limitless_infra/
│   │   ├── serverless_stack.py           # Lambda + DDB + EventBridge stack ⭐
│   │   ├── free_tier_stack.py            # EC2 t3.micro + SSM stack
│   │   └── stack.py                      # Fargate + Secrets Manager stack
│   └── README.md                         # 完整部署/維運指南
│
├── Dockerfile                            # mm-loop EC2 / Fargate 容器
├── Lambda.Dockerfile                     # Lambda container image
└── run-mm-local.sh                       # 本機 24/7 + auto-restart wrapper
```

## 路線圖

- [x] v0.1:Polymarket 掃描器(讀取分析)
- [x] v0.2:Limitless 掃描器(讀取,主要交易場域)
- [x] v0.3:Cross-arb 訊號(PM↔LM 價差)
- [x] v0.4:**Limitless 自動下單**(dry-run + 安全限額 + EIP-712 簽名透過官方 SDK)
- [x] v0.5a:**Limitless 雙 BID 做市**(單市場、對稱報價)
- [x] v0.5b:做市進階防禦(quote skew + PM oracle)
- [x] v0.6a:**鯨魚跟單系統**(list / watch / follow)
- [x] v0.6b:**做市 5 模組**(microprice + toxicity 偵測 + 主動清庫存 + 結算偵測 + mm-rank 市場排序)
- [x] v0.7:**24/7 自動換市場**(mm-loop:rerank + 多市場並行 + graceful shutdown)
- [x] v0.8:**AWS CDK 雲端部署**(serverless Lambda / EC2 free tier / Fargate 三種 stack)
- [ ] v0.9:Telegram / Discord 告警 + 每日 PnL 摘要
- [ ] v1.0:跨平台真實對沖(若 LM 流動性允許,真正鎖死利潤而非統計訊號)

## 風險提醒

1. **訊號交易不是套利**：crossarb 的價差訊號可能因為 LM 真的有特殊資訊而存在，不一定會收斂
2. **流動性陷阱**：crossarb 找出的價差通常存在於 LM 流動性差的市場，下單可能滑價或不成交
3. **私鑰安全**：自動下單需要私鑰，**永不要 commit 真實私鑰**，建議用獨立 wallet 只放下單需要金額
4. **法規風險**：雖然 Limitless ToS 未禁止台灣，但區塊鏈預測市場在台灣法律灰色地帶
