# limitless mm-loop AWS 部署

把 `mm-loop` 跑在 AWS 24/7,讓做市自動換市場、結算自動退場、不用本機開機。

## 三種 stack — 任選

| | `serverless`(預設) ⭐ | `free` | `managed` |
|---|---|---|---|
| 運算 | Lambda(ZIP)+ EventBridge | EC2 t3.micro | ECS Fargate |
| 狀態 | DynamoDB(provisioned 25/25) | systemd in container | systemd in container |
| 秘密 | SSM Parameter Store | SSM Parameter Store | Secrets Manager |
| 映像 | S3 zip asset(無 ECR)| S3 asset | ECR |
| **真實月成本** | **$0**(永久免費) | $0 → ~$8(12 個月後) | $11(永久) |
| OS 維運 | 完全沒有 | 你要負責 | AWS 全託管 |
| 崩潰恢復 | Lambda 下次觸發自動恢復 | systemd 自動重啟 | ECS 自動重啟 |
| Iteration 反應速度 | 60-120s | 30s | 30s |
| 切換指令 | `STACK_TIER=serverless`(預設) | `STACK_TIER=free` | `STACK_TIER=managed` |

### 預估月成本

**Serverless(預設)**:

| 元件 | 月用量 | 永久免費額度 | 月費 |
|---|---|---|---|
| Lambda invocations | 43k | 1M | $0 |
| Lambda GB-seconds | 55k | 400k | $0 |
| EventBridge | 43k | 14M | $0 |
| DynamoDB provisioned 25/25 | < 1 ops/sec | 25 RCU + 25 WCU 永久 | **$0** |
| CloudWatch Logs | < 1 GB | 5 GB | $0 |
| SSM Parameter Store | 3 個 | unlimited | $0 |
| **總計** | | | **$0** |

**Free tier(EC2)**:

| 元件 | 12 個月內 | 12 個月後 |
|---|---|---|
| EC2 t3.micro 730 hr | $0(750 hr free) | ~$7.50 |
| EBS gp3 12 GB | $0(30 GB free) | ~$1 |
| SSM Parameter Store(Standard) | $0 | $0 |
| CloudWatch Logs(5 GB free) | $0 | ~$0.50 |
| S3 asset(<10 MB) | $0 | < $0.01 |
| 資料傳輸(100 GB free) | $0 | ~$1 |
| **總計** | **$0** | **~$10** |

**Managed(Fargate)**:

| 元件 | 月費 |
|---|---|
| Fargate 0.25 vCPU + 0.5 GB(730h) | ~$8.76 |
| Secrets Manager × 3 | $1.20 |
| CloudWatch Logs(1 個月過期) | ~$0.50 |
| ECR storage(< 1 GB) | $0.10 |
| 資料傳輸 | ~$1 |
| **總計** | **$11-12** |

## 前置條件

1. **AWS 帳號** + IAM user with Admin(或最少 ECS / IAM / Secrets / Logs / ECR / VPC / CloudFormation)
2. 本機裝好 `aws` CLI 並跑過 `aws configure`
3. 本機裝好 Docker(只 EC2 / Fargate stack 需要;serverless 不需要)
4. 本機裝好 Node.js(CDK CLI 要)
5. Python 3.11+

## 部署步驟

### 1. 設好 AWS 認證

```bash
aws configure
# AWS Access Key ID:    <你的 IAM key>
# AWS Secret Access Key: <secret>
# Default region:        ap-northeast-1
# Default output format: json

# 驗證
aws sts get-caller-identity
```

### 2. 裝 CDK CLI(全域)

```bash
npm install -g aws-cdk
cdk --version  # 看到 2.xxx.x 才對
```

### 3. 裝 stack 的 Python 依賴

