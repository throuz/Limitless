#!/bin/bash
# 本機 24/7 mm-loop wrapper
#
# 功能:
#   - 自動重啟(若 bot crash)
#   - 寫 log 到 ~/limitless-mm.log,自動 rotate(留最近 7 天)
#   - 接 SIGINT/SIGTERM,完整 propagate 給 mm-loop(讓它 graceful shutdown)
#
# 用法:
#   chmod +x run-mm-local.sh
#   tmux new -s mm
#   ./run-mm-local.sh              # dry-run
#   ./run-mm-local.sh --execute    # 真實下單
#
# 結束:在 tmux 內按 Ctrl-C(會等 mm-loop 收乾淨後退出,不會重啟)

set -u

EXECUTE_FLAG=""
EXEC_ENV=""
if [ "${1:-}" = "--execute" ]; then
    EXECUTE_FLAG="--execute"
    EXEC_ENV="1"
fi

PROJECT_DIR="/Users/mac/Projects/limitless"
LOG_FILE="${HOME}/limitless-mm.log"
MAX_LOG_SIZE_MB=50
MAX_LOG_FILES=7

cd "$PROJECT_DIR"

# Log rotation:每天分檔,留最近 N 天
rotate_log() {
    if [ -f "$LOG_FILE" ]; then
        local size_mb=$(du -m "$LOG_FILE" 2>/dev/null | cut -f1)
        if [ "${size_mb:-0}" -gt "$MAX_LOG_SIZE_MB" ]; then
            local stamp=$(date +%Y%m%d-%H%M%S)
            mv "$LOG_FILE" "${LOG_FILE}.${stamp}"
            # 留最近 N 個
            ls -t "${LOG_FILE}".* 2>/dev/null | tail -n +$((MAX_LOG_FILES + 1)) | xargs rm -f
        fi
    fi
}

# 設定 trap 讓 Ctrl-C 傳給子程序
PID=""
cleanup() {
    echo "[wrapper] 收到中斷信號,通知 mm-loop graceful shutdown..."
    if [ -n "$PID" ]; then
        kill -INT "$PID" 2>/dev/null
        wait "$PID" 2>/dev/null
    fi
    echo "[wrapper] 已退出"
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "[wrapper] 啟動 mm-loop ($([ -n "$EXECUTE_FLAG" ] && echo REAL || echo DRY-RUN))"
echo "[wrapper] log: $LOG_FILE"
echo "[wrapper] 用 Ctrl-C 結束(會等 mm-loop 收乾淨)"
echo ""

RESTART_COUNT=0
while true; do
    rotate_log

    echo "[wrapper] $(date -Iseconds) 啟動 mm-loop (restart #${RESTART_COUNT})" | tee -a "$LOG_FILE"

    LIMITLESS_EXECUTE="$EXEC_ENV" caffeinate -i .venv/bin/python -m limitless.cli mm-loop \
        --total-capital 80 \
        --max-positions 1 \
        --capital-per-market 60 \
        --quote-size 5 \
        --oracle pm \
        --rank-refresh-s 1800 \
        --iter-sleep-s 60 \
        --rank-min-volume 100 \
        --rank-min-days 2.5 \
        --rank-min-spread-bps 80 \
        $EXECUTE_FLAG \
        2>&1 | tee -a "$LOG_FILE" &
    PID=$!
    wait "$PID"
    EXIT_CODE=$?

    echo "[wrapper] $(date -Iseconds) mm-loop 退出 exit=${EXIT_CODE}" | tee -a "$LOG_FILE"

    # 正常退出(0)= 用戶 Ctrl-C → 不重啟
    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "[wrapper] 正常結束,不重啟"
        break
    fi

    # 異常退出 → 等 30 秒重啟(避免 API rate limit 連續打)
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "[wrapper] 30 秒後重啟..."
    sleep 30
done
