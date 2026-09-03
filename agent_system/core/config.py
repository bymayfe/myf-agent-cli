"""
config.py — Sistem yapilandirmasi.

Proje dizini dinamiktir. Varsayilan: projects/output_project/
set_output_dir() ile belirli bir proje klasorune gecilebilir.
"""

from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Any

load_dotenv_available = False
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv_available = True
except ImportError:
    pass

_HERE = Path(__file__).parent
_SYSTEM_ROOT = _HERE.parent
_PROVIDERS_PATH = _SYSTEM_ROOT / "llm" / "providers_config.json" if (_SYSTEM_ROOT / "llm" / "providers_config.json").exists() else _SYSTEM_ROOT / "providers_config.json"
PROJECTS_BASE_DIR = _SYSTEM_ROOT / "projects"
PROJECTS_BASE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# Pipeline Model Sabitleri
# (settings.py'den okunur — /settings ile degistirilebilir)
# ─────────────────────────────────────────────

def _get_pipeline_models():
    """settings.py singleton'dan canli model degerlerini al."""
    try:
        from settings import settings as _s
        return (
            _s.planning_model,
            _s.code_model,
            _s.micro_fix_model,
            _s.micro_fix_max_tries,
            _s.full_autonomy_cap,
        )
    except Exception:
        return (
            "ollama/qwen3.5:4b",
            "ollama/qwen3.5:4b",
            "ollama/qwen3.5:4b",
            3,
            50,
        )

# Modul yuklendiginde bir kez oku
_pm, _cm, _mfm, _mft, _fac = _get_pipeline_models()

PLANNING_MODEL      = _pm   # Planlama (PRD + Mimari)
ESCALATION_MODEL    = _cm   # Agir kod / Escalation
MICRO_FIX_MODEL     = _mfm  # Hizli syntax onarimi
MICRO_FIX_MAX_TRIES = _mft  # kac basarisiz micro-fix sonrasi escalation

# Default output dir points to projects/ directory
_CURRENT_OUTPUT_DIR = str(PROJECTS_BASE_DIR / "Yeni_Proje")


# ─────────────────────────────────────────────
# Proje Dizini Yonetimi
# ─────────────────────────────────────────────

def get_output_dir() -> str:
    """Mevcut aktif proje klasorunun mutlak yolunu dondur."""
    return _CURRENT_OUTPUT_DIR


def set_output_dir(target_name_or_path: str) -> str:
    """
    Aktif proje klasorunu degistir.
    Eger hedef bir isim ise (örn "Tarim_Platformu"), projects/ altinda acilir.
    Eger mutlak bir yol ise (örn "C:/Users/..."), direkt o yol kullanilir.
    """
    global _CURRENT_OUTPUT_DIR
    target = (target_name_or_path or "").strip()

    if not target or target in ("/", "\\", "."):
        target = "Yeni_Proje"

    p = Path(target)
    if p.is_absolute() and str(p.resolve()) not in ("/", "\\", "/bin", "/etc", "/usr", "/var", "/dev", "/proc", "/sys", "/root"):
        final_path = p
    else:
        # Gecersiz karakterleri temizle
        safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in target).strip("_")
        if not safe_name or safe_name in ("/", "\\", "."):
            safe_name = "Yeni_Proje"
        final_path = PROJECTS_BASE_DIR / safe_name

    _CURRENT_OUTPUT_DIR = str(final_path.resolve())
    return _CURRENT_OUTPUT_DIR


def list_all_projects() -> list[dict[str, Any]]:
    """
    projects/ klasorundeki tum proje klasorlerini tara ve liste dondur.
    """
    results = []
    # 1. projects/ altindaki klasorler
    for item in PROJECTS_BASE_DIR.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            files = [f for f in item.rglob("*") if f.is_file() and not f.name.startswith(".")]
            mtime = item.stat().st_mtime
            results.append({
                "name":       item.name,
                "path":       str(item),
                "file_count": len(files),
                "mtime":      mtime,
            })

    # 2. Eski agent_system/output_project klasoru varsa ve projeler icindeyse tara
    legacy_dir = _HERE / "output_project"
    if legacy_dir.exists() and legacy_dir.is_dir():
        files = [f for f in legacy_dir.rglob("*") if f.is_file() and not f.name.startswith(".")]
        if len(files) > 0:
            results.append({
                "name":       "output_project (eski)",
                "path":       str(legacy_dir),
                "file_count": len(files),
                "mtime":      legacy_dir.stat().st_mtime,
            })

    # En son degistirilene gore sirala
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


# Backward compatibility alias
@property
def OUTPUT_DIR() -> str:
    return get_output_dir()


# ─────────────────────────────────────────────
# Provider yukleme
# ─────────────────────────────────────────────

