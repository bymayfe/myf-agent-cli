#!/usr/bin/env python3
"""
MYF AI Agent CLI — Ana Başlatıcı (Root Entrypoint)

Kullanım:
  python main.py                  -> İnteraktif Ajan Sohbet Konsolu (REPL)
  python main.py chat             -> İnteraktif Ajan Sohbet Konsolu (REPL)
  python main.py pipeline "proje" -> Otonom 5-Aşamalı Multi-Agent Pipeline Motoru
  python main.py --help           -> Yardım menüsü
"""

import sys
from pathlib import Path

# Modül yolunu ayarla
ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent_system"))

# agent_system.main sembollerini tam uyumluluk için dışa aktar
try:
    import agent_system.main as _asm
    sys.modules["main"] = _asm
    for _k, _v in _asm.__dict__.items():
        if not _k.startswith("__"):
            globals()[_k] = _v
except Exception:
    pass

def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "pipeline":
        from agent_system.main import main as pipeline_main
        # Argümanları kaydırıp pipeline'ı çalıştır
        sys.argv.pop(1)
        pipeline_main()
    else:
        # Varsayılan: İnteraktif REPL Sohbeti
        from agent_system.chat import main as chat_main
        if len(sys.argv) > 1 and sys.argv[1] == "chat":
            sys.argv.pop(1)
        chat_main()

if __name__ == "__main__":
    main()
