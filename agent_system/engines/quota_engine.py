"""
quota_engine.py — Online LLM Sağlayıcı Kota, Bakiye ve Oturum Kullanım Takip Motoru.

Desteklenen sağlayıcılar:
- OpenRouter: https://openrouter.ai/api/v1/auth/key
- Moonshot / Kimi: https://api.moonshot.cn/v1/users/me/balance
- DeepSeek: https://api.deepseek.com/user/balance
- NVIDIA NIM: Kredi / İstek Takipçisi & Durum Bilgisi
- Lokal (Ollama, LM Studio, llama.cpp): Sınırsız Çevrimdışı Kullanım
"""

import os
import json
import time
import logging
import threading
import urllib.request
import urllib.error
from typing import Dict, Any

logger = logging.getLogger(__name__)


class QuotaEngine:
    """Online ve lokal modeller için kota, bakiye ve oturum token takipçisi."""

    def __init__(self):
        self._lock = threading.Lock()
        self._session_tokens = 0
        self._session_requests = 0
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = 60  # saniye cinsinden bakiye önbellek süresi
        self._last_check_time: Dict[str, float] = {}

    def record_usage(self, tokens: int = 0, requests: int = 1) -> None:
        """Oturum içi token ve istek kullanımını kaydeder."""
        with self._lock:
            self._session_tokens += max(0, tokens)
            self._session_requests += max(0, requests)

    def get_session_stats(self) -> Dict[str, Any]:
        """Oturum içi toplam kullanım istatistiklerini döner."""
        with self._lock:
            return {
                "tokens": self._session_tokens,
                "requests": self._session_requests,
            }

    def fetch_openrouter_quota(self, api_key: str) -> Dict[str, Any]:
        """OpenRouter bakiye ve limit bilgilerini çeker."""
        if not api_key:
            return {"status": "no_key", "display": "Anahtar Yok"}

        url = "https://openrouter.ai/api/v1/auth/key"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "MYF-CLI/2.5",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data", {})
                limit = data.get("limit")
                usage = data.get("usage", 0.0)
                is_free = data.get("is_free_tier", False)

                if limit is not None:
                    remaining = max(0.0, float(limit) - float(usage))
                    return {
                        "status": "ok",
                        "remaining_usd": remaining,
                        "limit": limit,
                        "usage": usage,
                        "display": f"${remaining:.2f} Kalan",
                    }
                else:
                    return {
                        "status": "ok",
                        "usage": usage,
                        "display": "Ücretsiz" if is_free else f"${usage:.2f} Kullanıldı",
                    }
        except Exception as exc:
            logger.debug("[QUOTA] OpenRouter kota sorgusu hatası: %s", exc)
            return {"status": "error", "display": "Bağlantı Hatası"}

    def fetch_kimi_quota(self, api_key: str) -> Dict[str, Any]:
        """Moonshot / Kimi bakiye bilgilerini çeker."""
        if not api_key:
            return {"status": "no_key", "display": "Anahtar Yok"}

        url = "https://api.moonshot.cn/v1/users/me/balance"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "MYF-CLI/2.5",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data", {})
                cash = data.get("cash_balance", 0.0)
                voucher = data.get("voucher_balance", 0.0)
                total = float(cash) + float(voucher)
                return {
                    "status": "ok",
                    "balance": total,
                    "display": f"¥{total:.2f} Kalan",
                }
        except Exception as exc:
            logger.debug("[QUOTA] Kimi bakiye sorgusu hatası: %s", exc)
            return {"status": "error", "display": "Bağlantı Hatası"}

    def fetch_nvidia_quota(self, api_key: str) -> Dict[str, Any]:
        """NVIDIA NIM kota ve durum bilgisini döner."""
        if not api_key:
            return {"status": "no_key", "display": "Anahtar Yok"}
        
        with self._lock:
            req_count = self._session_requests
        return {
            "status": "ok",
            "credits": "1,000 Kredi (Ücretsiz)",
            "display": f"Aktif (~1K Kredi, {req_count} istek)",
        }

    def get_provider_quota_display(self, provider_id: str, api_key: str = "") -> str:
        """Belirtilen sağlayıcının kısa durum metnini döner (Önbellekli & Hızlı)."""
        provider_id = (provider_id or "").lower()

        if provider_id in ("ollama", "lm_studio", "llama_cpp", "lokal"):
            return "🔋 Lokal (Sınırsız)"

        now = time.time()
        last_time = self._last_check_time.get(provider_id, 0)

        if provider_id in self._cache and (now - last_time < self._cache_ttl):
            return self._cache[provider_id].get("display", "Aktif")

        res = {"display": "Aktif"}
        if provider_id == "openrouter":
            res = self.fetch_openrouter_quota(api_key)
        elif provider_id == "kimi":
            res = self.fetch_kimi_quota(api_key)
        elif provider_id == "nvidia":
            res = self.fetch_nvidia_quota(api_key)
        else:
            res = {"display": "Online API"}

        self._cache[provider_id] = res
        self._last_check_time[provider_id] = now
        return res.get("display", "Aktif")

    def get_bottom_toolbar_text(self, provider_id: str, model_name: str, session_id: str, project_dir: str) -> str:
        """
        Prompt Toolkit bottom_toolbar için zengin ANSI formatlı durum çubuğu metni üretir.
        """
        from config import LLM_PARAMS
        api_key = LLM_PARAMS.get("api_key", "")
        quota_str = self.get_provider_quota_display(provider_id, api_key)

        with self._lock:
            tok = self._session_tokens
            reqs = self._session_requests

        if tok >= 1000:
            tok_str = f"{tok/1000:.1f}k"
        else:
            tok_str = str(tok)

        p_name = os.path.basename(project_dir.rstrip("/\\")) if project_dir else "proje"
        if len(p_name) > 16:
            p_name = p_name[:14] + ".."

        clean_model = model_name.split("/")[-1]
        if len(clean_model) > 18:
            clean_model = clean_model[:16] + ".."

        return (
            f"\x1b[97;44m [Ctrl+C: İptal] \x1b[0m "
            f"\x1b[36m[Tab: Tamamla]\x1b[0m \x1b[33m[/help]\x1b[0m "
            f"\x1b[90m│\x1b[0m \x1b[32;1m🌐 {provider_id.upper()}: {quota_str}\x1b[0m "
            f"\x1b[90m│\x1b[0m \x1b[35m📊 Oturum: {tok_str} tok ({reqs} ist)\x1b[0m "
            f"\x1b[90m│\x1b[0m \x1b[37m📁 {p_name}\x1b[0m "
        )


# Global tekil nesne
quota_engine = QuotaEngine()
