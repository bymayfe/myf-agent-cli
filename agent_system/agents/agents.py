"""
agents.py — Dinamik agent yukleme sistemi.

agents_config.json + role_templates.json'dan agentlari yukler.
Sabit 5-agent sistemi yerine tamamen dinamik CRUD destekli yaklasim.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
_CONFIG_PATH    = _HERE / "agents_config.json"
_TEMPLATES_PATH = _HERE / "role_templates.json"


# ─────────────────────────────────────────────
# Veri yapisi
# ─────────────────────────────────────────────

@dataclass
class AgentDefinition:
    """Tek bir agent'in tam tanimi."""
    id: str
    display_name: str
    role_type: str                 # role_templates.json'daki anahtar
    model: str
    pipeline_order: int
    enabled: bool
    description: str
    system_prompt: str             # templates'tan veya custom_prompt'tan gelir
    output_brain_section: str
    expects_input: list[str]
    produces_output: str
    custom_prompt: Optional[str] = None
    icon: str = "[???]"


# ─────────────────────────────────────────────
# JSON yukleme
# ─────────────────────────────────────────────

def load_templates() -> dict:
    """role_templates.json'u yukle."""
    with open(_TEMPLATES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("templates", {})


def load_config() -> dict:
    """agents_config.json'u yukle."""
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"agents_config.json bulunamadi: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict):
    """agents_config.json'a geri yaz."""
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_agents(enabled_only: bool = True) -> list[AgentDefinition]:
    """
    agents_config.json + role_templates.json birlestirir,
    pipeline_order'a gore sirali AgentDefinition listesi dondurur.

    Args:
        enabled_only: True ise sadece enabled=true agentlari dondur.
    """
    config    = load_config()
    templates = load_templates()
    agents    = []

    for entry in config.get("agents", []):
        if enabled_only and not entry.get("enabled", True):
            continue

        role_type = entry.get("role_type", "custom")
        tmpl      = templates.get(role_type, templates.get("custom", {}))

        # System prompt: custom_prompt varsa onu kullan, yoksa template'tan al
        custom_prompt = entry.get("custom_prompt")
        system_prompt = custom_prompt if custom_prompt else tmpl.get("system_prompt", "")
        if system_prompt:
            from datetime import datetime
            cur_date = datetime.now().strftime("%d.%m.%Y")
            system_prompt = f"BUGÜNÜN GÜNCEL TARİHİ: {cur_date}\n(Tüm kütüphane, paket ve mimari kararlarını güncel tarihe göre ver).\n\n" + system_prompt

        agents.append(AgentDefinition(
            id                  = entry["id"],
            display_name        = entry.get("display_name", role_type),
            role_type           = role_type,
            model               = entry.get("model", "ollama/qwen2.5-coder:7b"),
            pipeline_order      = entry.get("pipeline_order", 99),
            enabled             = entry.get("enabled", True),
            description         = entry.get("description", tmpl.get("description", "")),
            system_prompt       = system_prompt,
            output_brain_section= tmpl.get("output_brain_section", "Cikti"),
            expects_input       = tmpl.get("expects_input", ["all_previous"]),
            produces_output     = tmpl.get("produces_output", "output"),
            custom_prompt       = custom_prompt,
            icon                = tmpl.get("icon", "[???]"),
        ))

    agents.sort(key=lambda a: a.pipeline_order)
    return agents


# ─────────────────────────────────────────────
# Prompt olusturma (dinamik)
# ─────────────────────────────────────────────

def build_agent_prompt(
    agent: AgentDefinition,
    context: dict,
    current_files: list[str],
) -> str:
    """
    Her agent icin gorev promptu olusturur.
    Akilli context filtreleme ve boyut budama yaparak 40K'lik siskinlikleri onler.
    """
    parts = []

    # Ajanin bekledigi alanlar veya genel kritik alanlar
    allowed_keys = set(agent.expects_input or [])
    # Otomatik hata/geri bildirim anahtarlarini her zaman ekle (TUM rollere gider,
    # cunku bunlar geri bildirim niteligindedir ve her rol icin anlamlidir)
    allowed_keys.update({"QA_FEEDBACK", "last_test_error", "STUCK_ALERT", "OPTIMIZER_FEEDBACK"})
    # OPTIMIZE_MODE ve MISSING_FILES SADECE kod yazan rollere anlamlidir
    # (PM/Architect/QA/Reviewer/Doc gibi kod uretmeyen rollere gonderilmesi
    # alakasiz/yanlis yonlendirici talimat sizmasina yol acar).
    if agent.role_type in ("developer", "optimizer"):
        allowed_keys.update({"OPTIMIZE_MODE", "MISSING_FILES"})
    if "all_previous" in allowed_keys:
        allowed_keys = set(context.keys())

    # Maksimum karakter sınırları (Büyük modeller ve tam mimari planları için genişletildi)
    char_limits = {
        "project_brief": 3000,
        "prd": 12000,
        "architecture": 16000,
        "code_files": 8000,
        "repomap": 4000,
        "codebase_graph": 4000,
        "QA_FEEDBACK": 6000,
        "last_test_error": 3000,
        "test_report": 6000,
        "optimization_report": 4000,
        "MISSING_FILES": 4000,
    }

    # Baglamlari sec ve ekle
    for key, value in context.items():
        if key not in allowed_keys:
            continue
        if value and value.strip():
            label = _context_label(key)
            max_len = char_limits.get(key, 3000)
            if len(value) > max_len:
                snippet = value[:max_len] + "\n...[kisaltildi]"
            else:
                snippet = value
            parts.append(f"=== {label} ===\n{snippet}")

    # Mevcut dosyalar (Kompakt)
    if current_files:
        files_snippet = "\n".join(current_files[:40])
        if len(current_files) > 40:
            files_snippet += f"\n... ve {len(current_files)-40} dosya daha"
        parts.append(f"=== MEVCUT DOSYALAR ===\n{files_snippet}")

    # Ajan gorevi
    task_hint = _task_hint(agent)
    # Eger QA Feedback varsa developer'a KESIN KOD ZORUNLULUGU ekle
    if agent.role_type == "developer" and ("QA_FEEDBACK" in context or "last_test_error" in context):
        task_hint += (
            "\n\n🚨 KESIN KURAL: YALNIZCA hatali dosyalari duzelten kod bloklari uret. "
            "Sohbet, aciklama veya metin raporu yazmak KESINLIKLE YASAKTIR. "
            "Cevabin SADECE projenin dilinde (Python, JS/TS, Go, Rust vb.) uygun "
            "yorum satiriyla filepath belirtilmis kod bloklari veya "
            "<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE bloklari icermelidir! "
            "Dosya uzantisini VE yorum sozdizimini projenin gercek diline gore sec, "
            "asla otomatik olarak .py veya python varsayma."
        )

    parts.append(f"=== GOREV ===\n{task_hint}")

    return "\n\n".join(parts)


def _context_label(key: str) -> str:
    labels = {
        "project_brief"      : "PROJE ISTEGI",
        "prd"                : "URUN GEREKSINIM BELGESI (PRD)",
        "architecture"       : "MIMARI TASARIM",
        "code_files"         : "MEVCUT KOD OZETI",
        "repomap"            : "KOD TABANI HARITASI & GRAFIGI",
        "codebase_graph"     : "KOD TABANI BILGI GRAFIGI",
        "web_research"       : "WEB & GITHUB ARASTIRMA SONUCLARI",
        "test_report"        : "TEST RAPORU",
        "security_report"    : "GUVENLIK RAPORU",
        "documentation"      : "DOKUMANTASYON",
        "all_previous"       : "ONCEKI CIKTILAR",
        "profiling_log"      : "PROFILING CALISTIRMA LOGU",
        "optimization_report": "OPTIMIZASYON RAPORU",
        "OPTIMIZE_MODE"      : "OPTIMIZE MODU TALIMATI",
        "QA_FEEDBACK"        : "QA TEST GERI BILDIRIMI",
        "STUCK_ALERT"        : "YAKLASIM DEGISTIRME UYARISI",
        "OPTIMIZER_FEEDBACK" : "OPTIMIZER GERI BILDIRIMI",
        "last_test_error"    : "FIZIKSEL TEST HATASI",
        "MISSING_FILES"      : "MIMARIDEKI HENUZ YAZILMAMIS EKSIK DOSYALAR",
    }
    return labels.get(key, key.upper())


def _task_hint(agent: AgentDefinition) -> str:
    hints = {
        "product_manager"     : "Yukaridaki proje istegine gore eksiksiz PRD hazirla. Gerekirse web/dokuman arastirmasi talep et.",
        "software_architect"  : "PRD'yi okuyup moduler Mimari Tasarim Belgesi hazirla.",
        "developer"           : (
            "Mimari tasarima ve mevcut kod tabanina gore TUM dosyalari eksiksiz yaz.\n\n"
            "🔴 EN KRITIK KURAL — HER KOD BLOGUNUN ILK SATIRINA FILEPATH YORUMU EKLE:\n"
            "  Python/Shell : # filepath: klasor/dosya.py\n"
            "  JS/TS/Go/Rust: // filepath: klasor/dosya.js\n"
            "  HTML         : <!-- filepath: dosya.html -->\n"
            "  CSS/SCSS     : /* filepath: styles.css */\n"
            "  JSON/YAML    : // filepath: manifest.json\n"
            "  Markdown     : <!-- filepath: README.md -->\n"
            "Bu yorum KOD BLOGUNUN ICINDE, BIRINCI SATIRDA olmali. "
            "Blok disinda baslik veya aciklama olarak degil, BLOGUN ICINDE.\n"
            "Bu yorum OLMADAN dosya KAYDEDILMEZ ve kodun uretilmemis sayilir!\n\n"
            "Mevcut bir dosyada degisiklik: tumu yerine sadece degisen kismi yaz:\n"
            "<<<<<<< SEARCH\n(eski kod)\n=======\n(yeni kod)\n>>>>>>> REPLACE\n\n"
            "Yeni dosya: tam icerigi uret. TODO veya pass birakmak KESINLIKLE YASAKTIR."
        ),
        "qa_tester"           : "Uretilen kodu incele, Test Raporu ve pytest testleri yaz.",
        "reviewer"            : (
            "QA raporundaki sorunlari duzelt. Mevcut dosyalarda tum dosyayi yazmak yerine "
            "<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE bloklari ile cerrahi duzeltme yap, "
            "CHANGELOG_ENTRY ekle."
        ),
        "optimizer"           : (
            "PROFILING CALISTIRMA LOGU ve KOD TABANI GRAFIGINI dikkatlice oku. "
            "Hot-spot'lari tespit et. Mevcut dosyalarda SEARCH/REPLACE bloklariyla sadece "
            "ilgili fonksiyonu optimize et. Rapor sonuna STATUS: OPTIMIZED veya STATUS: NEEDS_MORE yaz."
        ),
        "security_auditor"    : "Kodu OWASP Top 10 cercevesinde guvenlik acisindan incele ve rapor yaz.",
        "documentation_writer": "Proje icin README.md, API.md ve CONTRIBUTING.md yaz.",
        "devops_engineer"     : "Dockerfile, docker-compose.yml ve GitHub Actions CI pipeline yaz.",
        "custom"              : "Yukaridaki tum baglami kullanarak gorevini yerine getir.",
    }
    return hints.get(agent.role_type, "Gorevini yerine getir.")


from code_parser import UniversalCodeParser, extract_code_blocks


def extract_changelog(text: str) -> str:
    """CHANGELOG_ENTRY bolumunu ayristir."""
    m = re.search(r"##\s*CHANGELOG_ENTRY\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else text[:500]


def extract_pipeline_marker(text: str) -> Optional[str]:
    """
    Koordinator agenttan PIPELINE_START isaretini ara.
    Dondur: refined proje ozeti (varsa), yoksa None.
    """
    m = re.search(r"##PIPELINE_START##\n?(.*?)(?:##PIPELINE_END##|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else None
