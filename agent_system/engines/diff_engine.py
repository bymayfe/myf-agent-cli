"""
diff_engine.py — Cerrahi Kod Düzenleme ve SEARCH/REPLACE Motoru

Özellikler:
  - Aider tarzı SEARCH/REPLACE bloklarını ayrıştırır ve mevcut dosyaya cerrahi olarak uygular.
  - Dosyanın tamamını yeniden yazmak yerine sadece değişen satırları günceller.
  - Tam eşleşme (exact match), boşluk/satır sonu toleranslı eşleşme (normalized match)
    ve satır bazlı kaydırmalı eşleşme (sliding window match) destekler.
  - Yeni dosyalar için tam içerik oluşturur.
"""

import re
import difflib
import logging
from pathlib import Path
from typing import Tuple, List, Optional

logger = logging.getLogger(__name__)

SEARCH_REPLACE_PATTERN = re.compile(
    r"<<<<<<<\s*SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>>\s*REPLACE",
    re.DOTALL
)


def has_diff_blocks(text: str) -> bool:
    """Metin içinde SEARCH/REPLACE bloğu var mı kontrol et."""
    return bool(SEARCH_REPLACE_PATTERN.search(text))


def extract_diff_blocks(text: str) -> List[Tuple[str, str]]:
    """
    Metin içindeki tüm (search_chunk, replace_chunk) çiftlerini çıkarır.
    """
    blocks = []
    for match in SEARCH_REPLACE_PATTERN.finditer(text):
        search_chunk = match.group(1)
        replace_chunk = match.group(2)
        blocks.append((search_chunk, replace_chunk))
    return blocks


def _normalize_lines(s: str) -> List[str]:
    return [line.rstrip() for line in s.splitlines()]


def _extract_anchors_from_chunk(chunk: str) -> List[Tuple[str, str]]:
    """Metin içindeki (kind, name) anchor çiftlerini çıkarır (örn: [('class', 'Dog'), ('def', 'speak'), ('trait', 'Display')])."""
    anchor_re = re.compile(
        r"^\s*(class|def|async\s+def|function|func|fn|struct|impl|trait|interface|type)\s+([a-zA-Z0-9_]+)",
        re.MULTILINE
    )
    return anchor_re.findall(chunk)


def _find_enclosing_anchor(lines: List[str], line_idx: int) -> Optional[str]:
    """Verilen satır indeksinin üzerindeki en yakın enclosing class/struct/impl/trait tanımını bulur."""
    anchor_re = re.compile(r"^\s*(class|struct|impl|trait|interface)\s+([a-zA-Z0-9_]+)")
    for i in range(min(line_idx, len(lines) - 1), -1, -1):
        m = anchor_re.match(lines[i])
        if m:
            return m.group(2)
    return None


def _has_context_anchor(
    search_chunk: str,
    original_text: str = "",
    start_line_idx: Optional[int] = None
) -> bool:
    """
    SEARCH bloğunun üst bağlam (class, def, struct, impl, trait vb.) içerip içermediğini
    ve bu bağlamın dosyada hedeflenen gerçek konumla (enclosing scope) eşleştiğini doğrular.
    """
    chunk_anchors = _extract_anchors_from_chunk(search_chunk)
    if not chunk_anchors:
        return False

    if not original_text:
        return True

    orig_lines = original_text.splitlines()

    # 1. Eğer start_line_idx verilmişse, hedeflenen konumun üst sınıf/yapı bağlamını doğrula
    if start_line_idx is not None and 0 <= start_line_idx < len(orig_lines):
        enclosing = _find_enclosing_anchor(orig_lines, start_line_idx)
        class_anchors = [name for kind, name in chunk_anchors if kind in ("class", "struct", "impl", "trait", "interface")]
        if class_anchors and enclosing:
            if enclosing not in class_anchors:
                return False

    # 2. chunk_anchors'taki tanımların dosyada gerçekte var olduğunu doğrula
    found_any = False
    for kind, name in chunk_anchors:
        if re.search(rf"\b{re.escape(kind)}\s+{re.escape(name)}\b", original_text):
            found_any = True
            break

    return found_any


