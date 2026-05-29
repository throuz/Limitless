"""CDK entry point。

三種 stack(用 STACK_TIER 環境變數切換):

- `STACK_TIER=serverless`(預設,**真正 $0/月**)
    Lambda + DynamoDB + EventBridge + SSM Parameter Store
    完全在 always-free 額度內,不靠 12 個月新帳號優惠

- `STACK_TIER=free` — EC2 t3.micro + SSM
    第一年 $0(12 個月新帳號免費期),之後 ~$7.50/月

- `STACK_TIER=managed` — ECS Fargate + Secrets Manager
    ~$11/月,zero-ops,真正 managed

部署:
    .venv/bin/cdk deploy                           # serverless(預設)
    STACK_TIER=free .venv/bin/cdk deploy           # EC2
    STACK_TIER=managed .venv/bin/cdk deploy        # Fargate
    STACK_EXECUTE=1 .venv/bin/cdk deploy           # 真實下單
    ALARM_EMAIL=you@example.com .venv/bin/cdk deploy  # 加 email 警報(serverless only)
"""

from __future__ import annotations

import os
import aws_cdk as cdk

from limitless_infra.stack import LimitlessMmLoopStack
from limitless_infra.free_tier_stack import LimitlessMmLoopFreeStack
from limitless_infra.serverless_stack import LimitlessMmLoopServerlessStack


app = cdk.App()

account = os.environ.get("CDK_ACCOUNT") or os.environ.get("CDK_DEFAULT_ACCOUNT")
region = (os.environ.get("CDK_REGION")
          or os.environ.get("CDK_DEFAULT_REGION")
          or "ap-northeast-1")

tier = os.environ.get("STACK_TIER", "serverless").lower()
execute_real = os.environ.get("STACK_EXECUTE", "0") == "1"
alarm_email = os.environ.get("ALARM_EMAIL") or None

env = cdk.Environment(account=account, region=region)

if tier == "managed":
    LimitlessMmLoopStack(
        app, "LimitlessMmLoopStack",
        env=env,
        execute_real_orders=execute_real,
    )
elif tier in ("free", "free-tier", "ec2"):
    LimitlessMmLoopFreeStack(
        app, "LimitlessMmLoopFreeStack",
        env=env,
        execute_real_orders=execute_real,
    )
elif tier in ("serverless", "lambda"):
    LimitlessMmLoopServerlessStack(
        app, "LimitlessMmLoopServerlessStack",
        env=env,
        execute_real_orders=execute_real,
        iteration_seconds=int(os.environ.get("ITERATION_SECONDS", "120")),
        rerank_minutes=int(os.environ.get("RERANK_MINUTES", "60")),
        alarm_email=alarm_email,
    )
else:
    raise ValueError(f"STACK_TIER={tier!r} 不認得;接受 serverless / free / managed")

app.synth()
