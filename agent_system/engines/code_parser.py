"""
code_parser.py — Evrensel Kod Bloğu ve Dosya Yolu Ayrıştırıcısı

Tüm programlama dilleri (C, C++, C#, Go, Rust, Java, Python, JS, TS, HTML, CSS, 
Dart, Swift, Kotlin, PHP, Ruby, Lua, Zig, SQL, Shell vb.) ve uzantısız yapılandırma 
dosyaları (Dockerfile, Makefile, .env) için çok katmanlı ayrıştırma motoru.

v2 — Whitelist tabanlı uzantı doğrulama:
  - Sayısal section başlıkları (7.5, 4.1) artık dosya adı sayılmaz.
  - API isimleri (performance.now, chrome.storage.session) artık dosya adı sayılmaz.
  - Blok öncesi serbest satır başı (\\n|^) eşleştirmesi kaldırıldı — sadece
    gerçek Markdown header (#, ##, ###, **) kabul edilir.
"""

from __future__ import annotations
import os
import re
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Geçerli dosya uzantıları (whitelist) ───────────────────────────────────
# Listeye DAHIL OLMAYAN uzantılar (5, now, session, vb.) hiçbir zaman dosya adı sayılmaz.
_VALID_EXTENSIONS: frozenset[str] = frozenset({
    # Web / Frontend
    "js", "ts", "jsx", "tsx", "mjs", "cjs",
    "vue", "svelte", "astro",
    "html", "htm", "xhtml",
    "css", "scss", "sass", "less", "styl",
    # Backend / System
    "py", "pyi", "pyw",
    "rb", "rake", "gemspec",
    "php", "phtml",
    "java", "kt", "kts", "groovy",
    "cs", "vb", "fs", "fsi",
    "go",
    "rs",
    "c", "cc", "cpp", "cxx", "h", "hpp", "hxx",
    "swift",
    "dart",
    "lua",
    "zig",
    "ex", "exs",
    "erl", "hrl",
    "clj", "cljs",
    "hs", "lhs",
    "ml", "mli",
    "nim",
    "cr",
    "jl",
    "r", "rmd",
    "scala", "sc",
    # Scripting / Shell
    "sh", "bash", "zsh", "fish",
    "ps1", "psm1", "psd1",
    "bat", "cmd",
    # Config / Data
    "json", "jsonc", "json5",
    "yaml", "yml",
    "toml",
    "ini", "cfg", "conf", "config",
    "xml", "xsd", "xsl", "xslt",
    "env",
    "properties",
    "plist",
    # Database
    "sql", "sqlite", "db",
    # Docs / Text
    "md", "mdx", "markdown",
    "txt", "text",
    "rst",
    "adoc", "asciidoc",
    "tex", "latex",
    "csv", "tsv",
    # Build / Package
    "lock",
    "gradle", "maven",
    "tf", "tfvars",          # Terraform
    "proto",                 # Protobuf
    "graphql", "gql",
    "prisma",
    # Misc
    "log", "svg",
    "wasm",
    "sol",                   # Solidity
    "asm", "s",              # Assembly
})

# Uzantısız özel dosya isimleri (bunlar her zaman geçerlidir)
_SPECIAL_FILES: frozenset[str] = frozenset({
    "Dockerfile", "dockerfile",
    "Makefile", "makefile",
    "Jenkinsfile", "jenkinsfile",
    ".env", ".gitignore", ".gitattributes", ".editorconfig",
    ".dockerignore", ".npmignore", ".prettierignore",
    "Procfile", "procfile",
    "Gemfile", "Rakefile",
    "Pipfile",
    "Vagrantfile",
})

# Dosya adı OLAMAYACAK bilinen API namespace / token listesi
_NON_FILE_KEYWORDS: frozenset[str] = frozenset({
    "performance", "chrome", "window", "document", "navigator",
    "console", "module", "exports", "require", "process",
    "globalThis", "self", "location", "history", "fetch",
    "localStorage", "sessionStorage", "indexedDB",
})

