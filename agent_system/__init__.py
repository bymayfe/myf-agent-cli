"""
agent_system package root
Otomatik sys.path yapılandırması ile tüm alt paketleri ve modülleri erişilebilir kılar.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()

# Alt paketleri arama yoluna güvenle ekle
_SUBDIRS = [
    _ROOT,
    _ROOT / "core",
    _ROOT / "engines",
    _ROOT / "agents",
    _ROOT / "llm",
    _ROOT / "storage",
    _ROOT / "tests",
]

for _d in _SUBDIRS:
    _d_str = str(_d)
    if _d.is_dir() and _d_str not in sys.path:
        sys.path.insert(0, _d_str)