def _check_ambiguity_and_anchor(
    search_chunk: str,
    original_text: str,
    start_line_idx: Optional[int] = None
) -> Tuple[bool, Optional[str]]:
    """
    Hem exact match hem sliding window fuzzy match için merkezi belirsizlik ve bağlam doğrulayıcı.
    
    Kontroller:
      1. SEARCH bloğu içindeki fonksiyon dosyada birden fazla sınıfta geçiyor mu? (Geçiyorsa sınıf bağlamı zorunlu)
      2. SEARCH bloğu sınıf/yapı anchor'ı içeriyorsa, bu anchor hedeflenen satırın üst bağlamı ile uyuşuyor mu?
    """
    chunk_anchors = _extract_anchors_from_chunk(search_chunk)
    class_anchors = [name for kind, name in chunk_anchors if kind in ("class", "struct", "impl", "trait", "interface")]

    # 1. Fonksiyon adı çoklu sınıf kontrolü
    fn_match = re.search(r"\b(?:def|function|func|fn)\s+([a-zA-Z0-9_]+)", search_chunk)
    if fn_match:
        fn_name = fn_match.group(1)
        fn_occurrences = len(re.findall(rf"\b(?:def|function|func|fn)\s+{re.escape(fn_name)}\b", original_text))
        if fn_occurrences > 1 and not class_anchors:
            return (
                False,
                f"Belirsiz eşleşme: {fn_name} dosyada {fn_occurrences} kez tekrar ediyor, SEARCH bloğuna sınıf/fonksiyon bağlamı ekleyin."
            )

    # 2. Üst bağlam uyuşmazlığı kontrolü
    if start_line_idx is not None and not _has_context_anchor(search_chunk, original_text, start_line_idx=start_line_idx):
        if class_anchors:
            return False, "Bağlam uyuşmazlığı: SEARCH bloğundaki sınıf bağlamı dosyadaki konumla uyuşmuyor."

    return True, None


def _record_fuzzy_audit(file_path: Optional[str], line_no: int, ratio: float) -> None:
    """[FUZZY-APPLIED] uyarısını loglara ve aktif proje AUDIT_LOG.md dosyasına kilitli olarak ekler."""
    log_entry = f"[FUZZY-APPLIED] {file_path or 'dosya'}:{line_no} - benzerlik: %{ratio * 100:.1f}"
    logger.warning(log_entry)
    try:
        from config import get_output_dir
        from brain import _locked_append
        audit_file = Path(get_output_dir()) / "AUDIT_LOG.md"
        entry = f"\n- ⚠️ **[FUZZY-APPLIED]** `{file_path or 'dosya'}:{line_no}` - Benzerlik: %{ratio * 100:.1f}\n"
        _locked_append(audit_file, entry)
    except Exception as exc:
        logger.warning("Audit log yazılırken hata: %s", exc)