# Mimari metinlerde sıkça geçen ama DOSYA OLMAYAN framework/teknoloji/ekosistem
# isimleri (örn. "Next.js", "TypeScript/Next.js", "Node.js"). Bunlar nokta içerdiği
# ve whitelist'teki bir uzantıyla (js/ts) bittiği için yanlışlıkla dosya sayılabilir.
# Karşılaştırma büyük/küçük harfe duyarsız ve base-name üzerinden yapılır.
_FRAMEWORK_NAME_BLACKLIST: frozenset[str] = frozenset({
    "next.js", "node.js", "vue.js", "nuxt.js", "react.js", "express.js",
    "nest.js", "ember.js", "backbone.js", "d3.js", "three.js", "chart.js",
    "socket.io", "typescript/next.js", "typescript/node.js",
    "javascript/next.js", "typescript/react", "typescript/vue",
})

# Kaynak dosyası DEĞİL, çalışma zamanı/log çıktısı olan ve mimari metinde
# yalnızca ÖRNEK/AÇIKLAMA olarak geçmesi muhtemel uzantılar. Bunlar
# "planlanan eksik dosya" tespitinden hariç tutulur (üretilmesi gereken bir
# kaynak dosyası değil, programın kendi çalışırken yazacağı bir çıktıdır).
_RUNTIME_OUTPUT_EXTENSIONS: frozenset[str] = frozenset({
    "log",
})


