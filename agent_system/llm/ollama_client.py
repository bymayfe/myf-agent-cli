"""
ollama_client.py — Doğrudan Ollama REST API istemcisi.

LiteLLM'in Ollama streaming modundaki 'thinking' alanı ayrıştırma hatasını (qwen3.5 modelleri)
bypass etmek için hafif, bağımsız ve hızlı doğrudan istemci.
"""

import json
import urllib.request
import urllib.error
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def is_ollama_provider(model: str, api_base: str = "") -> bool:
    """Belirtilen model/api_base Ollama sunucusunu mu işaret ediyor?"""
    b = (api_base or "").lower()
    # Bulut sağlayıcıları kesinlikle Ollama değildir
    if any(k in b for k in ("nvidia.com", "openrouter.ai", "moonshot.cn", "deepseek.com", "openai.com", "anthropic.com", "googleapis.com")):
        return False
    m = (model or "").lower()
    return "ollama" in m or "localhost:11434" in b or "127.0.0.1:11434" in b


def call_ollama_chat(
    messages: list[dict],
    model: str,
    api_base: str = "http://localhost:11434",
    stream: bool = False,
    on_token: Optional[Callable[[str, str], None]] = None,  # (token, token_type)
    think_mode: bool = False,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    num_ctx: Optional[int] = None,
) -> str:
    """
    Ollama /api/chat endpoint'ine doğrudan HTTP POST isteği gönderir.
    """
    model_name = model.split("/")[-1] if "/" in model else model
    endpoint = f"{api_base.rstrip('/')}/api/chat"

    # Context window: parametre verilmemişse model boyutuna göre güvenli varsayılan
    num_ctx_val = num_ctx or (4096 if "27b" in model_name.lower() else 8192)

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": num_ctx_val,
        }
    }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    full_chunks: list[str] = []
    think_chunks: list[str] = []
    inside_think_tag = False

    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            for line in resp:
                if not line:
                    continue
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    chunk = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                msg = chunk.get("message", {})
                content_token  = msg.get("content", "")
                thinking_token = msg.get("thinking", "") or chunk.get("thinking", "")

                # 1. Doğrudan thinking alanı varsa kaydet
                if thinking_token:
                    think_chunks.append(thinking_token)
                    if think_mode and on_token:
                        on_token(thinking_token, "thinking")

                # 2. Content içindeki <think> ... </think> taglerini ayrıştır
                if content_token:
                    if "<think>" in content_token:
                        inside_think_tag = True
                        content_token = content_token.replace("<think>", "")
                    if "</think>" in content_token:
                        parts = content_token.split("</think>", 1)
                        if parts[0]:
                            think_chunks.append(parts[0])
                            if think_mode and on_token:
                                on_token(parts[0], "thinking")
                        inside_think_tag = False
                        content_token = parts[1] if len(parts) > 1 else ""

                    if inside_think_tag:
                        if content_token:
                            think_chunks.append(content_token)
                            if think_mode and on_token:
                                on_token(content_token, "thinking")
                    else:
                        if content_token:
                            full_chunks.append(content_token)
                            if on_token:
                                on_token(content_token, "content")

                if chunk.get("done", False):
                    break

    except KeyboardInterrupt:
        if on_token:
            on_token("\n[kullanıcı tarafından durduruldu]\n", "content")
        logger.info("Ollama streaming kullanici tarafindan durduruldu.")
    except urllib.error.URLError as err:
        logger.error("Ollama baglanti hatasi: %s", err)
        raise RuntimeError(
            f"Ollama sunucusuna baglanilamadi ({endpoint}). "
            f"Lutfen 'ollama serve' komutunun calistigindan emin olun. Detay: {err}"
        ) from err

    res = "".join(full_chunks).strip()
    if not res and think_chunks:
        # Eğer ana content boş ama model thinking üretmişse thinking içeriğini dön
        res = "".join(think_chunks).strip()

    return res


def unload_all_ollama_models(api_base: str = "http://localhost:11434") -> None:
    """
    Program kapanırken RAM ve VRAM'deki tüm Ollama modellerini anında boşaltır.
    """
    try:
        endpoint_ps = f"{api_base.rstrip('/')}/api/ps"
        req_ps = urllib.request.Request(endpoint_ps, method="GET")
        with urllib.request.urlopen(req_ps, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name") or m.get("model") for m in data.get("models", [])]

        endpoint_gen = f"{api_base.rstrip('/')}/api/generate"
        for m in models:
            if not m:
                continue
            payload = {"model": m, "keep_alive": 0}
            req_unload = urllib.request.Request(
                endpoint_gen,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            try:
                urllib.request.urlopen(req_unload, timeout=3)
            except Exception:
                pass
    except Exception:
        pass


import atexit
atexit.register(unload_all_ollama_models)
