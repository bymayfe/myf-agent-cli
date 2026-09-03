"""
log_viewer.py — Bağımsız Log ve Denetim Görüntüleyici
Pipeline arka planda çalışırken bile başka bir terminalden anlık adımları, promptları ve çıktıları incelemenizi sağlar.

Kullanım:
  python log_viewer.py [proje_dizini] [komut]
  python log_viewer.py /path/to/project steps
  python log_viewer.py /path/to/project step 1
"""

import sys
import os
from pathlib import Path
from colorama import init, Fore, Style

init(autoreset=True)

# Path ayarı
_HERE = Path(__file__).parent.resolve()
_SYS_ROOT = _HERE.parent
for _sub in [_SYS_ROOT, _SYS_ROOT / "core", _SYS_ROOT / "engines", _SYS_ROOT / "agents", _SYS_ROOT / "llm", _SYS_ROOT / "storage", _SYS_ROOT / "tests"]:
    _s = str(_sub)
    if _sub.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

from log_store import log_store
from session_manager import session_manager


def c(text: str, *colors: str) -> str:
    return "".join(colors) + str(text) + Style.RESET_ALL


def print_banner():
    print(c("\n╔════════════════════════════════════════════════════════════════╗", Fore.CYAN, Style.BRIGHT))
    print(c("║       Multi-Agent Canlı Süreç ve Denetim Görüntüleyici         ║", Fore.CYAN, Style.BRIGHT))
    print(c("╚════════════════════════════════════════════════════════════════╝\n", Fore.CYAN, Style.BRIGHT))


