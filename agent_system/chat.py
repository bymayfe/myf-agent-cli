"""
chat.py — Ana giris noktasi. Interaktif terminal sohbet arayuzu.

Mimari:
  ChatUI      : Terminal renklendirme ve cikti yardimcilari
  InputHandler: prompt_toolkit tabanli gelismis input (tab, gecmis, tema)
  CommandHub  : /komut isleyici
  ChatSession : Ana sohbet dongusu (REPL)

Ozellikler:
  - Streaming output (tokenlar aninda ekranda belirir)
  - Ctrl+C ile anlık iptal
  - Tab tamamlama / komut gecmisi (prompt_toolkit)
  - /think on|off — think modu anlık degistirme
  - /settings — interaktif ayarlar menusu
  - /name <isim> — koordinator ismini aninda degistir
  - Baslangiçta model warmup (GPU hemen hazir)
"""

from __future__ import annotations
import sys
import os
import re
import json
import time
import subprocess
from pathlib import Path
from typing import Optional, Any
import warnings
import logging

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="litellm")

_HERE = Path(__file__).parent.resolve()
for _sub in [_HERE, _HERE / "core", _HERE / "engines", _HERE / "agents", _HERE / "llm", _HERE / "storage", _HERE / "tests"]:
    _s = str(_sub)
    if _sub.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

# Eger sistem python ile calistirildiysa .venv paketlerini sys.path'e ekle
for _site in (_HERE / ".venv" / "lib").glob("python*/site-packages"):
    if _site.is_dir() and str(_site) not in sys.path:
        sys.path.insert(0, str(_site))

# ── Renkler (colorama) ────────────────────────────────────────────────────
try:
    from colorama import init as _cinit, Fore, Style
    _cinit(autoreset=True)
    _HAS_COLOR = True
except ImportError:
    _HAS_COLOR = False
    class _D:
        def __getattr__(self, _): return ""
    Fore = Style = _D()

# ── Gelismis input (prompt_toolkit) ──────────────────────────────────────
try:
    from prompt_toolkit             import prompt as _pt_prompt
    from prompt_toolkit.completion  import WordCompleter
    from prompt_toolkit.history     import InMemoryHistory
    from prompt_toolkit.formatted_text import ANSI
    _HAS_PT = True
except ImportError:
    _HAS_PT = False

# ── Proje modulleri ───────────────────────────────────────────────────────
from settings          import settings
from config            import (print_config, list_providers,
                                get_active_provider_name,
                                set_active_provider, reload_config,
                                list_provider_models, add_provider_model, set_provider_active_model,
                                get_output_dir)
from coordinator_agent import CoordinatorAgent
from agents            import load_agents
from brain             import list_output_files
from session_manager   import session_manager
from reach_engine      import reach_engine
from codebase_graph    import codebase_graph
from command_registry  import CommandRegistry

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(_HERE / "agent_system.log",
                                   encoding="utf-8", mode="a")],
)


# ═══════════════════════════════════════════════════════════════════════════
# ChatUI
# ═══════════════════════════════════════════════════════════════════════════

class ChatUI:
    """Terminal cikti yardimcilari. Tum metodlar @classmethod."""

    @staticmethod
    def _c(text: str, color: str = "", style: str = "") -> str:
        if not _HAS_COLOR:
            return str(text)
        return f"{style}{color}{text}{Style.RESET_ALL}"

    @classmethod
    def header(cls) -> None:
        from permission_manager import permission_manager
        c = cls._c
        curr = session_manager.current_session
        mode_tag = {"sequential": "1 · PIPELINE", "subagent": "2 · SUBAGENT", "interactive": "3 · CHAT"}.get(
            settings.execution_mode, settings.execution_mode.upper()
        )
        print(c("\n╭" + "─" * 68 + "╮", Fore.CYAN, Style.BRIGHT))
        print(c("│  MYF CLI", Fore.CYAN, Style.BRIGHT) + c(f"  •  {mode_tag:<17}", Fore.GREEN, Style.BRIGHT) + c(" " * 29 + "│", Fore.CYAN))
        print(c(f"│  Oturum: {curr.session_id:<18} Klasör: {curr.folder_name[:25]:<25} │", Fore.CYAN))
        print(c("│  ", Fore.CYAN) + f"Güvenlik: {permission_manager.get_status_badge()}" + c(" " * 8 + "│", Fore.CYAN))
        print(c("╰" + "─" * 68 + "╯", Fore.CYAN, Style.BRIGHT))

    @classmethod
    def system(cls, text: str) -> None:
        print(cls._c(f"\n  [SYS] {text}", Fore.YELLOW))

    @classmethod
    def success(cls, text: str) -> None:
        print(cls._c(f"\n  [OK]  {text}", Fore.GREEN, Style.BRIGHT))

    @classmethod
    def error(cls, text: str) -> None:
        print(cls._c(f"\n  [!!]  {text}", Fore.RED, Style.BRIGHT))

    @classmethod
    def info(cls, text: str) -> None:
        print(cls._c(f"  {text}", Fore.WHITE))

    @classmethod
    def think_status(cls, is_on: bool) -> None:
        state = (cls._c("ACIK  [yavas/derin]", Fore.GREEN, Style.BRIGHT)
                 if is_on else cls._c("KAPALI [hizli]", Fore.RED))
        print(cls._c(f"\n  [THINK] {state}", Fore.CYAN))

    @classmethod
    def progress(cls, step: int, total: int, agent, event_type: str = "start", details: dict = None) -> None:
        """Pipeline real-time ilerleme ciktisi."""
        c = cls._c
        details = details or {}
        pct = min(100, int(100 * step / total)) if total > 0 else 0
        filled = min(20, int(20 * step / total)) if total > 0 else 0
        bar = c("=" * filled, Fore.GREEN, Style.BRIGHT) + c("-" * (20 - filled), Fore.WHITE)

        retry = details.get("retry_count", 0)
        loop_tag = c(f" [DONGU {retry}]", Fore.YELLOW, Style.BRIGHT) if retry > 0 else ""

        if event_type == "start":
            import datetime as _dt
            now_str = _dt.datetime.now().strftime("%H:%M:%S")
            print(c(f"\n  [{bar}] {pct}% (Adim {step}/{total}){loop_tag}", Fore.CYAN))
            disp_name = getattr(agent, "display_name", None) or details.get("agent_name", "Subagent")
            icon = getattr(agent, "icon", None) or details.get("icon", "[SUB]")
            role_type = getattr(agent, "role_type", None) or details.get("role", "specialist")
            model_val = getattr(agent, "model", None) or details.get("model", settings.code_model)
            model_short = model_val.split('/')[-1] if model_val else "default"
            print(f"  {c('▶ ', Fore.GREEN, Style.BRIGHT)} {c(disp_name, Fore.WHITE, Style.BRIGHT)} {icon}  {c(now_str, Fore.WHITE)}")
            print(f"    Rol: {role_type}  |  Model: {model_short}")
        elif event_type == "generating":
            import datetime as _dt
            now_str = _dt.datetime.now().strftime("%H:%M:%S")
            prep_s = details.get("prep_sec", 0.0)
            print(f"    {c('⚙️', Fore.YELLOW)} Prompt hazirlandi ({details.get('prompt_len', 0)} char — {prep_s}s)  {c(f'[{now_str}]', Fore.WHITE)}")
        elif event_type == "completed":
            elapsed = details.get("elapsed_sec")
            elapsed_str = c(f" — {elapsed}s", Fore.YELLOW, Style.BRIGHT) if elapsed else ""

            written = details.get("written", [])
            if written:
                print(f"    {c('✓', Fore.GREEN, Style.BRIGHT)} {len(written)} dosya olusturuldu{elapsed_str}:")
                for f in written:
                    print(f"      {c('↳', Fore.CYAN)} {f}")
            else:
                print(f"    {c('✓', Fore.GREEN, Style.BRIGHT)} Rapor hazirlandi ({details.get('raw_len', 0)} karakter){elapsed_str}.")

            tr = details.get("test_results")
            if tr:
                if tr.get("error"):
                    err_msg = tr["error"].strip()
                    indented_err = "\n".join("      " + line for line in err_msg.splitlines())
                    if tr.get("resolved"):
                        print(f"    🛠️  {c('[GİDERİLEN HATA DENETİMİ]', Fore.CYAN, Style.BRIGHT)} {c('(Micro-Fix / Cerrahi Yama ile Çözüldü):', Fore.GREEN, Style.BRIGHT)}\n{c(indented_err, Fore.WHITE)}")
                    else:
                        print(f"    ❌ {c('[AKTİF ÇÖZÜLEMEMİŞ HATA]:', Fore.RED, Style.BRIGHT)}\n{indented_err}")
                elif tr.get("executed"):
                    print(f"    🧪 {c('Fiziksel Calistirma Testi Basarili!', Fore.GREEN, Style.BRIGHT)} Kodlar ornek veri ile calistirildi.")
                    if tr.get("output"):
                        out_str = tr["output"].strip()
                        if out_str:
                            print(c("      ┌── [Terminal Ciktisi] ──────────────────────────", Fore.BLACK, Style.BRIGHT))
                            for line in out_str[:1500].splitlines():
                                print(f"      │ {line}")
                            print(c("      └────────────────────────────────────────────────", Fore.BLACK, Style.BRIGHT))
        elif event_type == "error":
            print(f"    {c('✗', Fore.RED, Style.BRIGHT)} Hata: {details.get('error')}")

    _stream_state = "none"
    _think_count = 0

    @classmethod
    def stream_begin(cls, name: str) -> None:
        """Streaming cevap basliyor — etiket yaz."""
        cls._stream_state = "none"
        cls._think_count = 0
        label = cls._c(f"  ┌─ {name}", Fore.CYAN, Style.BRIGHT)
        hint = cls._c("  Ctrl+C: durdur", Fore.YELLOW)
        print(f"\n{label}{hint}\n  │ ", end="", flush=True)

    @classmethod
    def stream_token(cls, token: str, token_type: str = "content") -> None:
        """Tek bir streaming tokenini yaz (thinking veya content)."""
        if token_type == "thinking":
            if cls._stream_state != "thinking":
                cls._stream_state = "thinking"
                cls._think_count += 1
                header = cls._c(f"\n  ┌── 💭 [DÜŞÜNCE {cls._think_count} / REASONING] ──────────────────────────\n  ", Fore.MAGENTA, Style.BRIGHT)
                print(header, end="", flush=True)
            # Düşünme tokenları: Parlak ve çok rahat okunan sıcak sarı/amber tonu
            print(cls._c(token, Fore.YELLOW), end="", flush=True)
        else:
            if cls._stream_state != "content":
                if cls._stream_state == "thinking":
                    header = cls._c(f"\n  └── 🎯 [YANIT {cls._think_count} / MODEL ÇIKTISI] ────────────────────────\n  ", Fore.GREEN, Style.BRIGHT)
                    print(header, end="", flush=True)
                cls._stream_state = "content"
            # Model yanıt tokenları: Kristal netliğinde parlak beyaz
            print(cls._c(token, Fore.WHITE, Style.BRIGHT), end="", flush=True)

    @classmethod
    def stream_end(cls) -> None:
        """Streaming bitti, yeni satira gec."""
        cls._stream_state = "none"
        cls._think_count = 0
        print(cls._c("\n  └─", Fore.CYAN))

    @classmethod
    def pending_edits(cls, edits: list[dict]) -> None:
        """Modelin önerdiği, henüz diske yazılmamış değişiklikleri göster."""
        print(cls._c("\n  ┌─ ÖNERİLEN DOSYA DEĞİŞİKLİKLERİ ─────────────────────────", Fore.YELLOW, Style.BRIGHT))
        for edit in edits:
            action = "güncelle" if edit["exists"] else "oluştur"
            print(f"  │  {cls._c(action.upper(), Fore.GREEN, Style.BRIGHT):<18} {edit['filename']}")
        print(cls._c("  └─ Diske yazılmadı. /apply ile uygula, /discard ile at. ───", Fore.YELLOW, Style.BRIGHT))

    @classmethod
    def agents_table(cls, agents) -> None:
        c = cls._c
        print(c("\n  AGENT LISTESI", Fore.CYAN, Style.BRIGHT))
        print(f"  {'S':<4} {'ID':<12} {'Isim':<22} {'Rol':<20} {'Dur':<6} Model")
        print("  " + "-" * 78)
        for a in agents:
            dur = c("ON", Fore.GREEN) if a.enabled else c("OFF", Fore.RED)
            model_s = a.model.split("/")[-1][:22]
            print(f"  {a.pipeline_order:<4} {a.id:<12} {a.display_name:<22} "
                  f"{a.role_type:<20} {dur:<6} {model_s}")


