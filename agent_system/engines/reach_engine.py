"""
reach_engine.py — Agent-Reach Tabanlı Dış Dünya ve Web Araştırma Motoru

Özellikler:
  - Sıfır API maliyetiyle GitHub, dokümantasyon, Reddit ve web aramalarını gerçekleştirir.
  - Güvenlik / Virüs Koruması: Yalnızca metin/markdown içeriği okur.
    İkili/çalıştırılabilir (.exe, .bat, .zip vb.) dosyaların indirilmesi KESİNLİKLE engellenir.
  - İzin Yöneticisi (permission_manager) ile entegredir.
  - Kısa süreli (TTL) önbellek: aynı sorgu/URL bir pipeline oturumu içinde tekrar
    istenirse gereksiz ağ trafiği ve gecikme oluşturmaz.
  - İki katmanlı arama: birincil kaynak (DuckDuckGo HTML) başarısız olursa
    ikincil kaynağa (DuckDuckGo Instant Answer JSON API) otomatik düşer —
    tek bir HTML yapısı değişikliğinin aramayı tamamen kırmasını önler.
  - read_url: birincil olarak Jina Reader (temiz markdown) dener; o başarısız
    olursa DOĞRUDAN URL'yi çekip basit HTML->metin ayıklamasıyla devam eder
    (tek nokta bağımlılığı azaltılır).
"""

from __future__ import annotations
import re
import time
import logging
import urllib.request
import urllib.parse
import urllib.error
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from permission_manager import permission_manager

logger = logging.getLogger(__name__)

# Yasaklı uzantılar (Güvenlik / Virüs Engelleme)
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".dll", ".so",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".iso", ".bin", ".scr"
}

# Önbellek TTL'i (saniye) — bir pipeline oturumu tipik olarak dakikalar
# sürdüğünden, 15 dakikalık bir pencere tekrarlanan aramaları önlemek için
# yeterli ama sonuçların çok bayatlamasını da engelleyecek kadar kısa.
_CACHE_TTL_SECONDS = 15 * 60


class _SimpleTextExtractor(HTMLParser):
    """
    bs4 mevcut değilse veya doğrudan-fetch fallback'inde kullanılan, bağımlılık
    gerektirmeyen minimal HTML->düz metin ayıklayıcı (script/style içeriğini atar).
    """

    def __init__(self):
        super().__init__()
        self._skip_tags = {"script", "style", "noscript", "head"}
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self.chunks)


