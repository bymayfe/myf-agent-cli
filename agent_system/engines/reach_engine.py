"""
reach_engine.py — Hibrit Dış Dünya ve Web Araştırma Motoru

Kanal hiyerarşisi (her kanal başarısız olursa bir sonrakine düşer):

  Sıfır-kurulum kanallar (her zaman çalışır):
    • DuckDuckGo HTML → DDG Lite → DDG Instant Answer  (genel web)
    • Jina Reader → doğrudan fetch                     (URL okuma)
    • NPM / PyPI resmi registry                        (paket sorgulama)
    • V2EX public JSON API                             (tech forum)
    • RSS/Atom (feedparser)                            (haber beslemeleri)

  Araç gerektiren kanallar (kuruluysa aktif, yoksa graceful fallback):
    • GitHub gh CLI                                    (repo/code/issue arama)
    • YouTube yt-dlp                                   (video metadata + altyazı)
    • Twitter twitter-cli (cookie gerekir)             (tweet arama)
    • Reddit rdt-cli / opencli (cookie gerekir)        (subreddit okuma)

  Güvenlik katmanı (tüm kanallar için):
    • permission_manager.check_permission() → izinsiz ağ erişimi yok
    • BLOCKED_EXTENSIONS → ikili/çalıştırılabilir dosya indirimi engeli
    • 15 dk TTL önbellek → tekrarlanan sorgularda ağ trafiği yok

  Akıllı yönlendirici:
    • search(query) → sorgu/URL analiz edip en uygun kanala yönlendirir
    • channel_status() → tüm kanalların canlı durumunu döner
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from permission_manager import permission_manager

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Sabitler
# ──────────────────────────────────────────────────────────────

# Yasaklı uzantılar (Güvenlik / Virüs Engelleme)
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".dll", ".so",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".iso", ".bin", ".scr"
}

# Önbellek TTL (saniye)
_CACHE_TTL_SECONDS = 15 * 60

# GitHub smart yönlendirme kalıpları
_GITHUB_PATTERNS = re.compile(
    r'\bgithub\b|github\.com|site:github\.com|'
    r'\b(?:repo|repository|repositories|open.?source)\b',
    re.IGNORECASE,
)
_YOUTUBE_PATTERNS = re.compile(
    r'youtube\.com|youtu\.be|'
    r'\b(?:youtube|video|youtube)\b',
    re.IGNORECASE,
)
_V2EX_PATTERNS = re.compile(
    r'v2ex\.com|site:v2ex\.com|\bv2ex\b',
    re.IGNORECASE,
)
_RSS_PATTERNS = re.compile(
    r'(?:/feed|/rss|\.xml|atom)(?:\?|$|/)',
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────
# HTML → Metin yardımcısı
# ──────────────────────────────────────────────────────────────

class _SimpleTextExtractor(HTMLParser):
    """
    bs4 mevcut değilse veya doğrudan-fetch fallback'inde kullanılan,
    bağımlılık gerektirmeyen minimal HTML→düz metin ayıklayıcı.
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
    """bs4 varsa onu, yoksa _SimpleTextExtractor'ı kullanarak metin çıkarır."""
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


# ──────────────────────────────────────────────────────────────
# Ana Motor
# ──────────────────────────────────────────────────────────────