```bash
cd infra
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. CDK bootstrap(每個 region/帳號只跑一次)

```bash
.venv/bin/cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/ap-northeast-1
```

### 5. 部署

**選 A:Serverless(預設,真 $0)** ⭐ 推薦

> 部署前必跑一次(把 Lambda 程式 + Linux 相容 deps 打包到 `infra/lambda_build/`):
> ```bash
> ./infra/build_lambda.sh
> ```
> 改 limitless 程式碼後也要重跑這個。


```bash
# Dry-run(預設)— STACK_TIER 沒設 = "serverless"
.venv/bin/cdk deploy

# 真實下單(驗證過 dry-run 行為再切)
STACK_EXECUTE=1 .venv/bin/cdk deploy

# 加 email 警報(可選)
ALARM_EMAIL=you@example.com .venv/bin/cdk deploy

# iteration 改成 60 秒(預設 120s,改快會用更多 Lambda 但還是免費)
ITERATION_SECONDS=60 .venv/bin/cdk deploy
```

部署完會印出:
- `TableName`、`IterateFunctionName`、`RerankFunctionName`
- `SetSecretsCommand`(下一步)
- `TailIterateLogsCommand`、`TailRerankLogsCommand`
- `ManualInvokeRerank`(手動觸發 rerank,不等下一輪)
- `BudgetSetupNote`

**選 B:Free tier(EC2)** — 想用實體 OS 玩

```bash
STACK_TIER=free .venv/bin/cdk deploy
STACK_TIER=free STACK_EXECUTE=1 .venv/bin/cdk deploy
```

**選 C:Managed(Fargate)** — 不想管 OS、能接受 $11/月

```bash
STACK_TIER=managed .venv/bin/cdk deploy
STACK_TIER=managed STACK_EXECUTE=1 .venv/bin/cdk deploy
```

### 6. 填 Secret

⚠️ **重要**:這個私鑰**只放這次做市用的 USDC**($100-500)。不要用主錢包。

#### Serverless / Free tier(SSM Parameter Store,完全一樣):

```bash
aws ssm put-parameter --overwrite --type SecureString \
  --name /limitless/api-token-id \
  --value "<你的 LIMITLESS_API_TOKEN_ID>"

aws ssm put-parameter --overwrite --type SecureString \
  --name /limitless/api-secret \
  --value "<你的 LIMITLESS_API_SECRET>"

aws ssm put-parameter --overwrite --type SecureString \
  --name /limitless/base-private-key \
  --value "0x<你的 BASE 私鑰>"
```

#### Managed(Secrets Manager):

```bash
aws secretsmanager put-secret-value \
  --secret-id limitless/limitless/api-token-id \
  --secret-string "<你的 LIMITLESS_API_TOKEN_ID>"

aws secretsmanager put-secret-value \
  --secret-id limitless/limitless/api-secret \
  --secret-string "<你的 LIMITLESS_API_SECRET>"

aws secretsmanager put-secret-value \
  --secret-id limitless/limitless/base-private-key \
  --secret-string "0x<你的 BASE 私鑰>"
```

### 7. 重啟讓服務讀新 secret

#### Serverless:

**不用做什麼**。Lambda 下次 invocation 觸發時(120s 內)會 cold start 自動讀新 SSM 值。要立刻生效:

```bash
# 強制把現有的 Lambda 容器作廢(下次觸發保證 cold start)
aws lambda update-function-configuration \
  --function-name limitless-mm-iterate \
  --environment "Variables={DDB_TABLE_NAME=limitless-mm-state}"  # 隨便改一個 env 就好
```

或更乾脆,直接手動觸發一次 rerank:

```bash
aws lambda invoke --function-name limitless-mm-rerank --invocation-type RequestResponse /tmp/out.json && cat /tmp/out.json
```

#### Free tier:

```bash
# user-data 在 boot 時讀 SSM 寫到 /etc/limitless/env;reboot 後重新讀
aws ec2 reboot-instances --instance-ids <INSTANCE_ID>
```

#### Managed:

```bash
aws ecs update-service \
  --cluster limitless-mm-loop \
  --service limitless-mm-loop \
  --force-new-deployment
