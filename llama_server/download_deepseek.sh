#!/usr/bin/env bash
# ==============================================================================
#  DeepSeek-R1-Distill-Llama-8B GGUF İndirici (llama_server için)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$SCRIPT_DIR/models"
mkdir -p "$TARGET_DIR"

FILE_NAME="DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf"
TARGET_PATH="$TARGET_DIR/$FILE_NAME"
URL="https://huggingface.co/unsloth/DeepSeek-R1-Distill-Llama-8B-GGUF/resolve/main/$FILE_NAME"
EXPECTED_BYTES=4920733856

echo "=========================================================="
echo "  🚀 DeepSeek-R1-Distill-Llama-8B (Q4_K_M) İndirici"
echo "  📁 Hedef: $TARGET_PATH"
echo "=========================================================="

while true; do
    CURRENT=0
    if [ -f "$TARGET_PATH" ]; then
        CURRENT=$(stat -c%s "$TARGET_PATH" 2>/dev/null || echo 0)
    fi

    if [ "$CURRENT" -ge "$EXPECTED_BYTES" ]; then
        echo "✅ $FILE_NAME zaten tam boyutta (4.58 GB)."
        break
    fi

    CUR_GB=$(awk "BEGIN {printf \"%.2f\", $CURRENT / 1073741824}")
    echo "⬇️ İndirme devam ediyor (Mevcut: $CUR_GB GB / 4.58 GB)..."

    nice -n 19 curl --http1.1 -s -S -L -C - --retry 999 --retry-delay 3 --retry-connrefused -o "$TARGET_PATH" "$URL" &
    CURL_PID=$!

    LAST_SIZE=$CURRENT
    LAST_TIME=$(date +%s)

    while kill -0 "$CURL_PID" 2>/dev/null; do
        sleep 10
        NOW_SIZE=0
        if [ -f "$TARGET_PATH" ]; then
            NOW_SIZE=$(stat -c%s "$TARGET_PATH" 2>/dev/null || echo 0)
        fi
        NOW_TIME=$(date +%s)

        TIME_DIFF=$((NOW_TIME - LAST_TIME))
        if [ "$TIME_DIFF" -ge 10 ]; then
            BYTES_DIFF=$((NOW_SIZE - LAST_SIZE))
            SPEED_MB=$(awk "BEGIN {printf \"%.2f\", ($BYTES_DIFF / $TIME_DIFF) / 1048576}")
            NOW_GB=$(awk "BEGIN {printf \"%.2f\", $NOW_SIZE / 1073741824}")
            PCT=$(awk "BEGIN {printf \"%.1f\", ($NOW_SIZE / 4920733856) * 100}")

            echo "  ⏳ İlerleme: $NOW_GB GB / 4.58 GB (%$PCT) | Hız: $SPEED_MB MB/s"

            LAST_SIZE=$NOW_SIZE
            LAST_TIME=$NOW_TIME
        fi
    done

    wait "$CURL_PID"
    STATUS=$?

    FINAL_SIZE=0
    if [ -f "$TARGET_PATH" ]; then
        FINAL_SIZE=$(stat -c%s "$TARGET_PATH" 2>/dev/null || echo 0)
    fi

    if [ "$STATUS" -eq 0 ] && [ "$FINAL_SIZE" -ge 4000000000 ]; then
        echo "✅ DeepSeek-R1-Distill-Llama-8B başarıyla tamamlandı!"
        ls -lh "$TARGET_PATH"
        break
    else
        echo "⚠️ Bağlantı yenileniyor. 3 saniye içinde kaldığı yerden devam edilecek..."
        sleep 3
    fi
done
