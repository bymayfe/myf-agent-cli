"""
llm_client.py — LLM çağrı merkezi wrapper.

Ollama için doğrudan REST client (ollama_client.py) kullanır (LiteLLM chunk parser hatalarını önler).
Bulut / API modelleri için LiteLLM completion() kullanır.
"""

import time
import math
import logging
import warnings
from typing import Optional, Callable

from litellm import completion
import litellm

from config import LLM_PARAMS, AGENT_MODELS
from ollama_client import is_ollama_provider, call_ollama_chat

# LiteLLM + Pydantic uyarılarını sustur
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="litellm")

logger = logging.getLogger(__name__)


# ─── Bilinen modeller için genel fallback context window sözlüğü ────────────
_KNOWN_MODEL_CONTEXT: dict[str, int] = {
    # Yerel Modeller (Ollama default KV-cache optimize: 4096 / 8192)
    "qwen3.5:4b": 8192,
    "qwen2.5-coder:7b": 8192,
    "qwen2.5-coder:14b": 8192,
    "qwen3.8:27b": 4096,
    "qwen3:27b": 4096,
    "deepseek-r1:8b": 8192,
    "deepseek-r1:14b": 8192,
    "deepseek-r1:32b": 4096,
    "ornith-1.5:9b": 8192,
    # Kimi / Moonshot
    "kimi-k2": 131072,
    "moonshot-v1-8k": 8192,
    "moonshot-v1-32k": 32768,
    "moonshot-v1-128k": 131072,
    # OpenRouter / Cloud
    "qwen3-235b": 32768,
    "qwen2.5-coder-32b": 32768,
    "claude-3.5-sonnet": 200000,
    "gpt-4o": 128000,
    "gemini-2.0-flash": 1000000,
    "deepseek-r1": 65536,
}


def estimate_tokens(text: str) -> int:
    """
    Türkçe ve İngilizce metinler için güvenli (muhafazakâr) token tahmini.
    Türkçe eklemeli yapısı ve karakter/token oranı göz önüne alınarak
    len(text) / 3.2 formülü ve tavan yuvarlama (ceil) kullanılır.
    """
    if not text:
        return 0
    return math.ceil(len(text) / 3.2)


def get_model_context_window(model_name: str, default_ctx: int = 8192) -> int:
    """
    Model için tanımlı context window limitini döndürür.
    Öncelik:
      1. providers_config.json model_context_windows
      2. providers_config.json default_context_window
      3. _KNOWN_MODEL_CONTEXT sözlüğü
      4. default_ctx (varsayılan: 8192)
    """
    if not model_name:
        return default_ctx

    clean_name = model_name.split("/")[-1].lower()

    # 1. Config'den dene
    try:
        from config import load_providers
        data = load_providers()
        for p_name, p_data in data.get("providers", {}).items():
            model_ctxs = p_data.get("model_context_windows", {})
            for m_key, ctx_val in model_ctxs.items():
                if m_key.lower() == clean_name or m_key.lower() in model_name.lower():
                    return int(ctx_val)
            if p_data.get("model_prefix", "") in model_name.lower():
                if "default_context_window" in p_data:
                    return int(p_data["default_context_window"])
    except Exception:
        pass

    # 2. _KNOWN_MODEL_CONTEXT'ten eşleştir
    for k, v in _KNOWN_MODEL_CONTEXT.items():
        if k in clean_name or k in model_name.lower():
            return v

    return default_ctx


