"""
session_manager.py — AGY / Antigravity CLI tarzı benzersiz session ve proje yönetimi.

Boş Oturum Temizleme Özelliği:
  - Kullanıcı mesaj yazmadan / kod ürettirmeden çıkarsa veya yeni oturum açarsa,
    boş oturum diske kaydedilmez ve geçici klasörü otomatik silinir.
  - Sadece en az 1 mesaj veya dosya olduğunda diske kaydedilir.
"""

from __future__ import annotations
import os
import re
import json
import uuid
import shutil
import tempfile
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from config import PROJECTS_BASE_DIR, set_output_dir


def _slugify(text: str, max_len: int = 30) -> str:
    """Türkçe karakterleri dönüştür ve klasör dostu slug üret."""
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜI", "cgiosuCGIOSUi")
    clean  = text.translate(tr_map).lower()
    clean  = re.sub(r"[^\w\s-]", "", clean)
    clean  = re.sub(r"[\s_-]+", "_", clean).strip("_")
    return clean[:max_len] or "proje"


def extract_smart_title_and_slug(text: str) -> tuple[str, str]:
    """
    Kullanıcı promptundan veya plan metninden akıllı, şık bir başlık ve slug çıkarır.
    Örn: 'React Native ve Expo kullanarak "Dijital Tesbih & Zikirmatik" uygulaması geliştir'
         -> ('Dijital Tesbih & Zikirmatik', 'dijital_tesbih_zikirmatik')
    """
    if not text or not text.strip():
        return "Yeni Proje", "yeni_proje"

    raw = text.strip()

    # 1. Tırnak içindeki proje ismi (Türkçe/İngilizce çift tırnak veya tek tırnak)
    quote_matches = re.findall(r'["\']([^"\']{3,40})["\']', raw)
    for qm in quote_matches:
        qm_clean = qm.strip()
        if not re.search(r'[{}\[\]();<>=]|(?:\b(?:import|export|from|npm|npx|cd|git|pip)\b)', qm_clean, re.I):
            if len(qm_clean.split()) <= 6:
                return qm_clean, _slugify(qm_clean)

    # 2. Kalıp Taraması (Doğal Dil Örüntüleri)
    patterns = [
        r'(?:(?:bir|şu|yeni)\s+)?([A-Za-zÇĞİÖŞÜçğıöşü0-9\s&-]{3,35})\s+(?:uygulaması|uygulamasi|projesi|servisi|api|backend|frontend|sistemi|botu|aracı|araci)\b',
        r'(?:ile|kullanarak)\s+([A-Za-zÇĞİÖŞÜçğıöşü0-9\s&-]{3,30})\s+(?:geliştir|yap|yaz|oluştur|inşa et)\b',
        r'([A-Za-zÇĞİÖŞÜçğıöşü0-9\s&-]{3,30})\s+(?:geliştir|yap|yaz|oluştur)\b',
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            cand = m.group(1).strip()
            cand = re.sub(r'^(?:bir|yeni|modern|ergonomik|hızlı|basit|kullanarak|ile)\s+', '', cand, flags=re.I).strip()
            if 3 <= len(cand) <= 40 and len(cand.split()) <= 5:
                return cand.title(), _slugify(cand)

    # 3. İlk anlamlı satırdan temizleyerek üret
    first_line = raw.splitlines()[0].strip()
    first_line = re.sub(r'^[#*->\s]+', '', first_line).strip()
    first_line = re.sub(r'^(?:selam|merhaba|hey|lütfen|bana|şöyle bir|bir)\s+', '', first_line, flags=re.I).strip()

    words = first_line.split()[:5]
    if words:
        cand = " ".join(words)
        if len(cand) > 35:
            cand = cand[:35].rsplit(" ", 1)[0]
        if cand:
            return cand.title(), _slugify(cand)

    return "Yeni Proje", "yeni_proje"


class Session:
    """Tek bir oturumu ve projesini temsil eden sınıf."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        title: str = "Yeni Proje",
        slug: str = "proje",
        folder_name: Optional[str] = None,
        conversation_history: Optional[list[dict]] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ):
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:8]}"
        self.title      = title
        self.slug       = _slugify(slug or title)

        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.folder_name = folder_name or f"{now_str}_{self.slug}"
        self.project_dir = PROJECTS_BASE_DIR / self.folder_name

        self.conversation_history = conversation_history or []
        self.created_at = created_at or datetime.now().isoformat()
        self.updated_at = updated_at or datetime.now().isoformat()

        # Aktif output dizinini güncelle
        set_output_dir(str(self.project_dir))

    @property
    def json_path(self) -> Path:
        """Her projenin kendi klasörü altındaki .myfcli/session.json dosyası."""
        if not self.project_dir:
            return Path("session_fallback.json")
        
        old_p = self.project_dir / "session.json"
        new_p = self.project_dir / ".myfcli" / "session.json"
        
        if old_p.exists() and not new_p.exists():
            new_p.parent.mkdir(parents=True, exist_ok=True)
            old_p.rename(new_p)
            
        return new_p

    def is_empty(self) -> bool:
        """Oturumda henüz mesaj veya kullanıcı dosyası yok mu?"""
        if len(self.conversation_history) > 0:
            return False

        if self.project_dir.exists():
            files = [
                f for f in self.project_dir.rglob("*")
                if f.is_file() and not f.name.startswith(".") and ".myfcli" not in f.parts
                and f.name not in {"session.json", ".agent_brain.md", "CHANGELOG.md", "ajan_sohbet_gunlugu.txt"}
            ]
            if len(files) > 0:
                return False
        return True

    def save(self, history: Optional[list[dict]] = None) -> bool:
        """
        Session bilgisini ve sohbet geçmişini session.json dosyasına atomik olarak yazar.
        Eğer oturum tamamen boşsa diske klasör/dosya YAZMAZ.
        """
        if history is not None:
            self.conversation_history = history

        # Boş ise diske kaydetme
        if self.is_empty():
            return False

        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now().isoformat()

        data = {
            "session_id":           self.session_id,
            "title":                self.title,
            "slug":                 self.slug,
            "folder_name":          self.folder_name,
            "project_dir":          str(self.project_dir),
            "created_at":           self.created_at,
            "updated_at":           self.updated_at,
            "conversation_history": self.conversation_history,
        }

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self.json_path.parent),
                prefix="sess_",
                suffix=".tmp",
                delete=False,
                encoding="utf-8"
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(json.dumps(data, ensure_ascii=False, indent=2))
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.json_path)
            session_manager.register_external_session(self.project_dir)
            return True
        except Exception as exc:
            logger.error("Session.save atomik yazım hatası: %s", exc)
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return False

    def cleanup_if_empty(self) -> bool:
        """Eğer oturum boşsa diski temizle ve sil. Silindi ise True döner."""
        if not self.is_empty():
            return False

        if self.project_dir.exists():
            try:
                shutil.rmtree(self.project_dir)
                return True
            except Exception:
                pass
        return True

    def set_title(self, title: str, slug: Optional[str] = None) -> None:
        """Proje basligini ve slug'ini guncelle, gerekirse klasor adini yeniden adlandir."""
        if not title or title.strip() in ("Yeni Oturum", "Yeni Proje", "yeni_proje"):
            return

        self.title = title.strip()
        new_slug   = slug or _slugify(self.title)

        if "yeni_proje" in self.folder_name or self.folder_name.endswith("_proje"):
            time_prefix = self.folder_name.split("_")[0] if "_" in self.folder_name else datetime.now().strftime("%Y%m%d_%H%M%S")
            if len(time_prefix) < 8 or not time_prefix.isdigit():
                time_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")

            new_folder_name = f"{time_prefix}_{new_slug}"
            new_project_dir = PROJECTS_BASE_DIR / new_folder_name

            if self.project_dir.exists() and not new_project_dir.exists():
                try:
                    self.project_dir.rename(new_project_dir)
                    self.folder_name = new_folder_name
                    self.project_dir = new_project_dir
                except Exception as exc:
                    logger.warning("Klasor yeniden adlandirilirken hata: %s", exc)
            else:
                self.folder_name = new_folder_name
                self.project_dir = new_project_dir

        self.slug = new_slug
        set_output_dir(str(self.project_dir))
        self.save()

    def set_custom_project_dir(self, custom_path: str | Path) -> Path:
        """Kullanicinin sectigi ozel konumun (orn. Desktop veya D:\\Projeler) aktif dizin yap."""
        p = Path(custom_path).resolve()
        if str(p) in ("/", "\\", "/bin", "/etc", "/usr", "/var", "/dev", "/proc", "/sys", "/root"):
            logger.warning("Geçersiz veya korumalı sistem kök dizini: %s, atlanıyor.", p)
            return self.project_dir
        p.mkdir(parents=True, exist_ok=True)
        self.project_dir = p
        self.folder_name = p.name
        set_output_dir(str(self.project_dir))
        self.save()
        return self.project_dir

    @classmethod
    def load_from_dir(cls, dir_path: Path) -> Optional["Session"]:
        """Dizin içindeki session.json dosyasından Session nesnesi yükle."""
        json_file = dir_path / ".myfcli" / "session.json"
        if not json_file.exists():
            json_file = dir_path / "session.json"
            if not json_file.exists():
                return None
        try:
            raw = json.loads(json_file.read_text(encoding="utf-8"))
            s = cls(
                session_id=raw.get("session_id"),
                title=raw.get("title", dir_path.name),
                slug=raw.get("slug", "proje"),
                folder_name=dir_path.name,
                conversation_history=raw.get("conversation_history", []),
                created_at=raw.get("created_at"),
                updated_at=raw.get("updated_at"),
            )
            s.project_dir = dir_path
            return s
        except Exception:
            return None

    def __repr__(self) -> str:
        return f"<Session {self.session_id} | {self.title} | {self.folder_name}>"


def _fast_count_files(dir_path: Path) -> int:
    """Klasör içindeki geçerli dosya sayısını milisaniyede hesaplar."""
    count = 0
    dir_str = str(dir_path)
    if not os.path.exists(dir_str):
        return 0
    for root, dirs, files in os.walk(dir_str):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}]
        rel_root = os.path.relpath(root, dir_str)
        if ".myfcli" in rel_root and "temp_codes" not in rel_root:
            continue
        for f in files:
            if not f.startswith(".") and f not in {"CHANGELOG.md", "ajan_sohbet_gunlugu.txt"}:
                count += 1
    return count


