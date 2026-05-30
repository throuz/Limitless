# Limitless MM Bot

24/7 自動做市 bot for Limitless Exchange(Base chain CTF prediction markets)。
策略:雙邊掛 BUY YES + BUY NO,等對手吃單,賺 spread。
部署:AWS Lambda + DynamoDB + EventBridge,**$0/月**(全在 free tier 內)。

## 程式碼地圖

```
limitless/
  market_maker.py   主邏輯:iterate() 是核心,內含 toxicity / microprice / unwind / emergency_close
  client.py         Limitless API wrapper(orderbook、markets、positions)
  trading.py        下單(EIP-712 簽名)、cancel_all
  serverless.py     DDB schema + ServerlessCfg(env var → config)
  chain.py          鏈上 USDC balance 讀取(純 httpx,免 web3.py)
  notify.py         Telegram 通知
  pnl.py            本機 SQLite PnL(Lambda 自動 disable)

lambda_handlers/
  iterate.py        每 5 min 跑 — 對每個 active market 跑 mm.iterate()
  rerank.py         每 60 min 跑 — 從鏈上讀 USDC 更新動態 cap、清結算市場、補新市場
  daily_summary.py  每天 UTC 22:00 — 發每日 Telegram 摘要

infra/
  build_lambda.sh                打包 ZIP 到 lambda_build/
  limitless_infra/serverless_stack.py   CDK 主 stack
  app.py                         CDK entry

approve_limitless.py             根目錄,單次 USDC + CTF approve(換合約時要重跑)
```

## 部署

```bash
./infra/build_lambda.sh
cd infra && STACK_EXECUTE=1 cdk deploy --require-approval never
```

`STACK_EXECUTE=1` 表示 Lambda 真的下單(沒設 → dry-run)。

## 觀察

```bash
# Lambda 即時 log
aws logs tail /aws/lambda/limitless-mm-iterate --since 10m --region ap-northeast-1

# 全域狀態
aws dynamodb get-item --table-name limitless-mm-state \
  --key '{"pk":{"S":"global"}}' --region ap-northeast-1 --consistent-read

# 手動觸發
aws lambda invoke --function-name limitless-mm-iterate \
  --invocation-type RequestResponse --region ap-northeast-1 /tmp/out.json
```

## 重要(常踩雷)

### 1. 資金限制是「動態」的

`MM_LOOP_TOTAL_CAPITAL` / `MM_LOOP_CAPITAL_PER_MARKET` env var **只是 fallback**。
真實 cap:rerank 每小時從 wallet 讀鏈上 USDC,× 0.9 buffer 寫到 `g.dynamic_total_cap`。
要調整 → 改 wallet 餘額(充值/提款),**不要動 env**。

`quote_size_shares` / `max_inventory_shares` 也自動從 `effective_per_market` 推。

### 2. Limitless 換 exchange 合約時要重新 approve

症狀:Telegram 收到「🚨 訂單被拒(可能需要重新 approve)」(rate-limit 6h)
處理:`.venv/bin/python approve_limitless.py`

### 3. NEVER commit `.env`

在 `.gitignore`。包含 `BASE_PRIVATE_KEY` / `LIMITLESS_API_SECRET` / `TELEGRAM_BOT_TOKEN`。
Lambda 用 SSM SecureString 拉這些(`bootstrap_secrets()` 在冷啟動時注入 `os.environ`)。

### 4. SSM SecureString 不能由 CloudFormation 建立

`infra/limitless_infra/serverless_stack.py` 只 reference 名字,**值必須用 aws cli 手動寫**:

```bash
aws ssm put-parameter --name /limitless-mm/BASE_PRIVATE_KEY \
  --type SecureString --value "0x..." --region ap-northeast-1
```

### 5. Lambda 內 PnL DB 會 disable

`pnl.py` 用 `os.environ.get("AWS_LAMBDA_FUNCTION_NAME")` 偵測 Lambda 環境,
偵測到就 no-op(否則寫 SQLite 會撞 read-only filesystem)。
本機跑 `mm-loop` CLI 才會記 PnL。

### 6. iterate 用 consistent read

`iterate.py` 用 `load_global_consistent`(`ConsistentRead=True`),
否則會讀到 stale 的 dynamic_total_cap,蓋掉 rerank 寫的值。

## DDB schema(table:`limitless-mm-state`)

```
pk='global'           GlobalState(total_capital_used, dynamic_total_cap, last_fill_at, ...)
pk='active'           ActiveList(slugs[])
pk='market#<slug>'    MarketState(capital_used, tox, last_quoted_*, exhausted, ...)
```

## 核心 invariant

- `_capital_used` 是 **fill-based**,不是 placed-based(GTC post_only 訂單 USDC 不會被鎖)
- `state.exhausted=True` 只會設,不會自動 reset(要 reset 需手動 update DDB)
- `_should_requote` 在價格穩定時 skip cancel+place,維持 maker queue position
- `emergency_close` 在距結算 < 24h 時觸發(撤所有單 + FAK 市價清倉)

## Region

`ap-northeast-1`(Tokyo)— 寫 AWS CLI 命令時記得加 `--region`。

## 如何快速判斷 bot 是否正常運作

```bash
aws logs tail /aws/lambda/limitless-mm-iterate --since 10m --region ap-northeast-1 \
  | grep iterate_done | tail -3
```

正常會看到:`processed: 3, errors: 0, emergencies: 0`。
errors > 0 → 看完整 log。沒任何 iterate_done → 看 rerank 是否 active 列表是空的。