def trim_prompt_to_context(
    system_prompt: str,
    user_prompt: str,
    max_allowed_tokens: int,
    safety_ratio: float = 0.85,
    model_name: Optional[str] = None,
) -> tuple[str, str]:
    """
    Prompt toplamı hedef modelin context penceresinin safety_ratio oranını aşıyorsa,
    system_prompt'u koruyarak user_prompt'un orta kısmını satır sınırlarına (\n) dikkat ederek kırpar.

    Eğer system_prompt tek başına sınırı aşıyorsa [CONTEXT-OVERFLOW-UNRECOVERABLE] hatası loglanır.
    """
    sys_tokens = estimate_tokens(system_prompt)
    max_safe_tokens = int(max_allowed_tokens * safety_ratio)

    if sys_tokens >= max_safe_tokens:
        logger.error(
            "[CONTEXT-OVERFLOW-UNRECOVERABLE] System prompt (%d tok) tek başına model (%s / %d tok) context sınırını aşıyor!",
            sys_tokens,
            model_name or "unknown",
            max_allowed_tokens,
        )
        return system_prompt, ""

    budget_for_user = max_safe_tokens - sys_tokens

    user_tokens = estimate_tokens(user_prompt)
    if user_tokens <= budget_for_user:
        return system_prompt, user_prompt

    # User prompt kırpılmalı (yaklaşık karakter bütçesi: budget * 3.0)
    max_user_chars = int(budget_for_user * 3.0)
    if len(user_prompt) <= max_user_chars:
        return system_prompt, user_prompt

    # ── Satır sınırına (\n) duyarlı kırpma (kod bloklarını bozmamak için) ──────
    raw_head = int(max_user_chars * 0.4)
    split_head = user_prompt.rfind("\n", 0, raw_head)
    if split_head == -1 or split_head < int(raw_head * 0.4):
        head_cut = raw_head
    else:
        head_cut = split_head

    raw_tail = int(max_user_chars * 0.5)
    tail_start = len(user_prompt) - raw_tail
    split_tail = user_prompt.find("\n", tail_start)
    if split_tail == -1 or split_tail > (tail_start + int(raw_tail * 0.6)):
        tail_cut = tail_start
    else:
        tail_cut = split_tail

    omitted_chars = len(user_prompt) - head_cut - (len(user_prompt) - tail_cut)

    trimmed_user = (
        user_prompt[:head_cut].rstrip()
        + f"\n\n... [BAĞLAM SINIRI: {omitted_chars} karakter kırpıldı] ...\n\n"
        + user_prompt[tail_cut:].lstrip()
    )
    logger.warning(
        "[CONTEXT-TRIM] User prompt satır sınırlarıyla kırpıldı: %d tok -> ~%d tok (Hedef model context: %d)",
        user_tokens,
        estimate_tokens(trimmed_user),
        max_allowed_tokens,
    )
    return system_prompt, trimmed_user


