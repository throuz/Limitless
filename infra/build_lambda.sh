#!/bin/bash
# 把 Lambda 程式 + 依賴打包成 infra/lambda_build/(CDK 從這裡 from_asset)
#
# 用 --platform manylinux2014_x86_64 + --only-binary=:all: 取得 Linux x86_64 wheel,
# 所以**不需要 Docker** 即可在 macOS 上建出 Lambda Linux 相容的 zip。
#
# 跑法:
#   ./infra/build_lambda.sh
# 或會自動被 cdk deploy 前置呼叫(在 README 步驟)。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_ROOT}/infra/lambda_build"
PYTHON="${PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"

echo "[build] cleaning ${BUILD_DIR}"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "[build] installing Linux x86_64 wheels..."
"$PYTHON" -m pip install --quiet \
    --target "$BUILD_DIR" \
    --platform manylinux2014_x86_64 \
    --only-binary=:all: \
    --implementation cp \
    --python-version 3.12 \
    --upgrade \
    httpx \
    pydantic \
    python-dotenv \
    'limitless-sdk>=1.0.10'

echo "[build] copying source code..."
# 我們的應用程式碼
cp -r "$PROJECT_ROOT/limitless" "$BUILD_DIR/"
cp -r "$PROJECT_ROOT/lambda_handlers" "$BUILD_DIR/"

# 清掉 __pycache__、test 檔、py.typed 等省空間
find "$BUILD_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$BUILD_DIR" -type d -name "tests" -prune -exec rm -rf {} +
find "$BUILD_DIR" -type d -name "*.dist-info" -prune -exec rm -rf {} +
find "$BUILD_DIR" -type f -name "*.pyc" -delete

SIZE=$(du -sh "$BUILD_DIR" | cut -f1)
FILES=$(find "$BUILD_DIR" -type f | wc -l | tr -d ' ')
echo "[build] done: ${SIZE} / ${FILES} files in ${BUILD_DIR}"
echo "[build] Lambda ZIP 上限:250 MB"
