#!/bin/bash
# Healthcheck: проверяет живость polling-цикла по heartbeat-файлу
# Бот обновляет /data/heartbeat при каждом polling-цикле.
# Если файл не обновлялся более STALE_SECONDS секунд — бот завис.

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/data/heartbeat}"
STALE_SECONDS=120  # 2 минуты без обновления = зависание

if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "UNHEALTHY: heartbeat file not found"
    exit 1
fi

LAST_MODIFIED=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null)
NOW=$(date +%s)
AGE=$((NOW - LAST_MODIFIED))

if [ "$AGE" -gt "$STALE_SECONDS" ]; then
    echo "UNHEALTHY: heartbeat stale for ${AGE}s (threshold: ${STALE_SECONDS}s)"
    exit 1
fi

echo "HEALTHY: heartbeat age ${AGE}s"
exit 0
