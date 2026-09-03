"""
settings.py — Kullanici ayarlari (genisletilmis).

settings.json'dan okur, degisiklikleri otomatik diske kaydeder.
Singleton gibi davranir: `from settings import settings`

Kategoriler:
  - Arayuz    : coordinator_name, think_mode, warmup, theme
  - LLM       : temperature, max_tokens, micro_fix_max_tries
  - Pipeline  : planning_model, code_model, micro_fix_model, escalation_model
  - RepoMap   : repomap_tokens
  - Guvenlik  : permission_mode
"""

from __future__ import annotations
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any


_HERE = Path(__file__).parent
_SYSTEM_ROOT = _HERE.parent
_PATH = _SYSTEM_ROOT / "settings.json" if (_SYSTEM_ROOT / "settings.json").exists() else _HERE / "settings.json"
logger = logging.getLogger(__name__)

_MODEL_KEYS = {
    "default_model", "planning_model", "code_model", "micro_fix_model", "coordinator_model",
}

# ─────────────────────────────────────────────
# Varsayilan ayarlar
# ─────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    # ── Model Yönetimi (Tek Noktadan Kontrol) ─
    "default_model":     "ollama/qwen3.5:4b",

    # ── Arayuz ──────────────────────────────
    "coordinator_name":  "MYF-Agent",
    "think_mode":        False,
    "warmup":            False,
    "theme":             "cyan",

    # ── LLM Parametreleri ────────────────────
    "temperature":       0.2,
    "max_tokens":        8192,

    # ── Pipeline Modelleri (default_model ile otomatik senkron) ──
    # Planlama (PRD + Mimari)
    "planning_model":    "ollama/qwen3.5:4b",
    # Agir kod uretimi + escalation + dokumantasyon
    "code_model":        "ollama/qwen3.5:4b",
    # Micro-Fix: VRAM'de hazir bekleyen hizli onarim modeli
    "micro_fix_model":   "ollama/qwen3.5:4b",
    # Koordinator (sohbet modeli)
    "coordinator_model": "ollama/qwen3.5:4b",

    # ── Micro-Fix / Escalation ───────────────
    # Kac basarisiz denemeden sonra escalation tetiklensin
    "micro_fix_max_tries": 3,
    # Full otonomi modunda maksimum pipeline adimi
    "full_autonomy_cap":   50,

    # ── RepoMap ──────────────────────────────
    "repomap_tokens":    2048,

    # ── Guvenlik & Denetim ───────────────────
    "permission_mode":   "ask",
    "auto_audit_log":    True,
    "execution_mode":    "sequential",  # "sequential" (1), "subagent" (2), "interactive" (3)
}


