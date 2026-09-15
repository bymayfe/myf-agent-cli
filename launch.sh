#!/usr/bin/env bash
# ==============================================================================
# Multi-Agent Yazılım Geliştirme Sistemi — Linux / macOS / WSL Başlatıcı
# ==============================================================================

set -e

# Eğer bir terminal penceresi olmadan (çift tıklamayla) açıldıysa otomatik yeni terminal penceresi aç
if [ ! -t 0 ] || [ ! -t 1 ]; then
    SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    for term in konsole alacritty x-terminal-emulator gnome-terminal xterm; do
        if command -v "$term" &>/dev/null; then
            if [ "$term" = "konsole" ]; then
                exec konsole -e bash -c "\"$SCRIPT_PATH\"; echo; echo 'Cikmak icin Enter tusuna basin...'; read"
            elif [ "$term" = "alacritty" ]; then
                exec alacritty -e bash -c "\"$SCRIPT_PATH\"; echo; echo 'Cikmak icin Enter tusuna basin...'; read"
            else
                exec "$term" -e bash -c "\"$SCRIPT_PATH\"; echo; echo 'Cikmak icin Enter tusuna basin...'; read"
            fi
        fi
    done
fi

# Renkler
CYAN='\033[1;36m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
NC='\033[0m' # No Color

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  Multi-Agent Yazilim Gelistirme Sistemi   ${NC}"
echo -e "${CYAN}  Koordinator + Pipeline v2.5 (Linux/WSL)  ${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# Proje dizinini bul ve agent_system klasörüne geç
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/agent_system"

# 1. Python kontrolü
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[HATA] python3 bulunamadı!${NC}"
    echo "Lütfen Python 3.10 veya üstünü yükleyin: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

# 2. Sanal ortam (.venv) kontrolü ve oluşturulması
if [ ! -f ".venv/bin/activate" ]; then
    echo -e "${YELLOW}[KURULUM 1/3] Sanal ortam (.venv) oluşturuluyor...${NC}"
    python3 -m venv .venv
    echo -e "${GREEN}[KURULUM 1/3] Sanal ortam başarıyla oluşturuldu.${NC}"
    echo ""
fi

# 3. Sanal ortamı aktifleştir
source .venv/bin/activate

# 4. Bağımlılık kontrolü ve yüklenmesi
if [ ! -f ".venv/.deps_installed" ]; then
    echo -e "${YELLOW}[KURULUM 2/3] Gerekli Python paketleri kuruluyor (ilk açılışta 1-2 dk sürebilir)...${NC}"
    pip install -r requirements.txt
    touch .venv/.deps_installed
    echo -e "${GREEN}[KURULUM 2/3] Tüm paketler başarıyla kuruldu.${NC}"
    echo ""
fi

# 5. Codebase Memory binary kontrolü (Linux için - opsiyonel)
CB_MEM_DIR="$SCRIPT_DIR/third_party/codebase_memory"
if [ ! -f "$CB_MEM_DIR/codebase-memory-mcp" ]; then
    if [ -f "$CB_MEM_DIR/install.sh" ]; then
        echo -e "${YELLOW}[KURULUM 3/3] codebase-memory eklentisi hazırlanıyor...${NC}"
        bash "$CB_MEM_DIR/install.sh" || true
    fi
else
    chmod +x "$CB_MEM_DIR/codebase-memory-mcp" 2>/dev/null || true
fi

# 6. Ollama servis kontrolü (opsiyonel uyarı)
if command -v ollama &> /dev/null; then
    if ! ollama list &> /dev/null; then
        echo -e "${YELLOW}[UYARI] Ollama servisi çalışmıyor görünüyor. Arka planda başlatmayı deneyin:${NC}"
        echo "        ollama serve"
        echo ""
    fi
else
    echo -e "${YELLOW}[UYARI] 'ollama' komutu bulunamadı. Yerel model kullanıyorsanız Ollama'yı yükleyin:${NC}"
    echo "        curl -fsSL https://ollama.com/install.sh | sh"
    echo ""
fi

# 6. Komut argümanı kontrolü (manage / agents komutları için)
if [ "$1" = "agents" ] || [ "$1" = "manage" ]; then
    shift
    python3 manage_agents.py "$@"
    exit 0
fi

# 7. Ana chat arayüzünü başlat
echo -e "${GREEN}[BAŞLATILIYOR] chat.py${NC}"
echo ""
python3 chat.py "$@"
EXIT_CODE=$?

echo ""
echo -e "${CYAN}============================================${NC}"
echo "  Program sonlandı."
echo -e "${CYAN}============================================${NC}"
read -r -p "Terminali kapatmak için Enter tuşuna basın..." _
