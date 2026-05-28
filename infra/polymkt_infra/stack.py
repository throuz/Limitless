"""ECS Fargate 24/7 做市調度 stack。

架構：
- 1 個 ECS Fargate Service(desired_count=1,永遠跑)
- Task definition 拉 Docker image(由 CDK 自動 build & push 到 ECR)
- 3 個 Secrets Manager secret(LIMITLESS_API_TOKEN_ID / _SECRET / BASE_PRIVATE_KEY)
  → 透過 ECS secret 機制注入為 env var,**不會出現在 task definition 明文**
- CloudWatch Log Group 收 stdout,1 個月過期
- 用 default VPC(避免額外 NAT Gateway 費用)
- 不開 inbound port,只出不進

預估月成本(ap-northeast-1):
- Fargate 0.25 vCPU + 0.5 GB × 730h ≈ $8.76
- Secrets Manager 3 × $0.40 = $1.20
- CloudWatch Logs(ingest + storage) ≈ $0.50
- ECR storage(<1 GB) ≈ $0.10
- 資料傳輸 ≈ $1
- **總計 $11-12/月**

安全要點(看 README.md「安全」章節更多):
- Secret 建立時為**空值**,CDK 部署完你必須去 AWS console 手動填入
- Task Role 只允許讀指定的 3 個 secret,無其他權限
- 容器以非 root 使用者跑(Dockerfile 已處理)
"""

from __future__ import annotations

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
    aws_secretsmanager as sm,
)
from constructs import Construct


class PolymktMmLoopStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        execute_real_orders: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------- 1. Secrets ----------
        # 建立空 secret;CDK 部署完你要手動去 AWS console 填值。
        # 用 SecretStringTemplate 不會把實際值寫進 CloudFormation。
        token_id_secret = sm.Secret(
            self,
            "LimitlessApiTokenId",
            secret_name="polymkt/limitless/api-token-id",
            description="Limitless HMAC token id (從 polymkt limitless auth-derive 取得)",
            removal_policy=RemovalPolicy.RETAIN,  # 別跟 stack 一起刪
        )
        api_secret = sm.Secret(
            self,
            "LimitlessApiSecret",
            secret_name="polymkt/limitless/api-secret",
            description="Limitless HMAC secret",
            removal_policy=RemovalPolicy.RETAIN,
        )
        private_key_secret = sm.Secret(
            self,
            "BasePrivateKey",
            secret_name="polymkt/limitless/base-private-key",
            description="Base 鏈 EOA 私鑰(獨立 wallet,只放下單金額)",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------- 2. CloudWatch Log Group ----------
        log_group = logs.LogGroup(
            self,
            "MmLoopLogs",
            log_group_name="/polymkt/mm-loop",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------- 3. VPC ----------
        # 使用 default VPC 省 NAT Gateway 費。Fargate task 需要公開 IP 出去打 API。
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        # ---------- 4. ECR + Docker image ----------
        # CDK 直接 build 並 push 到 ECR(SDK-managed asset)
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "MmLoopImage",
            directory="..",  # 從 infra/ 上去到 repo root 才有 Dockerfile
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # ---------- 5. ECS Cluster ----------
        cluster = ecs.Cluster(
            self,
            "MmLoopCluster",
            vpc=vpc,
            cluster_name="polymkt-mm-loop",
            container_insights=False,  # 開了會增加 CloudWatch 費用
        )

        # ---------- 6. Task Definition ----------
        task_def = ecs.FargateTaskDefinition(
            self,
            "MmLoopTask",
            cpu=256,           # 0.25 vCPU
            memory_limit_mib=512,
            family="polymkt-mm-loop",
        )

        # 允許 task role 讀指定的三個 secret
        for sec in (token_id_secret, api_secret, private_key_secret):
            sec.grant_read(task_def.task_role)

        # 容器設定
        container = task_def.add_container(
            "mm-loop",
            image=ecs.ContainerImage.from_docker_image_asset(image_asset),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="mm-loop",
                log_group=log_group,
            ),
            # 環境變數(明文 OK 的)
            environment={
                # 全域行為 — 改這些不用重 build,只要 ECS update-service --force-new-deployment
                "MM_LOOP_TOTAL_CAPITAL": "500",
                "MM_LOOP_MAX_POSITIONS": "3",
                "MM_LOOP_CAPITAL_PER_MARKET": "100",
                "MM_LOOP_QUOTE_SIZE": "10",
                "MM_LOOP_TARGET_PROFIT_PCT": "4",
                "MM_LOOP_HALF_SPREAD_PCT": "1",
                "MM_LOOP_MAX_INVENTORY": "30",
                "MM_LOOP_RANK_REFRESH_S": "3600",
                "MM_LOOP_RANK_MIN_VOLUME": "200",
                "MM_LOOP_RANK_MIN_DAYS": "2",
                "MM_LOOP_RANK_MIN_SPREAD_BPS": "100",
                "MM_LOOP_RANK_MAX_NEWS_RISK": "2",
                "MM_LOOP_ITER_SLEEP_S": "30",
                "MM_LOOP_ORACLE": "pm",
                "MM_LOOP_USE_MICROPRICE": "1",
                "MM_LOOP_EMERGENCY_HOURS": "24",
                # 容器內仍尊重 polymkt 內建的雙保險;改 1 才真實下單。
                "LIMITLESS_EXECUTE": "1" if execute_real_orders else "0",
                # SafetyLimits
                "LIMITLESS_MAX_PER_ORDER": "30",      # 比預設 50 緊一點,容器內保守
                "LIMITLESS_MAX_PER_SESSION": "500",   # 與 total_capital 對齊
            },
            # 從 Secrets Manager 注入(不會出現在 task definition 明文)
            secrets={
                "LIMITLESS_API_TOKEN_ID": ecs.Secret.from_secrets_manager(token_id_secret),
                "LIMITLESS_API_SECRET": ecs.Secret.from_secrets_manager(api_secret),
                "BASE_PRIVATE_KEY": ecs.Secret.from_secrets_manager(private_key_secret),
            },
            # SIGTERM → 給容器 60 秒收乾淨
            stop_timeout=Duration.seconds(60),
        )

        # ---------- 7. ECS Service ----------
        service = ecs.FargateService(
            self,
            "MmLoopService",
            cluster=cluster,
            task_definition=task_def,
            service_name="polymkt-mm-loop",
            desired_count=1,
            assign_public_ip=True,   # default VPC 沒 NAT → 直接給 task public IP 出去
            min_healthy_percent=0,    # 單實例服務,允許 0% 再起一個
            max_healthy_percent=100,
            enable_execute_command=True,  # 允許 ecs exec 進去看現場
            # SIGTERM 後給 60s
            health_check_grace_period=None,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        # ---------- Outputs ----------
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "ServiceName", value=service.service_name)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(
            self,
            "SetSecretsCommand",
            value=(
                f"aws secretsmanager put-secret-value "
                f"--secret-id {token_id_secret.secret_name} "
                f"--secret-string <token-id>  &&  "
                f"aws secretsmanager put-secret-value "
                f"--secret-id {api_secret.secret_name} "
                f"--secret-string <secret>  &&  "
                f"aws secretsmanager put-secret-value "
                f"--secret-id {private_key_secret.secret_name} "
                f"--secret-string 0x<private-key>"
            ),
            description="部署完跑這串(替換三個值)把 secret 填好",
        )
        CfnOutput(
            self,
            "TailLogsCommand",
            value=f"aws logs tail {log_group.log_group_name} --follow",
        )
        CfnOutput(
            self,
            "ExecCommand",
            value=(
                f"aws ecs execute-command --cluster {cluster.cluster_name} "
                f"--task <TASK_ARN> --container mm-loop --interactive --command /bin/bash"
            ),
        )