class UniversalCodeParser:
    """
    Markdown metinleri içerisindeki kod bloklarını (```) ve bunlara ait dosya yollarını
    dil bağımsız olarak ayrıştıran evrensel motor.
    """

    COMMENT_PREFIXES = r"(?:#|//|--|/\*|<!--|%|;)"

    # Geçerli dosya adı: harf/rakam/alt çizgi ile başlar, yol ayırıcı içerebilir,
    # nokta + whitelist uzantısı ile biter.
    # Uzantı kısmı SADECE whitelist'ten gelir (sayısal ve API isimleri engellenir).
    _EXT_GROUP = "|".join(sorted(_VALID_EXTENSIONS, key=len, reverse=True))
    FILE_PATTERN = (
        rf"(?:[a-zA-Z0-9_][a-zA-Z0-9_\-\./\\]*\.(?:{_EXT_GROUP})"
        rf"|{'|'.join(re.escape(s) for s in sorted(_SPECIAL_FILES, key=len, reverse=True))})"
    )

    @classmethod
    def _sanitize_path(cls, raw_path: Optional[str], base_dir: Optional[Path | str] = None) -> Optional[str]:
        """
        Dosya yolunu temizler, normalleştirir ve path traversal / mutlak yol kaçışlarını engeller.
        """
        if not raw_path:
            return None

        cleaned = raw_path.strip("`'\"*:,;()[]{} \t\r\n").replace("\\", "/")

        # Baştaki ./ temizliği
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]

        if not cleaned:
            return None

        # 1. Path traversal '..' kontrolü
        parts = [p for p in cleaned.split("/") if p]
        if ".." in parts:
            logger.warning("[PATH-TRAVERSAL-BLOCKED] %s", raw_path)
            return None

        # 2. Mutlak yol kontrolü (/etc/passwd, C:/Windows...)
        if cleaned.startswith("/") or re.match(r"^[a-zA-Z]:", cleaned):
            logger.warning("[PATH-TRAVERSAL-BLOCKED] %s", raw_path)
            return None

        # 3. Path resolve kontrolü (base_dir ile sınır dışına çıkış engeli)
        if base_dir:
            try:
                base_p = Path(base_dir).resolve()
                target_p = (base_p / cleaned).resolve()
                target_p.relative_to(base_p)
            except (ValueError, Exception):
                logger.warning("[PATH-TRAVERSAL-BLOCKED] %s", raw_path)
                return None

        return cleaned

    @classmethod
    def _parse_fence_tag(cls, fence_str: str) -> tuple[str, Optional[str]]:
        """
        Fence başlığını (örn. 'python:src/main.py', 'python path="src/utils.py"', 'typescript')
        ayrıştırıp (lang, filename) döner.
        """
        fence_str = fence_str.strip()
        if not fence_str:
            return "", None

        # 1. Format: python:src/main.py veya json:package.json
        if ":" in fence_str:
            parts = fence_str.split(":", 1)
            lang = parts[0].strip().lower()
            fname_cand = parts[1].strip()
            fname = cls._sanitize_path(fname_cand)
            if fname and cls._is_valid_filename(fname):
                return lang, fname

        # 2. Format: python path="src/utils.py" veya python filepath=src/utils.py veya python file='src/utils.py'
        attr_match = re.search(r'(?:path|filepath|file|filename)=["\']?([^"\'\s>]+)["\']?', fence_str, re.I)
        if attr_match:
            lang = fence_str.split()[0].strip().lower()
            fname_cand = attr_match.group(1).strip()
            fname = cls._sanitize_path(fname_cand)
            if fname and cls._is_valid_filename(fname):
                return lang, fname

        # 3. Sadece dil adı (örn: python, js, rust)
        first_token = fence_str.split()[0].strip().lower()
        return first_token, None

    @classmethod
    def extract_blocks(cls, text: str) -> list[dict]:
        """
        Markdown ``` bloklarını ve dosya yollarını çok katmanlı ayrıştırır.
        Döndürür: [{"lang": "javascript", "filename": "popup.js", "content": "..."}]
        """
        if not text:
            return []

        # 1. Eğer metin en başında '# filepath: README.md' veya '<!-- filepath: ... -->' ile başlıyorsa ve
        #    bir dokümantasyon/markdown dosyasıysa, gömülü blokları parçalamak yerine tüm dokümanı tek dosya yap:
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        top_fp_match = re.match(
            rf"^{cls.COMMENT_PREFIXES}\s*(?:filepath|path|file|dosya):\s*([^\s\*\->;<`]+)",
            first_line, re.I,
        )
        if top_fp_match:
            top_fname = cls._sanitize_path(top_fp_match.group(1).strip().rstrip("*/->;`").strip())
            if top_fname and cls._is_valid_filename(top_fname) and top_fname.lower().endswith((".md", ".txt", ".rst", ".adoc", ".html")):
                lines = text.strip().splitlines()
                cleaned_doc = "\n".join(lines[1:]).strip()
                return [{"lang": "markdown", "filename": top_fname, "content": cleaned_doc}]

        pattern = r"```([^\n]*)\n(.*?)```"
        blocks = []
        for m in re.finditer(pattern, text, re.DOTALL):
            fence_str = m.group(1).strip()
            content = m.group(2)
            lang, fence_candidate = cls._parse_fence_tag(fence_str)
            filename, cleaned_content = cls._resolve_filename(
                content=content,
                full_text=text,
                start_pos=m.start(),
                lang=lang,
                fence_candidate=fence_candidate
            )

            # Son kontrol: bulunan isim gerçekten geçerli mi?
            if filename and not cls._is_valid_filename(filename):
                filename = None

            blocks.append({"lang": lang, "filename": filename, "content": cleaned_content})
        return blocks

    # ─── Doğrulayıcı ────────────────────────────────────────────────────────

    @classmethod
    def _is_valid_filename(cls, name: str) -> bool:
        """
        Bulunan ismin gerçekten bir dosya adı olup olmadığını doğrular.

        Reddedilenler:
          - Sayısal section başlıkları: "7.5", "4.1", "3.2"  (base tamamen rakam)
          - API adları: "performance.now", "chrome.storage.session"
          - Whitelist dışı uzantılar: "session", "now", tek rakam vb.
        """
        if not name or len(name) < 2:
            return False

        # Özel dosyalar (Dockerfile, .env vb.) her zaman geçerli
        base_name = name.split("/")[-1].split("\\")[-1]
        if base_name in _SPECIAL_FILES or name in _SPECIAL_FILES:
            return True

        # Nokta yoksa → uzantısız ve özel listede değil → geçersiz
        if "." not in base_name:
            return False

        # Uzantıyı al (son noktadan sonraki kısım)
        dot_idx = base_name.rfind(".")
        base    = base_name[:dot_idx]
        ext     = base_name[dot_idx + 1:].lower()

        # Uzantı whitelist'te olmalı
        if ext not in _VALID_EXTENSIONS:
            return False

        # Base tamamen rakamsa → section numarası (7, 4, 1 gibi) → geçersiz
        # Örnek: "7.5" → base="7" → reddedilir
        deepest_base = base.split("/")[-1].split("\\")[-1]
        if re.fullmatch(r"\d+", deepest_base):
            return False

        # Bilinen API çağrıları (console.log, performance.now vb.) → geçersiz
        # Ancak console.py, process.py, document.py, fetch.py gibi gerçek kod dosyaları GEÇERLİDİR!
        if deepest_base.lower() in _NON_FILE_KEYWORDS and ext in ("log", "now", "session", "error", "warn", "info", "dir", "table", "trace"):
            return False

        if "." in deepest_base:
            # Birden fazla nokta içeren sahte dosya/API adları (örn: chrome.storage.session)
            first_seg = deepest_base.split(".")[0].lower()
            if first_seg in _NON_FILE_KEYWORDS:
                return False

        return True

    # ─── Ayrıştırıcı ─────────────────────────────────────────────────────────

    @classmethod
    def _resolve_filename(
        cls,
        content: str,
        full_text: str,
        start_pos: int,
        lang: str,
        fence_candidate: Optional[str] = None
    ) -> tuple[Optional[str], str]:
        """
        Dosya adını 4 aşamalı öncelik hiyerarşisi ile çıkarır:

        1. Öncelik 1: Fence tag içi yol (```python:src/main.py veya ```python path="src/main.py")
        2. Öncelik 2: İlk 1-5 satır yorum sözleşmesi (# FILE: path, // FILE: path, /* FILE: ... */, # filepath: ...)
        3. Öncelik 3: Kod fence'inin HEMEN ÜSTÜNDEKİ 1-2 satırdaki Markdown başlığı (### `main.py`, File: foo.py)
        4. Öncelik 4: Özel dil fallback'leri (dockerfile → Dockerfile, makefile → Makefile)

        Çelişki Kuralı: Birden fazla kaynak farklı dosya yolları üretirse, en yüksek öncelikli olan
        kullanılır ve [PATH-CONFLICT] uyarısı loglanır.
        """
        lines = content.splitlines()

        # ── Priority 2: Blok içi # FILE: veya # filepath: yorumları ────────────────
        comment_candidate = None
        comment_idx = -1
        for idx in range(min(5, len(lines))):
            line = lines[idx].strip()

            # # FILE: dosya.py  |  // FILE: dosya.js  |  # filepath: dosya.py  |  <!-- FILE: foo.html -->
            fp_match = re.match(
                rf"^{cls.COMMENT_PREFIXES}\s*(?:file|filepath|path|dosya):\s*([^\s\*\->;<`]+)",
                line, re.I,
            )
            if fp_match:
                cand = cls._sanitize_path(fp_match.group(1).strip().rstrip("*/->;`").strip())
                if cand and cls._is_valid_filename(cand):
                    comment_candidate = cand
                    comment_idx = idx
                    break

            # Doğrudan dosya adı yorumu (// main.go | <!-- index.html --> | # Program.cs)
            direct_fp = re.match(
                rf"^{cls.COMMENT_PREFIXES}\s*({cls.FILE_PATTERN})\s*(?:-->|\*/|;)?$",
                line, re.I,
            )
            if direct_fp:
                cand = cls._sanitize_path(direct_fp.group(1).strip())
                if cand and cls._is_valid_filename(cand):
                    comment_candidate = cand
                    comment_idx = idx
                    break

        # ── Priority 3: Blok öncesi HEMEN ÜSTÜNDEKİ 1-2 satır Markdown başlığı ───
        # Kod bloğunun hemen üstündeki max 2 satıra bak (arada en fazla 1 boş satır olabilir)
        header_candidate = None
        last_block_end = full_text.rfind("```", 0, start_pos)
        boundary = (last_block_end + 3) if last_block_end != -1 else 0
        prefix_text = full_text[boundary:start_pos]

        prefix_lines = prefix_text.splitlines()
        recent_lines = []
        for pline in reversed(prefix_lines):
            if not pline.strip():
                if len(recent_lines) == 0:
                    continue  # bloğun hemen üstündeki tek boş satır izinli
                else:
                    break  # ikinci boş satır veya daha uzaktaki metne bakma
            recent_lines.append(pline.strip())
            if len(recent_lines) >= 2:
                break

        for pline in recent_lines:
            # Sadece gerçek başlık (#, ##, ###, **), veya "File:" etiketi
            is_header_or_label = (
                pline.startswith(("#", "###", "##", "####", "**", "`", "- ", "* ")) or
                bool(re.match(r"^(?:file|dosya|path|filepath)\s*:", pline, re.I)) or
                bool(re.match(r"^\d+[\.\)]\s+", pline))
            )
            if not is_header_or_label:
                continue

            if "=" in pline or "(" in pline or "[" in pline or "{" in pline:
                continue

            tokens = re.findall(r"[`'\"*]*([a-zA-Z0-9_\-\./\\]+)[`'\"*]*", pline)
            for tok in tokens:
                cand = cls._sanitize_path(tok)
                if cand and cls._is_valid_filename(cand):
                    header_candidate = cand
                    break
            if header_candidate:
                break

        # ── Priority 4: Dil adı fallback ──────────────────────────────────────
        fallback_candidate = None
        lang_map = {
            "dockerfile": "Dockerfile",
            "docker":     "Dockerfile",
            "makefile":   "Makefile",
        }
        if lang in lang_map:
            fallback_candidate = lang_map[lang]

        # ── Tie-Break & Çelişki Yönetimi ──────────────────────────────────────
        candidates_list = [
            ("Fence tag", fence_candidate),
            ("FILE comment", comment_candidate),
            ("Markdown header", header_candidate),
        ]
        valid_cands = [(src, path) for src, path in candidates_list if path]

        chosen_name = None
        winner_src = None
        cleaned_content = content

        if fence_candidate:
            chosen_name = fence_candidate
            winner_src = "Fence tag"
        elif comment_candidate:
            chosen_name = comment_candidate
            winner_src = "FILE comment"
        elif header_candidate:
            chosen_name = header_candidate
            winner_src = "Markdown header"
        elif fallback_candidate:
            chosen_name = fallback_candidate
            winner_src = "Language fallback"

        # Çelişki kontrolü: birden fazla farklı yol tespit edildiyse uyar
        if len(valid_cands) > 1:
            unique_paths = {p for _, p in valid_cands}
            if len(unique_paths) > 1:
                conflict_details = " vs ".join(f"{src}: '{p}'" for src, p in valid_cands)
                logger.warning("[PATH-CONFLICT] %s — %s kullanıldı.", conflict_details, winner_src)

        # Eğer comment satırı bulunduysa, dosyadan temizle
        if comment_idx != -1:
            cleaned_content = "\n".join(l for i, l in enumerate(lines) if i != comment_idx)

        return chosen_name, cleaned_content