def call_llm(
    agent_name: str,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_retries: int = 3,
    retry_delay: float = 5.0,
    fallback_models: Optional[list[str]] = None,
) -> str:
    """
    LLM'e senkron istek gönder ve metin yanıtı döndür.
    Model context window kontrolü, kurtarılamaz system prompt taşmasında fallback'e geçiş
    ve satır sınırlarına duyarlı prompt kırpma (trim) uygular.
    """
    selected_model = model or AGENT_MODELS.get(agent_name, list(AGENT_MODELS.values())[0])
    model_chain = [selected_model]
    if fallback_models:
        for fb in fallback_models:
            if fb not in model_chain:
                model_chain.append(fb)

    last_exc: Optional[Exception] = None

    for m_idx, current_model in enumerate(model_chain):
        is_fallback = (m_idx > 0)
        ctx_window = get_model_context_window(current_model)
        tot_tok = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
        sys_tokens = estimate_tokens(system_prompt)

        # 1. System prompt tek başına hedef model sınırını aşıyorsa bu modeli atla ve sonraki modele geç
        if sys_tokens >= int(ctx_window * 0.85):
            logger.error(
                "[CONTEXT-OVERFLOW-UNRECOVERABLE] System prompt (%d tok) tek başına model (%s / %d tok) sınırını aşıyor.",
                sys_tokens,
                current_model,
                ctx_window,
            )
            if m_idx < len(model_chain) - 1:
                logger.warning("[%s] Daha büyük context'li fallback modele geçiliyor...", agent_name)
                continue
            else:
                raise ValueError(
                    f"[{agent_name}] [CONTEXT-OVERFLOW-UNRECOVERABLE] System prompt ({sys_tokens} tok) hiçbir modelin context limitine sığmıyor."
                )

        # 2. Context kontrolü & Satır duyarlı Trim
        curr_sys_prompt = system_prompt
        curr_user_prompt = user_prompt
        if tot_tok > int(ctx_window * 0.85):
            logger.warning(
                "[CONTEXT-LIMIT] Prompt (%d tok) model context sınırının (%%85 = %d tok / model=%s) üstünde, kırpılıyor.",
                tot_tok,
                int(ctx_window * 0.85),
                current_model,
            )
            curr_sys_prompt, curr_user_prompt = trim_prompt_to_context(
                curr_sys_prompt,
                curr_user_prompt,
                max_allowed_tokens=ctx_window,
                safety_ratio=0.85,
                model_name=current_model,
            )

        messages = [
            {"role": "system", "content": curr_sys_prompt},
            {"role": "user",   "content": curr_user_prompt},
        ]

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "[%s] → model=%s | attempt=%d | sys=%d tok | user=%d tok",
                    agent_name,
                    current_model,
                    attempt,
                    estimate_tokens(curr_sys_prompt),
                    estimate_tokens(curr_user_prompt),
                )
                if is_fallback:
                    print(f"  [{agent_name}] Fallback model ({current_model}) deneniyor... (deneme {attempt}/{max_retries})")
                else:
                    print(f"  [{agent_name}] LLM cagrisi yapiliyor... (deneme {attempt}/{max_retries})")

                # Ollama provider ise doğrudan Ollama REST client kullan (sıfır hata)
                if is_ollama_provider(current_model, LLM_PARAMS.get("api_base", "")):
                    content = call_ollama_chat(
                        messages=messages,
                        model=current_model,
                        api_base=LLM_PARAMS.get("api_base", "http://localhost:11434"),
                        stream=False,
                        think_mode=False,
                        temperature=LLM_PARAMS["temperature"],
                        max_tokens=LLM_PARAMS["max_tokens"],
                        num_ctx=ctx_window,
                        top_p=LLM_PARAMS.get("top_p"),
                        top_k=LLM_PARAMS.get("top_k"),
                    )
                else:
                    completion_kwargs = {
                        "model": current_model,
                        "messages": messages,
                        "temperature": LLM_PARAMS["temperature"],
                        "max_tokens": LLM_PARAMS["max_tokens"],
                        "api_base": LLM_PARAMS["api_base"],
                        "api_key": LLM_PARAMS["api_key"],
                    }
                    if "top_p" in LLM_PARAMS and LLM_PARAMS["top_p"] is not None:
                        completion_kwargs["top_p"] = LLM_PARAMS["top_p"]
                    response = completion(**completion_kwargs)
                    content = response.choices[0].message.content

                print(f"  [{agent_name}] Yanit alindi.")

                if not content or not content.strip():
                    raise ValueError("LLM bos veya 0 karakterli yanit dondu.")

                try:
                    from engines.quota_engine import quota_engine
                    tok_in = estimate_tokens(curr_sys_prompt) + estimate_tokens(curr_user_prompt)
                    tok_out = estimate_tokens(content)
                    quota_engine.record_usage(tokens=tok_in + tok_out, requests=1)
                except Exception:
                    pass

                return content

            except Exception as exc:
                last_exc = exc
                logger.warning("[%s] Deneme %d başarısız (%s): %s", agent_name, attempt, current_model, exc)
                print(f"  [!] [{agent_name}] Hata ({current_model}, deneme {attempt}): {exc}")
                if attempt < max_retries:
                    print(f"     {retry_delay:.0f}s bekleniyor...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    # Model deneme hakkı bitti, eğer varsa zincirdeki bir sonraki fallback modeline geç
                    break

    raise RuntimeError(
        f"[{agent_name}] {len(model_chain)} model ve {max_retries} denemeden sonra LLM yanıt vermedi."
    ) from last_exc
