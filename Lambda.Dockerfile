# AWS Lambda container image for polymkt serverless 做市
#
# 兩個 Lambda(iterate / rerank)共用同一個 image,差別只在 CMD,
# 由 CDK 的 LambdaFunction 設定 image_config.command 覆寫。

FROM public.ecr.aws/lambda/python:3.12

# 依賴 + 本專案
COPY pyproject.toml README.md ${LAMBDA_TASK_ROOT}/
COPY polymkt ${LAMBDA_TASK_ROOT}/polymkt
COPY lambda_handlers ${LAMBDA_TASK_ROOT}/lambda_handlers

# 安裝 polymkt(含 trade extra)+ boto3(Lambda runtime 已內建,但確保版本)
# --no-cache-dir 縮小 image
WORKDIR ${LAMBDA_TASK_ROOT}
RUN pip install --no-cache-dir -e '.[trade]'

# 預設 CMD 是 iterate;rerank Lambda 會在 CDK 覆寫為 lambda_handlers.rerank.handler
CMD ["lambda_handlers.iterate.handler"]
