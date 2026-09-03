"""
manage_agents.py — Agent CRUD CLI yonetim araci.

Kullanim:
  python manage_agents.py list
  python manage_agents.py add
  python manage_agents.py edit <id>
  python manage_agents.py remove <id>
  python manage_agents.py enable <id>
  python manage_agents.py disable <id>
  python manage_agents.py reorder
  python manage_agents.py show <id>
  python manage_agents.py reset
"""

from __future__ import annotations
import sys
import json
import uuid
import argparse
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

# Renk destegi
try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    C = True
except ImportError:
    C = False
    class _D:
        def __getattr__(self, _): return ""
    Fore = Style = _D()

from agents import load_config, save_config, load_templates, load_agents

# ─────────────────────────────────────────────
# Renk yardimcilari
# ─────────────────────────────────────────────

def _c(text, color="", style=""):
    if not C: return str(text)
    return f"{style}{color}{text}{Style.RESET_ALL}"

def ok(msg):   print(_c(f"  [OK] {msg}", Fore.GREEN, Style.BRIGHT))
def err(msg):  print(_c(f"  [HATA] {msg}", Fore.RED))
def info(msg): print(_c(f"  {msg}", Fore.CYAN))
def warn(msg): print(_c(f"  [!] {msg}", Fore.YELLOW))


# ─────────────────────────────────────────────
# Komutlar
# ─────────────────────────────────────────────

def cmd_list(args):
    """Tum agentlari tablo formatinda listele."""
    try:
        agents = load_agents(enabled_only=False)
    except Exception as e:
        err(f"Yuklenemedi: {e}"); return

    if not agents:
        warn("Hic agent tanimli degil. 'add' komutu ile ekleyin.")
        return

    print(_c("\n  AGENT LISTESI", Fore.CYAN, Style.BRIGHT))
    print(_c("  " + "=" * 80, Fore.CYAN))
    header = f"  {'Sira':<5} {'ID':<12} {'Isim':<22} {'Rol':<22} {'Durum':<8} {'Model'}"
    print(_c(header, Fore.WHITE, Style.BRIGHT))
    print(_c("  " + "-" * 80, Fore.CYAN))

    for a in agents:
        durum = _c("AKTIF", Fore.GREEN) if a.enabled else _c("PASIF", Fore.RED)
        model_short = a.model.split("/")[-1][:22]
        row = f"  {a.pipeline_order:<5} {a.id:<12} {a.display_name:<22} {a.role_type:<22}"
        print(row + f" {durum:<8} {model_short}")

    aktif  = sum(1 for a in agents if a.enabled)
    print(_c("  " + "-" * 80, Fore.CYAN))
    print(f"  Toplam: {len(agents)} agent | Aktif: {aktif} | Pasif: {len(agents) - aktif}\n")


def cmd_show(args):
    """Bir agentin tam detayini goster."""
    agent_id = _get_arg(args, "id")
    if not agent_id: return

    config    = load_config()
    templates = load_templates()

    entry = _find_agent(config, agent_id)
    if not entry:
        err(f"Agent bulunamadi: {agent_id}"); return

    tmpl = templates.get(entry.get("role_type", "custom"), {})
    system_prompt = entry.get("custom_prompt") or tmpl.get("system_prompt", "(template'tan gelir)")

    print(_c(f"\n  AGENT DETAYI: {entry['id']}", Fore.CYAN, Style.BRIGHT))
    print(_c("  " + "=" * 60, Fore.CYAN))
    _field("ID",             entry["id"])
    _field("Isim",           entry.get("display_name",""))
    _field("Rol tipi",       entry.get("role_type",""))
    _field("Model",          entry.get("model",""))
    _field("Pipeline sirasi",entry.get("pipeline_order",""))
    _field("Durum",          "AKTIF" if entry.get("enabled") else "PASIF")
    _field("Aciklama",       entry.get("description",""))
    print(_c("\n  System Prompt (ilk 400 karakter):", Fore.WHITE, Style.BRIGHT))
    print(f"  {system_prompt[:400]}{'...' if len(system_prompt)>400 else ''}\n")


