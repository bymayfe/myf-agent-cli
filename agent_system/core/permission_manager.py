"""
permission_manager.py — Antigravity / AGY tarzı İzin ve Güvenlik Yöneticisi

Özellikler:
  - Dizin & Proje Bazlı İzin Kapsamı (Project Scoped vs External System Scoped):
      • Proje İçi (Local Scope)   : Aktif proje klasörü altındaki dosya yazmaları varsayılan oturum izinlidir.
      • Proje Dışı (System Scope) : Proje klasörü DIŞINDAKİ dosya/dizin erişimlerinde KRİTİK GÜVENLİK UYARISI verilir.
  - Proje Bazlı İzin İzolasyonu (.agent_permissions.json):
      • Her projenin kendi güvenlik politikaları ve özel izinleri proje klasöründe saklanır.
  - 4 Farklı İzin Kapsamı: write_file, delete_file, run_command, network
"""

from __future__ import annotations
import sys
import json
from pathlib import Path
from typing import Literal
from colorama import Fore, Style
from settings import settings
from config import get_output_dir

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ActionType = Literal["write_file", "delete_file", "run_command", "network"]


class PermissionManager:
    """AGY / Antigravity tarzı interaktif izin ve güvenlik yöneticisi."""

    def __init__(self):
        self._session_grants: set[str] = set()
        self._resource_grants: set[str] = set()
        self._dir_grants: set[str] = set()

    def get_status_badge(self) -> str:
        """Görsel izin rozeti ve ikonunu döndürür (Header ve Status'te görünür)."""
        mode = settings.permission_mode
        if mode == "ask":
            return f"{Fore.YELLOW}{Style.BRIGHT}[IZIN: SIKI (Her Islemde Sor)]{Style.RESET_ALL}"
        elif mode == "always_allow":
            return f"{Fore.MAGENTA}{Style.BRIGHT}[IZIN: TAM IZIN VERILDI (Always Allow)]{Style.RESET_ALL}"
        else:
            return f"{Fore.CYAN}{Style.BRIGHT}[IZIN: PROJE IZINLI (Proje Ici Acik)]{Style.RESET_ALL}"

    def is_inside_project(self, resource: str) -> bool:
        """Hedef yol aktif proje klasörünün İÇİNDE mi?"""
        try:
            proj_dir = Path(get_output_dir()).resolve()
            p = Path(resource)
            if not p.is_absolute():
                target_path = (proj_dir / p).resolve()
            else:
                target_path = p.resolve()

            return target_path.is_relative_to(proj_dir)
        except Exception:
            return False

    def check_permission(self, action: ActionType, resource: str, agent_name: str = "agent") -> bool:
        """
        İşlem öncesi izin kontrolü yapar.
        - Proje İçi işlemler için normal oturum politikası uygulanır.
        - Proje Dışı işlemler için KRİTİK GÜVENLİK SORUSU sorulur.
        """
        mode = settings.permission_mode
        is_internal = self.is_inside_project(resource)

        # 1. Always Allow modu aktifse doğrudan izin ver
        if mode == "always_allow":
            return True

        # reach_engine güvenli okuma/araştırma izni (Salt-okunur web & doküman)
        if agent_name == "reach_engine" and action == "network":
            return True

        # 2. Kaynak veya Dizin bazlı önceden verilmiş izinler
        p = Path(resource).resolve() if Path(resource).is_absolute() else (Path(get_output_dir()) / resource).resolve()
        p_str = str(p)
        parent_str = str(p.parent)

        if p_str in self._resource_grants:
            return True
        if parent_str in self._dir_grants:
            return True

        # 3. Proje İÇİ işlemler: session_allow veya önceden verilen aksiyon izni varsa
        if is_internal:
            if action in self._session_grants or "all" in self._session_grants:
                return True
            if mode == "session_allow" and action == "write_file":
                return True

        action_names = {
            "write_file":  "DOSYA YAZMA / GUNTELLEME",
            "delete_file": "DOSYA SILME",
            "run_command": "TERMINAL KOMUTU CALISTIRMA",
            "network":     "AG / API ISTEGI YAPMA",
        }
        action_label = action_names.get(action, action.upper())

        print()
        if not is_internal:
            print(f"  {Fore.RED}{Style.BRIGHT}⚠️  [KRITIK - PROJE DISI ETIM IZNI GEREKLI]{Style.RESET_ALL}")
            print(f"  Ajan {Fore.CYAN}'{agent_name}'{Style.RESET_ALL} {Fore.RED}{Style.BRIGHT}PROJE KLASORI DISINDA{Style.RESET_ALL} islem yapmak istiyor!")
            print(f"  Proje Dizin : {get_output_dir()}")
            print(f"  Hedef Konum : {p_str}")
        else:
            print(f"  {Fore.YELLOW}{Style.BRIGHT}🛡️  [IZIN GEREKLI]{Style.RESET_ALL}")
            print(f"  Ajan {Fore.CYAN}'{agent_name}'{Style.RESET_ALL} su islemi yapmak istiyor:")
            print(f"  >> {Fore.WHITE}{Style.BRIGHT}[{action_label}]{Style.RESET_ALL} {p_str}")

        print()
        print("  Izin Secenekleri:")
        print(f"    {Fore.GREEN}[1] 🟢 Sadece 1 kez izin ver (Once){Style.RESET_ALL}")
        print(f"    {Fore.CYAN}[2] 📄 Bu dosyaya her zaman izin ver (File){Style.RESET_ALL}")
        print(f"    {Fore.CYAN}[3] 📂 Bu dizindeki dosyalara izin ver (Directory){Style.RESET_ALL}")
        print(f"    {Fore.BLUE}[4] 🚀 Bu projede '{action_label}' islemlerine hep izin ver (Project){Style.RESET_ALL}")
        print(f"    {Fore.YELLOW}[5] 🏰 Proje Ici Tam Yetki (Bu klasordeki HER SEYE izin ver, disari cikamasin){Style.RESET_ALL}")
        print(f"    {Fore.MAGENTA}[6] 🔓 Her Zaman, Her Yerde Izin Ver (Global Tam Otonom Mod){Style.RESET_ALL}")
        print(f"    {Fore.RED}[7] ❌ REDDET (Deny){Style.RESET_ALL}")
        print()

        try:
            choice = input(f"  Izin Seciminiz [1-7, Enter=1]: ").strip()
        except (KeyboardInterrupt, EOFError):
            choice = "7"

        if not choice:
            choice = "1"

        if choice == "2":
            self._resource_grants.add(p_str)
            print(f"  {Fore.CYAN}[OK] Dosya icin kalici izin verildi: {p.name}{Style.RESET_ALL}")
            self._save_project_policy()
            return True
        elif choice == "3":
            self._dir_grants.add(parent_str)
            print(f"  {Fore.CYAN}[OK] Dizin icin kalici izin verildi: {p.parent.name}/{Style.RESET_ALL}")
            self._save_project_policy()
            return True
        elif choice == "4":
            self._session_grants.add(action)
            print(f"  {Fore.BLUE}[OK] Proje boyunca '{action_label}' islemlerine izin verildi.{Style.RESET_ALL}")
            self._save_project_policy()
            return True
        elif choice == "5":
            self._session_grants.add("all")
            print(f"  {Fore.YELLOW}[OK] Proje icindeki TUM islemlere izin verildi (Sandbox Modu).{Style.RESET_ALL}")
            self._save_project_policy()
            return True
        elif choice == "6":
            self.set_mode("always_allow")
            return True
        elif choice == "7":
            print(f"  {Fore.RED}[ENGEL] Islem kullanici tarafindan engellendi.{Style.RESET_ALL}")
            return False
        else:
            return True

    def set_mode(self, new_mode: str) -> None:
        """Güvenlik modunu güncelle ve kullanıcıya görsel onay ver."""
        if new_mode in ("ask", "session_allow", "always_allow"):
            settings.permission_mode = new_mode
            badge = self.get_status_badge()
            print(f"\n  [GUVENLIK GUNCELLENDI] Guvenlik Modu Degistirildi -> {badge}\n")
            self._save_project_policy()

    def _save_project_policy(self) -> None:
        """Proje klasörü içine .agent_permissions.json kaydet."""
        try:
            agy_dir = Path(get_output_dir()) / ".myfcli"
            agy_dir.mkdir(parents=True, exist_ok=True)
            p = agy_dir / ".agent_permissions.json"
            data = {
                "permission_mode": settings.permission_mode,
                "session_grants":  list(self._session_grants),
                "resource_grants": list(self._resource_grants),
                "dir_grants":      list(self._dir_grants),
            }
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load_project_policy(self) -> None:
        """Aktif projenin .agent_permissions.json dosyasından izinleri yükle."""
        try:
            # Geriye dönük uyumluluk
            p = Path(get_output_dir()) / ".myfcli" / ".agent_permissions.json"
            if not p.exists():
                p = Path(get_output_dir()) / ".myfcli" / ".agent_permissions.json"
                
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if "permission_mode" in raw:
                    settings.permission_mode = raw["permission_mode"]
                if "session_grants" in raw:
                    self._session_grants = set(raw["session_grants"])
                if "resource_grants" in raw:
                    self._resource_grants = set(raw["resource_grants"])
                if "dir_grants" in raw:
                    self._dir_grants = set(raw["dir_grants"])
        except Exception:
            pass

    def reset_session_grants(self):
        """Oturum izinlerini temizle."""
        self._session_grants.clear()
        self._resource_grants.clear()
        self._dir_grants.clear()


permission_manager = PermissionManager()
