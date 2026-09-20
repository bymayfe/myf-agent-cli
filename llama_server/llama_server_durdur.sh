#!/usr/bin/env bash
# ==============================================================================
#  MYF AI — llama.cpp & Ollama Sunucularını Tamamen Kapatıcı
# ==============================================================================

echo ""
echo "=========================================================="
echo "  🛑 llama-server & Ollama Modelleri Kapatılıyor..."
echo "=========================================================="
echo ""

# 1. llama-server süreçlerini öldür
if pgrep -f "llama-server" > /dev/null; then
    pkill -9 -f "llama-server" 2>/dev/null
    echo "✓ llama-server başarıyla durduruldu."
else
    echo "✓ Aktif çalışan llama-server bulunamadı."
fi

# 2. Port 8080 (llama-server default portu) temizliği
if command -v fuser &>/dev/null; then
    fuser -k 8080/tcp 2>/dev/null || true
fi

# 3. Varsa Ollama üzerinden çalışan modelleri boşalt / durdur
if command -v ollama &>/dev/null; then
    for model in $(ollama ps 2>/dev/null | awk 'NR>1 {print $1}'); do
        echo "✓ Ollama modeli bellekten atılıyor: $model"
        ollama stop "$model" 2>/dev/null || true
    done
fi

echo ""
echo "=========================================================="
echo "  ✅ Tüm yerel yapay zeka süreçleri sonlandırıldı!"
echo "  ❄️ RAM ve GPU VRAM belleği tamamen boşaltıldı."
echo "=========================================================="
echo ""
sleep 1
