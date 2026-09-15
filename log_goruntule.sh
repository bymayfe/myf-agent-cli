#!/usr/bin/env bash
# ==============================================================================
# Multi-Agent Canlı Log ve Denetim Görüntüleyici (Linux / macOS)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/agent_system"

PY_SCRIPT="log_viewer.py"
if [ -f "storage/log_viewer.py" ]; then
    PY_SCRIPT="storage/log_viewer.py"
fi

if [ -f ".venv/bin/python3" ]; then
    .venv/bin/python3 "$PY_SCRIPT" "$@"
elif [ -f ".venv/bin/python" ]; then
    .venv/bin/python "$PY_SCRIPT" "$@"
else
    python3 "$PY_SCRIPT" "$@"
fi

echo ""
read -r -p "Kapatmak için Enter tuşuna basın..." _
