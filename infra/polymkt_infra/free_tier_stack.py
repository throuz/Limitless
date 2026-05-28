"""EC2 free-tier 24/7 做市 stack(對等於 PolymktMmLoopStack,但全用免費資源)。

差別 vs Fargate stack:
- ~~Fargate~~ → **EC2 t3.micro**(12 個月免費,之後 ~$7.50/月)
- ~~Secrets Manager~~ → **SSM Parameter Store SecureString**(永久免費)
- ~~ECR~~ → **S3 asset**(CDK 把 polymkt source 包成 zip 上傳;EC2 啟動時下載 + 本機 docker build)
- 其餘相同:CloudWatch Logs / VPC default

預估月成本:
- 前 12 個月:$0
- 之後:~$7.50/月(EC2 t3.micro + EBS gp2)

維運差別:
- 你要負責 OS 安全更新(`sudo dnf update` 或讓它 auto-update)
- 改秘密後要重啟 EC2 一次(systemd 才重新讀 /etc/polymkt/env)
- 改 polymkt source code → 重 `cdk deploy` 會替換 EC2(新 image / 新 user-data)

Session Manager 進去看現場(不用開 SSH port):
    aws ssm start-session --target <INSTANCE_ID>
"""

from __future__ import annotations

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3_assets as s3_assets,
    aws_ssm as ssm,
)
from constructs import Construct


# 把 SSM Parameter Store 用 SecureString 必須走 escape hatch(L1)。L2 不支援。
def _create_secure_string_parameter(scope: Stack, id_: str, *,
                                    name: str, description: str) -> ssm.CfnParameter:
    return ssm.CfnParameter(
        scope, id_,
        name=name,
        type="SecureString",   # L2 沒這個選項
        value="REPLACE_ME",    # 必填,部署後手動 put-parameter 覆蓋
        description=description,
        tier="Standard",       # standard 免費,advanced 才會收費
    )


class PolymktMmLoopFreeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        execute_real_orders: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------- 1. SSM Parameter Store(SecureString,永久免費)----------
        token_id_param = _create_secure_string_parameter(
            self, "LimitlessApiTokenIdParam",
            name="/polymkt/limitless/api-token-id",
            description="Limitless HMAC token id",
        )
        api_secret_param = _create_secure_string_parameter(
            self, "LimitlessApiSecretParam",
            name="/polymkt/limitless/api-secret",
            description="Limitless HMAC secret",
        )
        private_key_param = _create_secure_string_parameter(
            self, "BasePrivateKeyParam",
            name="/polymkt/limitless/base-private-key",
            description="Base 鏈 EOA 私鑰",
        )

        # ---------- 2. CloudWatch Log Group ----------
        log_group = logs.LogGroup(
            self, "MmLoopLogs",
            log_group_name="/polymkt/mm-loop",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------- 3. S3 asset:把整個 repo 打包成 zip ----------
        # CDK 自動 zip + upload + 在 user-data 注入下載 URL
        source_asset = s3_assets.Asset(
            self, "PolymktSource",
            path="..",   # repo root(infra 上一層)
            exclude=[
                ".venv",
                "infra/.venv",
                "infra/cdk.out",
                "**/__pycache__",
                "**/*.pyc",
                ".git",
                ".env",
                ".env.*",
                "*.egg-info",
                ".DS_Store",
            ],
        )

        # ---------- 4. VPC + Security Group(只出不進)----------
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
        sg = ec2.SecurityGroup(
            self, "MmLoopSg",
            vpc=vpc,
            description="polymkt mm-loop egress-only(SSM Session Manager 不用 inbound)",
            allow_all_outbound=True,
        )
        # 不開任何 inbound;用 SSM Session Manager 進去(走 outbound 連 AWS endpoint)

        # ---------- 5. IAM role:SSM + Parameter Store + Logs + S3 asset ----------
        role = iam.Role(
            self, "MmLoopInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                # SSM Session Manager + Patch Manager + 基本 instance management
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                # 寫 CloudWatch Logs(CloudWatch agent 用)
                iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"),
            ],
        )
        # 允許讀指定三個 SSM Parameter
        for p in (token_id_param, api_secret_param, private_key_param):
            role.add_to_policy(iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[self.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name=p.name.lstrip("/"),
                )],
            ))
        # 允許 KMS 解密(SSM SecureString 用 AWS-managed key)
        role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt"],
            resources=["*"],   # AWS-managed key 不能限定 ARN
            conditions={
                "StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}
            },
        ))
        # 允許讀 S3 asset
        source_asset.grant_read(role)

        # ---------- 6. EC2 user-data(裝 Docker、build、systemd 服務)----------
        execute_flag = "1" if execute_real_orders else "0"
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -euo pipefail",
            "exec > >(tee /var/log/polymkt-bootstrap.log) 2>&1",
            "echo '[boot] start: $(date -Iseconds)'",

            # Amazon Linux 2023 用 dnf
            "dnf update -y",
            "dnf install -y docker unzip awscli",
            "systemctl enable --now docker",

            # 下載 polymkt source(CDK token 替換成真實 S3 URL)
            "mkdir -p /opt/polymkt",
            f"aws s3 cp s3://{source_asset.s3_bucket_name}/{source_asset.s3_object_key} /tmp/polymkt.zip",
            "unzip -o /tmp/polymkt.zip -d /opt/polymkt",
            "rm /tmp/polymkt.zip",

            # 從 SSM 拉 secrets 寫到 /etc/polymkt/env(只 root 可讀)
            "mkdir -p /etc/polymkt && chmod 700 /etc/polymkt",
            f"REGION={self.region}",
            "TOKEN_ID=$(aws ssm get-parameter --name /polymkt/limitless/api-token-id --with-decryption --query Parameter.Value --output text --region $REGION)",
            "API_SECRET=$(aws ssm get-parameter --name /polymkt/limitless/api-secret --with-decryption --query Parameter.Value --output text --region $REGION)",
            "PRIV_KEY=$(aws ssm get-parameter --name /polymkt/limitless/base-private-key --with-decryption --query Parameter.Value --output text --region $REGION)",
            "cat > /etc/polymkt/env <<EOF",
            "LIMITLESS_API_TOKEN_ID=$TOKEN_ID",
            "LIMITLESS_API_SECRET=$API_SECRET",
            "BASE_PRIVATE_KEY=$PRIV_KEY",
            f"LIMITLESS_EXECUTE={execute_flag}",
            "LIMITLESS_MAX_PER_ORDER=30",
            "LIMITLESS_MAX_PER_SESSION=500",
            "MM_LOOP_TOTAL_CAPITAL=500",
            "MM_LOOP_MAX_POSITIONS=3",
            "MM_LOOP_CAPITAL_PER_MARKET=100",
            "MM_LOOP_QUOTE_SIZE=10",
            "MM_LOOP_TARGET_PROFIT_PCT=4",
            "MM_LOOP_HALF_SPREAD_PCT=1",
            "MM_LOOP_MAX_INVENTORY=30",
            "MM_LOOP_RANK_REFRESH_S=3600",
            "MM_LOOP_RANK_MIN_VOLUME=200",
            "MM_LOOP_RANK_MIN_DAYS=2",
            "MM_LOOP_RANK_MIN_SPREAD_BPS=100",
            "MM_LOOP_RANK_MAX_NEWS_RISK=2",
            "MM_LOOP_ITER_SLEEP_S=30",
            "MM_LOOP_ORACLE=pm",
            "MM_LOOP_USE_MICROPRICE=1",
            "MM_LOOP_EMERGENCY_HOURS=24",
            "EOF",
            "chmod 600 /etc/polymkt/env",

            # Build image
            "cd /opt/polymkt && docker build -t polymkt:latest .",

            # systemd unit
            "cat > /etc/systemd/system/polymkt-mm.service <<'EOF'",
            "[Unit]",
            "Description=polymkt mm-loop 24/7 market maker",
            "After=docker.service network-online.target",
            "Requires=docker.service",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Restart=always",
            "RestartSec=15",
            "TimeoutStopSec=70",
            "ExecStartPre=-/usr/bin/docker rm -f polymkt-mm",
            "ExecStart=/usr/bin/docker run --rm --name polymkt-mm "
                "--env-file /etc/polymkt/env "
                "--log-driver=awslogs "
                f"--log-opt awslogs-region={self.region} "
                f"--log-opt awslogs-group={log_group.log_group_name} "
                "--log-opt awslogs-stream=mm-loop-$(hostname) "
                "polymkt:latest",
            "ExecStop=/usr/bin/docker stop -t 60 polymkt-mm",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOF",
            "systemctl daemon-reload",
            "systemctl enable --now polymkt-mm",
            "echo '[boot] done: $(date -Iseconds)'",
        )

        # ---------- 7. EC2 instance ----------
        instance = ec2.Instance(
            self, "MmLoopInstance",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3,
                ec2.InstanceSize.MICRO,
            ),   # t3.micro:free tier 12 個月
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=sg,
            role=role,
            user_data=user_data,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size=12,        # 留點 buffer 給 Docker layer
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                ),
            ],
            user_data_causes_replacement=True,
        )

        # ---------- Outputs ----------
        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(
            self,
            "SetSecretsCommand",
            value=(
                "aws ssm put-parameter --overwrite --type SecureString "
                f"--name /polymkt/limitless/api-token-id --value <token-id>  && "
                "aws ssm put-parameter --overwrite --type SecureString "
                f"--name /polymkt/limitless/api-secret --value <secret>  && "
                "aws ssm put-parameter --overwrite --type SecureString "
                f"--name /polymkt/limitless/base-private-key --value 0x<priv-key>"
            ),
            description="部署完跑這串(替換三個值)。改完要 reboot instance 才生效。",
        )
        CfnOutput(
            self,
            "RebootCommand",
            value=f"aws ec2 reboot-instances --instance-ids {instance.instance_id}",
            description="改 secret 後重啟讀新值",
        )
        CfnOutput(
            self,
            "SessionManagerCommand",
            value=f"aws ssm start-session --target {instance.instance_id}",
            description="進 EC2 看現場(免 SSH key)",
        )
        CfnOutput(
            self,
            "TailLogsCommand",
            value=f"aws logs tail {log_group.log_group_name} --follow",
        )
