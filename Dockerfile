# limitless mm-loop 容器映像
#
# 設計：
# - python:3.13-slim 基底,小、快
# - 把 limitless 安裝成 editable package(含 [trade]),所以 limitless-sdk 進來
# - 用 tini 當 PID 1 → 轉發 SIGTERM 給 limitless → graceful shutdown 工作
# - 預設執行 mm-loop --from-env --execute(由環境變數控制行為)
# - LIMITLESS_EXECUTE 預設仍要外部明確設,雙保險

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini:小型 init,正確傳遞 SIGTERM,避免 PID 1 zombie 問題
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先複製依賴 metadata 以利 Docker cache layer
COPY pyproject.toml README.md ./
COPY limitless ./limitless

# 含 trade extra(limitless-sdk)
RUN pip install -e '.[trade]'

# 不以 root 跑(基本 hardening)
RUN useradd --create-home --shell /bin/bash limitless
USER limitless

# Healthcheck:輸出能否取得活躍市場
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
    CMD python -c "import asyncio; from limitless.client import LimitlessClient; \
asyncio.run((lambda: LimitlessClient().fetch_active_markets(max_markets=1))()) " || exit 1

ENTRYPOINT ["tini", "--", "python", "-m", "limitless.cli"]
CMD ["limitless", "mm-loop", "--from-env"]