class ReachEngine:
    """
    Ajanlara web, doküman, GitHub, YouTube, V2EX ve daha fazlasını
    araştırma yeteneği sağlayan güvenli, hibrit motor.
    """

    def __init__(self):
        # Panniantong/Agent-Reach kanal sınıfları (araç probe'u için)
        self._channels: Dict[str, Any] = {}
        self._channels_loaded = False

        # Önbellekler
        self._search_cache: Dict[str, Tuple[str, float]] = {}
        self._url_cache:    Dict[str, Tuple[str, float]] = {}

    # ──────────────────────────────────────────────────────────
    # Kanal yükleme (lazy — ilk kullanımda)
    # ──────────────────────────────────────────────────────────

    def _load_channels(self) -> None:
        if self._channels_loaded:
            return
        try:
            from agent_reach.channels import (
                GitHubChannel, YouTubeChannel, RSSChannel,
                V2EXChannel, WebChannel, RedditChannel, TwitterChannel,
            )
            self._channels = {
                "github":  GitHubChannel(),
                "youtube": YouTubeChannel(),
                "rss":     RSSChannel(),
                "v2ex":    V2EXChannel(),
                "web":     WebChannel(),
                "reddit":  RedditChannel(),
                "twitter": TwitterChannel(),
            }
            logger.info("Panniantong/Agent-Reach kanalları yüklendi.")
        except ImportError:
            logger.warning(
                "agent_reach paketi bulunamadı — kanal probe'ları devre dışı. "
                "Yüklemek için: pip install git+https://github.com/Panniantong/Agent-Reach.git"
            )
            self._channels = {}
        self._channels_loaded = True

    def _channel_ok(self, name: str) -> bool:
        """Belirtilen kanalın kurulu ve sağlıklı olup olmadığını kontrol eder."""
        self._load_channels()
        ch = self._channels.get(name)
        if ch is None:
            return False
        try:
            status, _ = ch.check()
            return status in ("ok", "warn")
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────
    # Önbellek yardımcıları
    # ──────────────────────────────────────────────────────────

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
        if len(cache) > 200:
            oldest = sorted(cache, key=lambda k: cache[k][1])[:50]
            for k in oldest:
                cache.pop(k, None)

    def clear_cache(self) -> None:
        """Test veya yeni oturum başında önbelleği elle temizle."""
        self._search_cache.clear()
        self._url_cache.clear()

    # ──────────────────────────────────────────────────────────
    # Kanal durumu
    # ──────────────────────────────────────────────────────────

    def channel_status(self) -> Dict[str, Dict[str, str]]:
        """
        Tüm kanalların anlık durumunu döner.

        Dönüş: {kanal_adı: {"status": "ok|warn|off|error", "message": str}}
        """
        self._load_channels()
        result: Dict[str, Dict[str, str]] = {}

        # Yerleşik kanallar (her zaman çalışır)
        result["web_ddg"]   = {"status": "ok",  "message": "DuckDuckGo HTML/Lite/API (3 katman)"}
        result["web_jina"]  = {"status": "ok",  "message": "Jina Reader + doğrudan fetch"}
        result["npm_pypi"]  = {"status": "ok",  "message": "NPM ve PyPI resmi registry"}

        # Probe gerektiren kanallar
        for name, ch in self._channels.items():
            try:
                status, message = ch.check()
                result[name] = {"status": status, "message": message}
            except Exception as exc:
                result[name] = {"status": "error", "message": str(exc)}

        return result

    def format_channel_status(self) -> str:
        """channel_status() çıktısını insan okunabilir markdown tablosuna çevirir."""
        status = self.channel_status()
        emoji = {"ok": "✅", "warn": "⚠️", "off": "❌", "error": "🔴"}
        lines = ["## 🌐 Reach Engine — Kanal Durumu\n", "| Kanal | Durum | Bilgi |", "|---|---|---|"]
        for name, info in status.items():
            icon = emoji.get(info["status"], "❓")
            lines.append(f"| `{name}` | {icon} {info['status']} | {info['message'][:80]} |")
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────
    # AKILLI YÖNLENDİRİCİ
    # ──────────────────────────────────────────────────────────

    def search(self, query: str, max_results: int = 5) -> str:
        """
        Sorguyu/URL'yi analiz edip en uygun kanala yönlendirir.

        Kural sırası:
          1. YouTube URL/keyword  → read_youtube() / search_web("youtube ...")
          2. GitHub URL/keyword   → search_github()
          3. V2EX URL/keyword     → search_v2ex()
          4. RSS URL              → read_rss()
          5. Genel               → search_web() (DDG stack)
        """
        q = query.strip()

        # YouTube
        if _YOUTUBE_PATTERNS.search(q):
            if "youtube.com" in q or "youtu.be" in q:
                return self.read_youtube(q)
            return self.search_web(f"youtube {q}", max_results=max_results)

        # GitHub
        if _GITHUB_PATTERNS.search(q):
            return self.search_github(q, max_results=max_results)

        # V2EX
        if _V2EX_PATTERNS.search(q):
            return self.search_v2ex(q, max_results=max_results)

        # RSS/Atom URL
        if _RSS_PATTERNS.search(q) or q.startswith("http"):
            if _RSS_PATTERNS.search(q):
                return self.read_rss(q, max_items=max_results * 2)

        # Genel web
        return self.search_web(q, max_results=max_results)

    # ──────────────────────────────────────────────────────────
    # KANAL 1: Web Arama (DuckDuckGo — 3 katman)
    # ──────────────────────────────────────────────────────────

    def search_web(self, query: str, max_results: int = 5) -> str:
        """
        Web araması yapar. Önce NPM/PyPI registry, sonra DDG HTML → Lite → Instant Answer.
        """
        if not permission_manager.check_permission("network", f"search://{query}", agent_name="reach_engine"):
            return "❌ Web arama izni reddedildi."

        cache_key = f"web::{query.strip().lower()}::{max_results}"
        cached = self._cache_get(self._search_cache, cache_key)
        if cached is not None:
            logger.info("Web araması önbellekten döndürüldü: %s", query)
            return cached

        logger.info("Web araması yapılıyor: %s", query)

        # 1. Resmi paket registry (NPM / PyPI)
        pkg_result = self._search_package_registry(query)

        # 2–4. DuckDuckGo katmanları
        result, primary_error = self._search_web_primary(query, max_results)
        if result is None:
            logger.info("Birincil arama boş (%s), Lite deneniyor...", primary_error)
            result, lite_error = self._search_web_lite(query, max_results)
        else:
            lite_error = None

        if result is None:
            logger.info("Lite arama da boş, Instant Answer deneniyor...")
            result, secondary_error = self._search_web_fallback(query, max_results)
        else:
            secondary_error = None

        # Birleştir
        if pkg_result:
            combined = f"## Araştırma Sonuçları: {query}\n\n{pkg_result}"
            if result:
                combined += f"\n---\n{result}"
            self._cache_set(self._search_cache, cache_key, combined)
            return combined

        if result is not None:
            self._cache_set(self._search_cache, cache_key, result)
            return result

        if isinstance(primary_error, ImportError):
            msg = "❌ Web arama başarısız: 'beautifulsoup4' kurulu değil. 'pip install beautifulsoup4' çalıştırın."
        else:
            msg = (
                f"❌ Web araması başarısız (birincil: {primary_error}; "
                f"lite: {lite_error}; ikincil: {secondary_error})."
            )
        logger.warning(msg)
        return msg

    def _search_package_registry(self, query: str) -> Optional[str]:
        """NPM / PyPI resmi registry sorgusu."""
        q = query.lower()

        npm_map = {
            "next": "next", "nextjs": "next", "next.js": "next",
            "react": "react", "reactjs": "react", "react-dom": "react-dom",
            "typescript": "typescript", "tailwindcss": "tailwindcss",
            "tailwind": "tailwindcss", "vue": "vue", "vuejs": "vue",
            "svelte": "svelte", "sveltekit": "@sveltejs/kit",
            "express": "express", "prisma": "prisma", "zustand": "zustand",
            "redux": "redux", "axios": "axios", "vite": "vite",
            "turbo": "turbo", "bun": "bun", "hono": "hono",
            "nestjs": "@nestjs/core", "remix": "@remix-run/react",
            "astro": "astro", "shadcn": "shadcn-ui",
            "lucide": "lucide-react", "zod": "zod",
        }
        pypi_map = {
            "fastapi": "fastapi", "django": "django", "flask": "flask",
            "pydantic": "pydantic", "sqlalchemy": "sqlalchemy",
            "celery": "celery", "pytest": "pytest", "requests": "requests",
            "numpy": "numpy", "pandas": "pandas", "torch": "torch",
            "pytorch": "torch", "transformers": "transformers",
            "langchain": "langchain", "litellm": "litellm",
            "uvicorn": "uvicorn",
        }

        explicit_npm  = re.search(r'\b(?:npm\s+i(?:nstall)?|package)\s+([a-zA-Z0-9_\-\@\/]+)', q)
        explicit_pypi = re.search(r'\b(?:pip\s+install|python\s+package)\s+([a-zA-Z0-9_\-]+)', q)

        matched_npm  = explicit_npm.group(1)  if explicit_npm  else None
        matched_pypi = explicit_pypi.group(1) if explicit_pypi else None

        if not matched_npm and not matched_pypi:
            tokens = re.findall(r'[a-zA-Z0-9_\.\-]+', q)
            for t in tokens:
                if t in npm_map:
                    matched_npm = npm_map[t]; break
                elif t in pypi_map:
                    matched_pypi = pypi_map[t]; break

        results = []

        if matched_npm:
            try:
                url = f"https://registry.npmjs.org/{matched_npm}/latest"
                req = urllib.request.Request(url, headers={"User-Agent": "MYF-Agent/1.0", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                pkg_name = data.get("name", matched_npm)
                version  = data.get("version", "Bilinmiyor")
                desc     = data.get("description", "")
                license_ = data.get("license", "MIT")
                results.append(
                    f"### 📦 NPM: `{pkg_name}`\n"
                    f"- **Sürüm:** `{version}` | **Lisans:** {license_}\n"
                    f"- **Açıklama:** {desc}\n"
                    f"- **Kurulum:** `npm install {pkg_name}@{version}`\n"
                    f"- https://www.npmjs.com/package/{pkg_name}\n"
                )
            except Exception as e:
                logger.debug("NPM registry hatası: %s", e)

        if matched_pypi:
            try:
                url = f"https://pypi.org/pypi/{matched_pypi}/json"
                req = urllib.request.Request(url, headers={"User-Agent": "MYF-Agent/1.0", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                info     = data.get("info", {})
                pkg_name = info.get("name", matched_pypi)
                version  = info.get("version", "Bilinmiyor")
                summary  = info.get("summary", "")
                results.append(
                    f"### 🐍 PyPI: `{pkg_name}`\n"
                    f"- **Sürüm:** `{version}`\n"
                    f"- **Özet:** {summary}\n"
                    f"- **Kurulum:** `pip install {pkg_name}=={version}`\n"
                    f"- https://pypi.org/project/{pkg_name}/\n"
                )
            except Exception as e:
                logger.debug("PyPI registry hatası: %s", e)

        return "\n".join(results) if results else None

    def _search_web_primary(self, query: str, max_results: int) -> Tuple[Optional[str], Optional[Exception]]:
        """Birincil kaynak: DuckDuckGo HTML."""
        try:
            data = urllib.parse.urlencode({"q": query}).encode("utf-8")
            req = urllib.request.Request(
                "https://html.duckduckgo.com/html/", data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": "https://html.duckduckgo.com/",
                }
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            results = []
            for item in soup.select(".result"):
                title_elem   = item.select_one(".result__title a")
                snippet_elem = item.select_one(".result__snippet")
                if title_elem and snippet_elem:
                    title = title_elem.get_text(strip=True)
                    link  = title_elem.get("href", "")
                    if "uddg=" in link:
                        m = re.search(r'uddg=([^&]+)', link)
                        if m:
                            link = urllib.parse.unquote(m.group(1))
                    snippet = snippet_elem.get_text(strip=True)
                    results.append(f"### [{title}]({link})\n{snippet}\n")
                if len(results) >= max_results:
                    break

            if results:
                return f"## 🌐 Web Arama: {query}\n\n" + "\n".join(results), None
            return None, RuntimeError("Birincil kaynak sonuç döndürmedi")
        except ImportError as exc:
            return None, exc
        except Exception as exc:
            logger.warning("Birincil web arama hatası: %s", exc)
            return None, exc

    def _search_web_lite(self, query: str, max_results: int) -> Tuple[Optional[str], Optional[Exception]]:
        """İkincil kaynak: DuckDuckGo Lite."""
        try:
            data = urllib.parse.urlencode({"q": query}).encode("utf-8")
            req = urllib.request.Request(
                "https://lite.duckduckgo.com/lite/", data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": "https://lite.duckduckgo.com/",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            from bs4 import BeautifulSoup
            soup     = BeautifulSoup(html, "html.parser")
            snippets = [td.get_text(strip=True) for td in soup.select(".result-snippet")]
            links    = []
            for a in soup.select(".result-link"):
                href = a.get("href", "")
                if "uddg=" in href:
                    m = re.search(r'uddg=([^&]+)', href)
                    if m:
                        href = urllib.parse.unquote(m.group(1))
                links.append((a.get_text(strip=True), href))

            results = []
            for i in range(min(len(snippets), len(links), max_results)):
                title, href = links[i]
                results.append(f"### [{title}]({href})\n{snippets[i]}\n")

            if results:
                return f"## 🌐 Web Arama (Lite): {query}\n\n" + "\n".join(results), None
            return None, RuntimeError("Lite kaynak sonuç döndürmedi")
        except ImportError as exc:
            return None, exc
        except Exception as exc:
            logger.warning("Lite web arama hatası: %s", exc)
            return None, exc

    def _search_web_fallback(self, query: str, max_results: int) -> Tuple[Optional[str], Optional[Exception]]:
        """Üçüncül kaynak: DuckDuckGo Instant Answer JSON API."""
        try:
            params = urllib.parse.urlencode({
                "q": query, "format": "json", "no_html": "1", "skip_disambig": "1",
            })
            req = urllib.request.Request(
                f"https://api.duckduckgo.com/?{params}",
                headers={"User-Agent": "Mozilla/5.0"}
            )
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
                    f"## 🌐 Web Arama (Instant Answer): {query}\n\n"
                    + "\n".join(results)
                    + "\n\n_Not: İkincil kaynaktan (DDG Instant Answer) geldi — kapsam dar olabilir._"
                ), None
            return None, RuntimeError("Instant Answer da sonuç döndürmedi")
        except Exception as exc:
            logger.warning("DDG Instant Answer hatası: %s", exc)
            return None, exc

    # ──────────────────────────────────────────────────────────
    # KANAL 2: URL Okuma (Jina Reader → doğrudan fetch)
    # ──────────────────────────────────────────────────────────

    def read_url(self, url: str) -> str:
        """
        Bir web sayfasının metin/markdown içeriğini okur.
        Güvenlik: ikili dosya indirimi yasaktır.
        Önce Jina Reader, başarısız olursa doğrudan fetch.
        """
        clean_url = url.split("?")[0].lower()
        for ext in BLOCKED_EXTENSIONS:
            if clean_url.endswith(ext):
                return f"❌ Güvenlik: '{ext}' uzantılı dosya indirimi yasaktır."

        if not permission_manager.check_permission("network", url, agent_name="reach_engine"):
            return f"❌ '{url}' adresine erişim izni verilmedi."

        cache_key = f"url::{url.strip()}"
        cached = self._cache_get(self._url_cache, cache_key)
        if cached is not None:
            return cached

        content, err = self._read_url_via_jina(url)
        if content is None:
            logger.warning("Jina Reader başarısız (%s), doğrudan fetch: %s", err, url)
            content, err2 = self._read_url_direct(url)
            if content is None:
                return f"❌ Sayfa okunamadı ({url}): Jina={err}; doğrudan={err2}"

        self._cache_set(self._url_cache, cache_key, content)
        return content

    def _read_url_via_jina(self, url: str) -> Tuple[Optional[str], Optional[Exception]]:
        target = f"https://r.jina.ai/{url}" if not url.startswith("https://r.jina.ai/") else url
        try:
            req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                if len(content) > 12000:
                    content = content[:12000] + "\n\n... (İçerik kırpıldı)"
                return f"## 📄 Kaynak: {url}\n\n{content}", None
        except Exception as exc:
            return None, exc

    def _read_url_direct(self, url: str) -> Tuple[Optional[str], Optional[Exception]]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            text = _extract_text_from_html(raw)
            if len(text) > 12000:
                text = text[:12000] + "\n\n... (İçerik kırpıldı)"
            return f"## 📄 Kaynak (doğrudan): {url}\n\n{text}", None
        except Exception as exc:
            return None, exc

    # ──────────────────────────────────────────────────────────
    # KANAL 3: GitHub (gh CLI)
    # ──────────────────────────────────────────────────────────

    def search_github(self, query: str, max_results: int = 5) -> str:
        """
        GitHub'da repo/kod/issue arar. gh CLI gerektirir.
        gh kurulu değilse DDG üzerinden site:github.com fallback yapar.
        """
        if not permission_manager.check_permission("network", f"github://{query}", agent_name="reach_engine"):
            return "❌ GitHub arama izni reddedildi."

        cache_key = f"github::{query.strip().lower()}::{max_results}"
        cached = self._cache_get(self._search_cache, cache_key)
        if cached is not None:
            return cached

        # gh CLI probe
        if not self._channel_ok("github"):
            logger.info("gh CLI kullanılamıyor, DDG fallback: %s", query)
            result = self._ddg_site_search("github.com", query, max_results)
            self._cache_set(self._search_cache, cache_key, result)
            return result

        # Sorgudan "github" keyword'ünü temizle
        clean_q = re.sub(r'\bgithub\b', '', query, flags=re.IGNORECASE).strip()
        clean_q = clean_q or query

        try:
            proc = subprocess.run(
                [
                    "gh", "search", "repos", clean_q,
                    "--limit", str(min(max_results, 10)),
                    "--json", "name,description,url,stargazersCount,language,updatedAt",
                ],
                capture_output=True, text=True, timeout=15,
                env={**__import__("os").environ,
                     "GH_TELEMETRY": "false", "DO_NOT_TRACK": "true"},
            )

            if proc.returncode != 0:
                logger.warning("gh search hatası: %s", proc.stderr[:200])
                result = self._ddg_site_search("github.com", query, max_results)
                self._cache_set(self._search_cache, cache_key, result)
                return result

            repos = json.loads(proc.stdout)
            if not repos:
                result = self._ddg_site_search("github.com", query, max_results)
                self._cache_set(self._search_cache, cache_key, result)
                return result

            lines = [f"## 🐙 GitHub Arama: {clean_q}\n"]
            for r in repos:
                stars = r.get("stargazersCount", 0)
                lang  = r.get("language") or ""
                desc  = r.get("description") or ""
                url   = r.get("url", "")
                name  = r.get("name", "")
                lines.append(
                    f"### [{name}]({url})\n"
                    f"⭐ {stars:,}  📝 {lang}\n"
                    f"{desc}\n"
                )

            result = "\n".join(lines)
            self._cache_set(self._search_cache, cache_key, result)
            return result

        except FileNotFoundError:
            result = self._ddg_site_search("github.com", query, max_results)
            self._cache_set(self._search_cache, cache_key, result)
            return result
        except Exception as exc:
            logger.warning("gh CLI çalışma hatası: %s", exc)
            result = self._ddg_site_search("github.com", query, max_results)
            self._cache_set(self._search_cache, cache_key, result)
            return result

    def _ddg_site_search(self, site: str, query: str, max_results: int) -> str:
        """site:X query formatıyla DDG arama — fallback için."""
        clean = re.sub(r'site:\S+', '', query).strip()
        return self.search_web(f"site:{site} {clean}", max_results=max_results)

    def search_github_repos(self, topic_or_query: str, max_results: int = 4) -> str:
        """Geriye dönük uyumluluk için search_github() wrapperi."""
        return self.search_github(topic_or_query, max_results=max_results)

    # ──────────────────────────────────────────────────────────
    # KANAL 4: YouTube (yt-dlp)
    # ──────────────────────────────────────────────────────────

    def read_youtube(self, url: str) -> str:
        """
        YouTube video metadata ve altyazısını okur. yt-dlp gerektirir.
        yt-dlp yoksa Jina Reader fallback.
        """
        if not permission_manager.check_permission("network", url, agent_name="reach_engine"):
            return "❌ YouTube erişim izni reddedildi."

        cache_key = f"youtube::{url.strip()}"
        cached = self._cache_get(self._url_cache, cache_key)
        if cached is not None:
            return cached

        if not self._channel_ok("youtube"):
            logger.info("yt-dlp yok, Jina Reader fallback: %s", url)
            result = self.read_url(url)
            self._cache_set(self._url_cache, cache_key, result)
            return result

        try:
            proc = subprocess.run(
                ["yt-dlp", "--dump-json", "--no-playlist", "--quiet", url],
                capture_output=True, text=True, timeout=30,
            )

            if proc.returncode != 0:
                logger.warning("yt-dlp hatası: %s", proc.stderr[:200])
                return self.read_url(url)

            data       = json.loads(proc.stdout.strip().splitlines()[0])
            title      = data.get("title", "")
            channel    = data.get("channel", data.get("uploader", ""))
            duration   = data.get("duration_string", data.get("duration", ""))
            view_count = data.get("view_count", 0)
            upload     = data.get("upload_date", "")
            desc       = (data.get("description") or "")[:500]
            webpage    = data.get("webpage_url", url)

            # Altyazı denemesi
            subtitle_text = ""
            try:
                sub_proc = subprocess.run(
                    [
                        "yt-dlp", "--skip-download", "--write-auto-subs",
                        "--sub-format", "vtt", "--sub-lang", "tr,en",
                        "--output", "/tmp/yt_sub_%(id)s.%(ext)s", url,
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                import glob
                sub_files = glob.glob(f"/tmp/yt_sub_{data.get('id', '*')}*.vtt")
                if sub_files:
                    raw_sub = Path(sub_files[0]).read_text(encoding="utf-8", errors="ignore")
                    # VTT temizleme
                    lines = []
                    for line in raw_sub.splitlines():
                        if "-->" in line or line.startswith("WEBVTT") or not line.strip():
                            continue
                        clean = re.sub(r'<[^>]+>', '', line).strip()
                        if clean and (not lines or lines[-1] != clean):
                            lines.append(clean)
                    subtitle_text = " ".join(lines[:200])
                    for f in sub_files:
                        Path(f).unlink(missing_ok=True)
            except Exception:
                pass

            result_parts = [
                f"## 🎬 YouTube: {title}",
                f"**Kanal:** {channel} | **Süre:** {duration} | **İzlenme:** {view_count:,}",
                f"**Yüklenme:** {upload} | **URL:** {webpage}",
                f"\n**Açıklama:**\n{desc}",
            ]
            if subtitle_text:
                result_parts.append(f"\n**Altyazı (özet):**\n{subtitle_text[:1000]}")

            result = "\n".join(result_parts)
            self._cache_set(self._url_cache, cache_key, result)
            return result

        except json.JSONDecodeError:
            return self.read_url(url)
        except Exception as exc:
            logger.warning("YouTube okuma hatası: %s", exc)
            return self.read_url(url)

    # ──────────────────────────────────────────────────────────
    # KANAL 5: V2EX (public JSON API — sıfır kurulum)
    # ──────────────────────────────────────────────────────────

    def search_v2ex(self, query: str = "", max_results: int = 10) -> str:
        """
        V2EX hot topics veya node konularını döner.
        Panniantong V2EXChannel'i kullanır, o yoksa yerleşik HTTP fallback.
        """
        if not permission_manager.check_permission("network", "https://www.v2ex.com", agent_name="reach_engine"):
            return "❌ V2EX erişim izni reddedildi."

        cache_key = f"v2ex::{query.strip().lower()}::{max_results}"
        cached = self._cache_get(self._search_cache, cache_key)
        if cached is not None:
            return cached

        result = self._v2ex_via_channel(query, max_results) or self._v2ex_direct(query, max_results)
        self._cache_set(self._search_cache, cache_key, result)
        return result

    def _v2ex_via_channel(self, query: str, max_results: int) -> Optional[str]:
        """Panniantong V2EXChannel üzerinden hot topics."""
        self._load_channels()
        ch = self._channels.get("v2ex")
        if ch is None:
            return None
        try:
            topics = ch.get_hot_topics(limit=max_results)
            if not topics:
                return None
            lines = [f"## 🔥 V2EX Hot Topics\n"]
            for t in topics[:max_results]:
                lines.append(
                    f"### [{t.get('title', '')}]({t.get('url', '')})\n"
                    f"💬 {t.get('replies', 0)} yanıt | 📂 {t.get('node_title', '')}\n"
                )
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("V2EX channel hatası: %s", exc)
            return None

    def _v2ex_direct(self, query: str, max_results: int) -> str:
        """Doğrudan V2EX API çağrısı (fallback)."""
        try:
            url = "https://www.v2ex.com/api/topics/hot.json"
            req = urllib.request.Request(url, headers={"User-Agent": "MYF-Agent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                topics = json.loads(resp.read().decode("utf-8"))

            lines = ["## 🔥 V2EX Hot Topics\n"]
            for t in topics[:max_results]:
                node    = t.get("node", {})
                title   = t.get("title", "")
                t_url   = t.get("url", f"https://www.v2ex.com/t/{t.get('id', '')}")
                replies = t.get("replies", 0)
                node_title = node.get("title", "") if isinstance(node, dict) else ""
                lines.append(
                    f"### [{title}]({t_url})\n"
                    f"💬 {replies} yanıt | 📂 {node_title}\n"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"❌ V2EX erişim hatası: {exc}"

    # ──────────────────────────────────────────────────────────
    # KANAL 6: RSS / Atom (feedparser)
    # ──────────────────────────────────────────────────────────

    def read_rss(self, url: str, max_items: int = 10) -> str:
        """
        RSS/Atom besleme okur. feedparser kullanır.
        feedparser yoksa Jina Reader fallback.
        """
        if not permission_manager.check_permission("network", url, agent_name="reach_engine"):
            return f"❌ RSS erişim izni reddedildi: {url}"

        cache_key = f"rss::{url.strip()}::{max_items}"
        cached = self._cache_get(self._url_cache, cache_key)
        if cached is not None:
            return cached

        try:
            import feedparser
            feed = feedparser.parse(url)

            if feed.bozo and not feed.entries:
                logger.warning("RSS parse hatası: %s", feed.bozo_exception)
                result = self.read_url(url)
                self._cache_set(self._url_cache, cache_key, result)
                return result

            feed_title = feed.feed.get("title", url)
            lines = [f"## 📰 RSS: {feed_title}\n"]

            for entry in feed.entries[:max_items]:
                title   = entry.get("title", "Başlıksız")
                link    = entry.get("link", "")
                summary = entry.get("summary", entry.get("description", ""))
                # HTML temizle
                summary = re.sub(r'<[^>]+>', ' ', summary).strip()[:200]
                published = entry.get("published", entry.get("updated", ""))
                lines.append(
                    f"### [{title}]({link})\n"
                    f"{published}\n"
                    f"{summary}\n"
                )

            result = "\n".join(lines)
            self._cache_set(self._url_cache, cache_key, result)
            return result

        except ImportError:
            logger.warning("feedparser yok, Jina fallback: %s", url)
            result = self.read_url(url)
            self._cache_set(self._url_cache, cache_key, result)
            return result
        except Exception as exc:
            logger.warning("RSS okuma hatası: %s", exc)
            return f"❌ RSS okunamadı ({url}): {exc}"

    # ──────────────────────────────────────────────────────────
    # KANAL 7: Twitter (opsiyonel — twitter-cli + cookie)
    # ──────────────────────────────────────────────────────────

    def search_twitter(self, query: str, max_results: int = 10) -> str:
        """
        Twitter/X araması. twitter-cli + cookie gerektirir.
        Yoksa DDG fallback.
        """
        if not permission_manager.check_permission("network", f"twitter://{query}", agent_name="reach_engine"):
            return "❌ Twitter arama izni reddedildi."

        cache_key = f"twitter::{query.strip().lower()}::{max_results}"
        cached = self._cache_get(self._search_cache, cache_key)
        if cached is not None:
            return cached

        if not self._channel_ok("twitter"):
            logger.info("twitter-cli yok veya cookie eksik, DDG fallback: %s", query)
            result = self._ddg_site_search("twitter.com OR x.com", query, max_results)
            self._cache_set(self._search_cache, cache_key, result)
            return result

        try:
            import os
            env = {**os.environ}
            # Cookie env vars
            auth_token = os.environ.get("TWITTER_AUTH_TOKEN", "")
            ct0        = os.environ.get("TWITTER_CT0", "")
            if auth_token:
                env["TWITTER_AUTH_TOKEN"] = auth_token
            if ct0:
                env["TWITTER_CT0"] = ct0

            proc = subprocess.run(
                ["twitter", "search", query, "-n", str(max_results)],
                capture_output=True, text=True, timeout=30, env=env,
            )

            if proc.returncode != 0 or not proc.stdout.strip():
                result = self._ddg_site_search("twitter.com OR x.com", query, max_results)
                self._cache_set(self._search_cache, cache_key, result)
                return result

            result = f"## 🐦 Twitter Arama: {query}\n\n{proc.stdout[:3000]}"
            self._cache_set(self._search_cache, cache_key, result)
            return result

        except FileNotFoundError:
            result = self._ddg_site_search("twitter.com OR x.com", query, max_results)
            self._cache_set(self._search_cache, cache_key, result)
            return result
        except Exception as exc:
            logger.warning("Twitter arama hatası: %s", exc)
            return self._ddg_site_search("twitter.com OR x.com", query, max_results)

    # ──────────────────────────────────────────────────────────
    # KANAL 8: Reddit (opsiyonel — rdt-cli / opencli + cookie)
    # ──────────────────────────────────────────────────────────

    def search_reddit(self, query: str, max_results: int = 5) -> str:
        """
        Reddit araması. rdt-cli veya opencli + cookie gerektirir.
        Yoksa DDG fallback.
        """
        if not permission_manager.check_permission("network", f"reddit://{query}", agent_name="reach_engine"):
            return "❌ Reddit arama izni reddedildi."

        cache_key = f"reddit::{query.strip().lower()}::{max_results}"
        cached = self._cache_get(self._search_cache, cache_key)
        if cached is not None:
            return cached

        if not self._channel_ok("reddit"):
            logger.info("Reddit CLI yok, DDG fallback: %s", query)
            result = self._ddg_site_search("reddit.com", query, max_results)
            self._cache_set(self._search_cache, cache_key, result)
            return result

        # rdt-cli veya opencli denemesi
        for cmd in [["rdt", "search", query], ["opencli", "reddit", "search", query, "-f", "yaml"]]:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=20,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    result = f"## 📕 Reddit Arama: {query}\n\n{proc.stdout[:3000]}"
                    self._cache_set(self._search_cache, cache_key, result)
                    return result
            except FileNotFoundError:
                continue
            except Exception:
                continue

        result = self._ddg_site_search("reddit.com", query, max_results)
        self._cache_set(self._search_cache, cache_key, result)
        return result


# ──────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────
reach_engine = ReachEngine()