def load_providers() -> dict:
    if not _PROVIDERS_PATH.exists():
        raise FileNotFoundError(f"providers_config.json bulunamadi: {_PROVIDERS_PATH}")
    with open(_PROVIDERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_providers(data: dict):
    with open(_PROVIDERS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_active_provider_name() -> str:
    env_provider = os.getenv("PROVIDER", "").strip().lower()
    if env_provider:
        return env_provider
    data = load_providers()
    return data.get("active_provider", "ollama")


def set_active_provider(provider_name: str):
    data = load_providers()
    if provider_name not in data.get("providers", {}):
        raise ValueError(f"Bilinmeyen saglayici: {provider_name}")
    data["active_provider"] = provider_name
    save_providers(data)


def get_provider_config(provider_name: str = None) -> dict:
    data      = load_providers()
    name      = provider_name or get_active_provider_name()
    providers = data.get("providers", {})
    if name not in providers:
        raise ValueError(f"Saglayici bulunamadi: {name}. Mevcut: {list(providers.keys())}")
    cfg = providers[name].copy()
    cfg["name"] = name

    key_env = cfg.get("api_key_env")
    if key_env:
        env_val = os.getenv(key_env, "").strip()
        if env_val:
            cfg["api_key"] = env_val

    return cfg


def list_providers() -> list[dict]:
    data      = load_providers()
    active    = get_active_provider_name()
    providers = data.get("providers", {})
    result    = []
    for name, cfg in providers.items():
        result.append({
            "name":         name,
            "label":        cfg.get("label", name),
            "description":  cfg.get("description", ""),
            "requires_key": cfg.get("requires_key", False),
            "key_url":      cfg.get("key_url", ""),
            "is_active":    name == active,
        })
    return result


# ─────────────────────────────────────────────
# LLM Parametreleri
# ─────────────────────────────────────────────

def _get_llm_params() -> dict:
    cfg = get_provider_config()
    return {
        "provider":    cfg.get("name", "ollama"),
        "api_base":    cfg.get("api_base", "http://localhost:11434"),
        "api_key":     cfg.get("api_key", ""),
        "temperature": float(os.getenv("TEMPERATURE", "0.3")),
        "max_tokens":  int(os.getenv("MAX_TOKENS", "8192")),
    }


def _get_agent_models() -> dict:
    cfg = get_provider_config()
    prov_models = cfg.get("agent_models", {})
    if prov_models:
        coord = prov_models.get("coordinator") or list(prov_models.values())[0]
        dev   = prov_models.get("developer") or coord
        return {
            "coordinator":         os.getenv("COORD_MODEL", coord),
            "product_manager":     os.getenv("PM_MODEL",    prov_models.get("product_manager", coord)),
            "software_architect":  os.getenv("ARCH_MODEL",  prov_models.get("software_architect", coord)),
            "developer":           os.getenv("DEV_MODEL",   dev),
            "qa_tester":           os.getenv("QA_MODEL",    prov_models.get("qa_tester", dev)),
            "reviewer":            os.getenv("REV_MODEL",   prov_models.get("reviewer", coord)),
            "optimizer":           prov_models.get("optimizer", dev),
            "security_auditor":    prov_models.get("security_auditor", dev),
            "documentation_writer":prov_models.get("documentation_writer", dev),
            "devops_engineer":     prov_models.get("devops_engineer", dev),
            "custom":              prov_models.get("custom", coord),
        }

    try:
        from settings import settings as _s
        default = _s.default_model
        p_model = _s.planning_model or default
        c_model = _s.code_model or default
        f_model = _s.micro_fix_model or default
        coord   = _s.coordinator_model or default
    except Exception:
        default = "ollama/qwen3.8:latest"
        p_model = c_model = f_model = coord = default

    return {
        "coordinator":         os.getenv("COORD_MODEL", coord),
        "product_manager":     os.getenv("PM_MODEL",    p_model),
        "software_architect":  os.getenv("ARCH_MODEL",  p_model),
        "developer":           os.getenv("DEV_MODEL",   c_model),
        "qa_tester":           os.getenv("QA_MODEL",    f_model),
        "reviewer":            os.getenv("REV_MODEL",   c_model),
        "optimizer":           f_model,
        "security_auditor":    c_model,
        "documentation_writer":c_model,
        "devops_engineer":     c_model,
        "custom":              default,
    }


LLM_PARAMS   = _get_llm_params()
AGENT_MODELS = _get_agent_models()
REPOMAP_TOKENS = int(os.getenv("REPOMAP_TOKENS", "2048"))


def reload_config():
    global PLANNING_MODEL, ESCALATION_MODEL, MICRO_FIX_MODEL, MICRO_FIX_MAX_TRIES
    _pm, _cm, _mfm, _mft, _fac = _get_pipeline_models()
    PLANNING_MODEL      = _pm
    ESCALATION_MODEL    = _cm
    MICRO_FIX_MODEL     = _mfm
    MICRO_FIX_MAX_TRIES = _mft
    LLM_PARAMS.clear()
    LLM_PARAMS.update(_get_llm_params())
    AGENT_MODELS.clear()
    AGENT_MODELS.update(_get_agent_models())


def print_config():
    from permission_manager import permission_manager
    active_name = get_active_provider_name()
    cfg = get_provider_config(active_name)
    print("\n" + "=" * 50)
    print("  Multi-Agent Sistem v2.1")
    print("=" * 50)
    print(f"  Saglayici : {cfg.get('label', active_name)}")
    print(f"  Aktif Diz : {get_output_dir()}")
    print(f"  Guvenlik  : {permission_manager.get_status_badge()}")
    print(f"  Temp      : {LLM_PARAMS['temperature']}")
    print(f"  MaxTok    : {LLM_PARAMS['max_tokens']}")
    print("\n  Ajan -> Model:")
    for ajan, model in AGENT_MODELS.items():
        short = model.split("/")[-1]
        print(f"    {ajan:<24} -> {short}")
    print()