```

### 8. 看 log

```bash
aws logs tail /limitless/mm-loop --follow
```

正常運作會看到類似:
```
[11:23:45] loop_start max=3 cap=$500 execute=true
[11:23:48] rank_picked +3 ['btc-up-or-down...', 'eth-...', 'sol-...']
[11:23:50] session_start btc-up-or-down... cap=$100 :: BTC Up or Down - Weekly
[11:24:20] tox btc-up-or-down... #2 tox=0.85
[11:25:00] session_end btc-up-or-down... iters=5 cap=$95.50
```

## 日常維運

### 改參數(不重 build image)

CDK stack 的 environment variables 改完跑:
```bash
.venv/bin/cdk deploy
aws ecs update-service --cluster limitless-mm-loop --service limitless-mm-loop --force-new-deployment
```

### 改程式(要重 build)

改完 limitless code 後:
```bash
.venv/bin/cdk deploy  # 自動偵測 image 變化、重 build、push、滾動更新
```

### 進去看現場

需要先在本機:`brew install --cask session-manager-plugin`

#### Serverless:

Lambda 沒有「進去看」的概念,只有 log 和狀態:

```bash
# 看 iterate logs
aws logs tail /aws/lambda/limitless-mm-iterate --follow --format short

# 看 rerank logs
aws logs tail /aws/lambda/limitless-mm-rerank --follow --format short

# 看 DynamoDB 目前狀態
aws dynamodb scan --table-name limitless-mm-state --output json | jq

# 看哪些市場在跑
aws dynamodb get-item --table-name limitless-mm-state \
  --key '{"pk": {"S": "active"}}' --output json | jq -r '.Item.slugs.L[].S'

# 看全域累計
aws dynamodb get-item --table-name limitless-mm-state \
  --key '{"pk": {"S": "global"}}' --output json | jq

# 手動觸發 rerank
aws lambda invoke --function-name limitless-mm-rerank \
  --invocation-type RequestResponse /tmp/out.json && cat /tmp/out.json
```

#### Free tier:

```bash
# 進 EC2 shell
aws ssm start-session --target <INSTANCE_ID>

# 進去後常用:
sudo journalctl -u limitless-mm -f          # 看 systemd 服務 log
sudo docker ps                             # 確認 container 在跑
sudo docker logs limitless-mm -f             # 看 container stdout
sudo systemctl restart limitless-mm          # 強制重啟服務
cat /var/log/limitless-bootstrap.log         # 看 user-data boot 過程
```

#### Managed:

```bash
# 1. 拿 task ARN
aws ecs list-tasks --cluster limitless-mm-loop --service-name limitless-mm-loop

# 2. 進去
aws ecs execute-command \
  --cluster limitless-mm-loop \
  --task <TASK_ARN> \
  --container mm-loop \
  --interactive --command /bin/bash
```

### 暫停 / 恢復

#### Serverless:

```bash
# 暫停(EventBridge rule disable,Lambda 就不會被觸發)
aws events disable-rule --name limitless-mm-iterate
aws events disable-rule --name limitless-mm-rerank

# 注意:暫停前最好先撤掉現有 LM 訂單(否則它們會留在 orderbook)
# 改 LIMITLESS_EXECUTE=0 然後觸發一次 iterate → iterate 內的 cancel_all 會清

# 恢復
aws events enable-rule --name limitless-mm-iterate
aws events enable-rule --name limitless-mm-rerank
```

#### Free tier:

```bash
# 暫停(stop EC2,instance 還在但不計 EC2 費,只計 EBS 約 $1/月)
aws ec2 stop-instances --instance-ids <INSTANCE_ID>

# 恢復
aws ec2 start-instances --instance-ids <INSTANCE_ID>
```

#### Managed:

```bash
# 暫停(不刪 stack,只把 desired 設 0)
aws ecs update-service --cluster limitless-mm-loop --service limitless-mm-loop --desired-count 0

