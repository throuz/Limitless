"""Serverless stack:Lambda + DynamoDB + EventBridge + SSM + Billing Alarm。

組件:
- DynamoDB(on-demand)— 存 ToxicityState、active markets、global capital
- Lambda iterate(container image,Python 3.12)— 每 N 秒跑一次
- Lambda rerank(同 image,不同 CMD)— 每小時跑一次
- EventBridge Rules — 觸發兩個 Lambda
- SSM Parameter Store SecureString × 3 — 跟 free_tier_stack 共用 secret 路徑
- CloudWatch Logs(Lambda 自動)
- Billing Alarm($1 警報)
- Reserved Concurrency = 1(防止 invocation 重疊)

預估月成本:
- Lambda 請求數(43k/月) → $0(1M free)
- Lambda 計算(~55k GB-sec) → $0(400k free)
- DynamoDB(on-demand, 260k RW)→ ~$0.40
- EventBridge → $0(14M free)
- CloudWatch Logs → $0(5 GB free)
- ECR storage(< 500 MB,12 個月免費後 ~$0.05) → $0-0.05
- **總計 $0 - $0.50/月**
"""

from __future__ import annotations

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_cloudwatch as cw,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_ssm as ssm,
)
from constructs import Construct


# SSM SecureString 不能用 CloudFormation 創建(CFN 限制),所以用 dataclass 描述
# 部署前/後需要手動跑 aws ssm put-parameter 創 SecureString。
from dataclasses import dataclass


@dataclass
class _ParamRef:
    name: str
    description: str


# CDK 不創 SSM Parameter,只引用名稱;部署後用 aws CLI 手動 put SecureString
PARAM_TOKEN_ID = _ParamRef("/limitless/api-token-id", "Limitless HMAC token id")
PARAM_API_SECRET = _ParamRef("/limitless/api-secret", "Limitless HMAC secret")
PARAM_PRIV_KEY = _ParamRef("/limitless/base-private-key", "Base 鏈 EOA 私鑰")
PARAM_TG_TOKEN = _ParamRef("/limitless/telegram-bot-token", "Telegram bot token(可選)")
PARAM_TG_CHAT = _ParamRef("/limitless/telegram-chat-id", "Telegram chat id(可選)")


class LimitlessMmLoopServerlessStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        execute_real_orders: bool = False,
        iteration_seconds: int = 120,
        rerank_minutes: int = 60,
        alarm_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------- 1. SSM SecureString secrets ----------
        # 注意:SecureString 不能用 CloudFormation 創,要部署後跑 aws ssm put-parameter
        # CDK 只引用名字(env var 注入 Lambda + IAM 政策)
        token_id = PARAM_TOKEN_ID
        api_secret = PARAM_API_SECRET
        priv_key = PARAM_PRIV_KEY
        telegram_token = PARAM_TG_TOKEN
        telegram_chat = PARAM_TG_CHAT

        # ---------- 2. DynamoDB(provisioned 25 RCU + 25 WCU 永久免費)----------
        state_table = ddb.Table(
            self, "MmStateTable",
            table_name="limitless-mm-state",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PROVISIONED,
            read_capacity=25,                               # always-free 上限
            write_capacity=25,                              # always-free 上限
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery=False,                   # 開了會收費
        )

        # ---------- 3. Lambda ZIP code (從 infra/lambda_build/ 載入)----------
        # 用 ZIP-based Lambda(不是 container image)→ 不需要 ECR → 真 $0
        # 部署前必須先跑:./infra/build_lambda.sh 把 manylinux 相容 deps 包好
        import os.path as _osp
        BUILD_DIR = _osp.join(_osp.dirname(__file__), "..", "lambda_build")
        if not _osp.isdir(BUILD_DIR):
            raise RuntimeError(
                f"找不到 {BUILD_DIR}。先跑:./infra/build_lambda.sh"
            )
        lambda_code = lambda_.Code.from_asset(BUILD_DIR)

        # 共用 env(兩個 Lambda 都一樣)
        common_env = {
            "DDB_TABLE_NAME": state_table.table_name,
            "LIMITLESS_EXECUTE": "1" if execute_real_orders else "0",
            "LIMITLESS_MAX_PER_ORDER": "30",
            "LIMITLESS_MAX_PER_SESSION": "500",      # 安全上限,實際 cap 由鏈上動態決定
            "MM_LOOP_TOTAL_CAPITAL": "100",          # 只在 chain read 失敗時 fallback
            "MM_LOOP_MAX_POSITIONS": "3",
            "MM_LOOP_CAPITAL_PER_MARKET": "33",      # fallback only(=total/positions)
            "MM_LOOP_QUOTE_SIZE": "10",
            "MM_LOOP_TARGET_PROFIT_PCT": "4",
            "MM_LOOP_HALF_SPREAD_PCT": "1",
            "MM_LOOP_MAX_INVENTORY": "30",
            "MM_LOOP_RANK_MAX_MARKETS": "500",
            "MM_LOOP_RANK_MIN_VOLUME": "200",
            "MM_LOOP_RANK_MIN_DAYS": "2",
            "MM_LOOP_RANK_MIN_SPREAD_BPS": "100",
            "MM_LOOP_RANK_MAX_NEWS_RISK": "2",
            "MM_LOOP_ITER_SLEEP_S": str(iteration_seconds),
            "MM_LOOP_ORACLE": "pm",
            "MM_LOOP_USE_MICROPRICE": "1",
            "MM_LOOP_EMERGENCY_HOURS": "24",
            # SSM Parameter 名(secrets handler 從這裡讀)
            "SSM_TOKEN_ID_NAME": token_id.name,
            "SSM_API_SECRET_NAME": api_secret.name,
            "SSM_PRIV_KEY_NAME": priv_key.name,
            # Telegram(可選)
            "SSM_TELEGRAM_BOT_TOKEN_NAME": telegram_token.name,
            "SSM_TELEGRAM_CHAT_ID_NAME": telegram_chat.name,
        }

        # 共用 secrets — Lambda env var 注入(從 SSM)
        # Lambda 沒有像 ECS 那種「secret env from SSM」直接整合。
        # 解法:在 Lambda function 啟動時自己呼叫 SSM API。
        # 為了避免每個 invocation 都呼叫 SSM,我們改用「同步注入」做法:
        # 把 SSM 值 deploy 時直接 inject 為 Lambda env(透過 CDK CustomResource)
        # 但這會把秘密寫進 CloudFormation,不安全。
        # 妥協方案:Lambda 用 SSM 名做 env var,程式 cold start 時讀 SSM 寫進 os.environ,
        # 同一 Lambda container 重用之間就免再讀(boto3 自然 cache layer)。
        # 已在 ServerlessCfg / LimitlessTradingClient 整合(於 handler 開頭注入 env)。
        # 不過為了簡化(且 SSM Standard Parameter API 永久免費),
        # 我們在 handler entry 直接讀 SSM 並注入 os.environ。
        # → 因此這裡只給 IAM 權限,handler 自己處理 fetch。

        iterate_fn = lambda_.Function(
            self, "IterateFunction",
            function_name="limitless-mm-iterate",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_code,
            handler="lambda_handlers.iterate.handler",
            memory_size=512,
            timeout=Duration.seconds(60),                 # 一輪 iterate 應該 < 60s
            environment=common_env,
            reserved_concurrent_executions=1,             # 防止重疊
            log_retention=logs.RetentionDays.TWO_WEEKS,   # 短一點省 CloudWatch
            tracing=lambda_.Tracing.DISABLED,             # X-Ray 不免費
        )

        rerank_fn = lambda_.Function(
            self, "RerankFunction",
            function_name="limitless-mm-rerank",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_code,
            handler="lambda_handlers.rerank.handler",
            memory_size=512,
            timeout=Duration.seconds(180),                # rerank 要撈很多 orderbook
            environment=common_env,
            reserved_concurrent_executions=1,
            log_retention=logs.RetentionDays.TWO_WEEKS,
            tracing=lambda_.Tracing.DISABLED,
        )

        # 每日 Telegram 摘要(每 24h 一次)
        daily_summary_fn = lambda_.Function(
            self, "DailySummaryFunction",
            function_name="limitless-mm-daily-summary",
            runtime=lambda_.Runtime.PYTHON_3_12,
            code=lambda_code,
            handler="lambda_handlers.daily_summary.handler",
            memory_size=512,
            timeout=Duration.seconds(60),
            environment=common_env,
            reserved_concurrent_executions=1,
            log_retention=logs.RetentionDays.TWO_WEEKS,
            tracing=lambda_.Tracing.DISABLED,
        )

        # ---------- 4. IAM:DDB + SSM 讀權限 ----------
        for fn in (iterate_fn, rerank_fn, daily_summary_fn):
            state_table.grant_read_write_data(fn)
            for p in (token_id, api_secret, priv_key, telegram_token, telegram_chat):
                fn.add_to_role_policy(iam.PolicyStatement(
                    actions=["ssm:GetParameter"],
                    resources=[self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=p.name.lstrip("/"),
                    )],
                ))
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=["*"],
                conditions={
                    "StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}
                },
            ))

        # ---------- 5. EventBridge Rules ----------
        iterate_rule = events.Rule(
            self, "IterateSchedule",
            rule_name="limitless-mm-iterate",
            schedule=events.Schedule.rate(Duration.seconds(iteration_seconds)),
            description=f"每 {iteration_seconds}s 觸發 iterate Lambda",
        )
        iterate_rule.add_target(targets.LambdaFunction(iterate_fn,
            retry_attempts=0,   # 不重試,失敗就等下一輪
        ))

        rerank_rule = events.Rule(
            self, "RerankSchedule",
            rule_name="limitless-mm-rerank",
            schedule=events.Schedule.rate(Duration.minutes(rerank_minutes)),
            description=f"每 {rerank_minutes} 分鐘觸發 rerank Lambda",
        )
        rerank_rule.add_target(targets.LambdaFunction(rerank_fn, retry_attempts=0))

        # 每日 22:00 UTC(台灣 06:00)觸發摘要
        daily_summary_rule = events.Rule(
            self, "DailySummarySchedule",
            rule_name="limitless-mm-daily-summary",
            schedule=events.Schedule.cron(hour="22", minute="0"),
            description="每日 UTC 22:00(台灣 06:00)發 Telegram 摘要",
        )
        daily_summary_rule.add_target(targets.LambdaFunction(daily_summary_fn, retry_attempts=0))

        # ---------- 6. Billing Alarm($1) + 可選 email 訂閱 ----------
        # CloudWatch Billing metric 必須在 us-east-1。
        # 跨 region 監控用 metric stream 或直接 us-east-1 stack。
        # 這裡用簡化版:Lambda invocation 異常 alarm(invocations 飆高 → 可能 bug)
        # 真正的 $1 alarm 要去 AWS Budget Console 手動設(README 會教)
        for fn in (iterate_fn, rerank_fn, daily_summary_fn):
            cw.Alarm(
                self, f"{fn.node.id}Errors",
                metric=fn.metric_errors(period=Duration.minutes(15)),
                threshold=3,
                evaluation_periods=1,
                datapoints_to_alarm=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description=f"{fn.function_name} 連續 errors > 3 in 15 min",
            )
            cw.Alarm(
                self, f"{fn.node.id}Throttles",
                metric=fn.metric_throttles(period=Duration.minutes(15)),
                threshold=5,
                evaluation_periods=1,
                datapoints_to_alarm=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description=f"{fn.function_name} 被 throttle 表示有 invocation 重疊",
            )

        if alarm_email:
            topic = sns.Topic(self, "AlarmTopic", topic_name="limitless-mm-alarm")
            topic.add_subscription(sns_subs.EmailSubscription(alarm_email))
            CfnOutput(self, "AlarmTopicArn", value=topic.topic_arn)

        # ---------- Outputs ----------
        CfnOutput(self, "TableName", value=state_table.table_name)
        CfnOutput(self, "IterateFunctionName", value=iterate_fn.function_name)
        CfnOutput(self, "RerankFunctionName", value=rerank_fn.function_name)
        CfnOutput(
            self,
            "SetSecretsCommand",
            value=(
                "aws ssm put-parameter --overwrite --type SecureString "
                f"--name {token_id.name} --value <token-id>  && "
                "aws ssm put-parameter --overwrite --type SecureString "
                f"--name {api_secret.name} --value <secret>  && "
                "aws ssm put-parameter --overwrite --type SecureString "
                f"--name {priv_key.name} --value 0x<priv-key>"
            ),
            description="部署完跑這串填 secret(serverless 改 secret 後不用重啟,下次 Lambda 觸發自動讀)",
        )
        CfnOutput(
            self,
            "TailIterateLogsCommand",
            value=f"aws logs tail /aws/lambda/{iterate_fn.function_name} --follow",
        )
        CfnOutput(
            self,
            "TailRerankLogsCommand",
            value=f"aws logs tail /aws/lambda/{rerank_fn.function_name} --follow",
        )
        CfnOutput(
            self,
            "ManualInvokeRerank",
            value=(
                f"aws lambda invoke --function-name {rerank_fn.function_name} "
                "--invocation-type RequestResponse /tmp/out.json && cat /tmp/out.json"
            ),
            description="手動觸發 rerank 立即挑市場(不等下一輪 EventBridge)",
        )
        CfnOutput(
            self,
            "BudgetSetupNote",
            value="去 AWS Console → Billing → Budgets 設 $5/月 hard cap 警報",
        )
