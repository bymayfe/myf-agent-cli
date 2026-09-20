"""
coordinator_agent.py — Koordinator agent.

Kullaniciyla cok turlu (multi-turn) sohbet eder.
Projeyi anlar, soru sorar, plan sunar, onay alinca pipeline'i tetikler.
"""

from __future__ import annotations
import re
import warnings
import logging
from typing import Callable

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="litellm")

from agents import extract_pipeline_marker, load_agents
from ollama_client import is_ollama_provider, call_ollama_chat
from session_manager import session_manager

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Sabitler
# ─────────────────────────────────────────────

_APPROVAL_WORDS = {
    "onayla", "onayladim", "onayliyorum", "onay", "plani onayla",
    "baslat", "pipeline baslat", "go", "proceed", "start",
    "plan tamam", "tamam basla", "tamamdir baslat", "hadi basla"
}

_CANCEL_WORDS = {
    "iptal", "dur", "cancel", "stop", "hayir", "vazgec",
}

_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert AI software project coordinator. Your name: {name}.
CURRENT DATE AND TIME: {current_date}
(IMPORTANT: Your perception of time, package research, and version checks must always be based on this current date).
You understand and manage the user's project requirements.

LIVE INTERNET, PACKAGE & WEB RESEARCH CAPABILITY (Agent-Reach):
- The "Agent-Reach" live web and official package registry (NPM, PyPI) research infrastructure is active in your system.
- NEVER use dismissive phrases like "I don't have internet access", "My knowledge is not up-to-date", or "I cannot search the web".
- When the user asks about a package, library, or modern topic and you have `[AGENT-REACH LIVE WEB RESEARCH RESULTS]`, provide direct and definitive information based on this live data.
- If you need up-to-date information, official documentation, or package versions and there are no search results in context, request a search on a single line in your response using `[SEARCH: search_term]`. The system will execute this search and return results.

ACTIVE EXECUTION ARCHITECTURE:
{agent_list}

{project_context}

TASK FLOW & EDITING STRATEGY:
1. MINOR FIX / SINGLE FILE ADJUSTMENT (Live Quick Edit - ZERO PIPELINE):
   - When the user requests a small tweak, color change, single function addition, or bug fix in an existing project (e.g. "darken the card background in styles.css", "add this event to app.js"):
   - DO NOT start the full pipeline or subagents!
   - Directly output the code block with the file path on the first line:
     ```css
     /* filepath: styles.css */
     /* updated code */
     ```
     or
     ```javascript
     // filepath: app.js
     // updated code
     ```
   - The system queues this code block as a proposed edit. It will not be written to disk until the user approves with /apply.

2. NEW PROJECT FROM SCRATCH OR MAJOR ARCHITECTURAL REFACTOR (Pipeline):
   - If a new project is being built from scratch or there is a major request rebuilding the whole system:
   - First present an architectural plan and ask the user for approval ("Does this plan look good, shall we proceed?").
   - Once the user gives definitive approval, ALWAYS use this exact marker format:

##PIPELINE_START##
[Clarified, complete project summary containing all additions and changes]
##PIPELINE_END##

RULES:
- OUTPUT LANGUAGE (MANDATORY): Always communicate with the user, explain your steps, and present plans in fluent Turkish (Türkçe). Keep all code, variable names, comments inside code files, and markers in English.
- For minor fixes, avoid lengthy unnecessary explanations and directly produce the code block.
- Courtesy & Confirmation Messages (e.g. "eyw", "sağol", "teşekkürler", "tamamdır", "eline sağlık"): Do not re-run tests or restart the project; politely acknowledge with "Rica ederim" and wait for a new request.
"""

_CHAT_MODE_RULES = """\