# ═══════════════════════════════════════════════════════════════════════════
# InputHandler — prompt_toolkit tabanli gelismis input
class InputHandler:
    """
    prompt_toolkit ile gelismis terminal input:
    - Tab tamamlama (/ ile baslayan komutlar)
    - Ok tusu ile gecmis (yukari/asagi)
    - Alt durum cubugu (Bottom Toolbar — MYF CLI)
    - Ctrl+C ile iptal / Ctrl+D ile cikis
    """

    def __init__(self):
        self._history   = InMemoryHistory() if _HAS_PT else None
        self._completer = WordCompleter(
            CommandRegistry.get_completer_words(), pattern=re.compile(r"/\w*"), sentence=True
        ) if _HAS_PT else None

    @staticmethod
    def _bottom_toolbar():
        """MYF CLI alt durum, kısayol ve canlı model kota/limit çubuğu."""
        try:
            from engines.quota_engine import quota_engine
            from config import LLM_PARAMS
            from settings import settings
            curr = session_manager.current_session
            sid = curr.session_id if curr else "sess"
            pdir = curr.project_dir if curr else ""
            provider_id = LLM_PARAMS.get("provider", "ollama")
            model_name = settings.coordinator_model
            return ANSI(quota_engine.get_bottom_toolbar_text(provider_id, model_name, sid, pdir))
        except Exception:
            curr = session_manager.current_session
            sid = curr.session_id if curr else "sess"
            return ANSI(
                f"\x1b[97;44m [Ctrl+C: İptal] \x1b[0m \x1b[36m[Tab: Tamamla]\x1b[0m \x1b[33m[/help]\x1b[0m \x1b[90m│\x1b[0m \x1b[37mOturum: {sid}\x1b[0m \x1b[90m│\x1b[0m \x1b[32;1mMYF CLI\x1b[0m "
            )

    def read(self, name: str, think_on: bool) -> str | None:
        """
        Kullanici girdisi al.
        Returns None => Ctrl+D (cikis), raises KeyboardInterrupt => Ctrl+C
        """
        # Her mod aynı REPL'i kullanır; prompt, kullanıcının hangi çalışma
        # biçiminde olduğunu terminale bakınca net biçimde göstermelidir.
        # Chat modunda bu, klasik bir CLI istemi gibi serbest metin girişidir;
        # yalnızca '/' ile başlayan satırlar yerleşik MYF komutlarıdır.
        mode = settings.execution_mode
        mode_tag = {
            "sequential": "1:pipe",
            "subagent": "2:agent",
            "interactive": "3:chat",
        }.get(mode, mode)
        think_tag = " think" if think_on else ""
        prompt_str = f"myf[{mode_tag}{think_tag}] > "

        if _HAS_PT:
            try:
                return _pt_prompt(
                    prompt_str,
                    history=self._history,
                    completer=self._completer,
                    bottom_toolbar=self._bottom_toolbar,
                    complete_while_typing=True,
                ).strip()
            except EOFError:
                return None
            # KeyboardInterrupt geçirilir — caller ele alır
        else:
            return input(prompt_str).strip()