# 恢復
aws ecs update-service --cluster limitless-mm-loop --service limitless-mm-loop --desired-count 1
```

### 完全拆掉

```bash
# Secret 因為 RetentionPolicy=RETAIN,不會被刪
.venv/bin/cdk destroy

# 要連 secret 一起刪(注意:Secret 有 7 天 recovery window)
aws secretsmanager delete-secret --secret-id limitless/limitless/api-token-id --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id limitless/limitless/api-secret --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id limitless/limitless/base-private-key --force-delete-without-recovery
```

## 安全

### 私鑰保護層級

1. **獨立 wallet**:私鑰只放這次做市用的 USDC,**不要重用主錢包**
2. **Secrets Manager 加密**:AWS KMS 加密 at rest,只有 ECS task role 能讀
3. **Task role 最小權限**:只能讀指定 3 個 secret,沒其他 AWS 權限
4. **不出現在 task definition 明文**:`ecs.Secret.from_secrets_manager` 確保 secret 不會出現在 ECS console / API 回應
5. **容器非 root 跑**:Dockerfile 已用 `limitless` 使用者

### 安全限額(雙保險)

| 層 | 機制 | 預設 |
|---|---|---|
| 1. CDK env | `LIMITLESS_EXECUTE` | 部署時 `STACK_EXECUTE=1` 才設 1 |
| 2. limitless | `LIMITLESS_EXECUTE != 1` → 永遠 dry-run | 雙保險 |
| 3. limitless | `LIMITLESS_MAX_PER_ORDER`(單筆) | 容器內預設 $30 |
| 4. limitless | `LIMITLESS_MAX_PER_SESSION`(累計) | 容器內預設 $500 |
| 5. mm-loop | `MM_LOOP_TOTAL_CAPITAL`(全域) | $500 |

要 100% 確保 dry-run,部署時**不要**設 `STACK_EXECUTE=1`。

### 第一次部署建議流程

1. **先 dry-run 跑 24 小時** → 看 CloudWatch 上行為合不合理
2. 確認 mm-rank 挑的市場合理、報價合理、沒亂發 API
3. 確認沒任何 error
4. **再** `STACK_EXECUTE=1 cdk deploy` 切真實
5. 一開始把 `MM_LOOP_TOTAL_CAPITAL` 設 $50-100 練手
6. 跑 1 週後再決定要不要加碼

## 故障排除

### Task 一直 fail / restart

```bash
aws ecs describe-services --cluster limitless-mm-loop --services limitless-mm-loop \
  --query 'services[0].events[0:5]'
```

最常見原因:
- Secret 沒填值 → task 起不來。回到步驟 6 把 secret 填好。
- 私鑰格式錯(沒 0x 前綴或長度不對) → ecs logs 看 `Web3 / EOA` 相關 error
- HMAC token 過期 → 重新跑本機 `limitless limitless auth-derive`,更新 secret

### CloudWatch 看不到 log

```bash
# 確認 log group 存在
aws logs describe-log-groups --log-group-name-prefix /limitless/mm-loop
```

### 改了程式但容器還跑舊版

```bash
# CDK 偵測不到 limitless/ 的變化是很罕見的;強制重 build
.venv/bin/cdk deploy --force
```

## 不在這個 stack 裡的東西(未來可加)

- **Telegram / Discord 告警**:每個 emergency_close、每天 PnL 摘要
- **CloudWatch Alarm**:task 連續 fail / log 內有 ERROR
- **每日 PnL 報表**:跑 Polymarket data API 對比 LM 庫存
- **多 region failover**:單區 Fargate 掛了還能存活
- **WAF / VPC endpoint**:reduce 公開 IP 暴露面

這些都不影響核心功能。先把基本版跑起來,確認真的能賺錢再加。