def extract_code_blocks(text: str) -> list[dict]:
    """Evrensel ayrıştırıcı köprüsü (Geriye dönük tam uyumlu fonksiyon)."""
    return UniversalCodeParser.extract_blocks(text)


def extract_planned_files_from_architecture(arch_text: str) -> list[str]:
    """
    Mimari tasarım belgesindeki ASCII dosya ağacını ve planlanan dosya yollarını
    TAM YOL BAĞLAMINI KORUYARAK ayıklar.

    Örnek girdi (indentation-bazlı tree, her satır sadece kendi segmentini gösterir):
        ├── app/
        │   ├── page.tsx
        │   ├── snippets/
        │   │   └── page.tsx
    Çıktı: ['app/page.tsx', 'app/snippets/page.tsx']  (İKİSİ DE AYRI, basename'e
    göre birleştirilmiyor — aksi halde farklı klasörlerdeki aynı isimli dosyalar
    yanlışlıkla "aynı dosya" sayılıp biri yazılınca diğerleri de "tamamlandı"
    zannedilir.)

    Ayrıca:
      - Framework/ekosistem isimleri (Next.js, Node.js vb.) dosya SAYILMAZ.
      - .log gibi çalışma zamanı çıktı uzantıları "planlanan kaynak dosyası"
        listesine dahil edilmez.
    """
    if not arch_text:
        return []

    planned: list[str] = []
    seen: set[str] = set()

    # Her tree derinliğinde (indent seviyesinde) o ana kadar biriken dizin
    # segmentini tutan yığın: {indent_level: "klasor_adi"}
    path_stack: dict[int, str] = {}

    for raw_line in arch_text.splitlines():
        if not raw_line.strip():
            continue

        # Tree çizgi karakterlerinin (│, ├──, └──) kapladığı görsel genişliği
        # indent seviyesi olarak kullan (her "├── " veya "│   " bloğu bir seviyedir).
        prefix_match = re.match(r"^((?:[│\s]{0,4}(?:├──|└──|\|--)?\s*)*)", raw_line)
        prefix_len = len(prefix_match.group(1)) if prefix_match else 0
        # Yaklaşık 4 karakterlik bloklar halinde bir derinlik seviyesi say
        indent_level = prefix_len // 4

        cleaned_line = re.sub(r"^[│├──└──\|\s\-\*#>\+]+", "", raw_line).strip()
        if not cleaned_line or cleaned_line.startswith("#"):
            continue

        # Yorumları temizle (örn: 'page.tsx   # Dashboard' -> 'page.tsx')
        cleaned_line = cleaned_line.split("#")[0].strip()
        if not cleaned_line:
            continue

        # Bu satır bir DİZİN mi (sonu / ile bitiyor) yoksa DOSYA mı?
        is_dir_line = cleaned_line.rstrip().endswith("/")
        segment = cleaned_line.rstrip().rstrip("/").strip("`'\"* ")
        # Segment icinde '/' KORUNMALI (örn. tek satırda 'lib/types.ts' gibi
        # düz yazılmış path'ler olabilir) — sadece gerçekten path-dışı gürültü
        # karakterlerini (virgül, parantez vb.) temizle.
        segment = re.findall(r"[a-zA-Z0-9_\-\.\[\]/]+", segment)
        segment = segment[0] if segment else ""
        if not segment:
            continue

        if is_dir_line:
            # Bu seviyenin dizin adını kaydet; daha derin seviyelerdeki eski
            # kayıtları geçersiz kıl (yeni bir dal başlıyor).
            path_stack[indent_level] = segment
            # Bu seviyeden daha derin (yüksek indent) eski kayıtları temizle
            for lvl in [l for l in path_stack if l > indent_level]:
                del path_stack[lvl]
            continue

        # DOSYA satırı: bu seviyeye kadar olan tüm ata dizinleri birleştirip
        # tam yolu yeniden inşa et.
        ancestor_parts = [path_stack[lvl] for lvl in sorted(path_stack) if lvl < indent_level]
        full_path = "/".join(ancestor_parts + [segment]) if ancestor_parts else segment
        cand = full_path.replace("\\", "/")

        base = cand.split("/")[-1].lower()

        # Dokümantasyon / audit dosyalarını atla
        if base in ("readme.md", "prd.md", "mimari.md", "audit_log.md", "architecture.md", "changelog.md"):
            continue

        # Framework/ekosistem isimlerini atla (Next.js, Node.js vb.)
        if base in _FRAMEWORK_NAME_BLACKLIST or cand.lower() in _FRAMEWORK_NAME_BLACKLIST:
            continue

        # Nokta yoksa ve özel dosya değilse atla
        if "." not in base and base not in ("dockerfile", "makefile"):
            continue

        # Çalışma zamanı çıktı uzantılarını (log vb.) planlanan kaynak
        # dosyası listesine dahil etme — bunlar üretilecek değil, programın
        # kendisinin çalışırken yazacağı dosyalardır.
        if "." in base:
            ext = base.rsplit(".", 1)[-1]
            if ext in _RUNTIME_OUTPUT_EXTENSIONS:
                continue

        if UniversalCodeParser._is_valid_filename(cand):
            norm = cand.strip()
            if norm.startswith("./"):
                norm = norm[2:]
            if norm not in seen:
                seen.add(norm)
                planned.append(norm)

    return planned