class CommandHub:
    """Slash komutlarini isleyen sinif."""

    def __init__(self, coordinator: CoordinatorAgent, session: Optional[Any] = None):
        self.coord = coordinator
        self.ui    = ChatUI
        self.session = session

    def is_command(self, text: str) -> bool:
        clean = text.strip().strip('"').strip("'")
        if not clean:
            return False

        parts = clean.split(maxsplit=1)
        first_word = parts[0].lower()

        # Eğer kullanıcı tek başına bir dosya veya klasör sürükleyip bıraktıysa (/attach)
        if len(parts) == 1 and Path(clean).exists() and not first_word.startswith("/"):
            return True

        # Bilinen geçerli slash komutları ve çıkış sözcükleri
        valid_cmds = {
            "/run", "/start", "/quit", "/exit", "/help", "/attach", "/file", "/import",
            "/permission", "/security", "/new", "/search", "/reach", "/graph", "/codemap",
            "/think", "/model", "/setmodel", "/mode", "/mod", "/settings", "/name",
            "/agents", "/add", "/remove", "/delete", "/clear", "/cls", "/provider",
            "/reset", "/status", "/dir", "/open", "/resume", "/sessions", "/history",
            "/continue", "/devam", "/purge", "/config", "/logs", "/quota", "/limits",
            "/bakiye", "/ui", "/checkpoints", "/commits", "/rollback", "/revert",
            "/changes", "/apply", "/discard"
        }
        if first_word in valid_cmds or first_word in ("exit", "quit"):
            return True

        return False

    def dispatch(self, raw: str) -> None:
        """Komutu isle."""
        clean = raw.strip().strip('"').strip("'")
        if not raw.startswith("/") and Path(clean).exists():
            self._attach_file_or_dir(clean)
            return

        parts = raw.strip().split(maxsplit=1)
        cmd   = parts[0].lower()
        arg   = parts[1].strip() if len(parts) > 1 else ""

        {
            "/run":        self._run_cmd,
            "/start":      self._run_cmd,
            "/quit":       self._quit,
            "/exit":       self._quit,
            "/help":       self._help,
            "/attach":     self._attach_file_or_dir,
            "/file":       self._attach_file_or_dir,
            "/import":     self._attach_file_or_dir,
            "/permission": self._permission_menu,
            "/security":   self._permission_menu,
            "/new":        self._new_session,
            "/search":     self._search_web,
            "/reach":      self._search_web,
            "/graph":      self._show_graph,
            "/codemap":    self._show_graph,
            "/think":      self._think,
            "/model":      self._model_cmd,
            "/setmodel":   self._model_cmd,
            "/mode":       self._mode,
            "/mod":        self._mode,
            "/settings":   self._settings_menu,
            "/name":       self._name,
            "/agents":     self._agents,
            "/add":        self._add_agent,
            "/remove":     self._delete_dispatcher,
            "/delete":     self._delete_dispatcher,
            "/clear":      self._clear_screen_and_chat,
            "/cls":        self._clear_screen_and_chat,
            "/provider":   self._provider,
            "/reset":      self._new_session,
            "/status":     self._status,
            "/changes":    self._changes_cmd,
            "/apply":      self._apply_cmd,
            "/discard":    self._discard_cmd,
            "/dir":        self._dir_open,
            "/open":       self._dir_open,
            "/resume":     self._resume,
            "/sessions":   self._resume,
            "/history":    self._resume,
            "/continue":   self._continue_cmd,
            "/devam":      self._continue_cmd,
            "/purge":      self._purge_sessions,
            "/config":     self._config_show,
            "/logs":       self._logs,
            "/quota":      self._quota_cmd,
            "/limits":     self._quota_cmd,
            "/bakiye":     self._quota_cmd,
            "/web":        self._web_cmd,
            "/harness":    self._web_cmd,
            "/ui":         self._show_ui,
            "/checkpoints":self._checkpoints_cmd,
            "/commits":    self._checkpoints_cmd,
            "/rollback":   self._rollback_cmd,
            "/revert":     self._rollback_cmd,
        }.get(cmd, self._unknown)(arg)

    # ── Komutlar ──────────────────────────────────────────

    def _run_cmd(self, _):
        if self.session:
            self.session._run_pipeline()

    def _changes_cmd(self, _):
        if self.session:
            self.session._show_pending_edits()

    def _apply_cmd(self, arg: str):
        if self.session:
            self.session._apply_pending_edits(arg)

    def _discard_cmd(self, _):
        if self.session:
            self.session._discard_pending_edits()

    def _continue_cmd(self, _):
        """Ctrl+C ile durdurulan pipeline'i checkpoint'ten devam ettirir."""
        if self.session:
            self.session._continue_pipeline()

    def _quit(self, _):
        from ollama_client import unload_all_ollama_models
        unload_all_ollama_models()
        session_manager.cleanup_on_exit()
        self.ui.system("Model bellekten bosaltildi. Gorusuruz!")
        sys.exit(0)

    def _unknown(self, _):
        self.ui.error("Bilinmeyen komut. /help yazin.")

    def _help(self, _):
        print(ChatUI._c(CommandRegistry.get_help_text(), Fore.WHITE))

    def _search_web(self, query: str):
        """
        /search veya /reach komutu — akıllı yönlendirici.

        Alt komutlar:
          /reach github <sorgu>   → GitHub repo arama (gh CLI)
          /reach youtube <url>    → YouTube metadata + altyazı
          /reach v2ex [sorgu]     → V2EX hot topics
          /reach rss <url>        → RSS/Atom besleme okuma
          /reach twitter <sorgu> → Twitter/X arama (cookie gerekir)
          /reach reddit <sorgu>  → Reddit arama (cookie gerekir)
          /reach status           → Kanal durumu tablosu
          /reach web <sorgu>      → Sadece DDG web arama
          /reach <genel sorgu>    → Otomatik kanal seçimi
        """
        if not query:
            self.ui.system(
                "Kullanım: /reach <sorgu>\n"
                "  /reach github <sorgu>    → GitHub repo arama\n"
                "  /reach youtube <url>     → YouTube video okuma\n"
                "  /reach v2ex              → V2EX hot topics\n"
                "  /reach rss <url>         → RSS besleme okuma\n"
                "  /reach twitter <sorgu>   → Twitter arama\n"
                "  /reach reddit <sorgu>    → Reddit arama\n"
                "  /reach status            → Kanal durumu\n"
                "  /reach web <sorgu>       → Web arama"
            )
            return

        # Alt komut parse
        parts = query.strip().split(maxsplit=1)
        sub   = parts[0].lower()
        rest  = parts[1].strip() if len(parts) > 1 else ""

        if sub == "status":
            self.ui.system("📡 Kanal durumu kontrol ediliyor...")
            print(f"\n{reach_engine.format_channel_status()}\n")
            return

        if sub == "github":
            if not rest:
                self.ui.error("Kullanım: /reach github <sorgu>")
                return
            self.ui.system(f"🐙 GitHub'da aranıyor: {rest}")
            print(f"\n{reach_engine.search_github(rest)}\n")
            return

        if sub == "youtube":
            if not rest:
                self.ui.error("Kullanım: /reach youtube <url>")
                return
            self.ui.system(f"🎬 YouTube okunuyor: {rest}")
            print(f"\n{reach_engine.read_youtube(rest)}\n")
            return

        if sub == "v2ex":
            self.ui.system("🔥 V2EX hot topics alınıyor...")
            print(f"\n{reach_engine.search_v2ex(rest)}\n")
            return

        if sub == "rss":
            if not rest:
                self.ui.error("Kullanım: /reach rss <url>")
                return
            self.ui.system(f"📰 RSS okunuyor: {rest}")
            print(f"\n{reach_engine.read_rss(rest)}\n")
            return

        if sub == "twitter":
            if not rest:
                self.ui.error("Kullanım: /reach twitter <sorgu>")
                return
            self.ui.system(f"🐦 Twitter'da aranıyor: {rest}")
            print(f"\n{reach_engine.search_twitter(rest)}\n")
            return

        if sub == "reddit":
            if not rest:
                self.ui.error("Kullanım: /reach reddit <sorgu>")
                return
            self.ui.system(f"📕 Reddit'te aranıyor: {rest}")
            print(f"\n{reach_engine.search_reddit(rest)}\n")
            return

        if sub == "web":
            target = rest or query
            self.ui.system(f"🌐 Web'de aranıyor: {target}")
            print(f"\n{reach_engine.search_web(target)}\n")
            return

        # Genel → akıllı yönlendirici
        self.ui.system(f"🌐 Araştırılıyor: {query}...")
        res = reach_engine.search(query)
        print(f"\n{res}\n")

    def _web_cmd(self, _=None):
        """MYF Web Agent & Dashboard sunucusunu başlatır ve tarayıcıda açar."""
        import subprocess, webbrowser
        url = "http://localhost:3005"
        self.ui.system(f"🚀 MYF Web Agent & Dashboard Başlatılıyor: {url}")
        try:
            py_bin = sys.executable
            server_script = Path(__file__).parent / "web_server.py"
            subprocess.Popen([py_bin, str(server_script), "--port", "3005", "--no-browser"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.6)
            webbrowser.open(url)
            self.ui.success(f"Web Kokpiti tarayıcınızda açıldı ({url})!")
        except Exception as e:
            self.ui.error(f"Web sunucusu açılamadı: {e}")

    def _show_ui(self, _=None):
        import webbrowser
        url = "http://localhost:9749"
        self.ui.system(f"🌐 Görsel Kod Haritası Web Arayüzü Açılıyor: {url}")
        try:
            webbrowser.open(url)
        except Exception as e:
            self.ui.error(f"Tarayıcı açılamadı: {e}")

    def _show_graph(self, arg: str):
        if arg.lower() in ("ui", "web", "open", "ac"):
            self._show_ui()
            return
        out_dir = get_output_dir()
        self.ui.system(f"📊 Kod tabani bilgi grafigi taraniyor: {out_dir}...")
        if arg:
            res = codebase_graph.search_graph(arg, out_dir)
        else:
            res = codebase_graph.get_summary_repomap(out_dir)
        print(f"\n{res}\n")

    def _checkpoints_cmd(self, _=None):
        """Otomatik oluşturulmuş git checkpoint listesini gösterir."""
        from git_guard import GitGuard
        out_dir = get_output_dir()
        guard = GitGuard(project_dir=out_dir)
        if not guard.is_git_repo():
            self.ui.system(f"Bu proje dizininde ({out_dir}) henüz bir git deposu veya checkpoint bulunmuyor.")
            return

        cps = guard.list_checkpoints(limit=15)
        if not cps:
            self.ui.system("Henüz kaydedilmiş bir [AUTO-CHECKPOINT] bulunamadı.")
            return

        print(self.ui._c("\n  ═══ OTOMATİK GİT CHECKPOINT LİSTESİ ═══", Fore.CYAN, Style.BRIGHT))
        for idx, cp in enumerate(cps, 1):
            print(f"  [{idx}] {self.ui._c(cp['short_hash'], Fore.YELLOW, Style.BRIGHT)}  {self.ui._c(cp['date'], Fore.WHITE)}  {cp['message']}")
        print(self.ui._c("\n  Geri dönmek için: /rollback <commit_hash> veya /rollback 1", Fore.CYAN))

    def _rollback_cmd(self, arg: str):
        """Belirtilen commit hash'ine veya liste sırasına göre güvenli rollback yapar."""
        from git_guard import GitGuard
        out_dir = get_output_dir()
        guard = GitGuard(project_dir=out_dir)
        if not guard.is_git_repo():
            self.ui.error("Geri alma başarısız: Git deposu bulunamadı.")
            return

        target_ref = arg.strip()
        if not target_ref or target_ref.lower() == "list":
            self._checkpoints_cmd()
            target_ref = input(self.ui._c("  Geri dönülecek commit hash veya no: ", Fore.YELLOW)).strip()

        if not target_ref:
            return

        # Sayı girildiyse checkpoint listesinden çöz
        if target_ref.isdigit():
            cps = guard.list_checkpoints(limit=15)
            idx = int(target_ref) - 1
            if 0 <= idx < len(cps):
                target_ref = cps[idx]["hash"]
            else:
                self.ui.error("Geçersiz checkpoint numarası.")
                return

        # ── Kullanıcıdan açık onay iste ────────────────────────────────────
        confirm = input(self.ui._c(
            f"  ⚠️  '{target_ref[:8]}' checkpoint'ine dönülecek (untracked dosyalar temizlenecek). Onaylıyor musunuz? [e/H]: ",
            Fore.YELLOW,
            Style.BRIGHT
        )).strip().lower()

        if confirm not in ("e", "evet", "y", "yes"):
            self.ui.system("Rollback işlemi kullanıcı tarafından iptal edildi.")
            return

        self.ui.system(f"⏪ Geri alma başlatılıyor -> Hedef: {target_ref[:8]}...")
        ok = guard.rollback(target_ref)
        if ok:
            self.ui.success(f"Başarıyla geri dönüldü ({target_ref[:8]}). Öncesi [PRE-ROLLBACK-SNAPSHOT] ile arşivlendi.")
        else:
            self.ui.error("Rollback işlemi başarısız oldu.")

    def _think(self, arg: str):
        if arg.lower() in ("on", "ac", "1", "true", "yes"):
            settings.think_mode = True
        elif arg.lower() in ("off", "kapat", "0", "false", "no"):
            settings.think_mode = False
        else:
            settings.toggle_think()
        self.ui.think_status(settings.think_mode)

    def _mode(self, arg: str):
        mode_labels = {
            "sequential": "Sıralı Otonom Pipeline (PM -> Mimar -> DEV -> QA -> Doc)",
            "subagent":   "Dinamik Subagent Orkestrasyonu (Özel Ajan Havuzu)",
            "interactive":"Doğrudan İnteraktif Sohbet (NOVA ile Canlı Kodlama)",
        }
        arg_clean = arg.strip().lower()
        if arg_clean in ("1", "sequential", "sirali", "waterfall"):
            settings.execution_mode = "sequential"
            self.ui.success(f"Çalışma Modu: [1] Sıralı Pipeline ({mode_labels['sequential']})")
        elif arg_clean in ("2", "subagent", "swarm", "ajanlar"):
            settings.execution_mode = "subagent"
            self.ui.success(f"Çalışma Modu: [2] Dinamik Subagent ({mode_labels['subagent']})")
        elif arg_clean in ("3", "interactive", "chat", "sohbet"):
            settings.execution_mode = "interactive"
            self.ui.success(f"Çalışma Modu: [3] İnteraktif Sohbet ({mode_labels['interactive']})")
        else:
            print(self.ui._c("\n  ══ ÇALIŞMA MODU SEÇİMİ ════════════════════════════════", Fore.CYAN, Style.BRIGHT))
            print(f"  [1] Sıralı Pipeline  : {mode_labels['sequential']}")
            print(f"  [2] Dinamik Subagent : {mode_labels['subagent']}")
            print(f"  [3] İnteraktif Chat  : {mode_labels['interactive']}")
            curr = settings.execution_mode
            print(f"\n  Aktif Mod: {self.ui._c(curr.upper(), Fore.GREEN, Style.BRIGHT)} ({mode_labels.get(curr, '')})\n")
            ans = input(self.ui._c("  Seçiminiz [1 / 2 / 3 / Enter=İptal]: ", Fore.YELLOW)).strip().lower()
            if ans in ("1", "sequential", "sirali"):
                settings.execution_mode = "sequential"
                self.ui.success(f"Mod: [1] {mode_labels['sequential']} seçildi.")
            elif ans in ("2", "subagent", "swarm"):
                settings.execution_mode = "subagent"
                self.ui.success(f"Mod: [2] {mode_labels['subagent']} seçildi.")
            elif ans in ("3", "interactive", "chat"):
                settings.execution_mode = "interactive"
                self.ui.success(f"Mod: [3] {mode_labels['interactive']} seçildi.")

    def _settings_menu(self, _):
        ui = ChatUI
        c  = ui._c

        # (key, label, type, category)
        fields = [
            # ── Arayuz ──
            ("coordinator_name",    "Koordinator ismi",          str,   "Arayuz"),
            ("think_mode",          "Think modu (on/off)",        bool,  "Arayuz"),
            ("warmup",              "Warmup (on/off)",            bool,  "Arayuz"),
            # ── LLM ──
            ("temperature",         "Sicaklik (0.0-2.0)",         float, "LLM"),
            ("max_tokens",          "Maks token",                 int,   "LLM"),
            ("top_p",               "Top-P Cekirdek (0.0-1.0)",   float, "LLM"),
            ("top_k",               "Top-K Siniri (1-200)",       int,   "LLM"),
            # ── Pipeline Modelleri ──
            ("default_model",       "Genel Model (Tüm Sistem)",   str,   "Modeller"),
            ("planning_model",      "Planlama modeli (PRD+Mimar)",str,   "Modeller"),
            ("code_model",          "Kod uretimi+Escalation",     str,   "Modeller"),
            ("micro_fix_model",     "Micro-Fix modeli",           str,   "Modeller"),
            ("coordinator_model",   "Koordinator (sohbet)",       str,   "Modeller"),
            # ── Pipeline Davranis & Denetim ──
            ("micro_fix_max_tries", "Micro-Fix max deneme (1-10)",int,   "Pipeline"),
            ("full_autonomy_cap",   "Full otonomi ust siniri",    int,   "Pipeline"),
            ("repomap_tokens",      "RepoMap token",              int,   "Pipeline"),
            ("auto_audit_log",      "Otomatik AUDIT_LOG.md yazimi",bool,  "Pipeline"),
        ]

        print(c("\n  ══ AYARLAR ══════════════════════════════════════════", Fore.CYAN, Style.BRIGHT))
        current_cat = ""
        for i, (key, label, _, cat) in enumerate(fields, 1):
            if cat != current_cat:
                current_cat = cat
                print(c(f"\n  ── {cat} ────────────────────────────────────────────", Fore.YELLOW))
            val = getattr(settings, key)
            val_str = str(val)
            print(f"  {c(str(i), Fore.YELLOW, Style.BRIGHT)}. {label:<32} {c(val_str, Fore.WHITE, Style.BRIGHT)}")
        print(f"\n  {c('0', Fore.WHITE)}. Cikis\n")

        secim = input(c(f"  Degistir [1-{len(fields)} / 0]: ", Fore.CYAN)).strip()
        if not secim.isdigit() or secim == "0":
            return

        idx = int(secim) - 1
        if not (0 <= idx < len(fields)):
            return

        key, label, typ, _ = fields[idx]
        current = getattr(settings, key)

        if typ == bool:
            settings.set(key, str(not getattr(settings, key)))
            ui.success(f"{key}: {'Acik' if getattr(settings, key) else 'Kapali'}")
            if key == "think_mode":
                self.ui.think_status(settings.think_mode)
        else:
            val = input(c(f"  {label} [{current}]: ", Fore.CYAN)).strip()
            if val:
                if key == "default_model":
                    new_val = settings.set_model_all(val)
                    self.coord.refresh_settings()
                    ui.success(f"Tüm sistem modelleri güncellendi: {new_val}")
                else:
                    ok = settings.set(key, val)
                    if ok:
                        new_val = getattr(settings, key)
                        if key == "coordinator_name":
                            self.coord.refresh_settings()
                        # Model degisince config'i de guncelle
                        if key in ("planning_model", "code_model", "micro_fix_model", "coordinator_model"):
                            try:
                                from config import reload_config
                                reload_config()
                            except Exception:
                                pass
                        ui.success(f"{key} = {new_val}")
                    else:
                        ui.error("Gecersiz deger.")

    def _model_cmd(self, arg: str):
        ui = ChatUI
        if not arg:
            print(ui._c(f"\n  Mevcut Varsayılan Model: {Fore.YELLOW}{settings.default_model}", Fore.CYAN, Style.BRIGHT))
            print(ui._c("  Kullanım: /model <model_adi> (Örn: /model qwen3.5:4b veya /model qwen3.8:27b)\n", Fore.WHITE))
            arg = input(ui._c(f"  Yeni model [{settings.default_model}]: ", Fore.CYAN)).strip()
        if arg:
            new_model = settings.set_model_all(arg)
            self.coord.refresh_settings()
            ui.success(f"Tüm sistem modelleri tek noktadan güncellendi: {new_model}")
            print(ui._c(f"  • Planlama, Kod, Micro-Fix ve Koordinatör -> {new_model}\n", Fore.WHITE))

    def _name(self, arg: str):
        if not arg:
            arg = input(ChatUI._c(
                f"  Yeni isim [{settings.coordinator_name}]: ", Fore.CYAN
            )).strip()
        if arg:
            settings.coordinator_name = arg
            self.coord.refresh_settings()
            ChatUI.success(f"Isim: {arg}")

    def _agents(self, _):
        try:
            agents = load_agents(enabled_only=False)
            ChatUI.agents_table(agents)
        except Exception as e:
            ChatUI.error(str(e))

    def _add_agent(self, _):
        manage = _HERE / "agents" / "manage_agents.py" if (_HERE / "agents" / "manage_agents.py").exists() else _HERE / "manage_agents.py"
        if manage.exists():
            subprocess.run([sys.executable, str(manage), "add"])
            self.coord.reset()
            ChatUI.system("Agent eklendi.")

    def _permission_menu(self, _):
        """Güvenlik ve izin modunu ayarla."""
        ui = ChatUI
        c  = ui._c

        print(c("\n  🛡️ GUVENLIK VE IZIN AYARLARI", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 55, Fore.CYAN))
        print(f"  Mevcut Mod: {c(settings.permission_mode.upper(), Fore.YELLOW, Style.BRIGHT)}")
        print()
        print("  1. ask           - Her dosya/komut isleminde IZIN SOR (Prompt)")
        print("  2. session_allow - Bu oturum boyunca otomatik izin ver (Varsayilan)")
        print("  3. always_allow  - HER SEYE HER ZAMAN IZIN VER (Tam Otonom Mod)")
        print()

        ans = input(c("  Mod seciniz [1-3, Enter=mevcut]: ", Fore.YELLOW)).strip()
        if ans == "1":
            settings.permission_mode = "ask"
            ui.success("Guvenlik modu: 'ask' (Her islemde sorulacak)")
        elif ans == "2":
            settings.permission_mode = "session_allow"
            ui.success("Guvenlik modu: 'session_allow' (Oturum boyunca otomatik izin)")
        elif ans == "3":
            settings.permission_mode = "always_allow"
            ui.success("Guvenlik modu: 'always_allow' (Her seye her zaman izin - Tam Otonom)")

    def _remove_agent(self, arg: str = ""):
        manage = _HERE / "agents" / "manage_agents.py" if (_HERE / "agents" / "manage_agents.py").exists() else _HERE / "manage_agents.py"
        if manage.exists():
            if arg:
                subprocess.run([sys.executable, str(manage), "remove", arg])
                self.coord.reset()
                return
            subprocess.run([sys.executable, str(manage), "list"])
            aid = input(ChatUI._c("\n  Silmek istediginiz Agent ID (0=iptal): ", Fore.YELLOW)).strip()
            if aid and aid != "0":
                subprocess.run([sys.executable, str(manage), "remove", aid])
                self.coord.reset()

    def _delete_dispatcher(self, arg: str):
        """
        /delete veya /remove komut yöneticisi.
        - /delete agent <id> -> Ajan sil
        - /delete session <id|no> -> Oturum/Proje sil
        - /delete all / /purge -> Tüm geçmişi sil
        - /delete (argümansız) -> İnteraktif silme menüsü
        """
        ui = ChatUI
        c  = ui._c
        arg_lower = arg.strip().lower()

        if arg_lower.startswith("agent") or arg_lower.startswith("ajan"):
            sub_arg = arg.split(maxsplit=1)[1].strip() if len(arg.split()) > 1 else ""
            self._remove_agent(sub_arg)
            return

        if arg_lower.startswith("session") or arg_lower.startswith("oturum") or arg_lower.startswith("proje"):
            sub_arg = arg.split(maxsplit=1)[1].strip() if len(arg.split()) > 1 else ""
            self._delete_session_interactive(sub_arg)
            return

        if arg_lower in ("all", "tumu", "tum", "purge"):
            self._purge_sessions("")
            return

        if arg_lower in ("chat", "sohbet", "history", "gecmis"):
            self._clear_screen_and_chat("")
            return

        # Argümansız ise İnteraktif Menü
        print(c("\n  🗑️  SİLME VE TEMİZLEME MENÜSÜ", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 55, Fore.CYAN))
        print("  1. Belirli Bir Oturumu / Projeyi Sil (/delete session)")
        print("  2. Mevcut Sohbet Geçmişini ve Ekranı Temizle (/clear)")
        print("  3. Özel Bir Ajanı Sil (/delete agent)")
        print("  4. TÜM Eski Oturumları ve Projeleri Sil (/purge)")
        print("  0. İptal")
        print()

        choice = input(c("  Seçiminiz [1-4 / 0]: ", Fore.YELLOW)).strip()
        if choice == "1":
            self._delete_session_interactive("")
        elif choice == "2":
            self._clear_screen_and_chat("")
        elif choice == "3":
            self._remove_agent("")
        elif choice == "4":
            self._purge_sessions("")
        else:
            ui.system("İptal edildi.")

    def _parse_selection_indices(self, raw: str, max_count: int) -> tuple[list[int], bool]:
        """
        'd1-5', 'd1,3,5 --files', '1-3 -f', 'del 2-4 --file' gibi girdileri:
        (0-tabanli_indis_listesi, delete_files_bool) olarak ayrıştırır.
        """
        delete_files = bool(re.search(r"(--files?|-f|--all|--hard|--full)\b", raw, flags=re.I))
        cleaned = re.sub(r"(--files?|-f|--all|--hard|--full)\b", "", raw, flags=re.I)
        cleaned = re.sub(r"^(d|del|delete)\s*", "", cleaned.strip(), flags=re.I).strip()

        indices = set()
        parts = cleaned.split(",")
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if "-" in p:
                sub_parts = p.split("-", maxsplit=1)
                if sub_parts[0].strip().isdigit() and sub_parts[1].strip().isdigit():
                    start = int(sub_parts[0].strip())
                    end = int(sub_parts[1].strip())
                    for i in range(min(start, end), max(start, end) + 1):
                        if 1 <= i <= max_count:
                            indices.add(i - 1)
            elif p.isdigit():
                val = int(p)
                if 1 <= val <= max_count:
                    indices.add(val - 1)
        return sorted(list(indices)), delete_files

    def _delete_session_interactive(self, target_id: str):
        ui = ChatUI
        c  = ui._c
        sessions = session_manager.list_all_sessions()
        if not sessions:
            ui.system("Silinecek oturum / proje bulunamadı.")
            return

        if target_id:
            indices, delete_files = self._parse_selection_indices(target_id, len(sessions))
            if indices:
                deleted_names = []
                for idx in indices:
                    t = sessions[idx]
                    if session_manager.delete_session(t["path"], delete_files=delete_files):
                        note = " (Dosyalar dahil)" if delete_files else " (Sohbet temizlendi, kodlar korundu)"
                        deleted_names.append(f"#{idx+1} {t['title']} ({t['session_id']}){note}")
                if deleted_names:
                    ui.success(f"{len(deleted_names)} adet oturum işlendi:\n  ↳ " + "\n  ↳ ".join(deleted_names))
                else:
                    ui.error("Oturumlar silinemedi.")
                return

            # ID veya klasör adıyla silme denemesi
            delete_files_direct = bool(re.search(r"(--files?|-f|--all)\b", target_id, flags=re.I))
            clean_id = re.sub(r"(--files?|-f|--all)\b", "", target_id, flags=re.I).strip()
            ok = session_manager.delete_session(clean_id, delete_files=delete_files_direct)
            if ok:
                note = " (Dosyalar dahil)" if delete_files_direct else " (Sohbet temizlendi, kodlar korundu)"
                ui.success(f"Oturum '{clean_id}' silindi.{note}")
            else:
                ui.error(f"'{clean_id}' kimlikli oturum bulunamadı veya silinemedi.")
            return

        print(c("\n  📋 SİLİNECEK OTURUMU SEÇİN", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 65, Fore.CYAN))
        print(c("  İpucu: Varsayılan olarak sadece sohbeti siler (kodları korur).", Fore.WHITE))
        print(c("         Dosyaları da silmek için sonuna '--files' veya '-f' ekleyin (Örn: 1 -f, 1-5 --files).", Fore.YELLOW))
        print("  " + "-" * 65)
        for i, s in enumerate(sessions, 1):
            print(f"  {i:<3} {c(s['session_id'], Fore.YELLOW):<14} {c(s['title'][:25], Fore.WHITE, Style.BRIGHT):<26} {s['mtime_str']}")
            print(f"      ↳ {s['folder_name']}")
        print()

        choice = input(c("  Silmek istediğiniz numara [Örn: 1, 1-5 (sohbet), 1 -f (dosyaları da sil) / 0=iptal]: ", Fore.YELLOW)).strip()
        indices, delete_files = self._parse_selection_indices(choice, len(sessions))
        if indices:
            deleted_names = []
            for idx in indices:
                t = sessions[idx]
                if session_manager.delete_session(t["path"], delete_files=delete_files):
                    note = " (Dosyalar dahil)" if delete_files else " (Sohbet temizlendi, kodlar korundu)"
                    deleted_names.append(f"#{idx+1} {t['title']} ({t['session_id']}){note}")
            if deleted_names:
                ui.success(f"{len(deleted_names)} adet oturum işlendi:\n  ↳ " + "\n  ↳ ".join(deleted_names))
            else:
                ui.error("Oturumlar silinemedi.")
        else:
            ui.system("İptal edildi.")

    def _clear_screen_and_chat(self, _):
        """Ekranı temizler ve mevcut NOVA sohbet geçmişini sıfırlar."""
        os.system("cls" if os.name == "nt" else "clear")
        self.coord.reset()
        ChatUI.header()
        ChatUI.success("Ekran ve aktif sohbet geçmişi temizlendi! Sıfırdan başlayabilirsiniz.")

    def _provider(self, _):
        ui = ChatUI
        c  = ui._c
        providers = list_providers()

        print(c("\n  LLM SAGLAYICI", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 46, Fore.CYAN))
        for i, p in enumerate(providers, 1):
            mark  = c(" << AKTIF", Fore.GREEN, Style.BRIGHT) if p["is_active"] else ""
            knote = c(" [key gerekli]", Fore.YELLOW) if p["requires_key"] else c(" [lokal]", Fore.GREEN)
            print(f"  {i}. {c(p['label'], Fore.WHITE, Style.BRIGHT)}{mark}{knote}")
            if p["requires_key"] and p.get("key_url"):
                print(f"     {p['key_url']}")
        print()

        secim = input(c(f"  [1-{len(providers)}, Enter=iptal]: ", Fore.YELLOW)).strip()
        if not secim.isdigit() or not (1 <= int(secim) <= len(providers)):
            return

        chosen = providers[int(secim) - 1]
        name   = chosen["name"]

        if chosen["requires_key"]:
            from config import load_providers as _lp
            prov_data = _lp().get("providers", {}).get(name, {})
            key_env = prov_data.get("api_key_env")
            cfg_key = prov_data.get("api_key", "").strip()

            active_key = os.getenv(key_env, "") if key_env else ""
            if not active_key and cfg_key:
                active_key = cfg_key
                if key_env:
                    os.environ[key_env] = cfg_key

            if active_key:
                masked = active_key[:8] + "..." + active_key[-4:] if len(active_key) > 12 else "***"
                print(c(f"  ✓ Kayıtlı API anahtarı: {masked}", Fore.GREEN))
                change = input(c("  Anahtarı değiştirmek ister misiniz? [e/H]: ", Fore.YELLOW)).strip().lower()
                if change in ("e", "evet", "y", "yes"):
                    active_key = ""

            if not active_key:
                print(c(f"\n  [!] {chosen['label']} için API anahtarı gerekiyor.", Fore.YELLOW))
                user_key = input(c(f"  API Anahtarını girin ({key_env or 'API_KEY'}) / Enter=iptal: ", Fore.CYAN)).strip()
                if user_key:
                    if key_env:
                        os.environ[key_env] = user_key
                        env_file = Path(__file__).parent / ".env"
                        try:
                            lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
                            lines = [l for l in lines if not l.startswith(f"{key_env}=")]
                            lines.append(f"{key_env}={user_key}")
                            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                        except Exception:
                            pass
                else:
                    ui.error("API key girilmedi. Sağlayıcı değiştirilemedi.")
                    return

        # Sağlayıcıyı aktif et
        set_active_provider(name)

        # ── Kayıtlı Modelleri Listele veya Yeni Model Ekle ──
        saved_models = list_provider_models(name)
        active_model = settings.default_model

        print(c(f"\n  📋 {chosen['label']} - KAYITLI MODELLER", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "-" * 50, Fore.CYAN))
        for m_i, m_name in enumerate(saved_models, 1):
            is_cur = (m_name in active_model or active_model.endswith(m_name))
            m_mark = c(" << AKTİF", Fore.GREEN, Style.BRIGHT) if is_cur else ""
            print(f"  {m_i}. {c(m_name, Fore.WHITE)}{m_mark}")
        add_option_idx = len(saved_models) + 1
        print(f"  {add_option_idx}. {c('[+ Yeni Model Ekle / Özel İsim Gir]', Fore.YELLOW, Style.BRIGHT)}")
        print()

        m_secim = input(c(f"  Model seçin [1-{add_option_idx}, Enter=varsayılan]: ", Fore.CYAN)).strip()
        selected_model = None

        if m_secim.isdigit():
            m_val = int(m_secim)
            if 1 <= m_val <= len(saved_models):
                selected_model = saved_models[m_val - 1]
            elif m_val == add_option_idx:
                new_m_name = input(c("  Yeni Model Adı (Örn: nvidia/nemotron-3.5-lightning-30b-a3b): ", Fore.YELLOW)).strip()
                if new_m_name:
                    add_provider_model(name, new_m_name)
                    selected_model = new_m_name
                    ui.success(f"Yeni model '{new_m_name}' kayıtlı modeller listesine eklendi!")

        if selected_model:
            set_provider_active_model(name, selected_model)

        reload_config()
        self.coord.refresh_settings()
        ui.success(f"Sağlayıcı: {chosen['label']} | Aktif Model: {settings.default_model}")

    def _quota_cmd(self, _):
        """Tüm sağlayıcıların kota, bakiye ve oturum kullanım durumunu raporlar."""
        from engines.quota_engine import quota_engine
        from config import LLM_PARAMS
        from settings import settings
        ui = ChatUI
        c = ui._c
        prov_id = LLM_PARAMS.get("provider", "ollama")
        stats = quota_engine.get_session_stats()

        print(c("\n  📊 LLM KOTA, BAKİYE VE KULLANIM DURUMU", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 55, Fore.CYAN))
        print(f"  Aktif Sağlayıcı : {c(prov_id.upper(), Fore.YELLOW, Style.BRIGHT)} ({settings.coordinator_model})")
        print(f"  Oturum Tokenı   : {c(str(stats['tokens']), Fore.GREEN, Style.BRIGHT)} token")
        print(f"  Toplam İstek    : {c(str(stats['requests']), Fore.GREEN, Style.BRIGHT)} LLM çağrısı")
        print("  " + "-" * 55)

        # OpenRouter
        or_key = os.getenv("OPENROUTER_API_KEY", "")
        or_info = quota_engine.fetch_openrouter_quota(or_key) if or_key else {"display": "Anahtar Tanımlı Değil"}
        print(f"  💳 OpenRouter   : {or_info.get('display', 'Bilinmiyor')}")

        # Kimi
        kimi_key = os.getenv("MOONSHOT_API_KEY", "")
        kimi_info = quota_engine.fetch_kimi_quota(kimi_key) if kimi_key else {"display": "Anahtar Tanımlı Değil"}
        print(f"  🌙 Kimi (Moon)  : {kimi_info.get('display', 'Bilinmiyor')}")

        # NVIDIA
        nv_key = LLM_PARAMS.get("api_key", "") if prov_id == "nvidia" else os.getenv("NVIDIA_API_KEY", "")
        nv_info = quota_engine.fetch_nvidia_quota(nv_key) if nv_key else {"display": "Anahtar Tanımlı Değil"}
        print(f"  🟢 NVIDIA NIM   : {nv_info.get('display', 'Bilinmiyor')}")

        # Lokal
        print(f"  🔋 Lokal Motor  : {c('Ollama / llama.cpp (Sınırsız / Çevrimdışı)', Fore.GREEN)}")
        print(c("  " + "=" * 55 + "\n", Fore.CYAN))

    def _dir_open(self, _):
        """Proje klasörünü aç veya yolunu göster."""
        abs_path = os.path.abspath(get_output_dir())
        ChatUI.info(f"Proje Klasoru: {abs_path}")
        try:
            if os.name == "nt":
                os.startfile(abs_path)
                ChatUI.success("Proje klasoru Windows Explorer'da acildi.")
            else:
                ChatUI.info("Linux/Mac icin: cd " + abs_path)
        except Exception as e:
            ChatUI.error(f"Klasor acilamadi: {e}")

    def _resume(self, _):
        """Mevcut oturumlardan birini secip sohbet + kod durumunu geri yukle."""
        sessions = session_manager.list_all_sessions()
        if not sessions:
            ChatUI.system("Devam ettirilecek oturum / proje bulunamadi.")
            return

        ui = ChatUI
        c  = ui._c
        curr_id = session_manager.current_session.session_id

        print(c("\n  📋 OTURUM / PROJE GECMISI", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 70, Fore.CYAN))
        print(f"  {'Sira':<4} {'Session ID':<15} {'Baslik / Klasor':<28} {'Mesaj':<7} {'Dosya':<7} Tarih")
        print("  " + "-" * 70)

        for i, s in enumerate(sessions, 1):
            is_active = (s["session_id"] == curr_id or s["path"] == str(session_manager.current_session.project_dir))
            marker    = c(" << AKTIF", Fore.GREEN, Style.BRIGHT) if is_active else ""
            title_s   = s['title'][:26]
            print(f"  {i:<4} {c(s['session_id'], Fore.YELLOW):<15} {c(title_s, Fore.WHITE, Style.BRIGHT):<28} "
                  f"{s['msg_count']:<7} {s['file_count']:<7} {s['mtime_str']}{marker}")
            print(f"       ↳ {c(s['folder_name'], Fore.CYAN)}")
        print("  " + "-" * 70)
        print(c("  💡 İpucu: 'd1' veya 'd1-5' sadece sohbeti temizler (kodları korur).", Fore.WHITE))
        print(c("            Dosyaları da silmek için: 'd1 -f' veya 'd1-5 --files'", Fore.YELLOW))
        print()
        secim = input(c("  Yükle [1-N], Sohbeti Sil [d1, d1-5], Dosyalarla Sil [d1 -f], 0=iptal: ", Fore.YELLOW)).strip().lower()

        if not secim or secim in ("0", "iptal", "exit", "q"):
            ui.system("Iptal edildi.")
            return

        if secim in ("clear", "purge", "all"):
            self._purge_sessions("")
            return

        # Çoklu / Aralık Silme (d1, d1-5, d1 -f, d1,3,5 --files)
        if secim.startswith("d") or secim.startswith("del"):
            indices, delete_files = self._parse_selection_indices(secim, len(sessions))
            if indices:
                deleted_names = []
                for idx in indices:
                    target = sessions[idx]
                    if session_manager.delete_session(target["path"], delete_files=delete_files):
                        note = " (Dosyalar dahil)" if delete_files else " (Sohbet temizlendi, kodlar korundu)"
                        deleted_names.append(f"#{idx+1} {target['title']} ({target['session_id']}){note}")
                if deleted_names:
                    ui.success(f"{len(deleted_names)} adet oturum işlendi:\n  ↳ " + "\n  ↳ ".join(deleted_names))
                else:
                    ui.error("Oturumlar silinemedi.")
            else:
                ui.error("Geçersiz silme parametresi. Örnek: d1, d1-5, d1 -f, d1,3,5 --files")
            return

        if secim.isdigit():
            idx = int(secim) - 1
            if not (0 <= idx < len(sessions)):
                ui.error("Gecersiz secim.")
                return

            chosen = sessions[idx]
            sess = session_manager.resume_session(chosen)

            self.coord.history = list(sess.conversation_history)
            self.coord._rebuild_system_prompt()
            files = list_output_files()

            from main import load_checkpoint
            chk = load_checkpoint(str(sess.project_dir))

            ui.success(f"Oturum '{sess.title}' ({sess.session_id}) yuklendi!")
            ui.info(f"📁 Klasor: {sess.project_dir}")
            ui.info(f"💬 {len(sess.conversation_history)} mesaj, 📄 {len(files)} dosya yuklendi.")

            if chk and chk.get("completed_roles"):
                done = ", ".join(chk["completed_roles"])
                ui.system(f"⚡ Yarıda kalan işlem tespit edildi (Tamamlanan: {done}).")
                ui.system("  ► Doğrudan kaldığı adımdan devam etmek için: /continue")
                ui.system("  ► Yeni istek, ekleme veya değişiklik yapmak için: NOVA'ya doğrudan yazın!")
            else:
                ui.system(
                    "💡 NOVA ile projeye devam edebilirsiniz:\n"
                    "  - 'Şunları da ekle', 'Şu modülü değiştir' gibi yeni isteklerinizi yazabilirsiniz.\n"
                    "  - Pipeline'ı sıfırdan başlatmak için: /run"
                )
        else:
            ui.error("Gecersiz secim.")

    def _purge_sessions(self, _):
        """Tüm oturumları ve projeleri disken temizle."""
        ui = ChatUI
        confirm = input(ui._c(
            "\n  [UYARI] TUM eski oturumlar ve projeler silinecek! Emin misiniz? [evet / h]: ",
            Fore.RED, Style.BRIGHT
        )).strip().lower()

        if confirm in ("e", "evet", "y", "yes"):
            count = session_manager.clear_all_sessions()
            self.coord.reset()
            ui.success(f"Tüm geçmiş temizlendi! ({count} oturum/klasör silindi)")
            ui.system("Taze bir oturum başlatıldı.")
        else:
            ui.system("Temizleme iptal edildi.")

    def _attach_file_or_dir(self, arg: str):
        """
        Sürüklenip bırakılan veya /attach ile verilen dosya veya klasörü bağlama yükle.
        """
        raw_path = arg.strip().strip('"').strip("'")
        if not raw_path:
            raw_path = input(ChatUI._c("  Eklemek istediginiz dosya veya klasor yolunu surukleyin / yazin: ", Fore.CYAN)).strip().strip('"').strip("'")

        if not raw_path:
            ChatUI.system("Iptal edildi.")
            return

        target_path = Path(raw_path).resolve()

        if not target_path.exists():
            ChatUI.error(f"Dosya veya klasor bulunamadi: {target_path}")
            return

        ui = ChatUI
        if target_path.is_dir():
            # Var olan klasör / proje eklendi -> Aktif proje klasörü yap
            sess = session_manager.current_session
            sess.set_custom_project_dir(target_path)
            sess.set_title(target_path.name)
            files = list_output_files()

            summary = f"Attached Project Folder: '{target_path.name}' ({len(files)} files):\n" + "\n".join(f"- {f}" for f in files[:25])
            self.coord.history.append({
                "role": "system",
                "content": f"The user attached an external project folder ({target_path}). "
                           f"Project files: {summary}. "
                           f"Assist with new requests and modifications on this existing project. "
                           f"Communicate with the user in fluent Turkish."
            })
            session_manager.current_session.save(self.coord.history)

            ui.success(f"Klasor baglandi: '{target_path.name}' ({len(files)} dosya)")
            ui.info(f"📁 Yolu: {target_path}")
            ui.system("NOVA var olan bu proje üzerinde calismaya hazir.")

        elif target_path.is_file():
            # Dosya eklendi -> İçeriğini okuyup hafızaya ekle
            try:
                content = target_path.read_text(encoding="utf-8", errors="ignore")
                file_size_kb = target_path.stat().st_size / 1024
                line_count = len(content.splitlines())

                snippet = content if len(content) < 8000 else content[:8000] + "\n... (truncated)"

                self.coord.history.append({
                    "role": "user",
                    "content": f"[ATTACHED DOSYA: {target_path.name}]\nDosya Yolu: {target_path}\nBoyut: {file_size_kb:.1f} KB, {line_count} satır\n\n```\n{snippet}\n```"
                })
                session_manager.current_session.save(self.coord.history)

                ui.success(f"Dosya ekleme basarili: '{target_path.name}' ({file_size_kb:.1f} KB, {line_count} satir)")
                ui.info(f"📄 Yolu: {target_path}")
                ui.system("Dosya NOVA'nin hafizasina yuklendi! Bu dosya hakkinda soru sorabilir veya komut verebilirsiniz.")

            except Exception as err:
                ui.error(f"Dosya okunamadi: {err}")

    def _new_session(self, arg: str):
        """Yeni bir oturum ve proje başlat."""
        title = arg.strip() if arg else "Yeni Oturum"
        slug  = arg.strip() if arg else "yeni_proje"

        sess = session_manager.create_new_session(title=title, slug=slug)
        self.coord.reset()

        ChatUI.success(f"Yeni oturum baslatildi! (Session ID: {sess.session_id})")
        ChatUI.info(f"📁 Klasor: {sess.project_dir}")
        ChatUI.system("NOVA ile yeni proje planlamasina baslayabilirsiniz.")

    def _reset(self, arg: str):
        self._new_session(arg)

    def _status(self, _):
        from config import get_output_dir
        files = list_output_files()
        active_dir = os.path.basename(get_output_dir())
        if not files:
            ChatUI.system(f"Aktif proje klasoru ({active_dir}) bos.")
        else:
            print(ChatUI._c(f"\n  Aktif Proje ({active_dir}) - {len(files)} dosya:", Fore.CYAN))
            for f in files:
                print(f"    >> {f}")

    def _config_show(self, _):
        print_config()

    def _logs(self, arg: str):
        """
        /logs                     → Son çalışmalar + son runın adımları + hatalar
        /logs errors              → Tüm hata olayları
        /logs summary             → Hata tiplerine göre özet tablo
        /logs type <tip>          → Tipe göre filtrele (syntax/import/runtime/assertion/pytest/llm/micro_fix/escalation/qa_failed)
        /logs status <durum>      → Duruma göre filtrele (success/failed/partial/running)
        /logs steps <run_id>      → Belirli run için agent adımları
        /logs run <run_id>        → Belirli run için adım + hata tablosu
        /logs step <step_id>      → Belirli adımın TAM çıktısını göster (planner dahil)
        """
        from log_store import log_store
        from config import get_output_dir

        c   = ChatUI._c
        ui  = ChatUI
        arg = arg.strip()
        arg_lower = arg.lower()

        project_dir = get_output_dir()

        print(c("\n  ═══ PIPELINE LOG VIEWER ═══", Fore.CYAN, Style.BRIGHT))
        print(c(f"  Proje: {project_dir}", Fore.WHITE))
        print()

        if arg_lower == "summary":
            # Hata tipi özeti
            print(c("  ── HATA TİPİ ÖZETİ ─────────────────────────────────", Fore.YELLOW, Style.BRIGHT))
            print(log_store.get_error_type_summary(project_dir=project_dir))
            types = log_store.get_all_error_types(project_dir=project_dir)
            if types:
                print(c(f"\n  Mevcut tipler: {', '.join(types)}", Fore.WHITE))
                print(c("  Kullanim: /logs type <tip>  (orn: /logs type syntax)", Fore.WHITE))

        elif arg_lower == "errors":
            # Tüm hatalar
            print(c("  ── HATA OLAYLARI (son 30) ───────────────────────────", Fore.RED, Style.BRIGHT))
            print(log_store.get_error_table(project_dir=project_dir, limit=30))

        elif arg_lower.startswith("type "):
            # Tipe göre filtrele
            etype = arg[5:].strip().lower()
            _KNOWN = {"syntax","import","runtime","assertion","pytest","llm",
                      "micro_fix","escalation","qa_failed"}
            if not etype:
                ui.error("Kullanim: /logs type <tip>")
                print(c(f"  Bilinen tipler: {', '.join(sorted(_KNOWN))}", Fore.WHITE))
            else:
                print(c(f"  ── HATA TİPİ: {etype.upper()} ────────────────────────", Fore.RED, Style.BRIGHT))
                print(log_store.filter_by_type(etype, project_dir=project_dir))

        elif arg_lower.startswith("status "):
            # Duruma göre filtrele
            status = arg[7:].strip().lower()
            _KNOWN_STATUS = {"success", "failed", "partial", "running"}
            if not status:
                ui.error("Kullanim: /logs status <durum>")
                print(c(f"  Bilinen durumlar: {', '.join(sorted(_KNOWN_STATUS))}", Fore.WHITE))
            else:
                print(c(f"  ── DURUM: {status.upper()} ─────────────────────────────", Fore.CYAN, Style.BRIGHT))
                print(log_store.filter_runs_by_status(status, project_dir=project_dir))

        elif arg_lower.startswith("steps "):
            # Belirli run için adımlar
            run_id = arg[6:].strip()
            if not run_id:
                ui.error("Kullanim: /logs steps <run_id>")
                return
            print(c(f"  ── AGENT ADIMLARI: {run_id} ──────────────────────────", Fore.CYAN, Style.BRIGHT))
            print(log_store.get_step_table(run_id=run_id, project_dir=project_dir))

        elif arg_lower.startswith("run "):
            # Belirli run için adım + hata
            run_id = arg[4:].strip()
            if not run_id:
                ui.error("Kullanim: /logs run <run_id>")
                return
            print(c(f"  ── AGENT ADIMLARI: {run_id} ──────────────────────────", Fore.CYAN, Style.BRIGHT))
            print(log_store.get_step_table(run_id=run_id, project_dir=project_dir))
            print()
            print(c(f"  ── HATA OLAYLARI: {run_id} ───────────────────────────", Fore.RED, Style.BRIGHT))
            print(log_store.get_error_table(run_id=run_id, project_dir=project_dir))

        elif arg_lower == "prompts" or arg_lower == "prompt":
            # Tüm adımların prompt boyutları ve özetleri
            rows = log_store._fetch(
                project_dir,
                """SELECT step_number, agent_name, agent_role, model, prompt_chars, started_at
                   FROM agent_steps ORDER BY started_at ASC"""
            )
            print(c("  ── AGENT PROMPTLARI (ÖZET) ──────────────────────────", Fore.CYAN, Style.BRIGHT))
            headers = ["#", "Ajan", "Rol", "Model", "Prompt Boyutu", "Başlangıç"]
            data = [(r["step_number"], r["agent_name"], r["agent_role"], r["model"].split("/")[-1], f"{r['prompt_chars']} ch", r["started_at"][11:19]) for r in rows]
            print(log_store._fmt_table(headers, data) if rows else "  (Henüz adım kaydı yok)")

        elif arg_lower == "outputs" or arg_lower == "output":
            # Tüm adımların çıktıları
            rows = log_store._fetch(
                project_dir,
                """SELECT step_number, agent_name, agent_role, response_chars, files_written, elapsed_sec
                   FROM agent_steps ORDER BY started_at ASC"""
            )
            print(c("  ── AGENT ÇIKTILARI (ÖZET) ───────────────────────────", Fore.GREEN, Style.BRIGHT))
            headers = ["#", "Ajan", "Rol", "Yanıt Boyutu", "Süre", "Dosyalar"]
            data = []
            for r in rows:
                f_list = json.loads(r["files_written"] or "[]")
                f_str = ", ".join(f_list) if f_list else "Rapor/Belge"
                data.append((r["step_number"], r["agent_name"], r["agent_role"], f"{r['response_chars']} ch", f"{r['elapsed_sec']}s", f_str[:30]))
            print(log_store._fmt_table(headers, data) if rows else "  (Henüz adım kaydı yok)")

        elif arg_lower.startswith("step ") or arg_lower.startswith("adim ") or arg_lower.startswith("adım "):
            # Belirli adımın TAM çıktısı (step_id veya adım numarası #1, #2, #3)
            s_val = arg.split(maxsplit=1)[1].strip().lstrip("#")
            if s_val.isdigit():
                # Adım numarasına göre ara
                rows = log_store._fetch(
                    project_dir,
                    "SELECT step_id, agent_name, agent_role, model, prompt_chars, response_chars, elapsed_sec, full_output FROM agent_steps WHERE step_number=? ORDER BY started_at DESC LIMIT 1",
                    (int(s_val),)
                )
            else:
                rows = log_store._fetch(
                    project_dir,
                    "SELECT step_id, agent_name, agent_role, model, prompt_chars, response_chars, elapsed_sec, full_output FROM agent_steps WHERE step_id=?",
                    (s_val,)
                )
            if not rows:
                ui.error(f"Adım '{s_val}' bulunamadı.")
                return
            r = rows[0]
            print(c(f"  ── ADIM #{s_val} TAM ÇIKTISI ({r['agent_name']} - {r['agent_role']}) ────", Fore.CYAN, Style.BRIGHT))
            print(c(f"  Model: {r['model']} | Prompt: {r['prompt_chars']} ch | Yanıt: {r['response_chars']} ch | Süre: {r['elapsed_sec']}s\n", Fore.WHITE))
            full = r["full_output"] or "(Boş çıktı)"
            print(full[:8000])
            if len(full) > 8000:
                print(c(f"\n  ... ({len(full)-8000} karakter daha var)", Fore.YELLOW))

        elif arg_lower == "export" or arg_lower == "json":
            # Tüm pipeline'ı tek JSON dosyasına aktar
            json_file = log_store.export_full_logs_json(project_dir=project_dir)
            ui.success(f"Tüm süreç ve LLM geçmişi JSON olarak kaydedildi:\n    ↳ {json_file}")

        elif arg_lower == "md" or arg_lower == "report" or arg_lower == "audit":
            # Tüm pipeline'ı okunabilir AUDIT_LOG.md dosyasına aktar
            md_file = log_store.export_full_logs_md(project_dir=project_dir)
            ui.success(f"Tüm promptlar, çıktılar ve kararlar Markdown raporu olarak kaydedildi:\n    ↳ {md_file}")

        else:
            # Varsayılan: çalışmalar + son run özeti
            print(c("  ── PIPELINE ÇALIŞMALARI (son 10) ────────────────────", Fore.CYAN, Style.BRIGHT))
            print(log_store.get_run_table(project_dir=project_dir, limit=10))

            latest = log_store.get_latest_run_id(project_dir=project_dir)
            if latest:
                print()
                print(c(f"  ── SON ÇALIŞMA AGENT ADIMLARI ({latest}) ─────────────", Fore.CYAN, Style.BRIGHT))
                print(log_store.get_step_table(run_id=latest, project_dir=project_dir))
                print()
                print(c(f"  ── HATA OLAYLARI ({latest}) ──────────────────────────", Fore.RED, Style.BRIGHT))
                print(log_store.get_error_table(run_id=latest, project_dir=project_dir))

            print()
            print(c("  ─────────────────────────────────────────────────────", Fore.WHITE))
            print(c("  Filtreler & İnceleme Komutları:", Fore.YELLOW, Style.BRIGHT))
            print(c("   /logs                      → Genel çalışma özeti", Fore.WHITE))
            print(c("   /logs step 1               → 1. Adımın (PM) tam raporunu göster", Fore.WHITE))
            print(c("   /logs step 2               → 2. Adımın (Mimar) mimari planını göster", Fore.WHITE))
            print(c("   /logs step 3               → 3. Adımın (Developer) kodlarını göster", Fore.WHITE))
            print(c("   /logs prompts              → Tüm ajanların prompt boyutlarını listele", Fore.WHITE))
            print(c("   /logs outputs              → Tüm ajanların çıktı ve dosyalarını listele", Fore.WHITE))
            print(c("   /logs md                   → Tek tıkla paylaşılabilir AUDIT_LOG.md raporu oluştur", Fore.WHITE))
            print(c("   /logs json                 → Tüm geçmişi full_pipeline_audit.json olarak dışa aktar", Fore.WHITE))
            print(c("   /logs summary              → Hata tipleri özeti", Fore.WHITE))
            print(c("   /logs errors               → Son hata kayıtları", Fore.WHITE))
        print()


# ═══════════════════════════════════════════════════════════════════════════
# ChatSession — Ana REPL
# ═══════════════════════════════════════════════════════════════════════════

class ChatSession:
    """
    Ana sohbet oturumu.
    - InputHandler : gelismis input (tab, gecmis)
    - CoordinatorAgent: streaming LLM
    - CommandHub   : komut isleyici
    """

    def __init__(self):
        self.ui    = ChatUI
        self.coord = CoordinatorAgent()
        self.input = InputHandler()
        self.cmds  = CommandHub(self.coord, session=self)
        # Model önerileri, kullanıcı /apply demeden çalışma dizinine yazılmaz.
        self._pending_edits: list[dict[str, Any]] = []

    # ── Baslangic ─────────────────────────────────────────

    def _startup(self) -> None:
        # Harici dizinden (myf / mayf komutu ile) başlatıldıysa o dizini bağla
        if len(sys.argv) > 1 and sys.argv[1] == "--workdir" and len(sys.argv) > 2:
            work_dir = Path(sys.argv[2]).resolve()
            if work_dir.exists():
                session_manager.current_session.set_custom_project_dir(work_dir)
                session_manager.current_session.set_title(work_dir.name)

        self.ui.header()

        # Provider secimi
        providers   = list_providers()
        active_name = get_active_provider_name()
        label = next((p["label"] for p in providers if p["is_active"]), active_name)
        print(ChatUI._c(f"\n  Aktif saglayici: {label}", Fore.CYAN))
        ans = input(ChatUI._c(
            "  Degistirmek ister misiniz? [e / Enter]: ", Fore.WHITE
        )).strip().lower()
        if ans in ("e", "evet", "y", "yes"):
            self.cmds._provider("")

        # Execution Mode secimi
        mode_labels = {
            "sequential": "Sıralı Otonom Pipeline (PM -> Mimar -> DEV -> QA -> Doc)",
            "subagent":   "Dinamik Subagent Orkestrasyonu (Özel Ajan Havuzu)",
            "interactive":"Doğrudan İnteraktif Sohbet (NOVA ile Canlı Kodlama)",
        }
        curr_mode = settings.execution_mode
        num_tag = "1" if curr_mode == "sequential" else ("2" if curr_mode == "subagent" else "3")
        print(ChatUI._c(f"\n  Çalışma Modu: [{num_tag}] {curr_mode.upper()} ({mode_labels.get(curr_mode, '')})", Fore.CYAN))
        mode_ans = input(ChatUI._c(
            "  Değiştirmek ister misiniz? [1: Sıralı / 2: Subagent / 3: Chat / Enter]: ", Fore.WHITE
        )).strip().lower()
        if mode_ans in ("1", "sequential", "sirali"):
            settings.execution_mode = "sequential"
            ChatUI.success(f"Seçilen Mod: [1] {mode_labels['sequential']}")
        elif mode_ans in ("2", "subagent", "swarm"):
            settings.execution_mode = "subagent"
            ChatUI.success(f"Seçilen Mod: [2] {mode_labels['subagent']}")
        elif mode_ans in ("3", "interactive", "chat"):
            settings.execution_mode = "interactive"
            ChatUI.success(f"Seçilen Mod: [3] {mode_labels['interactive']}")

        print_config()
        self.ui.think_status(settings.think_mode)

        # Model warmup (ayarlardan kontrol edilir)
        if settings.warmup:
            self.ui.system("Model VRAM'a yukleniyor (warmup)...")
            ok = CoordinatorAgent.warmup()
            if ok:
                self.ui.success("Model hazir! GPU aktif.")
            else:
                self.ui.error("Warmup basarisiz. Ollama calistigindan emin olun.")
        else:
            self.ui.system("Warmup kapali. Ilk istek biraz gec gelebilir. (/settings ile acilabilir)")

        print(ChatUI._c(
            "\n  Hazır. Terminale doğrudan isteğinizi yazın.\n"
            "  Mod 3 (Chat): Her satır serbest sohbet isteğidir; pipeline yalnızca /run ile başlar.\n"
            "  /help ile komutları görün; Tab ile komut tamamlama aktiftir.\n",
            Fore.WHITE, Style.BRIGHT
        ))

    # ── Ana dongu ─────────────────────────────────────────

    def run(self) -> None:
        self._startup()
        empty_ctrl_c = False  # Ust uste Ctrl+C => cikis

        while True:
            try:
                user_input = self.input.read(
                    name=settings.coordinator_name,
                    think_on=settings.think_mode,
                )
            except KeyboardInterrupt:
                if empty_ctrl_c:
                    session_manager.cleanup_on_exit()
                    self.ui.system("Cikiliyor (Ctrl+C x2)...")
                    break
                self.ui.system("Ctrl+C — Cikis icin tekrar basin.")
                empty_ctrl_c = True
                continue
            except EOFError:
                session_manager.cleanup_on_exit()
                self.ui.system("Cikiliyor...")
                break

            if user_input is None:
                break

            empty_ctrl_c = False

            if not user_input:
                continue

            # Komut mu?
            if self.cmds.is_command(user_input):
                self.cmds.dispatch(user_input)
                continue
                
            # Bir yolun sohbet metninde geçmesi dosya okunması veya proje
            # değiştirilmesi için yeterli değildir. Bunun yerine /attach kullanılır.
            import re
            path_pattern = r'((?:[A-Za-z]:[\\/]|/(?:home|Users|var/www|srv|mnt|media|tmp|opt|Desktop|Projects)/|[.]{1,2}[\\/])[^\s"\'<>\|]+)'
            matches = []  # Bağlama yalnızca /attach veya tek başına sürükle-bırak ile eklenir.
            for match in matches:
                p = Path(match).resolve()
                if str(p) in ("/", "\\") or str(p) in ("/bin", "/etc", "/usr", "/var", "/dev", "/proc", "/sys", "/root"):
                    continue
                if p.is_file():
                    try:
                        content = p.read_text(encoding="utf-8", errors="ignore")
                        sample_content = content[:1200]
                        if len(content) > 1200:
                            sample_content += "\n... (örnek veri devam ediyor) ..."
                        user_input += f"\n\n--- [REFERANS DOSYA: {p.name}] ---\n```\n{sample_content}\n```\n"
                        self.ui.system(f"Otomatik dosya icerigi eklendi (Örnek Şema): {p.name}")
                    except Exception:
                        pass
                elif p.is_dir() and p != session_manager.current_session.project_dir:
                    try:
                        sess = session_manager.current_session
                        sess.set_custom_project_dir(p)
                        sess.set_title(p.name)
                        
                        # Dizin icini tara
                        from brain import list_output_files
                        files = list_output_files()
                        summary = f"Klasor: {p.name} ({len(files)} dosya)\n" + "\n".join(f"- {f}" for f in files[:25])
                        user_input += f"\n\n--- [SISTEM TARAFINDAN OTOMATIK BAGLANAN PROJE KLASORU: {p.name}] ---\n{summary}\n"
                        self.ui.system(f"Otomatik proje klasoru baglandi: {p.name}")
                    except Exception:
                        pass

            # Otomatik Agent-Reach Web Araştırma Algılama (Genişletilmiş Doğal Dil & Akıllı Temizleme)
            s_query = None
            for line in user_input.strip().splitlines():
                line_clean = line.strip()
                prefix_m = re.match(r'^(?:[-*#>\s]*)(?:ara|webde ara|araştır|arastir|search|githubda ara|reach):\s*(.+)$', line_clean, re.IGNORECASE)
                if prefix_m:
                    s_query = prefix_m.group(1).strip()
                    break

            # Açık prefix yoksa doğal dil araştırma / paket sürümü / web arama niyetini tespit et
            if not s_query:
                tr_research_intent = re.search(
                    r'(?:internetten|internette|webden|webde|web\'den|web\'de|google\'dan|google\'da|online)\b.*?\b(?:ara|araştır\w*|arastir\w*|bak\w*|bul\w*|öğren\w*|ogren\w*|sürüm\w*|surum\w*|haber\w*|bilgi\w*|doküman\w*|dokuman\w*)|'
                    r'\b(?:en güncel|güncel sürüm\w*|güncel versiyon\w*|son sürüm\w*|latest version|paket sürüm\w*|kütüphane sürüm\w*)\b|'
                    r'\b(?:araştır\w*|arastir\w*|arama yap|araştırması yap)\b',
                    user_input,
                    re.IGNORECASE
                )
                if tr_research_intent:
                    first_line = user_input.strip().splitlines()[0].strip()
                    q_cand = re.sub(r'^[#*->\s]+', '', first_line)
                    # Konuşma dolgu ve rica ifadelerini temizle
                    filler_patterns = [
                        r'\b(?:internetten|internette|webden|webde|web\'den|web\'de|google\'dan|google\'da|online)\b',
                        r'\b(?:araştır\w*|arastir\w*|ara\w*)\s*(?:m[ıiüu]?[sş][ıiüu]+n\w*)?\b',
                        r'\b(?:bak\w*)\s*(?:m[ıiüu]?[sş][ıiüu]+n\w*)?\b',
                        r'\b(?:bul\w*)\s*(?:m[ıiüu]?[sş][ıiüu]+n\w*)?\b',
                        r'\b(?:öğren\w*|ogren\w*|söyle\w*)\s*(?:m[ıiüu]?[sş][ıiüu]+n\w*)?\b',
                        r'\b(?:lütfen|lutfen|rica\s*etsem|rica\s*ederim|acaba)\b',
                        r'\b(?:hakkında\s*bilgi\s*ver\w*|bilgi\s*ver\w*)\b',
                        r'\b(?:nedir|nelerdir)\b',
                    ]
                    for pat in filler_patterns:
                        q_cand = re.sub(pat, ' ', q_cand, flags=re.IGNORECASE)
                    q_cand = re.sub(r'[\?\.!,;:]+', ' ', q_cand)
                    q_cand = re.sub(r'\s+', ' ', q_cand).strip()
                    s_query = q_cand if len(q_cand) >= 2 else first_line[:80]

            if s_query:
                from datetime import datetime
                from colorama import Fore, Style
                now = datetime.now()
                tr_months = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
                cur_month_tr = tr_months[now.month - 1]
                month_year_tr = f"{cur_month_tr} {now.year}"
                
                clean_q = re.sub(r'[\r\n]+', ' ', s_query).strip()[:80]
                has_year_or_month = any(k in clean_q.lower() for k in [
                    str(now.year), "202", "203", "ocak", "şubat", "mart", "nisan",
                    "mayıs", "haziran", "temmuz", "ağustos", "eylül", "ekim", "kasım", "aralık",
                    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"
                ])
                if not has_year_or_month:
                    search_q = f"{clean_q} {month_year_tr}"
                else:
                    search_q = clean_q

                self.ui.system(f"🌐 Agent-Reach ile canlı web araştırması yapılıyor: '{search_q}'...")
                s_results = reach_engine.search_web(search_q, max_results=5)

                # Kullanıcıya terminalde canlı web bulgularını göster
                print()
                print(f"  {Fore.CYAN}{Style.BRIGHT}┌── 🌐 [CANLI WEB ARAŞTIRMA BULGULARI ({month_year_tr})] ──────────────────{Style.RESET_ALL}")
                for r_line in s_results.splitlines():
                    if r_line.strip():
                        print(f"  {Fore.CYAN}│ {Fore.WHITE}{r_line}{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}{Style.BRIGHT}└── 🎯 [Bulgular Koordinatöre ({settings.coordinator_name}) Aktarıldı] ─────────────────────────{Style.RESET_ALL}\n")

                user_input += f"\n\n--- [AGENT-REACH CANLI WEB ARAŞTIRMA SONUÇLARI (Tarih: {month_year_tr}): {search_q}] ---\n{s_results}\n----------------------------------------------------------\nYukarıdaki güncel web araştırma sonuçlarını kullanarak soruma ayrıntılı ve doğru yanıt ver."

            # Koordinatora gonder — streaming
            self.ui.stream_begin(settings.coordinator_name)

            full_text  = ""
            start_pipe = False

            try:
                full_text, start_pipe = self.coord.chat(
                    user_input,
                    on_token=self.ui.stream_token,
                    allow_pipeline=settings.execution_mode != "interactive",
                )
            except KeyboardInterrupt:
                self.ui.stream_end()
                self.ui.system("Uretim durduruldu. Devam edebilirsiniz.")
                continue
            except Exception as exc:
                self.ui.stream_end()
                self.ui.error(f"LLM hatasi: {exc}")
                self.ui.system("'ollama serve' calistigindan emin olun.")
                continue

            self.ui.stream_end()

            # Koordinatör yanıtında canlı arama çağrısı [SEARCH: ...] varsa çalıştır ve cevabı tamamla
            search_tool_match = re.search(r'\[SEARCH:\s*([^\]]+)\]', full_text, re.IGNORECASE)
            if search_tool_match and not start_pipe:
                tool_q = search_tool_match.group(1).strip()
                from colorama import Fore, Style
                self.ui.system(f"🌐 Koordinatör canlı araç araması talep etti: '{tool_q}'...")
                tool_results = reach_engine.search_web(tool_q, max_results=5)
                print()
                print(f"  {Fore.CYAN}{Style.BRIGHT}┌── 🌐 [KOORDİNATÖR ARAÇ ÇAĞRISI (SEARCH: {tool_q})] ──────────────────{Style.RESET_ALL}")
                for r_line in tool_results.splitlines():
                    if r_line.strip():
                        print(f"  {Fore.CYAN}│ {Fore.WHITE}{r_line}{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}{Style.BRIGHT}└── 🎯 [Araç Bulguları Koordinatöre İletildi] ───────────────────────────{Style.RESET_ALL}\n")

                self.ui.stream_begin(settings.coordinator_name)
                try:
                    followup_text, followup_pipe = self.coord.chat(
                        f"--- [CANLI ARAŞTIRMA SONUÇLARI: {tool_q}] ---\n{tool_results}\nLütfen bu güncel bilgileri kullanarak soruma net yanıt ver.",
                        on_token=self.ui.stream_token,
                        allow_pipeline=settings.execution_mode != "interactive",
                    )
                    full_text += f"\n\n{followup_text}"
                    if followup_pipe:
                        start_pipe = True
                except Exception as exc:
                    self.ui.error(f"Arama sonrası LLM hatası: {exc}")
                finally:
                    self.ui.stream_end()

            # Pipeline dışındaki kod blokları otomatik yazılmaz; kullanıcı
            # /apply ile onaylayana kadar yalnızca bekleyen değişiklik olur.
            if not start_pipe and full_text:
                self._stage_code_edits(full_text)
            if start_pipe:
                self._run_pipeline()
            continue

    def _stage_code_edits(self, response: str) -> None:
        """Yanıttaki geçerli kod bloklarını güvenli önizleme kuyruğuna al."""
        from code_parser import extract_code_blocks
        from diff_engine import apply_surgical_edit, has_diff_blocks

        session = session_manager.current_session
        if not session:
            return
        project_dir = Path(session.project_dir).resolve()
        staged: list[dict[str, Any]] = []
        for block in extract_code_blocks(response):
            filename = block.get("filename")
            content = block.get("content", "")
            if not filename or not content.strip():
                continue
            target = (project_dir / filename).resolve()
            try:
                target.relative_to(project_dir)
            except ValueError:
                self.ui.error(f"Güvenlik: proje dışına çıkan dosya atlandı: {filename}")
                continue

            exists = target.exists()
            if exists and has_diff_blocks(content):
                original = target.read_text(encoding="utf-8", errors="replace")
                proposed, ok, message = apply_surgical_edit(original, content, file_path=str(target))
                if not ok:
                    self.ui.error(f"{filename}: önerilen yama hazırlanamadı ({message})")
                    continue
            else:
                proposed = content
            staged.append({"filename": filename, "path": target, "content": proposed, "exists": exists})

        if staged:
            self._pending_edits = staged
            self.ui.pending_edits(staged)

    def _show_pending_edits(self) -> None:
        if not self._pending_edits:
            self.ui.system("Bekleyen dosya değişikliği yok.")
            return
        self.ui.pending_edits(self._pending_edits)

    def _discard_pending_edits(self) -> None:
        if not self._pending_edits:
            self.ui.system("Atılacak bekleyen değişiklik yok.")
            return
        count = len(self._pending_edits)
        self._pending_edits.clear()
        self.ui.system(f"{count} bekleyen değişiklik atıldı.")

    def _apply_pending_edits(self, arg: str = "") -> None:
        """Kullanıcı onayı ve izin yöneticisi ile bekleyen değişiklikleri diske yaz."""
        if not self._pending_edits:
            self.ui.system("Uygulanacak bekleyen değişiklik yok.")
            return
        requested = arg.strip()
        edits = self._pending_edits
        if requested:
            edits = [edit for edit in edits if edit["filename"] == requested]
            if not edits:
                self.ui.error(f"Bekleyen değişiklik bulunamadı: {requested}")
                return

        names = ", ".join(edit["filename"] for edit in edits)
        confirm = input(ChatUI._c(f"  {len(edits)} dosya yazılacak ({names}). Onaylıyor musun? [e/H]: ", Fore.YELLOW)).strip().lower()
        if confirm not in ("e", "evet", "y", "yes"):
            self.ui.system("Değişiklikler uygulanmadı; kuyrukta tutuluyor.")
            return

        from permission_manager import permission_manager
        applied: list[str] = []
        for edit in edits:
            path = edit["path"]
            if not permission_manager.check_permission("write_file", str(path), agent_name=settings.coordinator_name):
                self.ui.system(f"İzin verilmedi, atlandı: {edit['filename']}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(edit["content"], encoding="utf-8")
            applied.append(edit["filename"])

        self._pending_edits = [edit for edit in self._pending_edits if edit not in edits or edit["filename"] not in applied]
        if applied:
            self.ui.success(f"Uygulandı: {', '.join(applied)}")

    # ── Pipeline ──────────────────────────────────────────

    def _run_pipeline(self) -> None:
        print()
        confirm = input(ChatUI._c(
            "  Pipeline baslatiliyor. Emin misiniz? [Enter=evet / h=hayir]: ",
            Fore.YELLOW
        )).strip().lower()

        if confirm in ("h", "hayir", "n", "no"):
            ChatUI.system("Iptal.")
            return

        # Proje kaydedilecek konumu ve klasor adini sor
        ui = ChatUI
        c  = ui._c
        curr_sess = session_manager.current_session

        # Varsayilan path olarak aktif proje klasorunu sun (auto-attach edilmisse onu gosterir)
        default_path = curr_sess.project_dir

        print(c("\n  📂 PROJE KAYIT KONUMU SECIN", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 60, Fore.CYAN))
        print("  Kodlarin uretilecegi tam klasor yolunu yazin veya klasor surukleyin.")
        print(f"  [Enter = Varsayilan: {default_path}]")
        print()

        raw_location = input(c("  Klasor Yolu / Konum: ", Fore.YELLOW)).strip().strip('"').strip("'")

        if raw_location:
            target_path = Path(raw_location).resolve()
            curr_sess.set_custom_project_dir(target_path)
            curr_sess.set_title(target_path.name)

        target_dir = str(curr_sess.project_dir)

        # Otonomi Modu (Sınırsız Döngü) Sorusu
        print(c("\n  🔄 OTONOMI SEVIYESI", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 60, Fore.CYAN))
        print("  Hata durumunda ajanlar farkli yaklasimlar deneyerek sorunu cozer.")
        print("  Kisir donguye girilirse otomatik olarak farkli strateji enjekte edilir.")
        print("  Optimize modu varsa Optimizer ajani STATUS: OPTIMIZED diyene kadar calisir.")
        print("  Hayir derseniz 3 denemeden sonra pes eder.")
        print()
        auto_choice = input(c("  Full Otonomi aktif edilsin mi? [e/H]: ", Fore.YELLOW)).strip().lower()
        
        if auto_choice in ("e", "evet", "y", "yes"):
            max_retries = -1  # run_pipeline'da akilli durdurma yonetiyor
            mode_text   = "FULL OTONOMI (Akilli Durdurma + Farkli Yaklasim)"
        else:
            max_retries = 3
            mode_text = "STANDART (Maks 3 Deneme)"

        # Guvenlik Modu Sorusu
        print(c("\n  🛡️ GUVENLIK / IZIN MODU", Fore.CYAN, Style.BRIGHT))
        print(c("  " + "=" * 60, Fore.CYAN))
        print("  Ajanlara proje klasoru icinde dosya yazma ve test calistirma")
        print("  konusunda tam yetki (Sandbox Modu) vermek ister misiniz?")
        print("  (Evet derseniz size hicbir sey sormadan arkaplanda calisirlar)")
        print()
        sandbox_choice = input(c("  Sandbox Modu (Tam Yetki) aktif edilsin mi? [e/H]: ", Fore.YELLOW)).strip().lower()
        
        from permission_manager import permission_manager
        if sandbox_choice in ("e", "evet", "y", "yes"):
            permission_manager._session_grants.add("all")
            permission_manager._save_project_policy()
            sec_text = "SANDBOX (Projeye Tam Yetki)"
        else:
            # Temizle, eger onceki oturumdan kalmissa
            if "all" in permission_manager._session_grants:
                permission_manager._session_grants.remove("all")
                permission_manager._save_project_policy()
            sec_text = "STANDART (Her Islemde Sor)"

        ChatUI.info(f"🆔 Session ID: {curr_sess.session_id}")
        ChatUI.info(f"📁 Proje Konumu: {target_dir}")
        ChatUI.info(f"⚙️ Calisma Modu: {mode_text}")
        ChatUI.info(f"🛡️ Guvenlik Modu: {sec_text}")

        brief         = self.coord.get_project_brief()

        # ── Mod 2: Dinamik Subagent Orkestrasyonu ──
        if settings.execution_mode == "subagent":
            ChatUI.success("Dinamik Subagent Orkestrasyonu başlatılıyor! (Lider + Uzman Ajan Havuzu)")
            from subagent_engine import subagent_orchestrator
            try:
                result = subagent_orchestrator.run(
                    project_brief=brief,
                    project_dir=target_dir,
                    progress_callback=self.ui.progress,
                )
                curr_sess.save(self.coord.history)
                abs_path = os.path.abspath(target_dir)
                ChatUI.success(
                    f"Subagent projesi başarıyla tamamlandı! {len(result['files_written'])} dosya üretildi.\n"
                    f"  🆔 Session ID: {curr_sess.session_id}\n"
                    f"  📁 Konum: {abs_path}\n"
                    f"  ⏱ Süre: {result['elapsed']:.1f}s"
                )
                self.cmds._status("")
                ChatUI.system(
                    "Sohbet aktif ve Düzenleme Modunda!\n"
                    "  - Projeye ekleme veya düzenleme istemek için direkt yazabilirsiniz.\n"
                    "  - /dir      -> Proje klasörünü Explorer'da aç\n"
                    "  - /resume   -> Eski oturumlardan birini yükle\n"
                    "  - /reset    -> Yeni bir projeye/oturuma başla"
                )
                return
            except KeyboardInterrupt:
                ChatUI.system("Subagent orkestrasyonu durduruldu.")
                return
            except Exception as exc:
                ChatUI.error(f"Subagent hatası: {exc}")
                import traceback
                traceback.print_exc()
                return

        # ── Mod 1: Sıralı Waterfall Pipeline ──
        active_agents = load_agents(enabled_only=True)
        ChatUI.success(f"Pipeline baslatiliyor! ({len(active_agents)} agent)")

        from main import run_pipeline
        try:
            result = run_pipeline(
                project_brief=brief,
                progress_callback=self.ui.progress,
                project_dir=target_dir,
                max_retries=max_retries,
            )
            curr_sess.save(self.coord.history)

            abs_path = os.path.abspath(target_dir)
            ChatUI.success(
                f"Proje basariyla tamamlandi! {len(result['files_written'])} dosya uretildi.\n"
                f"  🆔 Session ID: {curr_sess.session_id}\n"
                f"  📁 Konum: {abs_path}\n"
                f"  ⏱ Süre: {result['elapsed']:.1f}s"
            )
            self.cmds._status("")

            # Devam etme bilgilendirmesi
            ChatUI.system(
                "Sohbet aktif ve Duzenleme Modunda!\n"
                "  - Projeye ekleme veya duzenleme istemek icin direkt yazabilirsiniz.\n"
                "  - /dir      -> Proje klasorunu Windows Explorer'da ac\n"
                "  - /resume   -> Eski oturumlardan birini yukle\n"
                "  - /reset    -> Yeni bir projeye/oturuma basla"
            )

        except KeyboardInterrupt:
            ChatUI.system(
                "Pipeline durduruldu.\n"
                "  ► /continue veya /devam yazarak kaldığınız yerden devam edebilirsiniz."
            )
        except Exception as exc:
            ChatUI.error(f"Pipeline hatasi: {exc}")
            import traceback
            traceback.print_exc()

    def _continue_pipeline(self) -> None:
        """Checkpoint.json'dan kaldığı yerden devam et."""
        from main import load_checkpoint, run_pipeline

        ui = ChatUI
        c  = ui._c
        curr_sess = session_manager.current_session
        project_dir = str(curr_sess.project_dir)

        chk = load_checkpoint(project_dir)
        if not chk:
            ui.error("Bu proje için bekleyen checkpoint bulunamadı.")
            ui.info(f"  Konum: {project_dir}\\.myfcli\\checkpoint.json")
            ui.system("Yeni bir pipeline başlatmak için /run yazın.")
            return

        status     = chk.get("status", "unknown")
        done_roles = chk.get("completed_roles", [])
        brief      = chk.get("brief", "")
        interrupted_at = chk.get("interrupted_at", "-")

        print(c("\n  ═══ CHECKPOINT BULUNDU ═══", Fore.CYAN, Style.BRIGHT))
        print(f"  Durum     : {c(status.upper(), Fore.YELLOW, Style.BRIGHT)}")
        print(f"  Kesilme   : {interrupted_at[11:19] if len(interrupted_at) > 11 else interrupted_at}")
        print(f"  Proje     : {project_dir}")
        print()
        if done_roles:
            print(c("  ✓ Tamamlanan ajanlar:", Fore.GREEN, Style.BRIGHT))
            for r in done_roles:
                print(f"    ↳ {r}")
        else:
            print(c("  (Henüz hiç ajan tamamlanmamış)", Fore.WHITE))

        # Hangi ajanlar kaldı?
        from agents import load_agents
        all_agents  = load_agents(enabled_only=True)
        pending     = [a for a in all_agents if a.role_type not in done_roles]
        if not pending:
            ui.success("Tüm ajanlar zaten tamamlanmış! /run ile yeni pipeline başlatın.")
            return

        print()
        print(c("  ▶ Devam edilecek ajanlar:", Fore.CYAN, Style.BRIGHT))
        for a in pending:
            print(f"    ↳ [{a.pipeline_order}] {a.display_name}  ({a.role_type})")
        print()

        confirm = input(c(
            "  Kaldığı yerden devam edilsin mi? [Enter=evet / h=hayir]: ",
            Fore.YELLOW
        )).strip().lower()
        if confirm in ("h", "hayir", "n", "no"):
            ui.system("İptal. /run ile baştan başlayabilirsiniz.")
            return

        # Sandbox modunu koru
        from permission_manager import permission_manager
        if chk.get("sandbox", False) or "all" in permission_manager._session_grants:
            permission_manager._session_grants.add("all")

        max_retries = chk.get("max_retries", -1)

        ui.success(
            f"Devam ediliyor! {len(done_roles)} ajan atlanıyor, "
            f"{len(pending)} ajan çalışacak."
        )

        try:
            result = run_pipeline(
                project_brief=brief,
                progress_callback=self.ui.progress,
                project_dir=project_dir,
                max_retries=max_retries,
                resume_checkpoint=chk,
            )
            curr_sess.save(self.coord.history)
            abs_path = os.path.abspath(project_dir)
            ui.success(
                f"Pipeline tamamlandı! {len(result['files_written'])} dosya mevcut.\n"
                f"  📁 Konum: {abs_path}\n"
                f"  ⏱ Süre: {result['elapsed']:.1f}s"
            )
            self.cmds._status("")
        except KeyboardInterrupt:
            ui.system(
                "Tekrar durduruldu. /continue ile yeniden devam edebilirsiniz."
            )
        except Exception as exc:
            ui.error(f"Devam hatası: {exc}")
            import traceback
            traceback.print_exc()



# ═══════════════════════════════════════════════════════════════════════════
# Giris noktasi
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    ChatSession().run()


if __name__ == "__main__":
    main()
