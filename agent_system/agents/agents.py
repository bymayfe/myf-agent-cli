"""
agents.py — Dinamik agent yukleme sistemi.

agents_config.json + role_templates.json'dan agentlari yukler.
Sabit 5-agent sistemi yerine tamamen dinamik CRUD destekli yaklasim.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass
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
            system_prompt = f"TODAY'S CURRENT DATE: {cur_date}\n(Base all library, package, and architectural decisions on the current date).\n\n" + system_prompt

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
                snippet = value[:max_len] + "\n...[truncated]"
            else:
                snippet = value
            parts.append(f"=== {label} ===\n{snippet}")

    # Current files (Compact)
    if current_files:
        files_snippet = "\n".join(current_files[:40])
        if len(current_files) > 40:
            files_snippet += f"\n... and {len(current_files)-40} more files"
        parts.append(f"=== CURRENT FILES ===\n{files_snippet}")

    # Agent task
    task_hint = _task_hint(agent)
    # If QA Feedback exists, enforce STRICT CODE ONLY constraint on developer
    if agent.role_type == "developer" and ("QA_FEEDBACK" in context or "last_test_error" in context):
        task_hint += (
            "\n\n🚨 STRICT RULE: Generate ONLY code blocks that fix the erroneous files. "
            "Chit-chat, conversational filler, or text reports are STRICTLY FORBIDDEN. "
            "Your response must ONLY contain code blocks with the appropriate filepath comment "
            "for the project's language (Python, JS/TS, Go, Rust, etc.) or "
            "<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE blocks! "
            "Choose the file extension AND comment syntax based on the actual project language, "
            "never assume .py or Python by default."
        )

    parts.append(f"=== TASK ===\n{task_hint}")

    return "\n\n".join(parts)


def _context_label(key: str) -> str:
    labels = {
        "project_brief"      : "PROJECT BRIEF",
        "prd"                : "PRODUCT REQUIREMENTS DOCUMENT (PRD)",
        "architecture"       : "ARCHITECTURAL DESIGN",
        "code_files"         : "CURRENT CODE SUMMARY",
        "repomap"            : "CODEBASE MAP & GRAPH",
        "codebase_graph"     : "CODEBASE KNOWLEDGE GRAPH",
        "web_research"       : "WEB & GITHUB RESEARCH RESULTS",
        "test_report"        : "TEST REPORT",
        "security_report"    : "SECURITY REPORT",
        "documentation"      : "DOCUMENTATION",
        "all_previous"       : "PREVIOUS OUTPUTS",
        "profiling_log"      : "PROFILING EXECUTION LOG",
        "optimization_report": "OPTIMIZATION REPORT",
        "OPTIMIZE_MODE"      : "OPTIMIZE MODE DIRECTIVE",
        "QA_FEEDBACK"        : "QA TEST FEEDBACK",
        "STUCK_ALERT"        : "APPROACH CHANGE ALERT",
        "OPTIMIZER_FEEDBACK" : "OPTIMIZER FEEDBACK",
        "last_test_error"    : "PHYSICAL TEST ERROR",
        "MISSING_FILES"      : "MISSING ARCHITECTURAL FILES NOT YET CREATED",
    }
    return labels.get(key, key.upper())


def _task_hint(agent: AgentDefinition) -> str:
    hints = {
        "product_manager"     : "Prepare a complete and comprehensive PRD based on the project brief above. Request web/doc research if needed. Present your output to the user in fluent Turkish.",
        "software_architect"  : "Read the PRD and prepare a modular Architectural Design Document. Present your document and explanations to the user in fluent Turkish.",
        "developer"           : (
            "Write ALL files completely according to the architectural design and existing codebase.\n\n"
            "🔴 CRITICAL RULE — ADD A FILEPATH COMMENT ON THE FIRST LINE OF EVERY CODE BLOCK:\n"
            "  Python/Shell : # filepath: folder/file.py\n"
            "  JS/TS/Go/Rust: // filepath: folder/file.js\n"
            "  HTML         : <!-- filepath: file.html -->\n"
            "  CSS/SCSS     : /* filepath: styles.css */\n"
            "  JSON/YAML    : // filepath: manifest.json\n"
            "  Markdown     : <!-- filepath: README.md -->\n"
            "This comment MUST be INSIDE the code block, on the FIRST line. "
            "NOT outside as a heading or markdown text, but INSIDE THE BLOCK.\n"
            "WITHOUT this comment, the file CANNOT BE SAVED and will be treated as missing!\n\n"
            "Modifying an existing file: write only the changed part instead of the whole file:\n"
            "<<<<<<< SEARCH\n(old code)\n=======\n(new code)\n>>>>>>> REPLACE\n\n"
            "New file: generate full content. Leaving TODOs or pass statements is STRICTLY PROHIBITED.\n"
            "OUTPUT LANGUAGE: Communicate user-facing notes in fluent Turkish."
        ),
        "qa_tester"           : "Inspect the generated code, write a Test Report and pytest/unit tests. Present reports and notes to the user in fluent Turkish.",
        "reviewer"            : (
            "Fix the issues mentioned in the QA report. Perform surgical fixes on existing files using "
            "<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE blocks instead of rewriting entire files. "
            "Add a CHANGELOG_ENTRY. Present explanations to the user in fluent Turkish."
        ),
        "optimizer"           : (
            "Carefully examine the PROFILING EXECUTION LOG and CODEBASE GRAPH. "
            "Identify hot-spots. Optimize only the relevant functions using SEARCH/REPLACE blocks. "
            "End your report with STATUS: OPTIMIZED or STATUS: NEEDS_MORE. Present explanations in fluent Turkish."
        ),
        "security_auditor"    : "Inspect the code for security vulnerabilities following OWASP Top 10 and write a report. Present findings in fluent Turkish.",
        "documentation_writer": "Write README.md, API.md, and CONTRIBUTING.md for the project. Present explanations in fluent Turkish.",
        "devops_engineer"     : "Write Dockerfile, docker-compose.yml, and GitHub Actions CI pipeline. Present explanations in fluent Turkish.",
        "custom"              : "Perform your task using all the context provided above. Present responses to the user in fluent Turkish.",
    }
    return hints.get(agent.role_type, "Fulfill your task. Present user-facing responses in fluent Turkish.")




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