def cmd_add(args):
    """Interaktif wizard ile yeni agent ekle."""
    config    = load_config()
    templates = load_templates()

    print(_c("\n  YENI AGENT EKLE", Fore.CYAN, Style.BRIGHT))
    print(_c("  " + "=" * 40, Fore.CYAN))

    # 1. Isim
    display_name = _input_required("Agent ismi (ornek: Guvenlik Denetcisi): ")

    # 2. Rol secimi
    role_keys = list(templates.keys())
    print(_c("\n  Mevcut roller:", Fore.WHITE, Style.BRIGHT))
    for i, key in enumerate(role_keys, 1):
        tmpl = templates[key]
        print(f"    {i}. {_c(key, Fore.YELLOW):<25} — {tmpl.get('display_name','')}: {tmpl.get('description','')}")

    role_input = _input_required(
        f"\n  Rol secin [1-{len(role_keys)}, ya da rol adi yazin]: "
    )
    if role_input.isdigit() and 1 <= int(role_input) <= len(role_keys):
        role_type = role_keys[int(role_input) - 1]
    elif role_input in templates:
        role_type = role_input
    else:
        warn(f"Bilinmeyen rol. 'custom' olarak devam ediliyor.")
        role_type = "custom"

    # 3. Custom prompt (sadece custom rol veya ozellestirilmek istenirse)
    custom_prompt = None
    if role_type == "custom":
        print(_c("\n  Ozel system prompt yazin (bitirmek icin bos satir birakip Enter):","",Style.BRIGHT))
        lines = []
        while True:
            line = input("  > ")
            if line == "" and lines:
                break
            lines.append(line)
        custom_prompt = "\n".join(lines).strip() or None
    else:
        override = input(_c(
            f"\n  '{role_type}' sablonu kullanilacak. Ozellestirilmis prompt girmek ister misiniz? [e/h]: ",
            Fore.CYAN
        )).strip().lower()
        if override in ("e", "evet", "y", "yes"):
            print("  Prompt yazin (bos satir = bitis):")
            lines = []
            while True:
                line = input("  > ")
                if line == "" and lines:
                    break
                lines.append(line)
            custom_prompt = "\n".join(lines).strip() or None

    # 4. Model
    mevcut_model = _get_default_model(config)
    model = input(_c(
        f"\n  Model (Enter = {mevcut_model}): ", Fore.CYAN
    )).strip() or mevcut_model

    # 5. Pipeline sirasi
    max_order = max((a.get("pipeline_order", 0) for a in config["agents"]), default=0)
    order_input = input(_c(
        f"\n  Pipeline sirasi (Enter = {max_order + 1}): ", Fore.CYAN
    )).strip()
    pipeline_order = int(order_input) if order_input.isdigit() else max_order + 1

    # 6. Aciklama
    tmpl_desc = templates.get(role_type, {}).get("description", "")
    description = input(_c(
        f"\n  Aciklama (Enter = '{tmpl_desc}'): ", Fore.CYAN
    )).strip() or tmpl_desc

    # Agent olustur
    new_agent = {
        "id":             f"agent-{str(uuid.uuid4())[:8]}",
        "display_name":   display_name,
        "role_type":      role_type,
        "model":          model,
        "pipeline_order": pipeline_order,
        "enabled":        True,
        "description":    description,
        "custom_prompt":  custom_prompt,
    }

    # Onay
    print(_c("\n  Olusturulacak agent:", Fore.WHITE, Style.BRIGHT))
    for k, v in new_agent.items():
        if k != "custom_prompt":
            _field(k, v)
    if custom_prompt:
        print(f"  custom_prompt: {custom_prompt[:80]}...")

    confirm = input(_c("\n  Kaydet? [e/h]: ", Fore.YELLOW)).strip().lower()
    if confirm not in ("e", "evet", "y", "yes", ""):
        warn("Iptal edildi."); return

    config["agents"].append(new_agent)
    save_config(config)
    ok(f"Agent eklendi: {new_agent['id']} ({display_name})")


