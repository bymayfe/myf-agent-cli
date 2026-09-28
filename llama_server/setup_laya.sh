#!/usr/bin/env bash
# ==============================================================================
#  Laya System 1 Karar Modeli Kurulum ve Test Betiği
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PATH="$ROOT_DIR/agent_system/.venv"

echo "=========================================================="
echo "  ⚡ Laya System 1 Karar Modeli Kurulumu (CPU / Fast Reflex)"
echo "  📁 Sanal Ortam: $VENV_PATH"
echo "=========================================================="

if [ ! -d "$VENV_PATH" ]; then
    echo "❌ Hata: Sanal ortam (.venv) bulunamadı! Lütfen önce launch.sh çalıştırın."
    exit 1
fi

source "$VENV_PATH/bin/activate"

echo "📦 1. 'laya' paketi yükleniyor..."
pip install laya

export PYTHONPATH="$ROOT_DIR/agent_system:$PYTHONPATH"
python -c "
from engines.laya_engine import get_laya_engine
engine = get_laya_engine()
print('Laya Engine hazır:', engine.is_available)
res = engine.classify_error('SyntaxError: invalid syntax')
print('Örnek Hata Analizi:', res)
"

echo "=========================================================="
echo "  ✅ Laya başarıyla yapılandırıldı!"
echo "  Artık FixEngine ve LoopBreaker kararları ~30ms'de CPU'da çalışacak."
echo "=========================================================="
