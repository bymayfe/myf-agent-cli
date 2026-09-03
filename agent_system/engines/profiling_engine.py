"""
profiling_engine.py — Profiling, Optimizasyon ve Stuck-Loop Tespit Motoru

Performans ölçümleri, CPU/Bellek profil oluşturma ve ajan kısırdöngü (stuck-loop)
tespit ve strateji değiştirme mekanizmalarını yönetir.
"""

from __future__ import annotations
import os
import subprocess
from pathlib import Path
from typing import Optional


class StuckLoopDetector:
    """Ajanların aynı hatada kısır döngüye girmesini engeller ve yeni strateji önerir."""

    HINTS = [
        ("[STUCK ALARM - FARKLI YAKLASIM #1] Ayni hata uc kez tekrarlandi. "
         "Mevcut yaklasimi tamamen birak. Farkli bir kutaphane, algoritma veya "
         "mimari sec (ornek: asyncio→threading, pandas→polars, OOP→fonksiyonel). "
         "Onceki dosyalara bagli kalma, sifirdan farkli bir cozum yaz."),
        ("[STUCK ALARM - FARKLI YAKLASIM #2] Ikinci stuck tespiti. Minimal parcala stratejisi: "
         "Once SADECE tek bir fonksiyonu dogru calistiran en kucuk kodu yaz. "
         "Karmasikligi adim adim ekle. Tum sistemi bir anda yazmaya calisma. "
         "Basit, saf Python kullan, dis bagimliligi minimuma indir."),
        ("[STUCK ALARM - FARKLI YAKLASIM #3] Ucuncu stuck. Hata veren modulu izole et: "
         "Sadece o modulu sifirdan farkli bir design pattern ile yeniden yaz "
         "(Factory, Strategy, Observer vb.). Geri kalan dosyalara dokunma."),
        ("[STUCK ALARM - FARKLI YAKLASIM #4] Dorduncu stuck. Sorunu en temel bilesenlerine indir: "
         "stdlib disinda hicbir dis kutuphane kullanmadan calis. "
         "Oncelik: sadece calisir bir sonuc. Guzellik, performans sonra."),
    ]

    @staticmethod
    def is_similar_error(e1: str, e2: str, chars: int = 120) -> bool:
        return e1[:chars].strip() == e2[:chars].strip()

    @classmethod
    def check_stuck(cls, history: list) -> bool:
        if len(history) < 3:
            return False
        return (cls.is_similar_error(history[-1], history[-2]) and
                cls.is_similar_error(history[-1], history[-3]))

    @classmethod
    def get_hint(cls, stuck_count: int) -> str:
        idx = min(stuck_count - 1, len(cls.HINTS) - 1)
        return cls.HINTS[max(0, idx)]


class ProfilingEngine:
    """Uygulamanın performans profilini yakalar ve optimizasyon ihtiyacını analiz eder."""

    OPTIMIZE_KEYWORDS = frozenset({
        "optimize", "optimizasyon", "hizlandir", "hızlandır", "performans",
        "verimli", "hiz", "hız", "speed", "performance",
        "optimize et", "optimize hale", "yavaş", "yavas",
        "bottleneck", "profil", "hizli yap", "daha hizli", "daha hızlı",
    })

    @classmethod
    def detect_optimize_request(cls, text: str) -> bool:
        text_lower = text.lower()
        return any(kw in text_lower for kw in cls.OPTIMIZE_KEYWORDS)

    @classmethod
    def run_profiling(cls, output_dir: str) -> str:
        """Koddaki @profile_timer fonksiyonlarını çalıştırıp sonuçları toplar."""
        out_path = Path(output_dir)
        profiling_log_path = out_path / "profiling_output.log"
        py_files = [
            f for f in out_path.rglob("*.py")
            if ".myfcli" not in str(f).replace("\\", "/")
        ]
        entry_files = [f for f in py_files if f.name in ("main.py", "run.py", "app.py")]
        parts = ["=== Profiling Calistirmasi ==="]
        if not entry_files:
            return "(Giris noktasi bulunamadi: main.py / run.py / app.py yok)"
        target = entry_files[0]
        parts.append(f"Hedef dosya: {target.name}")
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(out_path)
            env["PROFILING_MODE"] = "1"
            res = subprocess.run(
                [os.sys.executable, str(target)],
                cwd=str(out_path),
                capture_output=True,
                text=True,
                timeout=12,
                env=env,
            )
            parts.append(f"Cikis kodu: {res.returncode}")
            if res.stdout:
                parts.append("STDOUT:\n" + res.stdout[:1500])
            if res.stderr:
                parts.append("STDERR:\n" + res.stderr[:1000])
        except subprocess.TimeoutExpired:
            parts.append("Zaman asimi (12s) — program profiling ile basariyla calisti.")
        except Exception as exc:
            parts.append(f"Calistirma hatasi: {exc}")

        if profiling_log_path.exists():
            try:
                log_content = profiling_log_path.read_text(encoding="utf-8", errors="ignore")
                parts.append("\n=== profiling_output.log ===")
                parts.append(log_content[:3000])
            except Exception as exc:
                parts.append(f"Log okuma hatasi: {exc}")
        else:
            parts.append("Not: profiling_output.log olusmadi (@profile_timer cagrildi mi?)")

        return "\n".join(parts)
