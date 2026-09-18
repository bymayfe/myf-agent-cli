"""
brain.py — Proje hafızası ve günlük yönetimi.

Yönetilen dosyalar:
  output_project/.agent_brain.md     → Proje Özeti, Mimari Kararlar, Bilinen Sorunlar
  output_project/CHANGELOG.md        → Tarih/saat damgalı değişiklik günlüğü
  output_project/ajan_sohbet_gunlugu.txt → Ham sohbet kayıtları
"""

import os
import sys
import re
import time
import contextlib
from datetime import datetime
from pathlib import Path

if sys.platform != "win32":
    import fcntl
else:
    import msvcrt

from config import get_output_dir, set_output_dir, PROJECTS_BASE_DIR


import logging

logger = logging.getLogger(__name__)

# ==============================================================================
# Çapraz Platform Dosya Kilidi (Cross-Platform File Lock)
# ==============================================================================

@contextlib.contextmanager
def _file_lock(file_obj, exclusive: bool = True, timeout: float = 5.0, poll_interval: float = 0.05):
    """
    Platforma duyarlı (Linux/macOS fcntl, Windows msvcrt) dosya kilidi context manager.
    Windows için non-blocking lock yerine timeout süresince bloklayıcı bekleme döngüsü uygular.
    """
    if sys.platform == "win32":
        mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
        pos = file_obj.tell()
        file_obj.seek(0)
        start_time = time.monotonic()
        locked = False
        while time.monotonic() - start_time < timeout:
            try:
                msvcrt.locking(file_obj.fileno(), mode, 1)
                locked = True
                break
            except (OSError, IOError):
                time.sleep(poll_interval)
        file_obj.seek(pos)
        if not locked:
            raise TimeoutError(f"Windows dosya kilidi zaman aşımına uğradı ({timeout}s)")
        try:
            yield
        finally:
            try:
                cur_pos = file_obj.tell()
                file_obj.seek(0)
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
                file_obj.seek(cur_pos)
            except (OSError, IOError) as exc:
                logger.warning("Windows dosya kilidi serbest bırakılırken hata: %s", exc)
    else:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(file_obj.fileno(), flags)
        try:
            yield
        finally:
            try:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError) as exc:
                logger.warning("POSIX dosya kilidi serbest bırakılırken hata: %s", exc)


def _locked_append(
    path: Path | str,
    content: str,
    max_retries: int = 3,
    retry_delay: float = 0.5
) -> bool:
    """
    Belirtilen dosyaya platforma duyarlı kilit alarak güvenle ekleme (append) yapar.
    Kilit alınamazsa max_retries kadar retry_delay aralıkla tekrar dener.
    """
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            with open(target_path, "a", encoding="utf-8") as f:
                with _file_lock(f, exclusive=True):
                    f.write(content)
                    f.flush()
            return True
        except (OSError, IOError, TimeoutError) as exc:
            last_err = exc
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                logger.error("Kilit alınamadı ve dosya eklenemedi: %s (Hata: %s)", target_path, last_err)
                return False
    return False


def _locked_write(
    path: Path | str,
    content: str,
    max_retries: int = 3,
    retry_delay: float = 0.5
) -> bool:
    """
    Belirtilen dosyayı platforma duyarlı kilit alarak güvenle baştan yazar.
    """
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            # Dosyayı r+ veya w+ ile açıp kilitleyerek güvenle yaz
            if target_path.exists():
                with open(target_path, "r+", encoding="utf-8") as f:
                    with _file_lock(f, exclusive=True):
                        f.seek(0)
                        f.write(content)
                        f.truncate()
                        f.flush()
            else:
                with open(target_path, "w", encoding="utf-8") as f:
                    with _file_lock(f, exclusive=True):
                        f.write(content)
                        f.flush()
            return True
        except (OSError, IOError, TimeoutError) as exc:
            last_err = exc
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                logger.error("Kilit alınamadı ve dosya yazılamadı: %s (Hata: %s)", target_path, last_err)
                return False
    return False


