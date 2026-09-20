#!/usr/bin/env bash
# ==============================================================================
#  MYF AI — llama.cpp GPU Sunucusunu Başlatıcı (Linux / RTX 4070 CUDA)
#  İşlemci: AMD Ryzen 7 8845HS (8 Core / 16 Thread)
#  Ekran Kartı: NVIDIA GeForce RTX 4070 (8GB VRAM)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$SCRIPT_DIR/models"

# PATH'e standart lokal dizinleri ekle
export PATH="$HOME/.local/bin:$HOME/.local/llama.cpp/build/bin:/usr/local/bin:$PATH"

# Eğer bir terminal penceresi olmadan (çift tıklamayla) açıldıysa otomatik yeni terminal penceresi aç
if [ ! -t 0 ] || [ ! -t 1 ]; then
    SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
    for term in konsole alacritty kitty foot x-terminal-emulator gnome-terminal xterm; do
        if command -v "$term" &>/dev/null; then
            exec "$term" -e bash -c "\"$SCRIPT_PATH\" \"$@\"; echo; read -r -p 'Kapatmak icin Enter tusuna basin...' _"
        fi
    done
fi

mkdir -p "$MODELS_DIR"

# llama-server binary arama
LLAMA_BIN=""
CANDIDATE_PATHS=(
    "$HOME/.local/bin/llama-server"
    "$HOME/.local/llama.cpp/build/bin/llama-server"
    "$SCRIPT_DIR/llama.cpp/build/bin/llama-server"
    "/usr/local/bin/llama-server"
    "/usr/bin/llama-server"
)

for p in "${CANDIDATE_PATHS[@]}"; do
    if [ -x "$p" ]; then
        LLAMA_BIN="$p"
        break
    fi
done

if [ -z "$LLAMA_BIN" ]; then
    LLAMA_BIN="$(which llama-server 2>/dev/null || true)"
fi

if [ -z "$LLAMA_BIN" ] || [ ! -x "$LLAMA_BIN" ]; then
    echo ""
    echo "=========================================================="
    echo "  ❌ [HATA] llama-server binary dosyası bulunamadı!"
    echo "=========================================================="
    echo "  Lütfen llama.cpp'nin kurulu olduğundan emin olun."
    echo "=========================================================="
    echo ""
    read -r -p "Kapatmak için Enter tuşuna basın..." _
    exit 1
fi

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
THREADS="${THREADS:-8}"  # Ryzen 7 8845HS 8 fiziksel çekirdek
BATCH_THREADS="16"       # 16 Thread (Hızlı İlk Giriş/Prefill)

# GPU Katman Yönetimi
GPU_ARGS=()
if [ -n "$NGL" ]; then
    GPU_ARGS=("-ngl" "$NGL")
    NGL_DISPLAY="$NGL (Kullanıcı Belirtti)"
else
    GPU_ARGS=("--fit" "on")
    NGL_DISPLAY="Otomatik VRAM Sığdırma (--fit on)"
fi

# ============================================================
#  Model Seçimi
# ============================================================

# Komut satırından direkt model belirtildiyse kullan
LOCAL_MODEL=""
if [ -n "$1" ]; then
    if [ -f "$1" ]; then
        LOCAL_MODEL="$1"
    elif [ -d "$1" ]; then
        MODELS_DIR="$1"
    else
        LOCAL_MODEL=$(find "$MODELS_DIR" -iname "*$1*.gguf" 2>/dev/null | head -n 1)
    fi
fi