def cmd_edit(args):
    """Mevcut agent'in alanlarini duzenle."""
    agent_id = _get_arg(args, "id")
    if not agent_id: return

    config = load_config()
    entry  = _find_agent(config, agent_id)
    if not entry:
        err(f"Agent bulunamadi: {agent_id}"); return

    templates = load_templates()
    print(_c(f"\n  AGENT DUZENLE: {entry['display_name']} ({agent_id})", Fore.CYAN, Style.BRIGHT))
    print("  Degistirmek istemediginiz alanlar icin Enter'a basin.\n")

    def _edit_field(label, key, current):
        val = input(_c(f"  {label} [{current}]: ", Fore.CYAN)).strip()
        if val:
            entry[key] = val

    _edit_field("Isim",     "display_name",   entry.get("display_name",""))
    _edit_field("Model",    "model",           entry.get("model",""))
    _edit_field("Sira",     "pipeline_order",  entry.get("pipeline_order",""))
    _edit_field("Aciklama", "description",     entry.get("description",""))

    # Prompt guncelleme
    print(_c(f"\n  Mevcut system prompt (ilk 200 karakter):", Fore.WHITE))
    tmpl_prompt = templates.get(entry.get("role_type","custom"), {}).get("system_prompt","")
    current_prompt = entry.get("custom_prompt") or tmpl_prompt
    print(f"  {current_prompt[:200]}...\n")

    change_prompt = input(_c("  System prompt'u guncelle? [e/h]: ", Fore.CYAN)).strip().lower()
    if change_prompt in ("e", "evet", "y", "yes"):
        print("  Yeni prompt (bos satir = bitis):")
        lines = []
        while True:
            line = input("  > ")
            if line == "" and lines: break
            lines.append(line)
        entry["custom_prompt"] = "\n".join(lines).strip() or None

    save_config(config)
    ok(f"Agent guncellendi: {agent_id}")


def cmd_remove(args):
    """Agent'i sil (onay ister)."""
    agent_id = _get_arg(args, "id")
    if not agent_id: return

    config = load_config()
    entry  = _find_agent(config, agent_id)
    if not entry:
        err(f"Agent bulunamadi: {agent_id}"); return

    warn(f"Silinecek: {entry['display_name']} ({agent_id})")
    confirm = input(_c("  Emin misiniz? [e/h]: ", Fore.RED)).strip().lower()
    if confirm not in ("e", "evet", "y", "yes"):
        info("Iptal edildi."); return

    config["agents"] = [a for a in config["agents"] if a["id"] != agent_id]
    save_config(config)
    ok(f"Agent silindi: {agent_id}")


def cmd_enable(args):
    _set_enabled(args, True)

def cmd_disable(args):
    _set_enabled(args, False)

def _set_enabled(args, enabled: bool):
    agent_id = _get_arg(args, "id")
    if not agent_id: return
    config = load_config()
    entry  = _find_agent(config, agent_id)
    if not entry:
        err(f"Agent bulunamadi: {agent_id}"); return
    entry["enabled"] = enabled
    save_config(config)
    durum = "aktiflestirildi" if enabled else "devre disi birakildi"
    ok(f"Agent {durum}: {entry['display_name']} ({agent_id})")


def cmd_reorder(args):
    """Numarali menu ile pipeline sirasi duzenle."""
    config = load_config()
    agents = sorted(config["agents"], key=lambda a: a.get("pipeline_order", 99))

    print(_c("\n  PIPELINE SIRASI DUZENLE", Fore.CYAN, Style.BRIGHT))
    print("  Ajanlar mevcut sirada listeleniyor.")
    print("  Her satira yeni sira numarasi yazin (Enter = degistirme):\n")

    for a in agents:
        durum = "" if a.get("enabled") else _c(" [PASIF]", Fore.RED)
        new_order = input(_c(
            f"  {a.get('pipeline_order',0):>3}. {a['display_name']}{durum} -> yeni sira: ",
            Fore.CYAN
        )).strip()
        if new_order.isdigit():
            a["pipeline_order"] = int(new_order)

    save_config(config)
    ok("Sira guncellendi.")
    cmd_list(args)