def apply_search_replace_block(
    original_text: str,
    search_chunk: str,
    replace_chunk: str,
    file_path: Optional[str] = None,
) -> Tuple[str, bool, str]:
    """
    Tek bir search/replace bloğunu original_text üzerinde uygular.

    Döndürür:
      (güncellenmiş_metin, başarı_durumu, mesaj)
    """
    # 1. Aşama: Birebir tam eşleşme
    if search_chunk in original_text:
        count = original_text.count(search_chunk)
        if count == 1:
            # Tekil eşleşmede dahi fonksiyon adı birden fazla sınıfta geçiyorsa sınıf bağlamını doğrula
            ok, err_msg = _check_ambiguity_and_anchor(search_chunk, original_text)
            if not ok:
                return original_text, False, err_msg

            new_text = original_text.replace(search_chunk, replace_chunk, 1)
            return new_text, True, "Tam eşleşme ile güncellendi."
        else:
            # Birden fazla birebir eşleşme var — belirsiz
            fn_match = re.search(r"\b(?:def|class|function|func|fn|struct)\s+([a-zA-Z0-9_]+)", search_chunk)
            target_name = fn_match.group(1) if fn_match else "kod bloğu"
            return (
                original_text,
                False,
                f"Belirsiz eşleşme: {target_name} dosyada {count} kez tekrar ediyor, SEARCH bloğuna sınıf/fonksiyon bağlamı ekleyin."
            )

    # 2. Aşama: Satır sonu ve boşluk normalize edilmiş eşleşme (Sliding window / Fuzzy)
    orig_lines = original_text.splitlines()
    search_lines = [line.strip() for line in search_chunk.splitlines() if line.strip()]

    if not search_lines:
        return original_text, False, "SEARCH bloğu boş veya geçersiz."

    orig_stripped = [line.strip() for line in orig_lines]
    s_len = len(search_lines)

    candidate_matches = []
    best_start = -1
    best_ratio = 0.0
    max_observed_ratio = 0.0
    search_str = "\n".join(search_lines)

    for i in range(len(orig_lines) - s_len + 1):
        window = orig_stripped[i:i + s_len]
        window_str = "\n".join(window)
        if window == search_lines:
            candidate_matches.append((i, 1.0))
            max_observed_ratio = 1.0
            if 1.0 > best_ratio:
                best_ratio = 1.0
                best_start = i
        else:
            matcher = difflib.SequenceMatcher(None, window_str, search_str)
            ratio = matcher.ratio()
            if ratio > max_observed_ratio:
                max_observed_ratio = ratio
            if ratio >= 0.70:
                candidate_matches.append((i, ratio))
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = i

    if best_start == -1 or best_ratio < 0.70:
        return original_text, False, f"SEARCH bloğu bulunamadı (En yüksek benzerlik: %{max_observed_ratio * 100:.1f})."

    # KRİTİK GÜVENLİK: Skor ne olursa olsun (%99 dahil), adayın bağlam doğrulaması ve belirsizlik kontrolü ÖNCE yapılır
    ok, err_msg = _check_ambiguity_and_anchor(search_chunk, original_text, start_line_idx=best_start)
    if not ok:
        return original_text, False, err_msg

    # Çoklu aday yakınlığı kontrolü
    if len(candidate_matches) > 1 and not _has_context_anchor(search_chunk, original_text):
        top_candidates = [c for c in candidate_matches if abs(c[1] - best_ratio) < 0.05]
        if len(top_candidates) > 1:
            fn_match = re.search(r"\b(?:def|class|function|func|fn|struct)\s+([a-zA-Z0-9_]+)", search_chunk)
            target_name = fn_match.group(1) if fn_match else "kod bloğu"
            return (
                original_text,
                False,
                f"Belirsiz eşleşme: {target_name} dosyada {len(top_candidates)} aday konumda bulundu, SEARCH bloğuna sınıf/fonksiyon bağlamı ekleyin."
            )

    end_idx = best_start + s_len
    replace_lines = replace_chunk.splitlines()
    new_lines = orig_lines[:best_start] + replace_lines + orig_lines[end_idx:]
    newline_char = "\r\n" if "\r\n" in original_text else "\n"
    new_text = newline_char.join(new_lines)
    line_no = best_start + 1

    # Kademeli skor davranışı:
    # %100 -> Tam satır eşleşmesi
    if best_ratio >= 0.999:
        return new_text, True, "Tam satır eşleşmesi ile güncellendi."

    # %90 - %99 -> Uygula ama uyar ve audit log'a kaydet
    if best_ratio >= 0.90:
        _record_fuzzy_audit(file_path, line_no, best_ratio)
        return new_text, True, f"Fuzzy eşleşme (%{best_ratio * 100:.1f}) ile uygulandı. [FUZZY-APPLIED]"

    # %70 - %89 -> Otomatik uygulama, permission_manager ile onay kontrolü yap
    allowed = False
    try:
        from permission_manager import permission_manager
        allowed = permission_manager.check_permission(
            "write_file",
            file_path or "fuzzy_diff_apply",
            agent_name="diff_engine"
        )
    except Exception as exc:
        logger.warning("PermissionManager denetiminde hata: %s", exc)
        allowed = False

    if allowed:
        _record_fuzzy_audit(file_path, line_no, best_ratio)
        return new_text, True, f"Fuzzy eşleşme (%{best_ratio * 100:.1f}) kullanıcı onayı ile uygulandı."
    else:
        return original_text, False, f"Fuzzy eşleşme (%{best_ratio * 100:.1f}) 70-89% aralığında olduğu için onaylanmadı/reddedildi."


def apply_surgical_edit(
    original_text: Optional[str],
    edit_content: str,
    file_path: Optional[str] = None,
) -> Tuple[str, bool, str]:
    """
    Gelen kod bloğunu işler:
      - Eğer original_text None veya boşsa -> Doğrudan yeni dosya olarak yazar.
      - Eğer SEARCH/REPLACE blokları varsa -> Sırasıyla uygular.
      - Blok yoksa -> Tam dosya içeriği olarak kabul eder.
    """
    if original_text is None or not original_text.strip():
        if has_diff_blocks(edit_content):
            blocks = extract_diff_blocks(edit_content)
            full_content = "\n\n".join(r for _, r in blocks)
            return full_content, True, "Yeni dosya SEARCH/REPLACE içeriğinden oluşturuldu."
        return edit_content, True, "Yeni dosya oluşturuldu."

    if not has_diff_blocks(edit_content):
        return edit_content, True, "Tam dosya içeriği güncellendi."

    current_text = original_text
    blocks = extract_diff_blocks(edit_content)
    applied_count = 0
    messages = []

    for idx, (search_c, replace_c) in enumerate(blocks, 1):
        updated_text, success, msg = apply_search_replace_block(current_text, search_c, replace_c, file_path=file_path)
        if success:
            current_text = updated_text
            applied_count += 1
            messages.append(f"Blok #{idx}: {msg}")
        else:
            messages.append(f"Blok #{idx} BAŞARISIZ: {msg}")
            logger.warning("Cerrahi düzenleme bloğu uygulanamadı #%d: %s", idx, msg)

    if applied_count == 0:
        return original_text, False, "Hiçbir SEARCH/REPLACE bloğu uygulanamadı: " + "; ".join(messages)

    status_msg = f"{applied_count}/{len(blocks)} blok uygulandı. " + "; ".join(messages)
    return current_text, (applied_count == len(blocks)), status_msg