def _ensure_myfcli_dir() -> Path:
    out_dir = get_output_dir()
    if not out_dir or str(Path(out_dir).resolve()) in ("/", "\\", "/bin", "/etc", "/usr", "/var", "/dev", "/proc", "/sys", "/root"):
        out_dir = str(PROJECTS_BASE_DIR / "Yeni_Proje")
        set_output_dir(out_dir)
    base = Path(out_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        base = PROJECTS_BASE_DIR / "Yeni_Proje"
        base.mkdir(parents=True, exist_ok=True)
        set_output_dir(str(base))
    myfcli_dir = base / ".myfcli"
    myfcli_dir.mkdir(parents=True, exist_ok=True)
    return myfcli_dir


# ==============================================================================
# Yollar
# ==============================================================================

def _migrate_file(old_name: str, new_name: str) -> None:
    old_p = Path(get_output_dir()) / old_name
    new_p = _ensure_myfcli_dir() / new_name
    if old_p.exists() and not new_p.exists():
        old_p.rename(new_p)


def _brain_path() -> Path:
    _migrate_file(".agent_brain.md", "agent_brain.md")
    return _ensure_myfcli_dir() / "agent_brain.md"

def _changelog_path() -> Path:
    _migrate_file("CHANGELOG.md", "CHANGELOG.md")
    return _ensure_myfcli_dir() / "CHANGELOG.md"

def _log_path() -> Path:
    _migrate_file("ajan_sohbet_gunlugu.txt", "ajan_sohbet_gunlugu.txt")
    return _ensure_myfcli_dir() / "ajan_sohbet_gunlugu.txt"

def _ensure_output_dir():
    os.makedirs(get_output_dir(), exist_ok=True)
    _ensure_myfcli_dir()


def get_brain_file() -> Path:
    return _brain_path()


def get_changelog_file() -> Path:
    return _changelog_path()


def get_chat_log_file() -> Path:
    return _log_path()


def reset_brain() -> None:
    _ensure_myfcli_dir()


# ─────────────────────────────────────────────
# .agent_brain.md okuma / yazma
# ─────────────────────────────────────────────

_BRAIN_TEMPLATE = """\
# Agent Brain — Proje Hafızası

## Proje Özeti
(henüz doldurulmadı)

## Mimari Kararlar
(henüz doldurulmadı)

## Dosya Yapısı
(henüz doldurulmadı)

## Bilinen Sorunlar
(henüz doldurulmadı)
"""


def read_brain() -> str:
    """Mevcut brain dosyasını oku; yoksa boş şablon döndür."""
    p = _brain_path()
    if p.exists():
        return p.read_text(encoding="utf-8")
    return _BRAIN_TEMPLATE


def _update_section(content: str, section: str, new_body: str) -> str:
    """
    Brain markdown içindeki belirtilen `## section` bölümünü günceller.
    Bölüm yoksa dosyanın sonuna ekler.
    """
    header = f"## {section}"
    # Bir sonraki ## başlığına kadar olan bloğu bul
    pattern = rf"(## {re.escape(section)}\n)(.*?)(?=\n## |\Z)"
    replacement = f"{header}\n{new_body.strip()}\n"
    
    # re.sub içinde backslash'lerin hata (bad escape) vermemesi için lambda kullanılır
    new_content, count = re.subn(pattern, lambda m: replacement, content, flags=re.DOTALL)
    if count == 0:
        new_content = content.rstrip() + f"\n\n{replacement}"
    return new_content


def write_brain_section(section: str, body: str):
    """Brain dosyasındaki bir bölümü kilitli ve güvenli şekilde güncelle / oluştur."""
    _ensure_output_dir()
    p = _brain_path()
    current = read_brain()
    updated = _update_section(current, section, body)
    _locked_write(p, updated)


def read_brain_section(section: str) -> str:
    """Belirtilen bölümü brain dosyasından oku."""
    content = read_brain()
    pattern = rf"## {re.escape(section)}\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, content, re.DOTALL)
    return m.group(1).strip() if m else ""


# ─────────────────────────────────────────────
# CHANGELOG.md
# ─────────────────────────────────────────────

def append_changelog(title: str, body: str):
    """
    CHANGELOG.md'ye tarih/saat damgalı bir giriş ekle.
    En yeni girdi en üstte olacak şekilde kilitli olarak eklenir.
    """
    _ensure_output_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n---\n## [{timestamp}] {title}\n\n{body.strip()}\n"

    p = _changelog_path()
    if p.exists():
        existing = p.read_text(encoding="utf-8")
    else:
        existing = "# CHANGELOG\n"

    # İlk ## bloğunun önüne ekle (header'dan sonra)
    if "\n## " in existing:
        insert_pos = existing.index("\n## ")
        new_content = existing[:insert_pos] + entry + existing[insert_pos:]
    else:
        new_content = existing + entry

    _locked_write(p, new_content)


# ─────────────────────────────────────────────
# Sohbet günlüğü
# ─────────────────────────────────────────────

def log_conversation(agent_name: str, system_prompt: str, user_prompt: str, response: str):
    """Ham sohbet kayitlarini kilitli olarak ajan_sohbet_gunlugu.txt'ye ekle."""
    _ensure_output_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    separator = "=" * 60
    entry = (
        f"\n{separator}\n"
        f"[{timestamp}] AJAN: {agent_name.upper()}\n"
        f"{separator}\n"
        f"--- SYSTEM ---\n{system_prompt[:500]}{'...' if len(system_prompt)>500 else ''}\n\n"
        f"--- USER ---\n{user_prompt[:1000]}{'...' if len(user_prompt)>1000 else ''}\n\n"
        f"--- YANIT ---\n{response[:2000]}{'...' if len(response)>2000 else ''}\n"
    )
    _locked_append(_log_path(), entry)


# ─────────────────────────────────────────────
# Dosya yazma (output_project içine) - Cerrahi Diff Destekli
# ─────────────────────────────────────────────

from permission_manager import permission_manager
from diff_engine import apply_surgical_edit, has_diff_blocks

def _sanitize_py_content(text: str, filename: str) -> str:
    """Python (.py) dosyalarının içine kaçan markdown başlıklarını (#) yoruma çevirerek sentaks hatalarını önler."""
    if not filename.endswith(".py"):
        return text
    lines = text.splitlines()
    new_lines = []
    in_triple_single = False
    in_triple_double = False
    for line in lines:
        if "'''" in line:
            in_triple_single = not in_triple_single
        if '"""' in line:
            in_triple_double = not in_triple_double
        
        if not (in_triple_single or in_triple_double):
            s = line.strip()
            if s.startswith("**") or s == "---" or s.startswith("###") or s.startswith("## "):
                new_lines.append(f"# {line}")
                continue
        new_lines.append(line)
    newline = "\r\n" if "\r\n" in text else "\n"
    return newline.join(new_lines)


def write_output_file(relative_path: str, content: str, agent_name: str = "developer") -> bool:
    """
    Üretilen kodu veya SEARCH/REPLACE diff bloklarını aktif proje klasörü altına uygular.
    Alt dizinler otomatik oluşturulur.
    """
    if not permission_manager.check_permission("write_file", relative_path, agent_name=agent_name):
        print(f"  ❌ Dosya yazımı engellendi: {relative_path}")
        return False

    out_dir = get_output_dir()
    full_path = Path(out_dir) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    original_text = None
    if full_path.exists():
        try:
            original_text = full_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            original_text = None

    # Cerrahi diff veya tam yazma uygula
    final_content, success, msg = apply_surgical_edit(original_text, content, file_path=relative_path)
    final_content = _sanitize_py_content(final_content, relative_path)

    if success or (original_text is None):
        full_path.write_text(final_content, encoding="utf-8")
        if has_diff_blocks(content):
            print(f"  ✂️  Cerrahi Düzenleme Yapıldı ({msg}): {relative_path}")
        else:
            print(f"  📄 Dosya yazıldı: {relative_path}")
    else:
        print(f"  ⚠️  Cerrahi Düzenleme Hatası ({msg}): {relative_path}")
        # Güvenli fallback: Yine de içeriği yaz
        full_path.write_text(final_content, encoding="utf-8")

    # Kod tabanı harita önbelleğini tazele
    try:
        from codebase_graph import codebase_graph
        codebase_graph.invalidate_cache(out_dir)
    except Exception:
        pass

    return True


IGNORED_SCAN_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".idea", ".vscode"
}

def list_output_files() -> list:
    """Aktif proje klasörü içindeki tüm geçerli dosyaları ultra hızlı listele."""
    out_str = str(get_output_dir())
    if not os.path.exists(out_str):
        return []

    result = []
    for root, dirs, files in os.walk(out_str):
        # Gereksiz ve devasa klasör ağaçlarına HİÇ GİRME (Ultra Hızlı Pruning)
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d not in IGNORED_SCAN_DIRS
        ]
        # .myfcli klasörü içinde sadece temp_codes'a izin ver
        rel_root = os.path.relpath(root, out_str)
        if ".myfcli" in rel_root and "temp_codes" not in rel_root:
            continue

        for fname in files:
            if fname.startswith(".") or fname in {"CHANGELOG.md", "ajan_sohbet_gunlugu.txt"}:
                continue
            full_p = os.path.join(root, fname)
            rel_p = os.path.relpath(full_p, out_str).replace("\\", "/")
            result.append(rel_p)

    return sorted(result)
