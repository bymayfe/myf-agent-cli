"""
llm_client.py — LLM çağrı merkezi wrapper.

Ollama için doğrudan REST client (ollama_client.py) kullanır (LiteLLM chunk parser hatalarını önler).
Bulut / API modelleri için LiteLLM completion() kullanır.
"""

import time
import math
import logging
import warnings
from typing import Optional

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
import threading


def is_local_endpoint(api_base: str = "", model: str = "") -> bool:
    """Belirtilen api_base veya model yerel (localhost / llama.cpp / ollama / lm_studio) mi?"""
    b = (api_base or "").lower()
    m = (model or "").lower()
    # Açık bulut sağlayıcıları kesinlikle yerel değildir
    if any(k in b for k in ("nvidia.com", "openrouter.ai", "moonshot.cn", "deepseek.com", "openai.com", "anthropic.com", "googleapis.com")):
        return False
    if any(k in m for k in ("openrouter/", "moonshot/", "anthropic/", "gemini/")):
        return False
    return (
        any(k in b for k in ("localhost", "127.0.0.1", "0.0.0.0", ":8080", ":11434", ":1234"))
        or any(k in m for k in ("ollama", "llama_cpp", "lm_studio"))
    )


class ColdStartWatcher:
    """
    Bulut API veya yerel model çağrılarında cold start / gecikmeleri
    terminalde şeffaf ve canlı sayaçla gösteren yardımcı bağlam yöneticisi.
    """
    def __init__(self, model_name: str, threshold: float = 4.0, api_base: str = ""):
        self.model_name = model_name
        self.threshold = threshold
        self.api_base = api_base
        self.is_local = is_local_endpoint(api_base, model_name)
        self._stop_event = threading.Event()
        self._thread = None
        self._start_time = 0.0

    def __enter__(self):
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        elapsed = time.time() - self._start_time
        if elapsed >= self.threshold:
            print(f" (Tamamlandı: {elapsed:.1f}s)", flush=True)

    def _run(self):
        if self.is_local:
            # Yerel model (llama.cpp, Ollama, LM Studio)
            # Yerel modellerde bulut kuyruğu veya cold-start olmaz.
            # İlk saniyeler prompt evaluation (prefill), sonrasında yanıt üretimi gerçekleşir.
            local_intervals = [3, 8, 15, 25, 40, 60, 90, 120, 150, 180, 240, 300]
            idx = 0
            while not self._stop_event.is_set():
                elapsed = int(time.time() - self._start_time)
                if idx < len(local_intervals) and elapsed >= local_intervals[idx]:
                    target = local_intervals[idx]
                    if target <= 4:
                        print(f"\n  [INFO] 🧠 Yerel model promptu işliyor ({elapsed}s)...", end="", flush=True)
                    else:
                        print(f"\n  [INFO] ⚡ Yerel model yanıtı üretiyor ({elapsed}s)...", end="", flush=True)
                    idx += 1
                time.sleep(0.5)
        else:
            # Bulut sağlayıcı (OpenRouter, NVIDIA, Kimi vb.)
            intervals = [4, 8, 15, 25, 40, 60, 90]
            idx = 0
            while not self._stop_event.is_set():
                elapsed = int(time.time() - self._start_time)
                if idx < len(intervals) and elapsed >= intervals[idx]:
                    target = intervals[idx]
                    if target <= 8:
                        print(f"\n  [INFO] ⏳ Sağlayıcıya bağlanıldı, yanıt bekleniyor ({elapsed}s)...", end="", flush=True)
                    elif target <= 20:
                        print(f"\n  [INFO] 🚀 Model uyandırılıyor (Cold-Start / Kuyruk bekleniyor - {elapsed}s)...", end="", flush=True)
                    else:
                        print(f"\n  [INFO] ⏳ Bulut sağlayıcı kuyruğu yoğun ({elapsed}s), lütfen bekleyin...", end="", flush=True)
                    idx += 1
                time.sleep(0.5)



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

                api_base = LLM_PARAMS.get("api_base", "")
                is_local = is_local_endpoint(api_base, current_model)

                # Ollama provider ise doğrudan Ollama REST client kullan (sıfır hata)
                if is_ollama_provider(current_model, api_base):
                    ollama_token_count = 0
                    t_ollama_start = time.time()
                    last_ollama_update = [0.0]

                    def _ollama_on_token(token: str, token_type: str):
                        nonlocal ollama_token_count
                        if token_type == "content" and token:
                            ollama_token_count += 1
                            now = time.time()
                            if now - last_ollama_update[0] >= 0.3:
                                last_ollama_update[0] = now
                                elapsed = now - t_ollama_start
                                tok_s = ollama_token_count / elapsed if elapsed > 0 else 0
                                print(f"\r  [INFO] ⚡ Yerel model üretiyor: {ollama_token_count} token ({tok_s:.1f} tok/s - {elapsed:.1f}s)...  ", end="", flush=True)

                    with ColdStartWatcher(current_model, api_base=api_base):
                        content = call_ollama_chat(
                            messages=messages,
                            model=current_model,
                            api_base=api_base or "http://localhost:11434",
                            stream=True,
                            on_token=_ollama_on_token,
                            think_mode=False,
                            temperature=LLM_PARAMS["temperature"],
                            max_tokens=LLM_PARAMS["max_tokens"],
                            num_ctx=ctx_window,
                            top_p=LLM_PARAMS.get("top_p"),
                            top_k=LLM_PARAMS.get("top_k"),
                        )
                    if ollama_token_count > 0:
                        elapsed_total = time.time() - t_ollama_start
                        tok_s_total = ollama_token_count / elapsed_total if elapsed_total > 0 else 0
                        print(f"\r  [INFO] ⚡ Yanıt alındı: {ollama_token_count} token ({elapsed_total:.1f}s — {tok_s_total:.1f} tok/s)                                \n", end="", flush=True)
                else:
                    completion_kwargs = {
                        "model": current_model,
                        "messages": messages,
                        "temperature": LLM_PARAMS["temperature"],
                        "max_tokens": LLM_PARAMS["max_tokens"],
                        "api_base": api_base,
                        "api_key": LLM_PARAMS.get("api_key", ""),
                        "stream": True,
                    }
                    if "top_p" in LLM_PARAMS and LLM_PARAMS["top_p"] is not None:
                        completion_kwargs["top_p"] = LLM_PARAMS["top_p"]

                    is_thinking_model = any(k in current_model.lower() for k in ("nemotron", "deepseek", "kimi", "r1"))
                    if is_thinking_model:
                        completion_kwargs["extra_body"] = {
                            "chat_template_kwargs": {"enable_thinking": True},
                            "reasoning_budget": min(LLM_PARAMS["max_tokens"], 16384),
                        }

                    # Canlı Streaming ve TTFT / Token / Hız Sayacı
                    try:
                        t_stream_start = time.time()
                        chunks = []
                        token_count = 0
                        last_update = 0.0

                        with ColdStartWatcher(current_model, api_base=api_base):
                            response = completion(**completion_kwargs)
                            resp_iter = iter(response)
                            first_chunk = next(resp_iter, None)

                        if first_chunk:
                            d = (
                                first_chunk.choices[0].delta.content
                                if (first_chunk.choices and hasattr(first_chunk.choices[0], "delta") and getattr(first_chunk.choices[0].delta, "content", None))
                                else ""
                            )
                            if d:
                                chunks.append(d)
                                token_count += 1

                        model_label = "Yerel model" if is_local else "Model"

                        for chunk in resp_iter:
                            delta = chunk.choices[0].delta if chunk.choices and hasattr(chunk.choices[0], "delta") else None
                            if delta:
                                token = getattr(delta, "content", "") or ""
                                if token:
                                    chunks.append(token)
                                    token_count += 1
                                    now = time.time()
                                    if now - last_update >= 0.3:
                                        last_update = now
                                        elapsed = now - t_stream_start
                                        tok_s = token_count / elapsed if elapsed > 0 else 0
                                        print(f"\r  [INFO] ⚡ {model_label} üretiyor: {token_count} token ({tok_s:.1f} tok/s - {elapsed:.1f}s)...  ", end="", flush=True)

                        elapsed_total = time.time() - t_stream_start
                        tok_s_total = token_count / elapsed_total if elapsed_total > 0 else 0
                        if token_count > 0:
                            print(f"\r  [INFO] ⚡ Yanıt alındı: {token_count} token ({elapsed_total:.1f}s — {tok_s_total:.1f} tok/s)                                \n", end="", flush=True)

                        content = "".join(chunks)

                    except Exception as stream_err:
                        logger.warning("[%s] Streaming hatası (%s), senkron denenecek: %s", agent_name, current_model, stream_err)
                        completion_kwargs["stream"] = False
                        with ColdStartWatcher(current_model, api_base=api_base):
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
