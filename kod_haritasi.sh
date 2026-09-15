#!/usr/bin/env bash
# ==============================================================================
# Codebase Memory — Görsel Kod Haritası (Linux / macOS / WSL)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CB_MEM_DIR="$SCRIPT_DIR/third_party/codebase_memory"
BINARY="$CB_MEM_DIR/codebase-memory-mcp"
URL="http://localhost:9749"

echo "======================================================"
echo "  Codebase Memory — Görsel Kod Haritası               "
echo "======================================================"
echo ""

# Tarayıcı açma fonksiyonu
open_browser() {
    if command -v xdg-open &> /dev/null; then
        xdg-open "$URL" &> /dev/null &
    elif command -v open &> /dev/null; then
        open "$URL" &> /dev/null &
    elif command -v wslview &> /dev/null; then
        wslview "$URL" &> /dev/null &
    fi
}

# 1. Port 9749 zaten aktif mi kontrol et (chat.py veya baslat.sh açıkken)
if (curl -s --head http://localhost:9749 &> /dev/null) || (command -v nc &> /dev/null && nc -z localhost 9749 &> /dev/null); then
    echo "[BİLGİ] Sunucu zaten arka planda çalışıyor!"
    echo "[AÇILIYOR] Tarayıcıda açılıyor: $URL ..."
    open_browser
    exit 0
fi

# 2. Sunucu çalışmıyorsa ikiliyi kontrol et ve başlat
if [ ! -f "$BINARY" ]; then
    echo "[KURULUM] codebase-memory-mcp Linux binary'si indiriliyor..."
    if [ -f "$CB_MEM_DIR/install.sh" ]; then
        bash "$CB_MEM_DIR/install.sh"
    fi
fi

if [ -f "$BINARY" ]; then
    chmod +x "$BINARY" 2>/dev/null || true
else
    echo "[HATA] codebase-memory-mcp bulunamadı!"
    exit 1
fi

echo "[1/2] Web Arayüzü Tarayıcıda Açılıyor ($URL)..."
open_browser

"$BINARY"
EXIT_CODE=$?

echo ""
echo "======================================================"
echo "  🛑 Sunucu durduruldu."
echo "======================================================"
read -r -p "Terminali kapatmak için Enter tuşuna basın..." _