CHAT MODE RULE:
- This is a terminal REPL conversation. Respond directly to whatever free text the user writes.
- Do not autonomously start a pipeline, ask for approval, or produce ##PIPELINE_START## / ##PIPELINE_END## markers.
- The pipeline is started separately when the user explicitly enters the /run command.
"""


# ─────────────────────────────────────────────
# Yardimci fonksiyonlar
# ─────────────────────────────────────────────

def _matches(text: str, word_set: set[str]) -> bool:
    """Metin icindeki tam kelimelerden biri word_set icinde var mi?"""
    words = set(re.findall(r'\b\w+\b', text.strip().lower()))
    return bool(words & word_set)


def _build_agent_list() -> str:
    """Format system prompt based on active mode and agent list."""
    from settings import settings
    mode = settings.execution_mode
    if mode == "subagent":
        return (
            "  [DYNAMIC SUBAGENT ORCHESTRATION MODE]\n"
            "  - Dynamically spawns task-specialized subagents (architect, developer, tester, debugger, researcher).\n"
            "  - The lead agent delegates tasks, subagents execute in isolated context, and results are synthesized."
        )
    elif mode == "interactive":
        return (
            "  [INTERACTIVE CHAT & LIVE CODING MODE]\n"
            "  - Direct live interaction with the user: question-answering, file inspection, and direct single-agent coding."
        )
    try:
        agents = load_agents(enabled_only=True)
        return "\n".join(
            f"  {i+1}. {a.icon} {a.display_name} - {a.description}"
            for i, a in enumerate(agents)
        )
    except Exception:
        return "  (failed to load agent list)"


# ─────────────────────────────────────────────
# Ana sinif
# ─────────────────────────────────────────────

class CoordinatorAgent:
    """
    Kullaniciyla interaktif sohbet yuruten, pipeline'i tetikleyen koordinator.

    Ozellikler:
    - Cok turlu (multi-turn) sohbet gecmisi
    - Think modu acilip kapatilabilir (settings.think_mode)
    - Onay tespiti (Turkce + Ingilizce)
    - ##PIPELINE_START## marker ile pipeline tetikleme
    """

    def __init__(self):
        self.history: list[dict] = []
        self._rebuild_system_prompt()

    # ── Sistem promptu ─────────────────────────────────────

    def _rebuild_system_prompt(self) -> None:
        """Settings, aktif agentlar ve mevcut proje durumuna gore sistem promptunu yenile."""
        from settings import settings
        from brain import read_brain, list_output_files

        files = list_output_files()
        project_ctx_parts = []
        if files:
            file_list = "\n".join(f"  - {f}" for f in files[:30])
            if len(files) > 30:
                file_list += f"\n  - ... and {len(files)-30} more files"
            project_ctx_parts.append(f"CURRENT PROJECT FILES ({len(files)} files):\n{file_list}")

        brain_txt = read_brain()
        if brain_txt and "(henüz doldurulmadı)" not in brain_txt:
            project_ctx_parts.append(f"PROJECT MEMORY (Brain):\n{brain_txt[:2500]}")

        proj_context_str = "\n\n".join(project_ctx_parts)
        if proj_context_str:
            proj_context_str = f"=== ACTIVE PROJECT STATUS ===\n{proj_context_str}\n"

        from datetime import datetime
        now = datetime.now()
        tr_days = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
        tr_months = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
        day_str = tr_days[now.weekday()]
        month_str = tr_months[now.month - 1]
        current_date_str = f"{now.day} {month_str} {now.year}, {now.strftime('%H:%M')} ({day_str})"

        self._system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            name=settings.coordinator_name,
            current_date=current_date_str,
            agent_list=_build_agent_list(),
            project_context=proj_context_str,
        )
        if settings.execution_mode == "interactive":
            self._system_prompt += _CHAT_MODE_RULES

    # ── Ana sohbet metodu ──────────────────────────────────

    def chat(self, user_input: str,
             on_token: Callable[[str, str], None] | None = None,
             allow_pipeline: bool = True) -> tuple[str, bool]:
        """
        Kullanici girdisini isle, (yanit, pipeline_baslayacak_mi) dondur.

        Args:
            user_input : Kullanicinin yazdigi metin
            on_token       : Her token geldiginde cagirilacak callback (token, token_type)
            allow_pipeline : False ise sohbet salt REPL olarak kalır; LLM'in
                             ürettiği marker veya onay metni pipeline başlatamaz.

        Returns:
            (response_text, should_start_pipeline)
        """
        user_input = user_input.strip()

        if _matches(user_input, _CANCEL_WORDS):
            return "Anlasildi, iptal ettim. Yeni istek yazabilirsin.", False

        lower_input = user_input.lower()

        # Doğrudan /run veya /start veya ##PIPELINE_START## girildiyse
        if allow_pipeline and (user_input in ("/run", "/start") or "##pipeline_start##" in lower_input):
            self.history.append({"role": "user", "content": user_input})
            session_manager.current_session.save(self.history)
            return "Onay alindi! Pipeline motoru baslatiliyor...", True

        # Kullanıcı sadece kısa bir onay mı verdi? (örn: "evet", "başla", "onay", "tamamdır")
        words = user_input.split()
        is_short_input = len(words) <= 4
        is_approval = (
            _matches(user_input, _APPROVAL_WORDS) or
            lower_input in ("evet", "başla", "basla", "tamam", "hadi", "onay", "ok", "onaylıyorum", "onayliyorum", "devam et")
        )

        has_previous_plan = any(
            msg["role"] == "assistant" and any(k in msg["content"].lower() for k in ["plan", "onay", "baslayalim", "uygun mu", "mimari", "özellik"])
            for msg in self.history
        )

        # Sadece kısa bir onay kelimesi varsa VE daha önce bir plan sunulmuşsa (Akıllı Hibrit / Sıfır Bekleme):
        if allow_pipeline and is_short_input and is_approval and has_previous_plan:
            self.history.append({"role": "user", "content": user_input})
            session_manager.current_session.save(self.history)
            return "Plan onaylandı! Pipeline motoru doğrudan başlatılıyor...", True

        # Kullanıcı ekleme yaptı, soru sordu veya detay verdi -> LLM ile planı güncelle
        self._rebuild_system_prompt()
        self.history.append({"role": "user", "content": user_input})

        raw = self._call_llm(on_token=on_token)
        self.history.append({"role": "assistant", "content": raw})
        session_manager.current_session.save(self.history)

        brief = extract_pipeline_marker(raw) if allow_pipeline else ""
        if brief:
            display = re.sub(r"##PIPELINE_(?:START|END)##", "", raw).strip()
            return display or "Harika! Pipeline basliyor...", True

        return raw, False

    # ── LLM cagrisi (streaming destekli) ──────────────────

    def _call_llm(self, on_token: Callable[[str, str], None] | None = None) -> str:
        """
        Streaming completion. Her token on_token callback'ine iletilir.
        Ollama ise dogrudan REST client, degilse LiteLLM kullanir.
        """
        from config import LLM_PARAMS, AGENT_MODELS
        from settings import settings
        from context_budgeter import context_budgeter

        model = AGENT_MODELS.get("coordinator", list(AGENT_MODELS.values())[0])

        # Dinamik Context Bütçesi & Otomatik Özetleme (should_summarize)
        active_history = self.history
        if context_budgeter.should_summarize(self._system_prompt, active_history, model, threshold=0.75):
            active_history = context_budgeter.summarize_history(active_history, keep_recent=4)

        messages = [{"role": "system", "content": self._system_prompt}] + active_history

        if is_ollama_provider(model, LLM_PARAMS.get("api_base", "")):
            return call_ollama_chat(
                messages=messages,
                model=model,
                api_base=LLM_PARAMS.get("api_base", "http://localhost:11434"),
                stream=True,
                on_token=on_token,
                think_mode=settings.think_mode,
                temperature=settings.temperature,
                max_tokens=min(settings.max_tokens, 2048),
            )
        else:
            from litellm import completion
            comp_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": settings.temperature,
                "max_tokens": max(settings.max_tokens, 4096) if settings.think_mode else settings.max_tokens,
                "api_base": LLM_PARAMS["api_base"],
                "api_key": LLM_PARAMS["api_key"],
                "stream": True,
            }
            # Nemotron, Kimi-K3, DeepSeek modelleri için akıl yürütme (thinking) parametreleri
            is_thinking_model = any(k in model.lower() for k in ("nemotron", "deepseek", "kimi", "r1"))
            if settings.think_mode or is_thinking_model:
                comp_kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": True},
                    "reasoning_budget": min(settings.max_tokens, 16384),
                }

            from llm_client import ColdStartWatcher
            with ColdStartWatcher(model, api_base=LLM_PARAMS.get("api_base", "")):
                resp = completion(**comp_kwargs)
                resp_iter = iter(resp)
                first_chunk = next(resp_iter, None)

            full_text = ""
            try:
                def _handle_chunk(chunk):
                    nonlocal full_text
                    delta = chunk.choices[0].delta if chunk and chunk.choices else None
                    if delta:
                        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
                        if reasoning and on_token and settings.think_mode:
                            on_token(reasoning, "thinking")

                        token = getattr(delta, "content", "") or ""
                        if token:
                            full_text += token
                            if on_token:
                                on_token(token, "content")

                if first_chunk:
                    _handle_chunk(first_chunk)

                for chunk in resp_iter:
                    _handle_chunk(chunk)
            except KeyboardInterrupt:
                if on_token:
                    on_token("\n[durduruldu]\n", "content")
            return full_text

    @classmethod
    def warmup(cls) -> bool:
        """
        Ollama'ya kucuk bir istek gondererek modeli VRAM'a yukle.
        Baslarken cagirilir, ilk gercek istegi hizlandirir.
        """
        from config import LLM_PARAMS, AGENT_MODELS
        try:
            model = AGENT_MODELS.get("coordinator", list(AGENT_MODELS.values())[0])
            if is_ollama_provider(model, LLM_PARAMS.get("api_base", "")):
                call_ollama_chat(
                    messages=[{"role": "user", "content": "hi"}],
                    model=model,
                    api_base=LLM_PARAMS.get("api_base", "http://localhost:11434"),
                    stream=False,
                    max_tokens=1,
                )
            return True
        except Exception as exc:
            logger.warning("Warmup basarisiz: %s", exc)
            return False

    # ── Yardimci metodlar ──────────────────────────────────

    def get_project_brief(self) -> str:
        """Pipeline icin proje ozetini sohbet gecmisinden cikart."""
        # 1. En son ##PIPELINE_START## varsa ve ici doluysa onu al
        for msg in reversed(self.history):
            if msg["role"] == "assistant":
                brief = extract_pipeline_marker(msg["content"])
                if brief and len(brief.strip()) > 30:
                    return brief.strip()

        # 2. Onay veya sistem enjeksiyonu olmayan gercek kullanici mesajlarini filtrele
        clean_user_msgs = []
        for m in self.history:
            if m["role"] != "user":
                continue
            txt = m["content"].strip()
            if txt.startswith("/"):
                continue
            # Parantezli sistem eklerini temizle: (Kullanici sunulan plani onayladi...)
            txt = re.sub(r"\(Kullanici sunulan plani onayladi.*?\)", "", txt, flags=re.I | re.DOTALL).strip()
            # Kısa onay kelimelerini filtrele
            if txt.lower() in ("başla", "basla", "evet", "tamam", "ok", "onay", "onaylıyorum", "onayliyorum", "hadi", "devam", "devam et", "ve"):
                continue
            if len(txt) > 0:
                clean_user_msgs.append(txt)

        if clean_user_msgs:
            # En uzun / en kapsamlı ana isteği başa al
            longest_msg = max(clean_user_msgs, key=len)
            other_msgs = [m for m in clean_user_msgs if m != longest_msg]
            
            # Asistanın sunduğu ve onaylanan mimari plan varsa onu da bağlama ekle
            last_plan = ""
            for msg in reversed(self.history):
                if msg["role"] == "assistant" and len(msg["content"].strip()) > 100:
                    last_plan = msg["content"].strip()
                    break

            base = longest_msg
            if other_msgs:
                additions = "\n\n".join(f"- {m}" for m in other_msgs)
                base += f"\n\n=== USER ADDITIONAL INSTRUCTIONS & UPDATES ===\n{additions}"
            
            if last_plan:
                base += f"\n\n=== APPROVED ARCHITECTURE & PLAN ===\n{last_plan}"
            
            return base
        return ""

    def reset(self, new_session: bool = False) -> None:
        """Sohbet gecmisini temizle ve promptu yenile. Sadece new_session=True ise yeni oturum acar."""
        self.history.clear()
        if new_session:
            session_manager.create_new_session("Yeni Oturum", "yeni_proje")
        self._rebuild_system_prompt()

    def refresh_settings(self) -> None:
        """Ayarlar degistikten sonra sistem promptunu guncelle."""
        self._rebuild_system_prompt()