def main():
    print_banner()

    args = sys.argv[1:]
    project_dir = None

    if args and Path(args[0]).is_dir():
        project_dir = Path(args[0])
        args = args[1:]
    else:
        # Son aktif oturumu veya varsayılan proje dizinini bul
        recent = session_manager.list_recent(limit=10)
        if not recent:
            print(c("  Henüz bir proje veya oturum bulunamadı.", Fore.YELLOW))
            return

        print(c("  Mevcut Projeler:", Fore.WHITE, Style.BRIGHT))
        for i, s in enumerate(recent, 1):
            p_name = s.custom_project_dir or s.title or s.session_id
            print(f"   [{i}] {c(s.title or 'Yeni Oturum', Fore.GREEN, Style.BRIGHT)}  ↳  {p_name}")

        choice = input(c(f"\n  İncelemek istediğiniz proje [1-{len(recent)}] (Enter=1): ", Fore.CYAN)).strip()
        idx = int(choice) - 1 if choice.isdigit() and 1 <= int(choice) <= len(recent) else 0
        selected = recent[idx]
        project_dir = Path(selected.custom_project_dir) if selected.custom_project_dir else Path(__file__).parent / "projects" / selected.project_dir_name

    print(c(f"\n  📁 Aktif Proje: {project_dir}", Fore.CYAN, Style.BRIGHT))

    # Komut argümanı var mı?
    cmd = " ".join(args).strip().lower() if args else ""

    while True:
        if not cmd:
            print(c("\n  ── MENÜ ────────────────────────────────────────────────", Fore.YELLOW, Style.BRIGHT))
            print(c("  [1] Son Çalışma Adımları ve Süreler (/logs)", Fore.WHITE))
            print(c("  [2] Ajan Prompt Boyutları (/logs prompts)", Fore.WHITE))
            print(c("  [3] Ajan Çıktıları ve Dosyalar (/logs outputs)", Fore.WHITE))
            print(c("  [4] Belirli Bir Adımın Tam Metnini Oku (Örn: 1, 2, 3)", Fore.WHITE))
            print(c("  [5] Hata ve Onarım Kayıtları (/logs errors)", Fore.WHITE))
            print(c("  [6] Canlı AUDIT_LOG.md Raporunu Aç / Dışa Aktar", Fore.GREEN, Style.BRIGHT))
            print(c("  [0] Çıkış", Fore.WHITE))

            choice = input(c("\n  Seçiminiz: ", Fore.CYAN)).strip()
            if choice == "0":
                break
            elif choice == "1":
                cmd = "summary"
            elif choice == "2":
                cmd = "prompts"
            elif choice == "3":
                cmd = "outputs"
            elif choice == "4":
                s_num = input(c("  Hangi adım numarası (1, 2, 3...): ", Fore.CYAN)).strip()
                cmd = f"step {s_num}"
            elif choice == "5":
                cmd = "errors"
            elif choice == "6":
                cmd = "md"
            else:
                cmd = choice

        # Komutu işlet
        if cmd == "summary" or cmd == "steps":
            latest = log_store.get_latest_run_id(project_dir=str(project_dir))
            if latest:
                print(c(f"\n  ── AGENT ADIMLARI (Son Çalışma: {latest}) ─────────────", Fore.CYAN, Style.BRIGHT))
                print(log_store.get_step_table(run_id=latest, project_dir=str(project_dir)))
            else:
                print(c("  Henüz kayıtlı bir adım bulunamadı.", Fore.YELLOW))

        elif cmd == "prompts":
            rows = log_store._fetch(
                str(project_dir),
                "SELECT step_number, agent_name, agent_role, model, prompt_chars, started_at FROM agent_steps ORDER BY started_at ASC"
            )
            print(c("\n  ── AGENT PROMPTLARI ─────────────────────────────────", Fore.CYAN, Style.BRIGHT))
            headers = ["#", "Ajan", "Rol", "Model", "Prompt Boyutu", "Başlangıç"]
            data = [(r["step_number"], r["agent_name"], r["agent_role"], r["model"].split("/")[-1], f"{r['prompt_chars']} ch", r["started_at"][11:19]) for r in rows]
            print(log_store._fmt_table(headers, data) if rows else "  (Kayıt yok)")

        elif cmd == "outputs":
            rows = log_store._fetch(
                str(project_dir),
                "SELECT step_number, agent_name, agent_role, response_chars, files_written, elapsed_sec FROM agent_steps ORDER BY started_at ASC"
            )
            print(c("\n  ── AGENT ÇIKTILARI ──────────────────────────────────", Fore.GREEN, Style.BRIGHT))
            import json
            headers = ["#", "Ajan", "Rol", "Yanıt Boyutu", "Süre", "Dosyalar"]
            data = []
            for r in rows:
                f_list = json.loads(r["files_written"] or "[]")
                f_str = ", ".join(f_list) if f_list else "Rapor/Belge"
                data.append((r["step_number"], r["agent_name"], r["agent_role"], f"{r['response_chars']} ch", f"{r['elapsed_sec']}s", f_str))
            print(log_store._fmt_table(headers, data) if rows else "  (Kayıt yok)")

        elif cmd.startswith("step "):
            s_val = cmd.split(maxsplit=1)[1].strip().lstrip("#")
            if s_val.isdigit():
                rows = log_store._fetch(
                    str(project_dir),
                    "SELECT step_id, agent_name, agent_role, model, prompt_chars, prompt_text, response_chars, elapsed_sec, full_output FROM agent_steps WHERE step_number=? ORDER BY started_at DESC LIMIT 1",
                    (int(s_val),)
                )
            else:
                rows = log_store._fetch(
                    str(project_dir),
                    "SELECT step_id, agent_name, agent_role, model, prompt_chars, prompt_text, response_chars, elapsed_sec, full_output FROM agent_steps WHERE step_id=?",
                    (s_val,)
                )
            if not rows:
                print(c(f"  Adım '{s_val}' bulunamadı.", Fore.RED))
            else:
                r = rows[0]
                print(c(f"\n  ════════════ ADIM #{s_val}: {r['agent_name']} ({r['agent_role']}) ════════════", Fore.CYAN, Style.BRIGHT))
                print(c(f"  Model: {r['model']} | Prompt: {r['prompt_chars']} ch | Yanıt: {r['response_chars']} ch | Süre: {r['elapsed_sec']}s\n", Fore.WHITE))
                if r["prompt_text"]:
                    print(c("  ┌── [Ajan'a Verilen Prompt (İlk 1500 Karakter)] ─────", Fore.YELLOW))
                    for l in r["prompt_text"][:1500].splitlines():
                        print(f"  │ {l}")
                    print(c("  └───────────────────────────────────────────────────\n", Fore.YELLOW))
                print(c("  ┌── [Ajan'ın Ürettiği Kod / Çıktı] ─────────────────", Fore.GREEN))
                full = r["full_output"] or "(Boş çıktı)"
                for l in full[:10000].splitlines():
                    print(f"  │ {l}")
                if len(full) > 10000:
                    print(c(f"  │ ... ({len(full)-10000} karakter daha var)", Fore.YELLOW))
                print(c("  └───────────────────────────────────────────────────", Fore.GREEN))

        elif cmd == "errors":
            print(c("\n  ── HATA OLAYLARI ────────────────────────────────────", Fore.RED, Style.BRIGHT))
            print(log_store.get_error_table(project_dir=str(project_dir)))

        elif cmd == "md":
            out_file = log_store.export_full_logs_md(project_dir=str(project_dir))
            print(c(f"\n  ✓ Canlı Denetim Raporu Güncellendi: {out_file}", Fore.GREEN, Style.BRIGHT))
            import webbrowser
            try:
                webbrowser.open(str(out_file))
            except Exception:
                pass

        if args:
            break
        cmd = ""


if __name__ == "__main__":
    main()
