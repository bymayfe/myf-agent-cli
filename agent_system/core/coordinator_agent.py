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
Sen bir yazilim projesi koordinatoru yapay zeka sistemisin. Adin: {name}.
BUGÜNÜN GÜNCEL TARİHİ VE SAATİ: {current_date}
(ÖNEMLİ: Zaman algın, paket araştırmaların ve sürüm kontrollerin her zaman bu güncel tarihe dayanmalıdır).
Kullanicinin proje istegini anlayip yonetirsin.

CANLI İNTERNET, PAKET VE WEB ARAŞTIRMASI YETENEĞİ (Agent-Reach):
- Sisteminde "Agent-Reach" canlı web ve resmi paket defteri (NPM, PyPI) araştırma altyapısı aktiftir.
- ASLA "İnternet erişimim yok", "Bilgim güncel değil", "Webde arama yapamam" gibi reddedici ifadeler KULLANMA.
- Kullanıcı bir paket, kütüphane veya güncel konu sorduğunda sana sağlanan `[AGENT-REACH CANLI WEB ARAŞTIRMA SONUÇLARI]` varsa, bu güncel verileri temel alarak doğrudan ve kesin bilgi ver.
- Eğer güncel bilgiye, resmi dokümantasyona veya paket sürümüne ihtiyacın varsa ve bağlamda arama sonucu yoksa, yanıtında tek satırda `[SEARCH: aranacak_terim]` formatını kullanarak anında arama isteyebilirsin. Sistem bu aramayı yapıp sonuçları getirecektir.

AKTIF CALISMA YAPISI:
{agent_list}

{project_context}

GOREV AKISI VE DUZENLEME STRATEJISI:
1. KUCUK DUZELTME / TEKIL DOSYA AYARI (Canli Hizli Duzenleme - SIFIR PIPELINE):
   - Kullanici var olan projede kucuk bir degisiklik, renk ayari, tekil fonksiyon ekleme veya kucuk bir hata duzeltmesi istediginde (orn: "styles.css'de kart arka planini koyulastir", "app.js'e su eventi ekle"):
   - Tum pipeline'i veya alt ajanlari BASLATMA!
   - Dogrudan ilgili dosyanin kod blogunu birinci satirinda dosya yolu olacak sekilde ver:
     ```css
     /* filepath: styles.css */
     /* guncel kodlar */
     ```
     veya
     ```javascript
     // filepath: app.js
     // guncel kodlar
     ```
   - Sistem bu kod blogunu değişiklik önerisi olarak kuyruğa alır. Kullanıcı /apply ile onaylamadan dosyaya yazmaz.

2. SIFIRDAN YENI PROJE VEYA KOKTEN BUYUK MIMARI DEGISIKLIK (Pipeline):
   - Eger sifirdan yeni bir proje insa ediliyorsa veya tum sistemi bastan kuracak buyuk bir istek varsa:
   - Once mimari plan sun ve kullanicidan onay iste ("Bu plan uygun mu, baslayalim mi?").
   - Kullanici kesin onay verince KESINLIKLE su formati kullan:

##PIPELINE_START##
[Netlestirilmis, tum ekleme ve degisiklikleri iceren tam proje ozeti]
##PIPELINE_END##

KURALLAR:
- Turkce, sade, net ve profesyonel konus.
- Kucuk duzeltmelerde gereksiz uzun aciklama yapma, dogrudan kod blogunu uret.
- Nezaket ve onay iletilerinde (örn: "eyw", "sağol", "teşekkürler", "tamamdır", "eline sağlık"): Projeyi veya testleri tekrar çalıştırma; nezaketle rica ederim de ve yeni bir istek bekle.
"""

_CHAT_MODE_RULES = """\

CHAT MODU KURALI:
- Bu bir terminal REPL sohbetidir. Kullanıcının yazdığı her serbest metni doğrudan yanıtla.
- Kendiliğinden pipeline başlatma, onay isteme veya ##PIPELINE_START## / ##PIPELINE_END## işaretlerini üretme.
- Pipeline, kullanıcı açıkça /run komutunu girdiğinde ayrı olarak başlatılır.
"""


# ─────────────────────────────────────────────
# Yardimci fonksiyonlar
# ─────────────────────────────────────────────

def _matches(text: str, word_set: set[str]) -> bool:
    """Metin icindeki tam kelimelerden biri word_set icinde var mi?"""
    words = set(re.findall(r'\b\w+\b', text.strip().lower()))
    return bool(words & word_set)


def _build_agent_list() -> str:
    """Aktif moda ve agent listesine gore sistem promptunu formatla."""
    from settings import settings
    mode = settings.execution_mode
    if mode == "subagent":
        return (
            "  [DİNAMİK SUBAGENT ORKESTRASYON MODU]\n"
            "  - Göreve özel alt uzmanlar (architect, developer, tester, debugger, researcher) dinamik oluşturulur.\n"
            "  - Lider ajan görev dağıtır, alt ajanlar izole bağlamda çalışır ve sonuçlar birleştirilir."
        )
    elif mode == "interactive":
        return (
            "  [İNTERAKTİF SOHBET & CANLI KODLAMA MODU]\n"
            "  - Doğrudan kullanıcı ile canlı soru-cevap, dosya inceleme ve tekli kodlama modu."
        )
    try:
        agents = load_agents(enabled_only=True)
        return "\n".join(
            f"  {i+1}. {a.icon} {a.display_name} - {a.description}"
            for i, a in enumerate(agents)
        )
    except Exception:
        return "  (agent listesi yuklenemedi)"


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
                file_list += f"\n  - ... ve {len(files)-30} dosya daha"
            project_ctx_parts.append(f"MEVCUT PROJE DOSYALARI ({len(files)} dosya):\n{file_list}")

        brain_txt = read_brain()
        if brain_txt and "(henüz doldurulmadı)" not in brain_txt:
            project_ctx_parts.append(f"PROJE HAFIZASI (Brain):\n{brain_txt[:2500]}")

        proj_context_str = "\n\n".join(project_ctx_parts)
        if proj_context_str:
            proj_context_str = f"=== AKTIF PROJE DURUMU ===\n{proj_context_str}\n"

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
            with ColdStartWatcher(model):
                resp = completion(**comp_kwargs)
            full_text = ""
            try:
                for chunk in resp:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta:
                        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
                        if reasoning and on_token and settings.think_mode:
                            on_token(reasoning, "thinking")

                        token = getattr(delta, "content", "") or ""
                        if token:
                            full_text += token
                            if on_token:
                                on_token(token, "content")
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
                base += f"\n\n=== KULLANICI EK TALIMATLARI VE GUNCELLEMELER ===\n{additions}"
            
            if last_plan:
                base += f"\n\n=== ONAYLANAN MIMARI VE PLAN ===\n{last_plan}"
            
            return base
        return ""

    def reset(self) -> None:
        """Sohbet gecmisini temizle, yeni proje oturumu ac."""
        self.history.clear()
        session_manager.create_new_session("Yeni Oturum", "yeni_proje")
        self._rebuild_system_prompt()

    def refresh_settings(self) -> None:
        """Ayarlar degistikten sonra sistem promptunu guncelle."""
        self._rebuild_system_prompt()