# Model belirtilmediyse interaktif menü göster
if [ -z "$LOCAL_MODEL" ]; then
    # Modelleri listele
    mapfile -t MODEL_LIST < <(find "$MODELS_DIR" -name "*.gguf" ! -name "*-0000[2-9]-of-*" 2>/dev/null | sort)

    if [ ${#MODEL_LIST[@]} -eq 0 ]; then
        echo ""
        echo "  ⚠️  models/ klasöründe .gguf dosyası bulunamadı."
        echo "  HuggingFace'ten Qwen2.5-Coder-7B-Instruct yükleniyor..."
        echo ""
        USE_HF=1
    elif [ ${#MODEL_LIST[@]} -eq 1 ]; then
        LOCAL_MODEL="${MODEL_LIST[0]}"
        echo ""
        echo "  ✅ Tek model bulundu, otomatik seçildi: $(basename "$LOCAL_MODEL")"
    else
        echo ""
        echo "=========================================================="
        echo "  🤖 MYF AI — Model Seçimi"
        echo "=========================================================="
        echo ""

        # Model bilgileri: İsim ve boyut
        declare -A MODEL_INFO
        MODEL_INFO["DeepSeek-R1-Distill-Llama-8B-Q4_K_M"]="8B  | Q4_K_M  | Akıl Yürütme (DeepSeek R1)"
        MODEL_INFO["Ornith-1.5-9B-Q4_K_M"]="9B  | Q4_K_M  | Genel Amaçlı"
        MODEL_INFO["Qwen2.5-Coder-7B-Instruct-Q4_K_M"]="7B  | Q4_K_M  | Kodlama (Standart)"
        MODEL_INFO["Qwen2.5-Coder-7B-Instruct-abliterated-Q5_K_M"]="7B  | Q5_K_M  | Kodlama (Sansürsüz, Yüksek Kalite)"
        MODEL_INFO["Qwen2.5-Coder-14B-Instruct-abliterated-IQ3_M"]="14B | IQ3_M   | Kodlama Büyük (Sansürsüz)"
        MODEL_INFO["Huihui-Qwen3.8-27B-abliterated-UD-DW-Q4_K_M"]="27B | Q4_K_M  | En Güçlü (Sansürsüz, ~16 GB VRAM)"

        for i in "${!MODEL_LIST[@]}"; do
            BASENAME=$(basename "${MODEL_LIST[$i]}" .gguf)
            SIZE=$(du -sh "${MODEL_LIST[$i]}" 2>/dev/null | cut -f1)
            INFO="${MODEL_INFO[$BASENAME]:-Lokal Model}"
            printf "  [%d] %-58s  %s  (%s)\n" $((i+1)) "$(basename "${MODEL_LIST[$i]}")" "$SIZE" "$INFO"
        done

        echo ""
        echo "  [0] HuggingFace'ten indir (Qwen2.5-Coder-7B-Instruct-Q4_K_M)"
        echo ""
        echo "=========================================================="
        read -r -p "  Seçiminiz (1-${#MODEL_LIST[@]}, varsayılan=1): " CHOICE
        echo ""

        if [ -z "$CHOICE" ] || [ "$CHOICE" = "1" ]; then
            LOCAL_MODEL="${MODEL_LIST[0]}"
        elif [ "$CHOICE" = "0" ]; then
            USE_HF=1
        elif [[ "$CHOICE" =~ ^[0-9]+$ ]] && [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le "${#MODEL_LIST[@]}" ]; then
            LOCAL_MODEL="${MODEL_LIST[$((CHOICE-1))]}"
        else
            echo "  ❌ Geçersiz seçim! İlk model otomatik seçildi."
            LOCAL_MODEL="${MODEL_LIST[0]}"
        fi
    fi
fi

# Context window ayarı: modele göre otomatik belirle
CTX="${CTX:-}"
if [ -n "$LOCAL_MODEL" ] && [ -z "$CTX" ]; then
    BASENAME_LOWER=$(basename "$LOCAL_MODEL" .gguf | tr '[:upper:]' '[:lower:]')
    if [[ "$BASENAME_LOWER" == *"27b"* ]]; then
        CTX=16384   # 27B için VRAM koruma
    elif [[ "$BASENAME_LOWER" == *"14b"* ]]; then
        CTX=32768   # 14B için tam context
    else
        CTX=32768   # 7B-9B için tam context
    fi
else
    CTX="${CTX:-32768}"
fi

echo ""
echo "=========================================================="
echo "  🚀 MYF AI — llama.cpp GPU Server (CUDA / RTX 4070)"
echo "  🌐 API Endpoint: http://localhost:$PORT/v1"
echo "  🧠 Context Size: $CTX token"
echo "  ⚡ CPU Thread: $THREADS Core / $BATCH_THREADS Batch (Ryzen 7 8845HS)"
echo "  ⚡ GPU Ayarı: $NGL_DISPLAY"
echo "  ⚡ FlashAttention: Aktif"
echo "  🔧 llama-server: $LLAMA_BIN"
if [ -n "$LOCAL_MODEL" ]; then
    echo "  📦 Yüklenen Model: $(basename "$LOCAL_MODEL")"
fi
echo "  🛑 Durdurmak için: bash llama_server_durdur.sh veya Ctrl+C"
echo "=========================================================="
echo ""

if [ -n "$LOCAL_MODEL" ] && [ -z "$USE_HF" ]; then
    "$LLAMA_BIN" \
        -m "$LOCAL_MODEL" \
        --host "$HOST" \
        --port "$PORT" \
        -c "$CTX" \
        -t "$THREADS" \
        -tb "$BATCH_THREADS" \
        "${GPU_ARGS[@]}" \
        --flash-attn on \
        --cache-type-k q8_0 \
        --cache-type-v q8_0 \
        -b 512 \
        -ub 512 \
        --cont-batching \
        --reasoning-format deepseek \
        --reasoning-preserve
    EXIT_CODE=$?
else
    echo "🌐 HuggingFace'ten Qwen2.5-Coder-7B-Instruct yükleniyor..."
    "$LLAMA_BIN" \
        --hf-repo "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF" \
        --hf-file "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf" \
        --host "$HOST" \
        --port "$PORT" \
        -c "${CTX:-32768}" \
        -t "$THREADS" \
        -tb "$BATCH_THREADS" \
        "${GPU_ARGS[@]}" \
        --flash-attn on \
        --cache-type-k q8_0 \
        --cache-type-v q8_0 \
        -b 512 \
        -ub 512 \
        --cont-batching \
        --reasoning-format deepseek \
        --reasoning-preserve
    EXIT_CODE=$?
fi

echo ""
echo "=========================================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "  🛑 llama.cpp GPU sunucusu durduruldu."
else
    echo "  ⚠️ llama.cpp sunucusu sonlandı (Çıkış Kodu: $EXIT_CODE)."
fi
echo "=========================================================="
echo ""
read -r -p "Terminali kapatmak için Enter tuşuna basın..." _