class SessionManager:
    """Tüm sessionları ve projeleri yöneten sınıf."""

    def __init__(self):
        PROJECTS_BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.current_session: Session = Session(title="Yeni Oturum", slug="yeni_proje")
        self._external_registry_path = PROJECTS_BASE_DIR / ".external_sessions.json"

    def register_external_session(self, p: Path):
        """Projeler dizini dışındaki özel konumları kaydeder."""
        try:
            p = p.resolve()
            if PROJECTS_BASE_DIR in p.parents or p == PROJECTS_BASE_DIR:
                return

            ext_sessions = []
            if self._external_registry_path.exists():
                ext_sessions = json.loads(self._external_registry_path.read_text(encoding="utf-8"))

            path_str = str(p)
            if path_str not in ext_sessions:
                ext_sessions.append(path_str)
                self._external_registry_path.write_text(json.dumps(ext_sessions, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def create_new_session(self, title: str = "Yeni Oturum", slug: str = "yeni_proje") -> Session:
        """Önceki oturum boşsa temizle, yeni benzersiz bir session başlat."""
        if hasattr(self, "current_session") and self.current_session:
            self.current_session.cleanup_if_empty()

        session = Session(title=title, slug=slug)
        self.current_session = session
        return session

    def cleanup_on_exit(self) -> None:
        """Uygulama kapanırken aktif oturum boşsa diski temizle."""
        if hasattr(self, "current_session") and self.current_session:
            self.current_session.cleanup_if_empty()

    def list_all_sessions(self) -> list[dict[str, Any]]:
        """
        projects/ altındaki oturumları ve kayıtlı harici oturumları listele. Boş klasörleri göstermez.
        """
        sessions = []
        paths_to_check = [item for item in PROJECTS_BASE_DIR.iterdir() if item.is_dir() and not item.name.startswith(".")]

        # Harici oturumları da ekle
        valid_ext_sessions = []
        if self._external_registry_path.exists():
            try:
                ext_sessions = json.loads(self._external_registry_path.read_text(encoding="utf-8"))
                for p_str in ext_sessions:
                    p = Path(p_str)
                    if p.exists() and p.is_dir():
                        paths_to_check.append(p)
                        valid_ext_sessions.append(p_str)
                # Geçersiz olanları temizle
                if len(valid_ext_sessions) != len(ext_sessions):
                    self._external_registry_path.write_text(json.dumps(valid_ext_sessions, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        for item in paths_to_check:
            if not item.exists() or not item.is_dir():
                continue

            session_obj = Session.load_from_dir(item)
            file_count = _fast_count_files(item)
            msg_count = len(session_obj.conversation_history) if session_obj else 0

            # Boş oturumları temizle ve listede gösterme
            if (session_obj and session_obj.is_empty()) or (file_count == 0 and msg_count == 0):
                try:
                    shutil.rmtree(item)
                except Exception:
                    pass
                continue

            # Oturum veya legacy proje için akıllı başlık tespiti
            auto_title = None
            if session_obj and (session_obj.title in ("Yeni Oturum", "Yeni Proje") or "yeni_proje" in session_obj.title):
                # 1. Sohbet geçmişindeki ilk kullanıcı isteğini tara
                for m in session_obj.conversation_history:
                    if m.get("role") == "user" and m.get("content"):
                        u_content = m["content"].strip()
                        if u_content.lower() not in ("selam", "merhaba", "/run", "/start", "evet", "başla", "onay", "ok"):
                            t, _ = extract_smart_title_and_slug(u_content)
                            if t and t not in ("Yeni Proje", "Yeni Oturum"):
                                auto_title = t
                                break

            # 2. Dosyalardan tespit (package.json / README.md / .agent_brain.md)
            if not auto_title and (not session_obj or session_obj.title in ("Yeni Oturum", "Yeni Proje") or "yeni_proje" in item.name):
                pkg_f = item / "package.json"
                if pkg_f.exists():
                    try:
                        pkg_data = json.loads(pkg_f.read_text(encoding="utf-8", errors="ignore"))
                        p_name = pkg_data.get("name")
                        if p_name and p_name not in ("test-app", "my-app"):
                            auto_title = p_name.replace("-", " ").replace("_", " ").title()
                    except Exception:
                        pass

                if not auto_title:
                    readme_f = item / "README.md"
                    if readme_f.exists():
                        try:
                            for rline in readme_f.read_text(encoding="utf-8", errors="ignore").splitlines():
                                rline = rline.strip()
                                if rline.startswith("# ") and len(rline) > 2:
                                    cand_readme = rline.lstrip("# ").strip()
                                    if cand_readme and cand_readme.lower() not in ("yeni proje", "readme"):
                                        auto_title = cand_readme[:30]
                                        break
                        except Exception:
                            pass

                if not auto_title:
                    brain_f = item / ".agent_brain.md"
                    if brain_f.exists():
                        try:
                            for bline in brain_f.read_text(encoding="utf-8", errors="ignore").splitlines():
                                bline = bline.strip()
                                if "proje:" in bline.lower() or "proje özeti:" in bline.lower():
                                    cand_brain = re.sub(r'^[#*\-:\s]+', '', bline).strip()
                                    if cand_brain and len(cand_brain) > 3:
                                        auto_title = cand_brain[:30]
                                        break
                        except Exception:
                            pass

            if auto_title:
                if session_obj:
                    session_obj.title = auto_title
                    try:
                        session_obj.save()
                    except Exception:
                        pass

            final_title = (session_obj.title if session_obj else auto_title) or item.name

            if session_obj and (file_count > 0 or msg_count > 0):
                mtime_dt = datetime.fromisoformat(session_obj.updated_at)
                sessions.append({
                    "session_id":   session_obj.session_id,
                    "title":        final_title,
                    "folder_name":  item.name,
                    "path":         str(item),
                    "file_count":   file_count,
                    "msg_count":    msg_count,
                    "mtime":        mtime_dt.timestamp(),
                    "mtime_str":    mtime_dt.strftime("%d.%m.%Y %H:%M"),
                    "session_obj":  session_obj,
                })
            elif file_count > 0:
                mtime = item.stat().st_mtime
                mtime_dt = datetime.fromtimestamp(mtime)
                sessions.append({
                    "session_id":   f"legacy-{item.name[:8]}",
                    "title":        final_title,
                    "folder_name":  item.name,
                    "path":         str(item),
                    "file_count":   file_count,
                    "msg_count":    0,
                    "mtime":        mtime,
                    "mtime_str":    mtime_dt.strftime("%d.%m.%Y %H:%M"),
                    "session_obj":  None,
                })

        sessions.sort(key=lambda x: x["mtime"], reverse=True)
        return sessions

    def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """Son aktif oturumları en yeniden eskiye sıralı olarak döndürür."""
        all_sess = self.list_all_sessions()
        return all_sess[:limit]

    def resume_session(self, session_dict: dict[str, Any]) -> Session:
        """Önceki boş oturumu temizle ve seçilen oturumu aktif yap."""
        if hasattr(self, "current_session") and self.current_session:
            self.current_session.cleanup_if_empty()

        if session_dict.get("session_obj"):
            s = session_dict["session_obj"]
        else:
            p = Path(session_dict["path"])
            s = Session(
                session_id=session_dict["session_id"],
                title=session_dict["title"],
                folder_name=p.name,
            )

        set_output_dir(str(s.project_dir))
        self.current_session = s
        return s

    def delete_session(self, target_path_or_id: str, delete_files: bool = False) -> bool:
        """
        Belirtilen oturumu siler.
        delete_files=False (Varsayilan): Sadece sohbet gecmisini ve .myfcli oturumunu siler, kaynak kod dosyalarini korur!
        delete_files=True (--files / -f): Proje klasorunu ve tum dosyalari disken tamamen siler.
        """
        p = Path(target_path_or_id)
        if not p.is_absolute():
            for item in PROJECTS_BASE_DIR.iterdir():
                if item.is_dir():
                    s = Session.load_from_dir(item)
                    if (s and s.session_id == target_path_or_id) or item.name == target_path_or_id:
                        p = item
                        break

        if p.exists() and p.is_dir():
            try:
                if delete_files:
                    shutil.rmtree(p)
                    return True
                else:
                    # Sadece oturum ve sohbet hafizasini temizle (.myfcli klasoru, session.json, logs.db)
                    myfcli_dir = p / ".myfcli"
                    if myfcli_dir.exists():
                        shutil.rmtree(myfcli_dir)
                    old_sess = p / "session.json"
                    if old_sess.exists():
                        old_sess.unlink()
                    old_brain = p / ".agent_brain.md"
                    if old_brain.exists():
                        old_brain.unlink()

                    # Eger klasorde baska hicbir dosya yoksa klasoru kaldir
                    remaining = [f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")]
                    if not remaining:
                        shutil.rmtree(p)
                    return True
            except Exception:
                pass
        return False

    def clear_all_sessions(self) -> int:
        """Tum oturumlari ve projeleri disken tamamen temizle."""
        count = 0
        for item in PROJECTS_BASE_DIR.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                try:
                    shutil.rmtree(item)
                    count += 1
                except Exception:
                    pass
        self.create_new_session("Yeni Oturum", "yeni_proje")
        return count


session_manager = SessionManager()