def _extract_text_from_html(html: str) -> str:
    """bs4 varsa onu, yoksa bağımlılıksız _SimpleTextExtractor'ı kullanarak metin çıkarır."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "head"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        parser = _SimpleTextExtractor()
        try:
            parser.feed(html)
        except Exception:
            pass
        return parser.get_text()


class ReachEngine:
    """Ajanlara web, doküman ve GitHub araştırma yeteneği sağlayan güvenli motor."""

    def __init__(self):
        self._has_agent_reach = False
        try:
            import agent_reach
            self._has_agent_reach = True
        except ImportError:
            self._has_agent_reach = False

        # (sonuc_metni, kaydedilme_zamani) — anahtar: normalize edilmiş sorgu/URL
        self._search_cache: Dict[str, Tuple[str, float]] = {}
        self._url_cache: Dict[str, Tuple[str, float]] = {}

    # ────────────────────────────────────────────────────────────────
    # Önbellek Yardımcıları
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_get(cache: Dict[str, Tuple[str, float]], key: str) -> Optional[str]:
        entry = cache.get(key)
        if not entry:
            return None
        value, saved_at = entry
        if (time.time() - saved_at) > _CACHE_TTL_SECONDS:
            cache.pop(key, None)
            return None
        return value

    @staticmethod
    def _cache_set(cache: Dict[str, Tuple[str, float]], key: str, value: str) -> None:
        cache[key] = (value, time.time())
        # Onbellek asiri buyumesin diye basit bir ust sinir (LRU degil, ama
        # yeterli: en eski 50'den fazlaysa en eski girdileri at).
        if len(cache) > 200:
            oldest_keys = sorted(cache, key=lambda k: cache[k][1])[:50]
            for k in oldest_keys:
                cache.pop(k, None)

    def clear_cache(self) -> None:
        """Test veya yeni oturum başında önbelleği elle temizlemek için."""
        self._search_cache.clear()
        self._url_cache.clear()

    # ────────────────────────────────────────────────────────────────
    # Web Arama (Birincil: DDG HTML, İkincil: DDG Instant Answer JSON)
    # ────────────────────────────────────────────────────────────────

    def search_web(self, query: str, max_results: int = 5) -> str:
        """
        Web üzerinde arama yapar ve özet markdown döner. Aynı sorgu TTL süresi
        içinde tekrar istenirse önbellekten döner (ağa gitmez).
        """
        if not permission_manager.check_permission("network", f"search://{query}", agent_name="reach_engine"):
            return "❌ Web arama izni reddedildi."

        cache_key = f"{query.strip().lower()}::{max_results}"
        cached = self._cache_get(self._search_cache, cache_key)
        if cached is not None:
            logger.info("Web araması önbellekten döndürüldü: %s", query)
            return cached

        logger.info("Web araması yapılıyor: %s", query)

        result, primary_error = self._search_web_primary(query, max_results)
        if result is not None:
            self._cache_set(self._search_cache, cache_key, result)
            return result

        # Birincil kaynak basarisiz oldu -> ikincil kaynagi dene
        logger.warning("Birincil arama kaynağı başarısız (%s), ikincil kaynağa geçiliyor.", primary_error)
        result, secondary_error = self._search_web_fallback(query, max_results)
        if result is not None:
            self._cache_set(self._search_cache, cache_key, result)
            return result

        # İkisi de başarısız — hatanın GERÇEK nedenini (eksik paket mi, ağ mı) belirt
        if isinstance(primary_error, ImportError):
            msg = (
                "❌ Web arama başarısız: 'beautifulsoup4' paketi kurulu değil. "
                "'pip install beautifulsoup4' ile kurup tekrar deneyin. "
                "(İkincil kaynak da sonuç veremedi.)"
            )
        else:
            msg = (
                f"❌ Web araması başarısız oldu (birincil: {primary_error}; "
                f"ikincil: {secondary_error}). Ağ bağlantısını kontrol edin."
            )
        logger.warning(msg)
        return msg

    def _search_web_primary(self, query: str, max_results: int) -> tuple[Optional[str], Optional[Exception]]:
        """Birincil kaynak: DuckDuckGo HTML Lite (POST ile canlı web araması)."""
        try:
            data = urllib.parse.urlencode({"q": query}).encode("utf-8")
            url = "https://html.duckduckgo.com/html/"
            req = urllib.request.Request(url, data=data, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Content-Type": "application/x-www-form-urlencoded"
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            results = []
            for item in soup.select(".result"):
                title_elem = item.select_one(".result__title a")
                snippet_elem = item.select_one(".result__snippet")
                if title_elem and snippet_elem:
                    title = title_elem.get_text(strip=True)
                    link = title_elem.get("href", "")
                    snippet = snippet_elem.get_text(strip=True)
                    results.append(f"### [{title}]({link})\n{snippet}\n")
                if len(results) >= max_results:
                    break

            if results:
                return f"## Canlı Web Arama Sonuçları: {query}\n\n" + "\n".join(results), None
            return None, RuntimeError("Birincil kaynak sonuç döndürmedi")
        except ImportError as exc:
            return None, exc
        except Exception as exc:
            logger.warning("Birincil web arama hatası: %s", exc)
            return None, exc

    def _search_web_fallback(self, query: str, max_results: int) -> tuple[Optional[str], Optional[Exception]]:
        """
        İkincil kaynak: DuckDuckGo Instant Answer JSON API.
        Kapsamı birincil HTML aramasından DAR olabilir (her sorguda sonuç
        garanti etmez) ama HTML yapısı değişse bile çalışmaya devam eder,
        çünkü tamamen farklı bir uç nokta ve format (JSON) kullanır.
        """
        try:
            params = urllib.parse.urlencode({
                "q": query, "format": "json", "no_html": "1", "skip_disambig": "1",
            })
            url = f"https://api.duckduckgo.com/?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))

            results = []
            if payload.get("AbstractText"):
                results.append(
                    f"### {payload.get('Heading', query)}\n{payload['AbstractText']}\n"
                    f"({payload.get('AbstractURL', '')})\n"
                )
            for topic in payload.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(f"- {topic['Text']} ({topic.get('FirstURL', '')})")

            if results:
                return (
                    f"## Web Arama Sonuçları (İkincil Kaynak): {query}\n\n" + "\n".join(results)
                    + "\n\n_Not: Bu sonuçlar ikincil kaynaktan (DDG Instant Answer) geldi, "
                    "birincil kaynak (HTML arama) geçici olarak kullanılamadı; kapsam daha "
                    "dar olabilir._"
                ), None
            return None, RuntimeError("İkincil kaynak da sonuç döndürmedi")
        except Exception as exc:
            logger.warning("İkincil web arama hatası: %s", exc)
            return None, exc

    # ────────────────────────────────────────────────────────────────
    # URL Okuma (Birincil: Jina Reader, İkincil: Doğrudan Fetch)
    # ────────────────────────────────────────────────────────────────

    def read_url(self, url: str) -> str:
        """
        Bir web sayfasının veya GitHub dosyasının metin/markdown içeriğini okur.
        Güvenlik: İkili dosya indirilmesi yasaktır!
        Önce Jina Reader (temiz markdown) dener; o başarısız olursa (zaman
        aşımı, ağ hatası, servis kesintisi) URL'yi DOĞRUDAN çekip basit
        HTML->metin ayıklamasıyla devam eder — tek servise bağımlılığı azaltır.
        """
        clean_url = url.split("?")[0].lower()
        for ext in BLOCKED_EXTENSIONS:
            if clean_url.endswith(ext):
                return f"❌ Güvenlik Uyarısı: '{ext}' uzantılı ikili/çalıştırılabilir dosyaları indirmek yasaktır."

        if not permission_manager.check_permission("network", url, agent_name="reach_engine"):
            return f"❌ '{url}' adresine erişim izni verilmedi."

        cache_key = url.strip()
        cached = self._cache_get(self._url_cache, cache_key)
        if cached is not None:
            logger.info("URL içeriği önbellekten döndürüldü: %s", url)
            return cached

        content, err = self._read_url_via_jina(url)
        if content is None:
            logger.warning("Jina Reader başarısız (%s), doğrudan fetch deneniyor: %s", err, url)
            content, err2 = self._read_url_direct(url)
            if content is None:
                msg = f"Sayfa okunamadı ({url}): birincil hata={err}; doğrudan fetch hatası={err2}"
                logger.warning(msg)
                return msg

        self._cache_set(self._url_cache, cache_key, content)
        return content

    def _read_url_via_jina(self, url: str) -> tuple[Optional[str], Optional[Exception]]:
        target_url = f"https://r.jina.ai/{url}" if not url.startswith("https://r.jina.ai/") else url
        try:
            req = urllib.request.Request(target_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                if len(content) > 12000:
                    content = content[:12000] + "\n\n... (İçerik kırpıldı)"
                return f"## Kaynak: {url}\n\n{content}", None
        except Exception as exc:
            return None, exc

    def _read_url_direct(self, url: str) -> tuple[Optional[str], Optional[Exception]]:
        """Jina Reader kullanılamadığında sayfayı doğrudan çekip basit metin çıkarımı yapar."""
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            text = _extract_text_from_html(raw)
            if len(text) > 12000:
                text = text[:12000] + "\n\n... (İçerik kırpıldı)"
            return f"## Kaynak (doğrudan erişim): {url}\n\n{text}", None
        except Exception as exc:
            return None, exc

    def search_github_repos(self, topic_or_query: str, max_results: int = 4) -> str:
        """GitHub üzerinde açık kaynaklı repo ve dokümanları araştırır."""
        query = f"site:github.com {topic_or_query}"
        return self.search_web(query, max_results=max_results)


# Singleton
reach_engine = ReachEngine()