def cmd_reset(args):
    """Varsayilan 5 agent konfigurasyonuna don."""
    warn("Bu islem mevcut agent listesini silecek!")
    confirm = input(_c("  Devam? [e/h]: ", Fore.RED)).strip().lower()
    if confirm not in ("e", "evet", "y", "yes"):
        info("Iptal."); return

    default_plan  = "ollama/qwen3:14b"
    default_code  = "ollama/qwen2.5-coder:14b"
    default = {
        "version": "1.0",
        "description": "Varsayilan 5-agent konfigurasyonu.",
        "agents": [
            {"id":"agent-001","display_name":"Urun Yoneticisi",   "role_type":"product_manager",    "model":default_plan,"pipeline_order":1,"enabled":True,"description":"PRD olusturur","custom_prompt":None},
            {"id":"agent-002","display_name":"Yazilim Mimari",    "role_type":"software_architect",  "model":default_plan,"pipeline_order":2,"enabled":True,"description":"Mimari tasarim olusturur","custom_prompt":None},
            {"id":"agent-003","display_name":"Yazilim Gelistirici","role_type":"developer",          "model":default_code,"pipeline_order":3,"enabled":True,"description":"Kaynak kodu uretir","custom_prompt":None},
            {"id":"agent-004","display_name":"QA Muhendisi",      "role_type":"qa_tester",           "model":default_code,"pipeline_order":4,"enabled":True,"description":"Test raporu yazar","custom_prompt":None},
            {"id":"agent-005","display_name":"Kod Gozlemcisi",    "role_type":"reviewer",            "model":default_plan,"pipeline_order":5,"enabled":True,"description":"Duzeltme ve CHANGELOG","custom_prompt":None},
        ]
    }
    save_config(default)
    ok("Varsayilan konfigurasyona donuldu.")
    cmd_list(args)


# ─────────────────────────────────────────────
# Yardimcilar
# ─────────────────────────────────────────────

def _find_agent(config: dict, agent_id: str) -> Optional[dict]:
    for a in config.get("agents", []):
        if a["id"] == agent_id:
            return a
    return None

def _get_arg(args, name: str) -> Optional[str]:
    val = getattr(args, name, None)
    if not val:
        val = input(_c(f"  Agent ID girin: ", Fore.CYAN)).strip()
    if not val:
        err("ID gerekli."); return None
    return val

def _input_required(prompt: str) -> str:
    while True:
        val = input(_c(prompt, Fore.CYAN)).strip()
        if val: return val
        warn("Bu alan zorunlu.")

def _field(label, value):
    print(f"  {_c(label+':', Fore.WHITE, Style.BRIGHT):<30} {value}")

def _get_default_model(config: dict) -> str:
    agents = config.get("agents", [])
    if agents:
        return agents[0].get("model", "ollama/qwen2.5-coder:7b")
    return "ollama/qwen2.5-coder:7b"


# ─────────────────────────────────────────────
# CLI giris
# ─────────────────────────────────────────────

COMMANDS = {
    "list":    (cmd_list,    "Tum agentlari listele"),
    "show":    (cmd_show,    "Agent detayini goster"),
    "add":     (cmd_add,     "Yeni agent ekle"),
    "edit":    (cmd_edit,    "Agent duzenle"),
    "remove":  (cmd_remove,  "Agent sil"),
    "enable":  (cmd_enable,  "Agent'i aktifle"),
    "disable": (cmd_disable, "Agent'i devre disi birak"),
    "reorder": (cmd_reorder, "Pipeline sirasi duzenle"),
    "reset":   (cmd_reset,   "Varsayilan 5 agenta don"),
}

def main():
    parser = argparse.ArgumentParser(
        description="Agent CRUD yonetim araci",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:<10} — {v}" for k, v in COMMANDS.items()),
    )
    parser.add_argument("command", nargs="?", choices=list(COMMANDS.keys()), help="Komut")
    parser.add_argument("id",      nargs="?", help="Agent ID (show/edit/remove/enable/disable icin)")
    args = parser.parse_args()

    if not args.command:
        print(_c("\n  Kullanim: python manage_agents.py <komut> [id]", Fore.CYAN))
        print(_c("  Komutlar:", Fore.WHITE, Style.BRIGHT))
        for k, (_, desc) in COMMANDS.items():
            print(f"    {_c(k, Fore.YELLOW):<20} {desc}")
        print()
        return

    fn, _ = COMMANDS[args.command]
    fn(args)


if __name__ == "__main__":
    main()