class Settings:
    """
    Kullanici ayarlarini yoneten sinif.

    Kullanim:
        from settings import settings

        # Okuma
        print(settings.planning_model)
        print(settings.temperature)

        # Yazma (otomatik diske kaydeder)
        settings.planning_model = "ollama/gemma4:31b"
        settings.temperature = 0.5

        # Genel set()
        settings.set("micro_fix_max_tries", "5")
    """

    def __init__(self, path: Path = _PATH):
        self._path = path
        self._data = self._load()

    # ── Okuma / Yazma ──────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            self._path.write_text(
                json.dumps(_DEFAULTS, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return dict(_DEFAULTS)
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            # Yeni eklenen anahtarlari default ile tamamla
            for k, v in _DEFAULTS.items():
                raw.setdefault(k, v)
            return raw
        except (json.JSONDecodeError, OSError):
            return dict(_DEFAULTS)

    def save(self, *, sync_models: bool = False) -> None:
        """Ayarları atomik yaz; model dosyalarını yalnızca gerektiğinde senkronize et."""
        filtered = {k: v for k, v in self._data.items() if not k.startswith("_")}
        self._atomic_write_json(self._path, filtered)
        if sync_models:
            self._sync_with_agent_and_provider_configs()

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        """Dosya yarım yazılamasın diye geçici dosya + os.replace kullan."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.error("Ayar dosyası yazılamadı (%s): %s", path, exc)
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def _sync_with_agent_and_provider_configs(self) -> None:
        """Model ayarlarini agents_config.json ve providers_config.json ile senkronize et."""
        # 1. agents_config.json senkronizasyonu
        agents_cfg_path = _SYSTEM_ROOT / "agents" / "agents_config.json"
        if agents_cfg_path.exists():
            try:
                agents_data = json.loads(agents_cfg_path.read_text(encoding="utf-8"))
                for ag in agents_data.get("agents", []):
                    role = ag.get("role_type", "")
                    if role in ("product_manager", "software_architect"):
                        ag["model"] = self.planning_model
                    elif role in ("developer", "reviewer", "documentation_writer", "devops_engineer", "security_auditor"):
                        ag["model"] = self.code_model
                    elif role in ("qa_tester", "optimizer"):
                        ag["model"] = self.micro_fix_model
                    elif role == "coordinator":
                        ag["model"] = self.coordinator_model
                self._atomic_write_json(agents_cfg_path, agents_data)
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("Agent model senkronizasyonu başarısız (%s): %s", agents_cfg_path, exc)

        # 2. providers_config.json senkronizasyonu (Ollama icin)
        providers_cfg_path = _SYSTEM_ROOT / "llm" / "providers_config.json"
        if providers_cfg_path.exists():
            try:
                prov_data = json.loads(providers_cfg_path.read_text(encoding="utf-8"))
                ollama_cfg = prov_data.get("providers", {}).get("ollama", {})
                if "agent_models" in ollama_cfg:
                    ollama_cfg["agent_models"]["coordinator"] = self.coordinator_model
                    ollama_cfg["agent_models"]["product_manager"] = self.planning_model
                    ollama_cfg["agent_models"]["software_architect"] = self.planning_model
                    ollama_cfg["agent_models"]["developer"] = self.code_model
                    ollama_cfg["agent_models"]["qa_tester"] = self.micro_fix_model
                    ollama_cfg["agent_models"]["reviewer"] = self.code_model
                    ollama_cfg["agent_models"]["optimizer"] = self.micro_fix_model
                    ollama_cfg["agent_models"]["security_auditor"] = self.code_model
                    ollama_cfg["agent_models"]["documentation_writer"] = self.code_model
                    ollama_cfg["agent_models"]["devops_engineer"] = self.code_model
                    ollama_cfg["agent_models"]["custom"] = self.code_model
                self._atomic_write_json(providers_cfg_path, prov_data)
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("Sağlayıcı model senkronizasyonu başarısız (%s): %s", providers_cfg_path, exc)

    # ── Arayuz ─────────────────────────────────────────────

    @property
    def coordinator_name(self) -> str:
        return self._data.get("coordinator_name", "MYF-Agent")

    @coordinator_name.setter
    def coordinator_name(self, value: str) -> None:
        self._data["coordinator_name"] = value.strip()
        self.save()

    @property
    def think_mode(self) -> bool:
        return bool(self._data.get("think_mode", False))

    @think_mode.setter
    def think_mode(self, value: bool) -> None:
        self._data["think_mode"] = bool(value)
        self.save()

    @property
    def warmup(self) -> bool:
        return bool(self._data.get("warmup", False))

    @warmup.setter
    def warmup(self, value: bool) -> None:
        self._data["warmup"] = bool(value)
        self.save()

    @property
    def theme(self) -> str:
        return self._data.get("theme", "cyan")

    @theme.setter
    def theme(self, value: str) -> None:
        self._data["theme"] = value
        self.save()

    # ── LLM Parametreleri ───────────────────────────────────

    @property
    def temperature(self) -> float:
        return float(self._data.get("temperature", 0.3))

    @temperature.setter
    def temperature(self, value: float) -> None:
        self._data["temperature"] = max(0.0, min(2.0, float(value)))
        self.save()

    @property
    def max_tokens(self) -> int:
        return int(self._data.get("max_tokens", 8192))

    @max_tokens.setter
    def max_tokens(self, value: int) -> None:
        self._data["max_tokens"] = max(256, int(value))
        self.save()

    # ── Pipeline Modelleri ──────────────────────────────────

    @property
    def default_model(self) -> str:
        return self._data.get("default_model", self.code_model)

    @default_model.setter
    def default_model(self, value: str) -> None:
        self.set_model_all(value)

    def set_model_all(self, model_name: str) -> str:
        """Tüm sistem modellerini tek seferde günceller ve her yere senkronize eder."""
        m = str(model_name).strip()
        if not m:
            return self.default_model
        if "/" not in m and not any(m.startswith(p) for p in ("ollama", "openrouter", "moonshot", "lm_studio")):
            m = f"ollama/{m}"
        self._data["default_model"] = m
        self._data["planning_model"] = m
        self._data["code_model"] = m
        self._data["micro_fix_model"] = m
        self._data["coordinator_model"] = m
        self.save(sync_models=True)
        try:
            from config import reload_config
            reload_config()
        except Exception:
            pass
        return m

    @property
    def planning_model(self) -> str:
        return self._data.get("planning_model", self._data.get("default_model", _DEFAULTS["planning_model"]))

    @planning_model.setter
    def planning_model(self, value: str) -> None:
        self._data["planning_model"] = value.strip()
        self.save(sync_models=True)

    @property
    def code_model(self) -> str:
        return self._data.get("code_model", _DEFAULTS["code_model"])

    @code_model.setter
    def code_model(self, value: str) -> None:
        self._data["code_model"] = value.strip()
        self.save(sync_models=True)

    @property
    def micro_fix_model(self) -> str:
        return self._data.get("micro_fix_model", _DEFAULTS["micro_fix_model"])

    @micro_fix_model.setter
    def micro_fix_model(self, value: str) -> None:
        self._data["micro_fix_model"] = value.strip()
        self.save(sync_models=True)

    @property
    def coordinator_model(self) -> str:
        return self._data.get("coordinator_model", _DEFAULTS["coordinator_model"])

    @coordinator_model.setter
    def coordinator_model(self, value: str) -> None:
        self._data["coordinator_model"] = value.strip()
        self.save(sync_models=True)

    # ── Micro-Fix / Escalation ──────────────────────────────

    @property
    def micro_fix_max_tries(self) -> int:
        return int(self._data.get("micro_fix_max_tries", 3))

    @micro_fix_max_tries.setter
    def micro_fix_max_tries(self, value: int) -> None:
        self._data["micro_fix_max_tries"] = max(1, min(10, int(value)))
        self.save()

    @property
    def full_autonomy_cap(self) -> int:
        return int(self._data.get("full_autonomy_cap", 50))

    @full_autonomy_cap.setter
    def full_autonomy_cap(self, value: int) -> None:
        self._data["full_autonomy_cap"] = max(5, min(200, int(value)))
        self.save()

    # ── RepoMap ─────────────────────────────────────────────

    @property
    def repomap_tokens(self) -> int:
        return int(self._data.get("repomap_tokens", 2048))

    @repomap_tokens.setter
    def repomap_tokens(self, value: int) -> None:
        self._data["repomap_tokens"] = max(512, min(16384, int(value)))
        self.save()

    # ── Guvenlik & Denetim ───────────────────

    @property
    def permission_mode(self) -> str:
        return str(self._data.get("permission_mode", "ask"))

    @permission_mode.setter
    def permission_mode(self, value: str) -> None:
        valid = ("ask", "session_allow", "always_allow")
        if value in valid:
            self._data["permission_mode"] = value
            self.save()

    @property
    def auto_audit_log(self) -> bool:
        return bool(self._data.get("auto_audit_log", True))

    @auto_audit_log.setter
    def auto_audit_log(self, value: bool) -> None:
        self._data["auto_audit_log"] = bool(value)
        self.save()

    @property
    def execution_mode(self) -> str:
        return str(self._data.get("execution_mode", "sequential"))

    @execution_mode.setter
    def execution_mode(self, value: str) -> None:
        mode_map = {
            "1": "sequential",
            "sequential": "sequential",
            "2": "subagent",
            "subagent": "subagent",
            "swarm": "subagent",
            "3": "interactive",
            "interactive": "interactive",
            "chat": "interactive",
        }
        val_str = str(value).strip().lower()
        if val_str in mode_map:
            self._data["execution_mode"] = mode_map[val_str]
            self.save()

    # ── Yardimci Metodlar ───────────────────────────────────

    def toggle_think(self) -> bool:
        """Think modunu ac/kapat. Yeni deger dondurur."""
        self.think_mode = not self.think_mode
        return self.think_mode

    def as_dict(self) -> dict[str, Any]:
        """Tum ayarlari dict olarak dondur."""
        return {k: v for k, v in self._data.items() if not k.startswith("_")}

    def set(self, key: str, value: Any) -> bool:
        """
        Anahtar-deger ile herhangi bir ayari guncelle.
        Bilinmeyen anahtar ise False dondurur.
        """
        if key not in _DEFAULTS:
            return False
        if key == "default_model":
            self.set_model_all(str(value))
            return True
        expected_type = type(_DEFAULTS[key])
        try:
            if expected_type == bool:
                converted = str(value).lower() in ("true", "1", "evet", "on", "yes")
            else:
                converted = expected_type(value)
        except (ValueError, TypeError):
            return False
        self._data[key] = converted
        self.save(sync_models=key in _MODEL_KEYS)
        return True

    def get_model_summary(self) -> str:
        """Pipeline modellerini okunabilir formatta dondur."""
        lines = [
            f"  Planlama (PM+Mimar) : {self.planning_model}",
            f"  Kod Uretimi+Escalation: {self.code_model}",
            f"  Micro-Fix           : {self.micro_fix_model}",
            f"  Koordinator (sohbet): {self.coordinator_model}",
            f"  Micro-Fix max deneme: {self.micro_fix_max_tries}",
            f"  Full Otonomi cap    : {self.full_autonomy_cap}",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Settings({self.as_dict()})"


# Modul seviyesinde singleton
settings = Settings()
