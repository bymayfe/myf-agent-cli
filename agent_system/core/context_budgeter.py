"""
context_budgeter.py — Dinamik Token Bütçesi ve Bağlam Yönetim Motoru

LLM çağrıları öncesinde kalan token bütçesini hesaplar, SQLite (log_store.py) 
üzerindeki geçmiş adım metriklerinden ortalama tüketimi öğrenir ve gerektiğinde 
sohbet hafızasını güvenle özetler/kısaltır.
"""

from __future__ import annotations
import logging
from typing import Optional
from pathlib import Path

from config import get_output_dir
from llm_client import estimate_tokens, get_model_context_window
from log_store import log_store

logger = logging.getLogger("context_budgeter")


class ContextBudgeter:
    """
    Dinamik Token Bütçeleme ve Hafıza Yönetimi.
    
    Özellikler:
      - SQLite Tabanlı Ortalama Token Öğrenme: log_store.py'den geçmiş adımların
        ortalama karakter/token harcamasını çeker. Veritabanı boşsa model context'inin
        %20'sine güvenle fallback yapar.
      - Ortak Token Tahmini: llm_client.py'deki Türkçe/İngilizce uyumlu estimate_tokens'ı
        kullanır (kod tekrarı yoktur).
      - Akıllı Özetleme Tetikleyici (should_summarize): Context doluluğu %75'e ulaştığında
        sohbet geçmişini son N mesajı koruyarak tek bir özet bloğunda birleştirir.
    """

    def __init__(self, project_dir: Optional[str | Path] = None):
        self.project_dir = Path(project_dir or get_output_dir()).resolve()

    def get_avg_tokens_per_turn(
        self,
        model_name: Optional[str] = None,
        default_fallback: Optional[int] = None,
        limit_steps: int = 20,
    ) -> int:
        """
        log_store SQLite veri tabanından son adımların ortalama token tüketimini hesaplar.
        Veri tabanı boşsa veya henüz kayıt yoksa model context penceresinin %20'sine
        (veya default_fallback'e) düşer. Asla 0 veya None dönmez.
        """
        fallback_val = default_fallback
        if fallback_val is None:
            ctx = get_model_context_window(model_name or "default")
            fallback_val = max(500, int(ctx * 0.20))

        try:
            if model_name:
                rows = log_store._fetch(
                    str(self.project_dir),
                    """
                    SELECT prompt_chars, response_chars
                    FROM agent_steps
                    WHERE (model = ? OR model LIKE ?) AND (prompt_chars > 0 OR response_chars > 0)
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (model_name, f"%{model_name}%", limit_steps),
                )
            else:
                rows = log_store._fetch(
                    str(self.project_dir),
                    """
                    SELECT prompt_chars, response_chars
                    FROM agent_steps
                    WHERE prompt_chars > 0 OR response_chars > 0
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (limit_steps,),
                )
            if not rows:
                return fallback_val

            total_chars = sum((r["prompt_chars"] or 0) + (r["response_chars"] or 0) for r in rows)
            avg_chars = total_chars / len(rows)
            avg_tok = estimate_tokens("a" * int(avg_chars))
            return max(100, avg_tok)

        except Exception as exc:
            logger.debug("[CONTEXT-BUDGET] SQLite ortalama token sorgusu başarısız, fallback kullanılıyor: %s", exc)
            return fallback_val

    def get_remaining_budget(
        self,
        system_prompt: str,
        conversation_history: list[dict],
        model_name: str,
        safety_ratio: float = 0.80,
    ) -> int:
        """
        Modelin context penceresi, mevcut mesajlar ve SQLite'dan öğrenilen tahmini tur tüketimine
        göre kalan güvenli net token bütçesini döner.
        """
        ctx_window = get_model_context_window(model_name)
        max_safe = int(ctx_window * safety_ratio)

        used_tokens = estimate_tokens(system_prompt)
        for msg in conversation_history:
            used_tokens += estimate_tokens(msg.get("content", ""))

        avg_turn = self.get_avg_tokens_per_turn(model_name)
        return max(0, max_safe - (used_tokens + avg_turn))

    def should_summarize(
        self,
        system_prompt: str,
        conversation_history: list[dict],
        model_name: str,
        threshold: float = 0.75,
    ) -> bool:
        """
        Mevcut toplam token yükü + SQLite'dan öğrenilen bir sonraki tur tahmini (avg_tokens_per_turn),
        model context limitinin eşiğini (%75) aşıyorsa öngörülü (proactive) olarak True döner.
        """
        ctx_window = get_model_context_window(model_name)
        total_tokens = estimate_tokens(system_prompt)
        for msg in conversation_history:
            total_tokens += estimate_tokens(msg.get("content", ""))

        projected_tokens = total_tokens + self.get_avg_tokens_per_turn(model_name)
        return projected_tokens >= int(ctx_window * threshold)

    def summarize_history(
        self,
        conversation_history: list[dict],
        keep_recent: int = 4,
    ) -> list[dict]:
        """
        Eski sohbet mesajlarını tek bir yapılandırılmış özet bloğunda birleştirir,
        son `keep_recent` mesajı ise aynen korur.
        """
        if len(conversation_history) <= keep_recent:
            return list(conversation_history)

        older_msgs = conversation_history[:-keep_recent]
        recent_msgs = conversation_history[-keep_recent:]

        summary_lines = []
        for m in older_msgs:
            role = m.get("role", "user")
            content = m.get("content", "").strip()
            # Kısa snippet al
            snippet = content[:150].replace("\n", " ")
            if len(content) > 150:
                snippet += "..."
            summary_lines.append(f"- [{role.upper()}]: {snippet}")

        summary_content = (
            "--- [SUMMARIZED CONVERSATION HISTORY] ---\n"
            + "\n".join(summary_lines)
            + "\n-----------------------------------------"
        )

        compacted = [{"role": "system", "content": summary_content}] + recent_msgs
        logger.info(
            "[CONTEXT-BUDGET] Sohbet geçmişi sıkıştırıldı: %d mesaj -> %d mesaj (özet + son %d mesaj)",
            len(conversation_history),
            len(compacted),
            keep_recent,
        )
        return compacted


context_budgeter = ContextBudgeter()
